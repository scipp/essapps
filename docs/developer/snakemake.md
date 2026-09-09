# What Snakemake's history says about the sketch

Companion to [architecture.md](architecture.md), [staging.md](staging.md), and [stateless.md](stateless.md).
The sketch rejects workflow engines, Snakemake among them, because none gives the stateless request the design rests on.
This note asks a different question: Snakemake has run scientific pipelines for over a decade, with a public issue tracker and a changelog that records every reversal, so what did it learn that applies here, and what did it get right that we should copy?
Facts below were checked against Snakemake's documentation source and issue tracker on 2026-09-09; the sources are listed at the end.
Analysis, not decisions; the last section says what was folded into the sketch and what was not.

## Snakemake in this document's terms

A Snakemake workflow is a set of rules.
Each rule declares its output files by name pattern, its input files, non-file parameters, and the code that runs.
The user asks for output files; Snakemake infers backwards which rules produce them and builds the graph.
A file's path is its identity, and a file is stale, and rerun, when an input is newer than it, the rule Make has used since 1976.
One invocation works on one directory, keeps its state in a hidden folder there, and exits when the requested files exist.
Batch is many wildcard values; automatic reduction is a scheduler re-invoking the workflow and relying on it to skip what already exists.

## What it learned the hard way

**Identity by path and staleness by timestamp.**
Most of Snakemake's special cases descend from this one inheritance: `ancient()` to declare an input whose timestamp must be ignored, `--touch` to fake completion, `--latency-wait` for filesystems that show a file late, `--cleanup-metadata` to forget what is known about a file, and ambiguity errors when two rules could produce one path.
Moving or renaming a file loses its history, because the history is keyed by the path.
The sketch's reference, "output X of record Y", has no path and no timestamp; a record is complete and immutable, and staleness is not a concept.
This is the largest single thing the sketch does right, and Snakemake's issue tracker is the evidence.

**Recording provenance was right; acting on it automatically was not.**
Until version 7.8, in May 2022, only a newer input triggered a rerun.
Version 7.8 began recording parameters, code, the input set, and the software environment per output, and rerunning when any of them changed.
Users upgrading found whole pipelines rerunning; a renamed variable or removed whitespace in a script triggered reruns downstream; a bug made the environment check fire on nothing.
Within a week the maintainer added `--rerun-triggers mtime` to restore the old behaviour and `--cleanup-metadata` to silence a trigger by hand, and wrote that the change "led to exactly the situation I wanted to avoid".
The recording stayed and users came to rely on it; the FAQ now explains that a rerun is always justified by a listed reason.
Two lessons.
The record must carry resolved parameters, code identity, and environment; the sketch does this, and Snakemake's users eventually agreed it was necessary.
And the framework must never decide on its own that a stored result is stale or, its mirror, that a request need not run; the sketch's explicit recompute and the rule that the backend never skips an equal request are the same stance.
A third, smaller lesson: hashing code was too fine a grain, because cosmetic edits changed the hash.
The sketch's optional code revision, at the grain of a package version or a commit, is the right grain.

**A shared filesystem shows a file late.**
`--latency-wait` exists because a file written by a cluster job is not visible to the submitting host for seconds to minutes over NFS; Snakemake polls for the output's existence and gives up after the wait.
The sketch's completion marker is the fix, provided that the marker is the only signal and is written after every output is flushed; the sketch now says so.

**Outputs vanish on failure; logs must not.**
Snakemake deletes a rule's outputs when the rule fails, so that a half-written file is never mistaken for a result.
The `log:` directive exists for the one file that must survive that deletion.
The sketch had a structured failure reason and nothing else.
A structured reason covers the failures that were foreseen; a segfault in a C extension or a process killed for memory only shows up in stdout and stderr.
The record now says where the runner's console output is kept.

**Data-dependent fan-out in the scheduler.**
Checkpoints, added in 5.4, let the graph depend on a rule's output: an input function calls `checkpoints.name.get()`, which raises an exception if the checkpoint has not run, and the scheduler catches it and re-evaluates the graph later.
The documentation carries a list of caveats, checkpoint outputs cannot be temporary and are not rerun when missing, and the mechanism is widely regarded as the most confusing part of the tool.
The sketch says no current workflow needs keys known only after reading the data.
When one does, the trigger loop is the mechanism: a rule on the completed producer instantiates a template per key.
That keeps the scheduler at one primitive, and the sketch now names it.

**Stale locks.**
Snakemake locks the working directory against a second instance.
A crash leaves the lock behind, and `--unlock` exists to remove it by hand; the FAQ lists power loss as the usual cause.
The sketch's lock is renewed by a live backend and lost by a dead one, which is a lease, and removes the manual step.

**An optimising scheduler.**
Snakemake's job scheduler solves an integer linear program to pick jobs under resource limits and to free temporary files early.
The solver hung often enough on large graphs that a `--scheduler greedy` fallback was added.
The sketch's scheduler has one primitive and no optimisation; keep it that way.

**An API that was a command line in disguise.**
For a decade the Python API was one function whose keyword arguments mirrored the command line, and nobody could embed it.
Snakemake 8, in late 2023, rewrote it as dataclass settings.
The same release moved every executor and every remote-storage provider behind versioned plugin interfaces, after the monolith had accumulated more than a dozen executors that nobody could test; third-party executors had to be rewritten.
The sketch's D9, the Python interface as the API with HTTP as a transport, is the lesson applied.
The launcher and the data store are the two seams where the sketch swaps implementations; both should be narrow and stable from the first implementation, because retrofitting an interface under existing implementations is what cost Snakemake a major version.

**Nesting workflows.**
`subworkflow`, which ran one workflow as a step of another with its own state, was deprecated for `module`, which includes another workflow's rules into the current graph, and removed in version 8.
Composition by chaining separate specs through references, as D4 does, is the surviving model.

## What paid off

**Declared outputs as the interface.**
A rule is known by what it produces, and the user names outputs, not steps.
The sketch's typed output model and "outputs can be inputs" are the same idea without the path.

**Dry run with reasons.**
`--dry-run` is the most used flag, and since 7.8 every planned job prints why it will run: a changed parameter, a missing input, updated code.
The sketch's validate operation separate from submit, and the record diff that slot inspection shows, cover this for runs.
The trigger loop now offers the same for datasets: why it fired or did not.

**Software environment per rule.**
A rule may name a conda environment or a container; Snakemake creates it, hashes it, and records the hash on every output.
This is a large part of why Snakemake pipelines reproduce years later.
The sketch records the environment as an opaque name and revision; at ESS environments are managed elsewhere, so recording and comparing is the right scope.

**Resources with an attempt number.**
A rule declares memory and threads, possibly as a function of input size and of how many times the job has been retried, so that memory doubles on retry.
This is what made cluster execution usable.
The sketch defers the cluster launcher; resource hints on the spec are now listed with the deferred items, and a retry record knows its attempt number from its link to the record it retries.

**Temporary and protected outputs.**
`temp()` deletes an output once every consumer has run; `protected()` write-protects one that was expensive.
Authors get these right, because they know which outputs are throwaway.
The sketch's retention question is per kind of run; an output-level flag on the spec is a better input to the policy, and is now the bracketed recommendation on that question.

**Between-workflow caching, done carefully.**
Since 5.8 a rule may opt into a shared cache keyed by a Merkle hash of its code, parameters, software environment, and input content.
It is opt-in per rule, refused for rules that read parameters the hash cannot see, and documented as unsuitable for private data and non-deterministic steps.
The sketch's second review pass removed identical-request reuse from the backend, and this note does not argue for putting it back.
If cross-user memoization is ever wanted, this is its shape: opt-in per spec, keyed by content, never a default.

**Sample sheets.**
Snakemake's integration with Portable Encapsulated Projects made a table with a key column the way to define a batch.
The sketch's batch, a template plus per-member overrides and a member key, is that table.

**Tests from real runs.**
`--generate-unit-tests` turns a successful run into a regression test with its real inputs and outputs.
A complete record already is that fixture.
The sketch now has a helper that recomputes a record and compares outputs, so that a workflow package can keep production records as regression tests.

**The notebook path.**
`--edit-notebook` opens a rule's code in Jupyter with its inputs loaded, and the edited notebook becomes the rule.
The sketch's in-process binding for notebooks, refused for publication unless the client overrides, is the same path in the other direction: develop against real inputs, then promote to a package entry point.

## Where the model does not transfer

Snakemake is one invocation over one directory that infers the graph backwards from requested file names.
Nothing in it addresses a request submitted over time, a warm workflow, a session, a slot, or a view.
Automatic reduction with Snakemake is a cron job re-invoking it, which works only because of skipping by file existence, and which the sketch rejects for the reasons in the 7.8 story.
So its lessons bear on shared mode, batch, and failure handling, and not on choices 1, 3, or D10.

## What was folded into the sketch

| Lesson | Change to the sketch | Where |
|---|---|---|
| Logs survive when outputs do not | Record says where the runner's console output is kept; logs kept as long as the record | Records and references; Failure handling |
| Shared filesystems show files late | Marker written after outputs are flushed; the only signal reconciliation trusts | Failure handling |
| Skipping done work is a user decision | Batch rerun of failures is a client operation; backend never skips an equal request; record store answers "records whose resolved request equals this one" | Choice 2; Components |
| Checkpoints | Fan-out with keys known only from data is a trigger rule, not a scheduler feature | Spec changes |
| Dry run with reasons | Trigger loop explains why it fired or did not on any dataset | Components |
| Plugin retrofit cost a major version | Launcher and data store interfaces narrow and stable from the first implementation | Components |
| Resources with attempt | Resource hints on the spec, deferred with the cluster launcher | Explicitly deferred |
| temp and protected | Recommendation on the retention question: an intermediate flag on outputs | Open questions |
| Tests from real runs | Helper that recomputes a record and compares outputs | Choice 3 |

Not folded in, and why:

- **Automatic rerun on changed parameters, code, or environment.** The 7.8 story; the sketch keeps recompute explicit.
- **Skipping by output existence, or a between-workflow cache in the backend.** Removed in the second review pass; the client-side query above is enough for batch rerun, and the opt-in cache shape is recorded here in case it is ever wanted.
- **Path patterns and graph inference.** Not applicable; the sketch's graph is explicit in the request.
- **Software environment creation.** Out of scope; ESS manages environments, and the sketch records and compares.
- **A lock file with a manual unlock.** The sketch's lease already avoids it.

## Sources

- [Change in rerun behavior in 7.8.0, issue 1677](https://github.com/snakemake/snakemake/issues/1677) and [the announcement, issue 1694](https://github.com/snakemake/snakemake/issues/1694)
- [Between-workflow caching](https://snakemake.readthedocs.io/en/stable/executing/caching.html)
- [Rules: checkpoints, log files, resources, temporary and protected files](https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html)
- [Migration to Snakemake 8](https://snakemake.readthedocs.io/en/v8.19.2/getting_started/migration.html) and the [executor plugin interface](https://github.com/snakemake/snakemake-interface-executor-plugins)
- [FAQ: locks, latency, rerun reasons](https://snakemake.readthedocs.io/en/stable/project_info/faq.html)
