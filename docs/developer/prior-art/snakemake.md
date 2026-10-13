# Snakemake

Snakemake is a workflow manager for file-based scientific pipelines, used mostly in bioinformatics.
A workflow is a set of rules.
Each rule declares its output files by name pattern, its input files, non-file parameters, and the code that runs.
The user asks for output files, and Snakemake works backwards to the jobs that make them.
One invocation works on one directory, keeps its metadata in a hidden folder there, and exits once the requested files exist.
A batch is many values of a wildcard; automatic reduction is a cron job that invokes Snakemake again and relies on it to skip what exists.

We looked at it because it has run scientific pipelines on clusters for over a decade, with a public issue tracker and changelog.
Its graph inference from file names does not apply to us, since a request names its inputs by reference ([README](../README.md#references)); its lessons bear on batch and automatic reduction, failures, and the service (scipp/essapps#24, scipp/essapps#27, scipp/essapps#33).
Facts were checked on 2026-10-13 against Snakemake 9.27 (commit [`4523356`](https://github.com/snakemake/snakemake/tree/4523356973a83aa237a121717ee81dd2e09175da)), the sdists of 5.4.0, 7.7.0 and 7.8.0, and the issue tracker.

## Findings

**A file's path is its identity, and staleness is decided by timestamp.**
Snakemake keys the metadata of each output by its path ([persistence](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/persistence/__init__.py#L372-L374)), and reruns a job when an input is newer than its output.
Many flags follow from this: `ancient()` ignores an input's timestamp, `--touch` marks outputs current without running, `--latency-wait` waits for late files, and two rules that can produce one path raise `AmbiguousRuleException`.
Moving a file loses its metadata.
For us: a reference names an output of a record or a dataset identity, and a record never changes ([README](../README.md#requests-and-records)), so none of these flags has a counterpart.

**Recording provenance stayed; acting on it automatically split the users.**
Snakemake 7.8.0 (May 2022) reruns a job when its parameters, code, set of inputs, or software environment changed, not only when an input is newer ([#1694](https://github.com/snakemake/snakemake/issues/1694)).
Users who upgraded saw whole pipelines rerun, and removed whitespace or a renamed variable in a rule triggered reruns downstream ([#1677](https://github.com/snakemake/snakemake/issues/1677)).
A bug made the environment trigger fire on unchanged environments; 7.8.1 fixed it (same thread).
The maintainer kept the new default and pointed users to `--rerun-triggers mtime`, which shipped with 7.8.0, and to `--cleanup-metadata <file>`, which existed before, to silence a trigger.
He reported as many requests for the new behaviour as complaints about it.
Every planned job prints the reason it runs ([FAQ](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/project_info/faq.rst#L526-L531)).
For us: the trigger loop counts a dataset as handled once a record under the rule's label names it, and no record names the rule ([automatic-reduction.md](../automatic-reduction.md#the-trigger-loop)).
So changing a rule's template reduces no handled dataset again, and whether a stored result is out of date stays the user's decision.
If a story asks for it, borrow `--list-changes params` ([cli.py](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/cli.py#L1230-L1236)): a query listing the records under a label whose request differs from what the rule would submit now, for the user to resubmit.

**What a record of a run holds, and at what grain.**
For each output, Snakemake stores the rule, input paths, parameter values, the code, checksums of small inputs, a hash of the software environment, and a list of package names and versions ([MetadataRecord](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/persistence/__init__.py#L32-L49), [filled](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/persistence/__init__.py#L428-L450)).
The code is the text of the shell command or of the rule's Python block, so a cosmetic edit changes it ([`_code`](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/persistence/__init__.py#L636-L643)).
For us: provenance lists software versions ([README, Provenance and publication](../README.md#provenance-and-publication)), not yet implemented, and what a finish records is open ([ADR 0002, Open](../adr/0002-a-value-lives-while-something-keeps-it.md#open)).
Package names and versions, as Snakemake's list holds, is a grain that cosmetic edits do not change.
The metadata format is at version 7, and each rerun check skips records written before the version that first stored its field reliably ([persistence](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/persistence/__init__.py#L588-L622)).
That is one way to keep old event formats readable as long as the log is kept ([ADR 0004](../adr/0004-history-is-append-only-lists.md#consequences)).

**Outputs are deleted on failure; logs survive, but only what the author redirects into them.**
Snakemake deletes a failed job's outputs, so a half-written file is never taken for a result.
Files named under `log:` are kept, but the directive captures nothing: the author redirects stdout and stderr into them ([rules.rst](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/snakefiles/rules.rst#L1407-L1422)).
On Slurm, failures such as `OUT_OF_MEMORY`, `TIMEOUT`, and `NODE_FAIL` come from the batch system, not from the job ([Slurm executor](https://github.com/snakemake/snakemake-executor-plugin-slurm/blob/8c2af56eb7dd1e0b78866691d6981f1d8327e0db/snakemake_executor_plugin_slurm/__init__.py#L1270-L1279)).
For us: the in-process backend records the message of the workflow's Python exception.
A segfault in a C extension, or a process killed for memory, raises none.
On the service (scipp/essapps#27), a failed record needs the workflow's console output and the batch system's reason, captured by the framework rather than by each binding.
Because its outputs are gone, Snakemake retries a failed job at the next invocation; our trigger loop counts a failed record as handled, and running it again is the user's decision ([automatic-reduction.md](../automatic-reduction.md#the-trigger-loop)).

**A shared file system shows files late, and the batch system reports late.**
After a cluster job ends, Snakemake waits up to `--latency-wait` seconds, 5 by default, for its outputs to appear ([cli.py](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/cli.py#L1389-L1397)); open issues report slow scheduling on high-latency file systems ([#2920](https://github.com/snakemake/snakemake/issues/2920), [#4240](https://github.com/snakemake/snakemake/issues/4240)).
The Slurm executor polls `sacct` or `squeue`, first after 40 s and then at most every 180 s, since Slurm forgets finished jobs after `MinJobAge`, 300 s by default ([Slurm executor](https://github.com/snakemake/snakemake-executor-plugin-slurm/blob/8c2af56eb7dd1e0b78866691d6981f1d8327e0db/snakemake_executor_plugin_slurm/__init__.py#L1280-L1296)).
For us: whether the service may submit cluster jobs is open ([systems](../../requirements/systems.md)).
If it does, a record persisted at submission should complete when the worker reports that its writes are closed, not when the backend sees a file.
Completion learned by polling Slurm lags by up to minutes, which matters for automatic reduction at about one request per second (scipp/essapps#27).

**Resources: declared by the author, overridden by the site, grown on retry.**
A rule's `resources:` may be a function of its inputs and of `attempt`, the retry number, so that memory doubles on each retry ([rules.rst](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/snakefiles/rules.rst#L1181-L1217)).
A profile overrides them per rule (`--set-resources rule:mem_mb=...`); without either, Snakemake assumes `min(max(2*input.size_mb, 1000), 8000)` MB ([resources.py](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/resources.py#L917-L924)).
For us: only the binding knows the memory a request needs, and the plain request over two plus two LoKI runs needs 7.5 to 9.9 GB ([ADR 0002, Open](../adr/0002-a-value-lives-while-something-keeps-it.md#open), scipp/essapps#27).
Snakemake's three layers (workflow author, site, default from input size) are one shape for that answer.

**Data-dependent fan-out in the scheduler.**
A checkpoint makes the graph depend on a rule's output: an input function calls `checkpoints.<name>.get()`, which raises `IncompleteCheckpointException` until the checkpoint has run, and Snakemake then evaluates the graph again ([rules.rst](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/snakefiles/rules.rst#L3034-L3068)).
It works only in input functions, and checkpoint outputs read this way are never temporary.
For us: a separate reduction for each value found only in the data is a non-goal ([requirements](../../requirements/README.md#non-goals)); a driver that reads a first reduction's outputs and then submits is ordinary code ([README, Drivers](../README.md#drivers)).

**A lock without liveness.**
Snakemake locks by writing the paths of its inputs and outputs into the metadata folder, and deletes them on exit ([persistence](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/src/snakemake/persistence/__init__.py#L478-L511)).
A machine powered off mid-run leaves a stale lock, which the user removes with `--unlock` ([FAQ](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/project_info/faq.rst#L119-L123)).
For us: a backend started on history that another backend still writes is refused ([system story H2](../system-stories.md#h2-backend-upgrade-with-runs-in-flight)).
The in-process backend holds its log file with `flock`, which the kernel releases when the process dies; a service backend needs the same property, never a lock that a person removes.

**Temporary and protected outputs.**
`temp()` deletes an output once every consumer has run, and `protected()` write-protects an expensive one ([rules.rst](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/snakefiles/rules.rst#L2132-L2160)).
The workflow author marks them, since the author knows which outputs are throwaway.
For us: nothing is written unless persisted, and a rule persists all outputs unless it names some ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md), [automatic-reduction.md](../automatic-reduction.md#rules)).
If a story asks, an author's mark on outputs meant only for inspection could set that default.

**Between-workflow caching is opt-in and narrow.**
A rule may opt into a cache shared across workflows, keyed by a Merkle hash of code, parameters, software environment, and raw input ([caching.rst](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/executing/caching.rst#L11-L57)).
The rule must take every parameter through `params:`, since the hash sees nothing else.
The cache is readable by all users, so it is not for private data, and the documentation calls it experimental.
For us: running a request again makes a new record, and nothing is reused across requests.
If a story ever needs reuse, this is its shape: opt-in per spec, keyed by content, never across proposals.
The per-request fixed cost in scipp/essapps#27, the can and direct beam computed again for each run, is a different problem: warm workers within one rule, not a shared cache.

**Sample sheets are workflow code, not an engine feature.**
Snakemake's guide puts per-sample configuration in a table that the Snakefile reads with pandas, or in a PEP file, validated against a JSON schema ([best practices](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/snakefiles/best_practices.rst#L42), [configuration.rst](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/docs/snakefiles/configuration.rst#L64-L194)).
The engine knows nothing about what a column means.
For us: this supports scipp/essapps#33, which moves pairing and batch configuration into applications and leaves the framework to submit one request per row and validate it.

**Plugins added late.**
Up to Snakemake 7 the Python API was one function, `snakemake()`, with 146 parameters that mirror the command-line flags (7.8.0 sdist).
Snakemake 8 (December 2023) replaced it with dataclass settings and moved executors and remote storage behind plugin interfaces, breaking existing usage ([CHANGELOG](https://github.com/snakemake/snakemake/blob/4523356973a83aa237a121717ee81dd2e09175da/CHANGELOG.md#L2195-L2218)).
For us: the service's executor is undecided (scipp/essapps#27); its interface is cheaper to keep narrow from the first implementation than to retrofit.

## Borrow

- Package names and versions in provenance; a version on each history entry.
- The workflow's console output and the batch system's failure reason on a failed record, captured by the framework.
- Completion reported by the worker after its writes are closed, never inferred from a file appearing.
- Memory declared by the binding's author, overridable by the site, with a default from input size.
- If asked for: a query for records whose request differs from what a rule would submit now.

## Do not borrow

- Rerunning on changed parameters, code, or environment; whether a result is out of date is the user's decision.
- Identity by path and staleness by timestamp.
- Data-dependent fan-out in the scheduler (checkpoints).
- A lock that a crash leaves behind.
- An optimising scheduler: its integer linear program gives up after 10 s and falls back to a greedy one, and users report the solver blocking for minutes ([#1374](https://github.com/snakemake/snakemake/issues/1374), [#871](https://github.com/snakemake/snakemake/issues/871)).
- A shared cache across workflows by default.
