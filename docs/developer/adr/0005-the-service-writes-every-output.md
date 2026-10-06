# ADR 0005: The service writes the outputs of every record to a file, and keeps no value for a client

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-10-06

## Context

Batch and automatic reduction run as services, "so leaks and peak memory must be bounded by how the services work, not by users freeing memory" ([users](../../requirements/users.md)).
Users find the results of a batch reduction they started the day before (same page).
An upgrade of the service is assumed to let the reductions running at that moment finish, and those that wait for them ([systems](../../requirements/systems.md), Assumed; system story H2).
The framework does not tell intermediate from final results ([users](../../requirements/users.md)), so a service cannot write only the final outputs.

[ADR 0002](0002-the-client-is-the-lifetime.md) decides what keeps a value in the user's process: the client that made a record keeps its values until it releases them or ends.
That rule fails on the service.
The trigger loop submits through one client and never releases anything, so automatic reduction would keep every output it makes.
A batch client that closes would take its values with it, so the next morning only the records would be left.

On the service, a request may run on another node than the record whose output it reads, and after a restart of the service.
A file is the one form of a value that outlives its client, survives a restart, and reaches another node.

[ADR 0006](0006-the-unit-is-an-accumulating-workflow.md) makes the reduction of one run part of the binding of a multi-run spec, never an output.
So what the service writes are the outputs of requests: results, and inputs worth keeping as results of their own, such as a beam centre or a direct-beam function.

## Decision

- **In the user's process**, [ADR 0002](0002-the-client-is-the-lifetime.md) holds.
- **On the service, the outputs of every record but the records of states of accumulators are written to a file when the record completes** (for those, see below). The store finds a record's file by the record's ID and the output's name; history names no file, so a record never changes. `client.output` of a record, and a reference to an output of a record, read its file.
- **The service keeps no value for a client.** There is no `save=` option and no release of values. A client that ends or vanishes leaves its records and their files, and nothing else.
- **Besides history and these files, the service holds caches, the held states of accumulators, and the outputs of records of states.** A cache, such as what a stage computed or a copy of a file the service has read, may be dropped at any time. How the service holds the held state of an accumulator, and bounds its memory, is open (see Open).
- **Nothing writes the outputs of the record of a state of an accumulator; the service holds them as it holds the held state.** A request that reads a state reads a record of the state's plain request, made at its submission, and those outputs are part of the held state ([ADR 0008](0008-a-read-of-a-state-is-a-record.md)). A driver that reads after every push, as story D7 does, would otherwise write one volume of hundreds of GB per read. If the outputs are lost, the record is computed again as its plain request. The request's own outputs are written like any other's. `client.output` of an accumulator returns the value and writes nothing.
- **The files are dropped with the proposal's history at the latest** ([ADR 0004](0004-history-is-append-only-lists.md)). A record whose file was dropped earlier, for example to free disk space (system story H1), behaves as a record whose value is not kept in the user's process: reading the output raises, a request that references it is refused at submission, and the record stays.

```python
# a client of the service
records = client.submit(requests, label='night')    # each output written when its record completes
# next day, any process
night = client.records(label='night')
client.output(night[0], 'iofq')                     # read from the record's file
```

## Open

To be designed with the service (scipp/essapps#27, scipp/essapps#23):

- **How the service holds the held state of an accumulator.** One job per accumulator on the cluster, with a memory size and a deadline that its client declares, fits one case: a spectroscopy volume of hundreds of GB that users look at while it grows ([spectroscopy](../../requirements/spectroscopy.md)). That case rests on items that are Assumed or Open there: one file per angle, the size of the grid, and how often users look. Most held states are small (0.34 MB for LoKI's I(Q), [ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)). For a binding without a held state of its own, the state is the template and the pushes, which history holds, so a read is a plain request. Whether the service may submit cluster jobs at all is open ([systems](../../requirements/systems.md)).
- **A memory bound for every request, not only for accumulators.** The plain request over two plus two LoKI runs needs 7.5 to 9.9 GB, and more with each run ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)). Only the binding knows what a request needs.
- **What a finish records besides the status.** Which outputs were written, so that an optional output the workflow left out differs from a file that was dropped or lost. And the software environment that ran the request, which provenance promises (README.md, Provenance and publication) and which only the worker that runs it knows.
- **When files are dropped before history** (system story H1), and by whom.
- **The file format of each output type, and the folder layout within a proposal's area** (scipp/essapps#23).

## Alternatives considered

- **The client keeps values on the service too, as in the user's process, with leases and a cap per client.** A lease ends a client that vanished, and a cap limits what one client keeps; both bound memory by what users release, or by an error once the cap is reached. The trigger loop would reach its cap, and a batch client that closes would still take its values with it.
- **Saving as an option per submission (`save=`).** An output submitted without it is kept by its client, so the service still needs leases and caps.
- **Writing only final outputs.** The framework does not tell intermediate from final results.
- **Writing every state of an accumulator.** A volume of hundreds of GB would be written once per run, and most states are never read.
- **The volume written to a file after each run, and cuts read from it.** The service would hold no held state. But cuts through a growing volume of hundreds of GB do not follow a slider when read from disk (Simon, 2026-10-05).

## Consequences

- A hosted client keeps no value, so the service needs no lease and no cap for values. When a client that vanished counts as ended matters for when its proposal goes idle ([ADR 0004](0004-history-is-append-only-lists.md), scipp/essapps#34), and for when its accumulators are released.
- The files go to an area per proposal that the framework owns. `publish` copies a file into the proposal's upload folder and registers it in SciCat, so a result needed for longer goes to SciCat, as the requirements ask ([tensions](../../requirements/tensions.md)).
- The store of these files is given to the backend, not built into it: each deployment configures its own, and tests use a fake.
- Writing outputs is first-release work (scipp/essapps#23).
- Every output a spec declares is written for every record, also an output meant for looking inside a workflow, such as the masked counts of story S3. A batch of 500 runs writes it 500 times.
- A record's outputs never change, so a copy of a file that a worker keeps never goes stale. A value that many requests read, such as a direct-beam function, can be read from its file once per worker.
- Placement (README.md, Symmetries) holds only if every output reads back from its file as the workflow returned it. The user's process passes values in memory, so nothing checks this yet.
