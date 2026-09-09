# What AiiDA's history says about the sketch

Companion to [architecture.md](architecture.md) and [snakemake.md](snakemake.md), which does the same for Snakemake.
AiiDA is the engine closest to the sketch in what it stores and furthest in how it executes, so its history bears mostly on the record model and on unattended operation.
Facts were checked against aiida-core's documentation source and changelog on GitHub on 2026-09-09; the sources are listed at the end.
The last section says what was folded into the sketch and what was not.

## AiiDA in the sketch's terms

AiiDA is a provenance database with an engine attached. Every piece of data is a node, every execution is a process node, and links between them (input, create, return, call) form an immutable directed graph. Data nodes are immutable once stored; process nodes are sealed when they finish. A daemon runs processes, submits jobs to remote computers over SSH through scheduler plugins, polls them, retrieves output files, and parses them into new data nodes. Long workflows are WorkChains: Python classes with an `outline` of steps, checkpointed to the database between steps so a daemon restart resumes them. Process control (submit, pause, play, kill) goes through a message broker. It is single-user by design: one profile, one database, one person.

It is the engine closest to the sketch in what it stores, and furthest in how it executes.

## What it learned the hard way

**A message broker for process control.** AiiDA 1.0 (2019) made RabbitMQ mandatory for talking to the daemon. In 2021 RabbitMQ 3.8.15 added a default consumer timeout of 30 minutes in a patch release; any AiiDA process running longer than that was killed, and the fix was a server-side configuration users had to apply by hand (issue 5105, still in the troubleshooting docs). Beyond that: `verdi process repair` had to be added to reconcile the database with the broker queue after a broker restart lost the tasks, and later extended to kill orphaned PostgreSQL connections from crashed workers. Version 2.6 (2024) made the broker optional; 2.9 (August 2026) shipped a built-in ZeroMQ broker so that nothing external is needed. The changelog for 2.6 says the requirement "significantly improved the scaling and responsiveness" but "made it more difficult to start using AiiDA". Lesson: the queue is not the truth, the database is; and a broker you do not own is an operational dependency whose defaults change under you. The sketch's choice 2 (no broker in local mode, re-examine for the cluster) and the esslivedata Kafka experience point the same way; AiiDA is the second data point.

**Install burden decides adoption.** PostgreSQL plus RabbitMQ was the number one complaint for a decade. AiiDA 2.5 added SQLite storage, 2.6 made the broker optional and added `verdi presto`, so that `pip install` plus one command gives a working profile. Test fixtures that need no services followed. The sketch starts from SQLite and no services; keep it that way for local mode and do not let shared mode leak requirements back into it.

**Two storage backends for years.** AiiDA maintained Django and SQLAlchemy backends of the same ORM in parallel until 2.0 (2022) merged them into one. The sketch plans SQLite now, Postgres later, and a Tiled disk tier as a second implementation. The AiiDA lesson: a second implementation of a store interface is only worth having if it will replace the first; two maintained in parallel doubled the cost for no user value.

**One file per node did not scale.** The original repository stored each node's files in its own directory. At millions of nodes this made backups impractical. 2.0 replaced it with `disk-objectstore`: content-addressed, deduplicated, packed into large files, with an order-of-magnitude backup speedup claimed. Our volumes are a few large arrays rather than millions of small files, so the pressure is different, but the lesson holds for layout: decide the on-disk layout with backup and export in mind, not per-record directories by default.

**Checkpointing live process state is fragile.** WorkChains resume after a daemon restart because the process object is serialised between steps. The price appears in every changelog: checkpoints could not hold numpy arrays until a fix, could not hold enums until another, and the unreleased next version says "process checkpoints created with earlier releases cannot be continued after upgrading; finish or terminate all active processes before upgrading". The FAQ tells users to restart the daemon after every code change. This is the strongest external confirmation of D2: a request is complete and stateless, the process is a cache, and nothing that survives a restart is a pickled object. It also warns about the session runner: warm workflows hold imported code, so a code change means a session restart, which the sketch should say.

**Everything recorded, then an escape hatch.** AiiDA records every process. Users wanted exploration that leaves no trace, so `store_provenance=False` and `dry_run` were added, marked "use with care". The sketch made the same choice, records for every rerun, and answered the same pressure with slots and retention rather than an unrecorded mode. Expect the request for a no-record mode anyway; the checkpoint model in stateless.md is where it would go.

**Deleting from a provenance graph needs traversal rules.** Because every node may be the input of another, `verdi node delete` follows rules per link type and direction: deleting an input deletes the calculations that used it, deleting an output deletes the calculation that created it, and so on, recursively. Export has the mirror rules. The sketch avoids the whole apparatus by never deleting a record singly and dropping a proposal whole; the AiiDA rules are what that decision saves. The export side is still relevant: the deferred "upload of records from a private store to a shared backend" is exactly AiiDA's archive import, and its traversal rules (include everything upstream of a result, nothing downstream by default) are the right default for that.

**Integer keys do not survive a move.** Nodes have a per-database integer pk and a UUID; the docs insist on UUIDs whenever data leaves a database, and import deduplicates by UUID. If records may ever move between stores, the record ID should be a UUID from the first commit, not a row number. Cheap now, painful later.

**Caching had its version problem too.** AiiDA's cache hashes a process's inputs and attributes; until 2.6 the hash also included the aiida-core and plugin versions, so every upgrade invalidated the whole cache. 2.6 removed versions from the hash and gave plugins a `CACHE_VERSION` counter to bump when their behaviour changes. Compare Snakemake's mirror-image mistake, code hashes too fine-grained. The stable point both reached: the author declares when the code's meaning changed, the framework does not guess. That is the sketch's optional code revision on the spec.

## What paid off

**The provenance graph as the product.** Immutable nodes, sealed processes, links as the only relationship, and a query builder over the graph. This is D1 in a different vocabulary: a record is a process node with its inputs, a reference is an input link. AiiDA proves the model scales to millions of nodes and years of use. The one thing it has that the sketch does not is logical provenance: `call` links from a workflow to the calculations it launched, so the graph answers "which workflow run produced this" as well as "which calculation". The sketch's batch ID and template version cover the batch case; a group ID on records submitted together would cover the rest and is nearly free.

**Exit codes declared on the spec.** A process declares named exit codes with messages; a finished process carries `exit_status` and `exit_message`; the docs draw the line between finished-with-nonzero-status (the calculation failed), excepted (the workflow code raised), and killed. Three terminal kinds, not one. This is what made `BaseRestartWorkChain` possible: handlers keyed on exit codes that fix inputs and resubmit, up to `max_iterations`, then abort or pause for a human. High-throughput unattended runs depend on it. The sketch's structured failure reason is the same idea; two refinements are worth taking when the time comes. First, let a spec declare its failure reasons, so that a UI and a retry rule can match on them, and separate workflow failure from framework failure and from cancellation in the state machine. Second, a retry policy for automatic reduction that keys on declared reasons is the AiiDA pattern, and belongs in the trigger loop, not the scheduler.

**Transient infrastructure failure pauses, it does not fail.** Transport tasks (upload, submit, poll, retrieve) retry with exponential backoff, and after the last attempt the process is paused rather than failed; `verdi process play` resumes it from the failed task once the SSH key or the filesystem is back. Each computer also has a minimum poll interval and a per-connection safe interval so that many workers do not hammer one login node. The sketch's "fetching inputs has a timeout and a distinct failure status" is the weaker rule. For phase 1, a slow or absent mount is the common failure and it is transient; a paused state that an operator can resume, or that resumes itself when the mount returns, keeps the record and its dispatch rather than burning a retry record per outage. This is the one AiiDA mechanism I would argue for adding.

**Mutable annotation beside immutable provenance.** Attributes and repository files are immutable once stored because descendants depend on them; extras are a mutable key-value bag "for the user to tag, group, comment", plus groups and comments as first-class objects. The sketch's record is immutable except status, and has no annotation channel. Users will want to mark a result "good", "use this vanadium", "superseded" after inspection, and a UI needs somewhere to keep that without touching provenance. A small mutable annotations field, explicitly outside the request and the outputs, is the AiiDA answer and costs nothing.

**Protocols plus overrides.** aiida-quantumespresso's `get_builder_from_protocol(structure, protocol='balanced', overrides={...})` became the way people run workflows: a named preset generates a complete input set, and a nested dict of overrides is merged on top. The sketch's template plus per-member overrides is the same shape, and the AiiDA experience says the merge semantics need care: a recent fix was for silently dropped overrides. Validate overrides against the template's schema and refuse unknown keys.

**Records of software versions.** Every process node stores the aiida-core and plugin versions. Same as the sketch.

**Automatic input serialisation.** Early AiiDA made users wrap every integer in `Int(3)`; automatic serialisation of Python base types was added later because the tax was too high. The sketch's vocabulary of plain JSON types with references only where data is named avoids this from the start.

## Where the model does not transfer

AiiDA orchestrates external codes on remote machines: it writes input files, submits scheduler jobs, polls, retrieves, parses. Its WorkChain is a stateful program whose steps launch those jobs, and its interactivity is pause and play on that program. Our workflow is a Python callable in the same process family as the framework, and our interactive loop is a warm object in memory. Nothing in AiiDA addresses sub-second reruns, views, or sessions. Its single-user design also means it never faced proposal scoping or per-user cluster accounts; AiiDAlab gives each user a container with their own profile, and shared results are published by export. That is a data point for the deferred "upload records" path being an export, and against a facility-wide multi-tenant AiiDA-like service.

## What was folded into the sketch

| Lesson | Change to the sketch | Where |
|---|---|---|
| Integer keys do not survive a move | Run IDs are UUIDs | Records and references |
| Extras beside immutable attributes | Annotations on a record: mutable, outside provenance, never read by a run | Records and references; Glossary |
| Call links, logical provenance | A group ID on records submitted together | Records and references; Glossary |
| Exit codes and three terminal states | A failed record says whether the workflow reported a declared reason, the workflow code raised, or the framework could not run it; a spec may declare failure reasons | Failure handling; Spec changes |
| BaseRestartWorkChain | A trigger rule may resubmit a failed record by declared reason, up to a limit | Components |
| Transport failures pause, then play | A paused status: retries at increasing intervals, then paused with the cause, resumed by an operator or when the location is reachable again | Failure handling |
| Restart the daemon after a code change | A code change takes effect in a new session, never a running one | Choice 3 |
| Archive traversal rules | The deferred upload carries everything upstream of a chosen record and nothing downstream | Explicitly deferred |

Not folded in, and why:

- **A message broker.** The sketch has none in local mode and revisits the question with the cluster launcher; AiiDA's history is a second reason to be slow about it.
- **A checkpointed workflow program.** D2 says the process is a cache; nothing that survives a restart is a serialised object.
- **A caching layer.** Removed in the second review pass; if wanted, the derivation reason "copy" on a new record is where a cache hit would go, which is also how AiiDA records one.
- **Nested workflows with call links as the composition mechanism.** D4 composes by chaining separate specs through references.
- **A query builder over the graph.** The record store's listing queries and "records that reference output X of record Y" are enough; a transitive walk is a loop over them.

## Sources

- aiida-core docs source: topics/provenance/caching.rst, topics/provenance/consistency.rst, topics/processes/usage.rst, topics/processes/concepts.rst, howto/workchains_restart.rst, howto/faq.rst, howto/share_data.rst, topics/storage.rst, topics/repository.rst
- aiida-core CHANGELOG.md: unreleased (checkpoint compatibility), v2.9.0 (ZeroMQ broker), v2.8 (`verdi process repair` orphaned connections), v2.6.0 (broker optional, `verdi presto`, caching version removal), v2.0 (single psql_dos backend, disk-objectstore)
- [AiiDA will no longer work with rabbitmq>3.7 by default, issue 5105](https://github.com/aiidateam/aiida-core/issues/5105)
- [Calculations concepts: exponential backoff and paused processes](https://aiida.readthedocs.io/projects/aiida-core/en/stable/topics/calculations/concepts.html)
- [AiiDA 1.0 paper, Scientific Data 2020: attributes immutable, extras mutable](https://www.nature.com/articles/s41597-020-00638-4)
- [aiida-quantumespresso protocol docs](https://aiida-quantumespresso.readthedocs.io/en/latest/topics/protocol.html)
- [AiiDA v2.0.0 release post](https://aiida.net/blog/2022-04-27-aiida-2-release/), [Simplifications to the installation since v2.0](https://aiida.net/blog/2024-09-20-simpler-installation/)
