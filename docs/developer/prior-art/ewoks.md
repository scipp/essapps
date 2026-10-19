# Ewoks as prior art for batch and automatic reduction

Ewoks is the workflow system of the ESRF, developed by its Data Automation Unit.
Its lead developer reports it at 38 ESRF beamlines, and one workflow in use at DESY P08 (W. De Nolf, introduction to the ewoks satellite meeting, NOBUGS 2026, slides 23 and 24; see also [the 2024 paper][paper]).
We read it to learn whether it could run our batch and automatic reduction, or the service ([#27][i27]), so that we write less.

Sources, read on 2026-10-13: the `ewoks-kit` repositories on GitHub (`ewokscore` [c17fe04][core], `ewoks` [88c4394][ewoks], `ewoksjob` [dd0b6ab][job], `ewoksutils`, `ewoksserver`), and the PyPI source archives of `blissoda` 1.11.0, `ewoksxrpd` 1.10.0, `ewoksreprocess` 0.2.0, `pyslurmutils` 1.7.0, and `pyicat-plus` 3.0.1.
`blissoda` and the technique packages are developed on ESRF's GitLab, which was not reachable, so their history and issues were not read.

## What it is

A *task* is a Python class with named inputs and outputs, optionally declared as pydantic models.
A *graph* is a JSON document: nodes name a task by import path, with no version ([model.py L86-L96][node]), and carry default inputs; links map an output of one node to an input of another.
`execute_graph(graph, inputs=...)` runs it on one of four engines: sequential, pypushflow (local processes, loops), dask, or Orange (a Qt canvas, one widget per task).
`ewoksjob` sends a graph to a Celery worker, which can forward it to Slurm.
`blissoda` connects the ESRF acquisition system BLISS to `ewoksjob`.
Graphs are small: the 64 graph files in the `blissoda` 1.11.0 archive have 1 to 17 nodes, median 6, and a task is one step over a whole scan, such as "integrate all frames with pyFAI and write NeXus".
So an ewoks task corresponds to one of our specs with its binding; the fine-grained sciline graph stays inside the binding.

## Findings

**Dependencies exist only inside one graph.**
A graph is fixed when it is submitted, and `ewoksjob` submits graphs independently: no Celery chain, chord, or group appears in any source read.
Here a request may reference an output of a pending record from an earlier submission ([References](../README.md#references)), and the backend runs it once that record has completed.
`ewoksjob` would leave that scheduling to us.

**What happened is kept in events, not in records.**
An event is the start or end of a job, a graph, or a node, with host, user, IDs, time, an error flag, and the storage URIs of inputs and outputs ([event_utils.py L1-L20][events]).
It holds no parameter values, no software versions, and no link from one job to another.
Events, the graph document, and output files are each written only when configured.
With output persistence turned on, a task whose output file already exists is skipped ([task.py L757-L761][skip]), and its start and end events look like those of a run.
Here every request makes a record with every parameter value, and running the same request again makes a new record ([Requests and records](../README.md#requests-and-records), [ADR 0004](../adr/0004-history-is-append-only-lists.md)).

**Values cross processes as pickles, and scipp does not pickle.**
Both persistence formats store anything other than plain scalars, lists, dicts, and numpy arrays as a pickle ([json_pickle.py L1-L13][pickle]).
`pyslurmutils` passes a job's arguments and return value as pickles, over TCP or files.
`pickle.dumps` of a scipp `Variable`, `DataArray`, or `DataGroup` raises `TypeError` (scipp 26.9.0, [scipp/scipp#3940][i3940] open), and scipp registers no serializer for dask either.
So any executor we pick for the service ([#27][i27], which considers dask.distributed) must receive requests as plain data and leave output values in the store, never return them.

**The software environment is captured well.**
A saved graph carries a `requirements` section: operating system, interpreter, every installed distribution with its version, and the package manager's lock or environment file ([base_manager.py L47-L63][req]).
For a distribution installed from a git checkout or URL it reads PEP 610 `direct_url.json` and records the commit or archive hash ([from_python L64-L100][pep610]); this needs only `importlib.metadata`.
`ewoks install` rebuilds an environment from a saved graph, and the main branch (7.0.0rc1, unreleased) adds conda (`conda env export`) next to pip, uv, poetry, and pixi ([conda.py L47][conda]).
`execute_graph` writes the graph with requirements only when given a destination ([bindings.py L79-L81][save]); most `blissoda` processors give one for each saved scan.
Our provenance does not record software versions yet ([README](../README.md), status line), and system story F2 asks for an environment that can be installed again ([system-stories.md](../system-stories.md)).
Ewoks has no task versions: an old run is reproduced by installing its environment, one answer to the question deferred in [#44][i44] of how old spec versions stay runnable.

**At ESRF, the acquisition side decides what to reduce, and with which inputs.**
A `blissoda` *processor* object lives in the BLISS session, keeps its parameters in Redis, and builds the submit arguments for each scan; 21 beamline directories hold processor subclasses.
It either submits from the BLISS process, or writes the complete submit arguments into the scan's metadata, which also lands in the raw file, for a listener to submit ([xrpd/processor.py L215-L234][bxrpd]).
The listener (`app/workflow_server`, 46 lines) starts at the newest scan (`get_last_scan_timetag`), so scans that arrive while it is down are never reduced.
This bears on the one question that blocks our design, how files say what role a run plays ([requirements](../../requirements/README.md#questions-that-block-the-design)): ESRF writes the reduction request itself into the raw data.
At ESS that would need NICOS to write it.
It agrees with [#33][i33]: the configuration per beamline lives in `blissoda`, beside the acquisition system, not in ewoks.
Our trigger loop instead counts a dataset as handled when a record under the rule's label names it, so downtime loses nothing ([automatic-reduction.md](../automatic-reduction.md#the-trigger-loop)).

**Calibrations are chosen from the state at trigger time.**
ID11 takes the last file in a directory by name (`sorted(glob(pattern))[-1]`, `id11/xrpd_processor.py` L113), ID31 reads the live energy motor (`id31/xrpd_processor.py` L151), and the streamline scanner takes the calibration scan nearest the current motor position (`streamline/scanner.py` L445).
Reducing a backlog again therefore gets today's choice, not the one valid for each scan.
For the lookups that applications will provide ([#24][i24], [#33][i33]): match against the run's own metadata, and let the record hold the resolved dataset identity, as it does ([Datasets](../README.md#datasets)).
`ewoksreprocess`, used by the ID31 reprocessing application, lists submitted jobs as futures in a Qt widget (`submit/jobs_list_widget.py`), lost when the window closes; here a batch is found again by its label ([Labels and members](../README.md#labels-and-members)).

**Warm workers pull against memory leaks.**
`ewoksxrpd` keeps pyFAI integrators in a process-global pool keyed by a hash of the configuration (`ewoksxrpd` 1.10.0, `tasks/worker.py` L22-L102).
Celery reuses its worker processes, so global state piles up; restarting after each job (`--max-tasks-per-child=1`) would drop the pyFAI cache and slow EXAFS, which runs a workflow every 2 seconds, so ESRF chose to hunt leaks instead ([ewoksjob#319][i319]).
`ewoksjob` 1.6.0rc2 added per-job memory logging and leak detection for this.
The same pull sits in [#27][i27] (warm workers at about one request per second) and in [tensions](../../requirements/tensions.md) (batch services bound leaks and peak memory by how they work).
ESRF's case suggests: a fresh process by default, and warm state only as a cache the service may drop.

**The Slurm executor is usable without Celery.**
The `ewoksjob` Slurm pool wraps `pyslurmutils.SlurmRestExecutor`, a `concurrent.futures` executor over Slurm's REST daemon `slurmrestd` ([slurm.py L108-L121][slurm]).
A Slurm job may take several tasks before it exits, which gives warm workers on the cluster.
The Slurm user and token are options of the worker process ([options.py L61-L78][opts]), so all its jobs run under one account.
This bears on the open question whether the services submit cluster jobs as the user who asked ([systems](../../requirements/systems.md#open)).
Whether the DMSC cluster runs `slurmrestd` is not known.
`ewoksjob` sets none of Celery's late-acknowledgement options, so with Celery's defaults a job whose worker dies is lost; system story H2 asks that persisted work survive an upgrade.

**Publication goes to ICAT, after a successful run, and returns no identifier.**
`execute_graph(..., upload_parameters=...)` calls `pyicat_plus` `store_processed_data` when the graph succeeds ([bindings.py L98-L127][upload]).
It names inputs by file path and sends a message to a broker, so no identifier comes back.
`blissoda` processors turn it on with the parameter `data_portal_upload`, off by default; once on, every saved scan is uploaded.
No source read mentions SciCat, although slide 24 shows the DESY portal as SciCat and says registration is "ideally not part of the workflow itself", done through "Ewoks events and Ewoks plugins"; no entry-point group for such plugins exists in the sources.
Our `client.publish` is likewise outside the workflow ([Provenance and publication](../README.md#provenance-and-publication)); whether batch results reach SciCat without a person is open ([tensions](../../requirements/tensions.md#open)).

**Monitoring is Celery's.**
The metrics slides (failures, workflows per minute, worker health, success rate) show Celery tooling: `ewoksjob monitor` is an alias of `celery flower` ([__main__.py L17-L18][monitor]), and the sources contain no metrics code.

**The project.**
MIT licensed; `ewokscore` has 93 releases on PyPI since July 2021.
One developer wrote more than three quarters of the commits to `ewokscore`, `ewoks`, and `ewoksjob`; no contributor from outside ESRF was found.
`ewoksserver` has no authentication.

## Borrow

- Record the software environment with PEP 610 origins, from `importlib.metadata`; consider lock files and `ewoks install` as the shape of system story F2.
- Try `pyslurmutils` first for a cluster launcher, once we know whether `slurmrestd` runs at ESS and under which account jobs may run.
- Fresh worker processes by default; warm state only as a cache that may be dropped ([#27][i27]).
- Failures per minute, requests per minute, and success rate, plus pending records per instrument, as the first service metrics ([systems](../../requirements/systems.md#assumed) assumes Grafana).

## Do not borrow

- Ewoks as the run request or the service: it lacks records, references to pending records, accumulators, labels, and SciCat publication, and brings a broker, a second store of job state, and pickled values.
- Calibration chosen from state at trigger time, and a listener that starts at the newest arrival.
- Skipping work because an output file exists.

## Open

- How does P08 register ewoks results in SciCat? A web search found DESY's SciCat test instance at P08 but no ewoks code; the DESY indico pages were not reachable.

[paper]: https://doi.org/10.1080/08940886.2024.2432305
[core]: https://github.com/ewoks-kit/ewokscore/tree/c17fe04c8340272047af07654d1e5832278f9b2b
[ewoks]: https://github.com/ewoks-kit/ewoks/tree/88c43943ab0632888e8d34aa101d754de9adf2be
[job]: https://github.com/ewoks-kit/ewoksjob/tree/dd0b6ab40daf6643e08feb6cd6737e51b23e728e
[node]: https://github.com/ewoks-kit/ewokscore/blob/c17fe04c8340272047af07654d1e5832278f9b2b/src/ewokscore/graph/schema/model.py#L86-L96
[events]: https://github.com/ewoks-kit/ewoksutils/blob/982a82e3ae4ad746ee9d8ea31730e5e6e73003d7/src/ewoksutils/event_utils.py#L1-L20
[skip]: https://github.com/ewoks-kit/ewokscore/blob/c17fe04c8340272047af07654d1e5832278f9b2b/src/ewokscore/task.py#L757-L761
[pickle]: https://github.com/ewoks-kit/ewokscore/blob/c17fe04c8340272047af07654d1e5832278f9b2b/src/ewokscore/_serialization/common/json_pickle.py#L1-L13
[i3940]: https://github.com/scipp/scipp/issues/3940
[req]: https://github.com/ewoks-kit/ewoks/blob/88c43943ab0632888e8d34aa101d754de9adf2be/src/ewoks/_requirements/utils/base_manager.py#L47-L63
[pep610]: https://github.com/ewoks-kit/ewoks/blob/88c43943ab0632888e8d34aa101d754de9adf2be/src/ewoks/_requirements/utils/metadata/from_python/__init__.py#L64-L100
[conda]: https://github.com/ewoks-kit/ewoks/blob/88c43943ab0632888e8d34aa101d754de9adf2be/src/ewoks/_requirements/conda.py#L47
[save]: https://github.com/ewoks-kit/ewoks/blob/88c43943ab0632888e8d34aa101d754de9adf2be/src/ewoks/bindings.py#L79-L81
[upload]: https://github.com/ewoks-kit/ewoks/blob/88c43943ab0632888e8d34aa101d754de9adf2be/src/ewoks/bindings.py#L98-L127
[bxrpd]: https://pypi.org/project/blissoda/1.11.0/
[slurm]: https://github.com/ewoks-kit/ewoksjob/blob/dd0b6ab40daf6643e08feb6cd6737e51b23e728e/src/ewoksjob/worker/slurm.py#L108-L121
[opts]: https://github.com/ewoks-kit/ewoksjob/blob/dd0b6ab40daf6643e08feb6cd6737e51b23e728e/src/ewoksjob/worker/options.py#L61-L78
[monitor]: https://github.com/ewoks-kit/ewoksjob/blob/dd0b6ab40daf6643e08feb6cd6737e51b23e728e/src/ewoksjob/__main__.py#L17-L18
[i319]: https://github.com/ewoks-kit/ewoksjob/issues/319
[i24]: https://github.com/scipp/essapps/issues/24
[i27]: https://github.com/scipp/essapps/issues/27
[i33]: https://github.com/scipp/essapps/issues/33
[i44]: https://github.com/scipp/essapps/issues/44
