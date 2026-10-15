# AiiDA

[AiiDA](https://github.com/aiidateam/aiida-core) is a Python workflow engine for computational materials science that stores every execution in a provenance graph.
Data nodes and process nodes are joined by input, create, return, and call links.
A stored node never changes, and a process node is *sealed* when it ends.
A daemon runs processes: it submits jobs to remote computers through scheduler plugins, polls them, and parses their output files into new data nodes.
Long workflows are WorkChains, Python classes whose state is checkpointed to the database between steps.
A profile serves one user; users share data by export and import ([installation.rst L536-541][install]).

We looked at it because it stores what we store: records that never change, every input linked, software versions.
It runs differently: it drives external programs on remote machines, and nothing in it serves sub-second reruns, stages, or accumulators.
Facts were checked against aiida-core at commit 952b9c8 (2026-10-12), its changelog, and its issues.

## Records and provenance

**Stored nodes never change; annotations sit beside them.** Attributes and files of a stored node are immutable, while *extras* are "added by the user and are not immutable, even when the node is stored" ([internals.rst L166-167][internals]).
Extras, comments, and groups exist "for the user to tag/group/comment on data" and can be deleted at any time ([data.rst L976-985][data]).
Our records never change except their status, and labels and members are fixed at submission ([README, Labels and members](../README.md#labels-and-members)).
No requirement asks to mark a record later, such as "good" or "use this can", and a second catalogue is a non-goal ([requirements](../../requirements/README.md#non-goals)).
If a story asks for it, AiiDA's answer fits [ADR 0004](../adr/0004-history-is-append-only-lists.md): one more list, of annotations, appended to and read by nothing in the framework.

**UUIDs when data leaves the database.** Every node has a per-database integer key and a UUID.
The docs ask for UUIDs whenever data is shared, and import deduplicates by UUID ([share_data.rst L46, L110][share]).
Our record IDs are random UUIDs already (`ess.dispatch.backend`), so `client.publish` can carry them into a SciCat entry unchanged.

**Software versions on every process node.** A process stores the versions of aiida-core and its plugin on its node when it starts ([process.py L786-789][version]).
There, the worker that creates the node also runs it.
Our backend logs a record before it decides where the request runs, so only the finish can say which versions ran it.
This bears on ADR 0002, Open ("what a finish records besides the status") and on software versions in provenance, which the README promises and the code lacks ([README, Provenance and publication](../README.md#provenance-and-publication)).

**A cache keyed on package versions reset at every upgrade.** Until v2.6 the cache hash included the aiida-core and plugin versions, so "the cache would essentially be reset each time AiiDA or any of the plugin packages would be updated".
v2.6 removed them and gave plugins a `CACHE_VERSION` that the author bumps when behaviour changes ([CHANGELOG L1673-1679, L1747][cl-cache]).
We have no result cache, but the lesson holds for anything that compares records: record software versions for provenance, never as identity.
The ess packages had 55 releases in nine months ([systems](../../requirements/systems.md)).
The spec's `version` plays the role of `CACHE_VERSION`: its author says when the meaning changed.

**Code from a notebook has its source recorded.** The unreleased aiida-core lets a process class defined in a notebook cell run on the daemon.
No import path finds such a class later, so `ProcessNode.class_source` stores its source on the node ([CHANGELOG L47-55][cl-notebook]).
Our `local()` binds a spec to draft code in the notebook ([README, Specs and bindings](../README.md#specs-and-bindings); [tensions](../../requirements/tensions.md), "Recording every reduction versus parameters given as Python functions").
The record then names a spec version that does not describe the code that ran.
Provenance should at least say that the binding came from no installed package; AiiDA goes further and keeps the source.

**An unrecorded mode came anyway.** Every process is recorded unless `store_provenance=False`, documented as "use with care" ([usage.rst L303][usage], [tricks_real_world_runs.rst L168][tricks]).
For us, `select=` and `client.output` of an accumulator already make no record, and open question 4 asks for looks that compute without one ([README, Open questions](../README.md#open-questions)).
AiiDA shows the demand is real; keeping such a result a copy, never an input, keeps provenance whole without a "use with care".

## Deleting and exporting

**Deleting from a provenance graph needs traversal rules.** `verdi node delete` follows links by type and direction: deleting an input deletes every calculation that used it, and deleting an output deletes the calculation that created it, recursively ([data.rst L951-953][data-del], [consistency.rst L36-54][consistency]).
Export mirrors this: exporting a calculation exports its inputs and outputs, and exporting a data node does not export the calculations that used it ([consistency.rst L57-61][consistency]).
We need no traversal: [ADR 0004](../adr/0004-history-is-append-only-lists.md) drops a proposal's history as a whole, and no other proposal reads it (system story G5).
Output values are not history ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md)), so dropping stored values (H1) or a mistaken file (A4) leaves every record intact; in AiiDA a node's files are part of the immutable node.
AiiDA's export default is what `client.provenance` and `client.publish` do: everything upstream, stopping at datasets, nothing downstream.

## Running unattended

**Three terminal states, and failures declared on the spec.** A process ends `Finished`, `Excepted` (an uncaught exception), or `Killed`.
`Finished` says only that the engine ran it to the end. A non-zero `exit_status` is a failure the process declared, such as `spec.exit_code(418, 'ERROR_I_AM_A_TEAPOT', '...')` ([concepts.rst L74-90][states], [usage.rst L231-255][exit]).
`BaseRestartWorkChain` keys handlers on exit codes and retries up to `max_iterations` (default 5), then aborts or pauses for a person ([workchains_restart.rst L76-115][restart]).
Our records end `completed`, `failed`, or `cancelled`, and `client.failure` returns a message ([README, Requests and records](../README.md#requests-and-records)).
`failed` covers a workflow that raised, an unreadable dataset, and a failed write.
If rules move into applications (scipp/essapps#33), the application decides retries, and today it can only match on a message.
AiiDA's split is the minimum it would need: the workflow reported a failure, the workflow code raised, or the framework could not run it.

**Transient infrastructure failures pause instead of failing.** A transport task (upload, submit, poll, retrieve) that raises is rescheduled at growing intervals.
After 5 consecutive failures the job is paused, and `verdi process play` resumes it from the failed task ([calculations/concepts.rst L176-200][backoff]).
Each computer also sets a minimum interval between scheduler polls ([computers.py L73-74][poll]).
For us, a slow or missing `/ess` mount is the likely transient failure on the service.
Today it fails the record, and since a failed record counts as handled ([automatic-reduction.md](../automatic-reduction.md), The trigger loop), automatic reduction never reduces that run.
Whether the backend retries before it fails a record, or marks the failure so a controller may resubmit, belongs to the service design (scipp/essapps#27).

**Checkpointed workflow state does not survive upgrades.** A WorkChain resumes after a daemon restart because its state is serialised between steps.
The unreleased version says such checkpoints "cannot be continued after upgrading. Finish or terminate all active processes before upgrading" ([CHANGELOG L11-12][cl-head]).
The FAQ says to restart the daemon after every code change ([faq.rst L29][faq]).
Our backend pickles nothing: after a restart it reruns persisted pending records from history ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md)).
On an upgrade with work in flight, AiiDA's answer is to drain first, the "old instance finishes them" option of ADR 0002, Open, and system story H2.
Warm workers (scipp/essapps#27) hold imported code as AiiDA's daemon workers do, so a package upgrade must restart them.

**A broker queue drifted from the database.** v1.0 made RabbitMQ mandatory for process control.
RabbitMQ 3.8.15, a patch release, added a default consumer timeout that cancelled long processes; the fix is server configuration, which usually needs administrator access ([issue 5105](https://github.com/aiidateam/aiida-core/issues/5105), [troubleshooting.rst L34-66][rabbit]).
`verdi process repair` (v2.5) compares active processes in the database with tasks in the broker, discards stray tasks, and revives processes that lost theirs; since v2.8 it also ends database connections left by crashed workers ([cmd_process.py L516-525][repair], [CHANGELOG L661-666][cl-repair]).
v2.6 made the broker optional, and v2.9 (August 2026) ships a built-in ZeroMQ broker, while RabbitMQ "remains the recommended broker" for high throughput ([CHANGELOG L1634-1639][cl-broker], [L143-160][cl-zmq]).
For us, history is the only record of what is pending ([ADR 0004](../adr/0004-history-is-append-only-lists.md)).
An executor such as dask.distributed (scipp/essapps#27) must stay a cache of history that a restart rebuilds, never a second record of pending work.

**Installation without services, and one storage implementation.** v2.5 added SQLite storage and v2.6 made the broker optional, so `pip install aiida-core` and `verdi presto` give a working profile, and test fixtures need no services ([CHANGELOG L1641-1660][cl-broker]).
The SQLite storage is a subclass of the PostgreSQL one ([sqlite_dos/backend.py L194][sqlite]), after years of parallel Django and SQLAlchemy backends that v2.0 merged ([CHANGELOG L3359-3362][cl-v2]).
`local()` needs no service either; if the service stores history differently from the in-process log, as ADR 0004 allows, AiiDA's path suggests one implementation that varies only the database engine.

**One directory per node did not scale.** At millions of files, "even just determining the difference between the original and backup of a repository, for example using rsync could take days" ([repository.rst L235-238][repo]); v2.0 moved to deduplicated, append-only pack files ([CHANGELOG L3364-3365][cl-v2]).
Our store's layout is open ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md), Open); a proposal holds far fewer files, but the layout should keep backup and dropping a proposal cheap.

## Summary

Borrow:

- A failure kind on each failed record (declared by the workflow, raised, or the framework's), so an application can decide retries (scipp/essapps#33).
- For the service, retry transient infrastructure failures before failing a record, so automatic reduction does not lose a run to a mount outage (scipp/essapps#27).
- Software versions in provenance, written with the finish, never part of a record's identity; and a mark, or the source, for a binding defined in a notebook.
- Drain before an upgrade, as one answer to system story H2.

Do not borrow:

- A message broker as a second record of pending work.
- Checkpointed workflow programs.
- Traversal rules for deleting single records: history is dropped per proposal.
- Anything keyed on package versions.
- One user per database with sharing by export: many clients share a proposal's history.

[install]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/installation.rst?plain=1#L536-L541
[internals]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/developer_guide/core/internals.rst?plain=1#L166-L167
[data]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/data.rst?plain=1#L976-L985
[data-del]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/data.rst?plain=1#L951-L953
[share]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/share_data.rst?plain=1#L46
[version]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/src/aiida/engine/processes/process.py#L786-L789
[cl-head]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L11-L12
[cl-notebook]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L47-L55
[cl-broker]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L1634-L1660
[cl-zmq]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L143-L160
[cl-cache]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L1673-L1679
[cl-repair]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L661-L666
[cl-v2]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/CHANGELOG.md?plain=1#L3359-L3365
[usage]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/topics/processes/usage.rst?plain=1#L303
[exit]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/topics/processes/usage.rst?plain=1#L231-L255
[tricks]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/tricks_real_world_runs.rst?plain=1#L168
[consistency]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/topics/provenance/consistency.rst?plain=1#L36-L61
[states]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/topics/processes/concepts.rst?plain=1#L74-L90
[restart]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/workchains_restart.rst?plain=1#L76-L115
[backoff]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/topics/calculations/concepts.rst?plain=1#L176-L200
[poll]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/src/aiida/orm/computers.py#L73-L74
[faq]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/howto/faq.rst?plain=1#L29
[rabbit]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/installation/troubleshooting.rst?plain=1#L34-L66
[repair]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/src/aiida/cmdline/commands/cmd_process.py#L516-L525
[sqlite]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/src/aiida/storage/sqlite_dos/backend.py#L194
[repo]: https://github.com/aiidateam/aiida-core/blob/952b9c86ed68a86e2cd466c69c4732c9930ed1e5/docs/source/internals/storage/repository.rst?plain=1#L235-L238
