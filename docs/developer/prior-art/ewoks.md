# Ewoks, and whether to build on it

> Unmaintained study, written on 2026-09-21 against the design at commit `4284493`.
> Ewoks sources read: the twelve repositories of [github.com/ewoks-kit](https://github.com/ewoks-kit) at that date (`ewoks` 7.0.0rc1, `ewokscore` 5.1), the PyPI source archives of `blissoda` 1.11, `ewoksxrpd` 1.10, `ewoksfluo` 1.3, `ewoksdata` 2.2, `est` 2.1, and `pypushflow` 2.0, and the slides of the introduction given by the lead developer at the NOBUGS 2026 satellite meeting.
> A spike ran `ewoks` 6.0.0, `ewokscore` 5.0.0, and `ewoksjob` 1.5.0.
> The documentation websites and `gitlab.esrf.fr` were not reachable, so the technique packages were read as released archives, without their history or issue trackers.

The question is whether ewoks could replace parts of this design, or carry it, so that less has to be written here.
The requirement is that the same things can be done, not that they are done the same way.

## Ewoks in this document's terms

Ewoks is the workflow system of the ESRF.
The slides report online data analysis at 38 ESRF beamlines, built over five years by a unit of about fifteen people.

A **task** is a Python class with named inputs and named outputs.
A task may declare them with pydantic models (`input_model`, `output_model`); the older style is a list of names without types.
A task is identified by its import path. It has no version.

A **graph** is a JSON document.
Nodes name tasks by import path and carry default input values.
Links map a named output of one node to a named input of another.
A graph may carry a `requirements` section: interpreter, operating system, every installed distribution with its version, and the lock files of pip, uv, or pixi.

`execute_graph(graph, inputs=[...])` runs a graph on an **engine**.
Four engines exist: a sequential one in `ewokscore`, pypushflow (loops, conditional links, a local process pool), dask (threads, processes, distributed), and Orange (a Qt canvas where each task is a widget).
An engine is a subclass of `WorkflowEngine` registered under the `ewoks.engines` entry-point group.

`ewoksjob` sends a graph and its inputs to a Celery worker and returns a future.
A worker started with `--pool=slurm` forwards each job to Slurm through Slurm's REST interface, using `pyslurmutils`.
`ewoksserver` and `ewoksweb` are a graph editor with an execute button.
`blissoda` connects the ESRF acquisition system BLISS to `ewoksjob`: a scan starts, a processor object held in the BLISS session submits a graph.

Ewoks calls the executed graph document, saved beside the results, the provenance of those results.

Graphs in production are small.
The 63 graphs shipped with `blissoda` have between 1 and 17 nodes, with a median of 6.
One task is one processing step over a whole scan, for example "integrate all frames of this scan with pyFAI and write NeXus".

## Where the two designs sit

Ewoks answers one question: how to describe a pipeline of coarse steps as a portable document that several engines and several interfaces can execute.

This design has two layers where ewoks has one.
Inside a workflow, sciline builds a fine-grained graph from Python types, and the framework never sees it.
Between workflows there is no graph document. There are records, and references between records.

An ewoks graph is fixed when it is submitted.
The structure between our records grows after submission: a rerun under a label, a series that gains a member, a rule that fires on a new dataset, a recompute that links to an older record.
Most rows marked "absent" below follow from this difference, and it is a difference of purpose, not an omission on either side.

## Component by component

| Our component | Closest part of ewoks | Verdict |
|---|---|---|
| Spec: parameter model, output model, name and version | task with `input_model` and `output_model` | Partial. The same idea for one task. No version, and no model for a graph as a whole. The class carries the model, so reading it imports workflow code. The pydantic style is still being redesigned upstream. |
| Callable `f(params, inputs) -> dict` | `Task.run()` reading `self.inputs`, writing `self.outputs` | Equivalent for one task. |
| Request as plain data | graph plus `inputs` list | Partial. Both are JSON. The ewoks document exposes every node; ours names one spec. |
| Record | none | Absent. What happened is spread over an optional event log, an optional graph dump, and output files. None is written by default and nothing ties them together. |
| Reference "output X of record Y" | `DataUri`: a storage location that contains a hash | Different. The hash covers input values and the task class, so it is a key for reuse, not an identity. It is accepted as a Python object only; the same reference written as a JSON dict is passed on as a literal value. |
| "Never skip or recompute silently" | with `varinfo` set, a task whose hashed output file exists is skipped, and nothing records that it was | Opposite policy. It is opt-in, and it is the behaviour that Snakemake's users objected to in 2022 (see [snakemake.md](snakemake.md)). |
| Resolved parameters and package versions in the record | `requirements` section of a saved graph | Partial, and better in one respect. The content is richer than our three version strings. It is captured only on an explicit save, not on each execution. |
| Record store with queries | event handlers for SQLite and Redis | Absent. The events are a log of start and end per job, workflow, and node. No parameters, no versions, no links between jobs, no scope by proposal, no query by dataset. In the spike, a workflow with a failed node reported `error=0` at workflow and job level when run with `raise_on_error=False`. |
| Pending output as input, groups | links inside one graph | Absent between jobs. No Celery chain, chord, or group is used anywhere. A request submitted later cannot wait on an earlier job. |
| Session, stages | none. Orange re-runs every widget downstream of a change, completely | Absent. The disk cache above is the nearest mechanism. It hashes every input value on each call and passes data through files, which does not meet reruns under a second over gigabytes held in memory. It would also need the inner graph cut into ewoks tasks. |
| Labels, supersede, retry as a new record, recompute | none | Absent. |
| Views | none. `ewoksweb` and `ewoksserver` never carry output data; plots are hand-written silx widgets, Flint, or h5web reading files | Absent. |
| Throwaway runner | `ewoksjob` worker; also a local mode on a process pool without a broker | Equivalent in function. See the launcher question below. |
| Cluster launcher (not built) | `ewoksjob` Slurm pool on `pyslurmutils.SlurmRestExecutor` | The one real candidate for reuse. |
| Dataset source, template, lookup with as-of fill, rule, `apply` | `blissoda` processor classes with mutable parameters in Redis | Absent. A calibration is "the newest file matching a pattern" or live instrument state at trigger time, so reprocessing a backlog does not reproduce what the live run chose. About twenty beamlines each have a subclass. |
| Trigger loop without memory | BLISS scan callbacks in the acquisition process, or the `blissoda` workflow server (`app/workflow_server`, 46 lines), which follows new scans in BLISS's Redis store and submits whatever job the scan's own metadata field `workflows` contains | Different, and bound to BLISS. The acquisition side chooses workflow and parameters and writes them into the scan; the listener only forwards. It starts from the newest scan at startup, so scans that arrive while it is down are not processed. Processing reads the file while the scan runs, which is esslivedata's territory here. |
| Batch table as a query, reprocess | hand-written loops per beamline over a scan cache in memory; `ewoksreprocess` 0.2, a small Qt widget library used by the ID31 reprocessing application, lists submitted jobs with their state and can cancel them | Absent as a stored table. The `ewoksreprocess` list is a set of futures in the memory of the application, so it is lost when the window closes and cannot be asked which scans failed last week. |
| Contribute and combine with `carry` | one task with a list input that re-reads all members on each call | The alternative that [aggregation.md](../aggregation.md) rejects. |
| Explicit publication to SciCat with a provenance snapshot | `upload_parameters` on `execute_graph`, which calls `pyicat_plus` when the run succeeds | Different policy, and ICAT only. A flag per processor uploads every processed scan. The source says re-uploading the same folder is allowed. The ICAT entry carries paths and a few metadata values. No SciCat code exists in any source read, although the slides show SciCat as the portal at DESY P08. |
| Sciline adapter | none; sciline is not mentioned in any repository | Ours to write in either case. Wrapping a pipeline as one task took ten lines in the spike and has the shape of `PipelineAdapter`. |
| Storing scipp outputs | both persistence schemes pickle the value | Does not work. A scipp `DataArray` cannot be pickled (scipp 26.8.0), so ewoks persistence fails on it. |

## Could we work differently, to use it?

**Ewoks graphs in place of sciline graphs.**
No.
Ewoks wires by output and input names, one link at a time; sciline wires by type, and our workflows have tens of providers.
Each ewoks edge costs a task instance, an event context, and either pickling or a file.
Nobody at ESRF uses ewoks at that granularity.

**An ewoks graph as the run request, with `ewoksjob` and `ewoksserver` as the service.**
A spec would become a one-node graph and a group a multi-node graph.
We would get Celery dispatch, the Slurm pool, a graph editor, and documents that ESRF tools can read.
We would still write the record store, references, pending outputs across submissions, sessions and stages, labels, views, rules and lookups, aggregation with `carry`, SciCat publication, and a persistence scheme for scipp.
That is every component of [architecture.md](../architecture.md#components) except the launcher and part of the runner.
We would take on a broker service, Celery's result backend as a second source of truth beside our records, a server without authentication, and a dependency with about one breaking change per package per year.
The gain is small and the cost is permanent.

**`ewoksjob` as the cluster launcher only.**
Possible, with each request as a one-node graph.
It brings Celery and a broker, which [operations.md](../operations.md) has so far avoided and marks as a question for exactly this moment.
Two properties do not fit without work.
`ewoksjob` sets no retry or redelivery options, so a job is lost when its worker dies; our completion markers would still do the reconciliation.
A Slurm worker holds one Slurm user and token (read from the how-to guide, not tested), while our design runs cluster jobs under the submitting user.
`pyslurmutils` alone gives the useful part, a `concurrent.futures` executor over Slurm's REST interface, without Celery.
Whether it applies depends on whether the ESS cluster runs `slurmrestd`, which this study could not find out.

**Our specs offered as ewoks tasks.**
Both contracts are "pydantic model in, named outputs out", so a generator from a spec to a task class is small.
It would let an ESS workflow run in an ewoks graph at another facility.
Nothing needs it today, and it needs no change to the design, so it can wait for a request.

## What to take from it

- **Environment capture.**
  `runner.package_versions()` records three version strings.
  Ewoks records every distribution from `importlib.metadata.distributions()`, and reads each distribution's `direct_url.json` (PEP 610) to get the git commit of a package that was not installed from an index.
  That is standard library only, and it closes a real gap: a record made from a development install currently cannot say which commit ran.
  The lock-file part, and `ewoks install` to rebuild an environment, show what `environment` could grow into.
- **Operations, as a layout and not as code.**
  ESRF runs Flower and a Grafana dashboard (worker health per beamline, failures per minute, workflows per minute, success rate) for 38 beamlines.
  Neither is part of ewoks.
  `ewoksjob monitor` is an alias of `celery flower`, and the ewoks sources contain no metrics code and no dashboard definition.
  Flower exports Prometheus metrics from Celery's events (`flower_worker_online`, `flower_events_total` by worker, event type, and task, `flower_task_runtime_seconds`), which is enough to build that dashboard; that ESRF built it this way is an inference.
  All of it exists only with Celery, and "worker health" describes long-lived workers, which our shared mode does not have.
  Here the same numbers are queries over the record store, grouped by status, instrument, and time, and the task table with its failure page is the status page already planned for phase 1.
  Flower is an operator's tool without scoping by proposal, so it would not replace that page even with Celery underneath.
  Their four numbers are a reasonable first set for our deferred metrics, with one addition that `ewoksjob` cannot give: the number of waiting and pending records per instrument.
  ESS already runs Grafana, so the display exists; what remains is for each backend to export these numbers in the format of the time-series store that feeds it.
- **Evidence for decisions already taken.**
  The `WorkerPool` in `ewoksxrpd`, a process-global cache that a task author wrote and keyed by a hash of the configuration, is the "state kept inside workflow code" that [stages.md](../stages.md) argues against.
  The list-input sum that re-reads every member is the cost `carry` removes.
  The newest-file calibration is the case for as-of fills anchored to the dataset.
- **`pyslurmutils`**, as a candidate when the cluster launcher is built.

## The dependency, if we took it

All `ewoks-kit` repositories are MIT. GPL enters only through Orange, in `ewoksorange`.
`ewokscore` has 93 releases on PyPI since July 2021; the GitHub organisation dates from December 2025, so the public issue history is short.
Three ESRF developers wrote 90 % of the 2020 commits to the eight core repositories in the last 24 months, and one of them wrote 58 %.
The technique packages and `blissoda` are developed on ESRF's GitLab.
No contributor from outside ESRF was found; an issue containing a P08 path confirms DESY as a user.
There is no governance document, roadmap, or deprecation policy, no `py.typed`, and no type checking in CI.
CI covers Python 3.8 to 3.14 on two operating systems.
The core is about 33 000 lines of Python.
The project is active and well engineered, and it is steered by one unit at one facility.

## Recommendation

Do not build on ewoks, and do not change the design toward it.
Ewoks does not contain the parts this design spends its effort on, and the part it does contain, a static graph of coarse tasks, is the part this design replaced with references between records.
The overlap is the launcher and runner, which are the smallest components here.

Three follow-ups, none urgent:

1. Extend `package_versions()` to all distributions with PEP 610 origins.
2. When the cluster launcher is built, try `pyslurmutils` first, and decide on a broker then.
3. Keep the door open for a spec-to-task generator, and build it when someone asks.

This recommendation would change if the ESS cluster could only be reached through something `ewoksjob` already handles, or if DESY's SciCat registration turned out to be reusable as it stands.

## Questions for the ewoks developers

- How does P08 register processed data in SciCat: a task at the end of the graph, an event handler, or code outside ewoks? Is it published?
- The slides name "ewoks plugins" for portal registration. No entry-point group for that exists in the sources. Is it planned?
- Is the workflow-level `error=0` after a failed node, with `raise_on_error=False`, intended?
- How is a backlog reprocessed at a beamline so that it picks the calibration that was valid for each scan?
- Does the portal start reprocessing through `ewoksserver`'s `/execute` endpoint, as the architecture slide suggests? With which authentication?
- Do Slurm jobs run under a beamline account or under the visiting user?
- Is a graph-level parameter model planned, beyond `workflow_input_schema`?

## Sources

- `ewokscore`: `task.py` (`execute`, the skip on `self.done`), `hashing.py`, `variable.py`, `persistence/`, `graph/schema/model.py`, `graph/execute/sequential.py`, `events/`, `doc/explanations/implementation.rst`, `doc/howtoguides/task_output_caching.rst`.
- `ewoks`: `bindings.py` (`_upload_context`, `convert_destination`), `_requirements/`, `doc/howtoguides/requirements.rst`.
- `ewoksjob`: `worker/slurm.py`, `client/local/`, `doc/howtoguides/slurm.rst`. `ewoksserver`: `app/routes/execution/router.py`, `README.md`.
- `ewoksutils`: `event_utils.py`.
- `blissoda` 1.11.0: `xrpd/processor.py`, `fluo/processor.py`, `persistent/parameters.py`, the shipped graph files. `ewoksxrpd` 1.10.0: `tasks/worker.py`.
- `ewoksreprocess` 0.2.0: `submit/workflow_executor.py`, `submit/jobs_list_widget.py`. `ewoksid31` 1.1.0, `darfix` 6.0.4, and `ewoks3dxrd` 1.2.0 were searched for SciCat and reprocessing code only.
- W. De Nolf, "Extensible Workflow System", introduction at the ewoks satellite meeting, NOBUGS 2026, 21 September 2026.
- W. De Nolf et al., Synchrotron Radiation News, 2024, [doi:10.1080/08940886.2024.2432305](https://doi.org/10.1080/08940886.2024.2432305).
