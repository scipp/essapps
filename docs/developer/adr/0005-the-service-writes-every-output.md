# ADR 0005: The service writes every output to a file, and holds an accumulator only as a job with a size and a deadline

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05

## Context

The requirements settle where values live ([tensions](../../requirements/tensions.md), "Fast interactive work versus bounded memory"):

> Interactive work, such as a notebook, runs in the user's own process, which keeps results in memory and frees them when it ends; batch and automatic reduction write each result to a file as soon as it is computed and keep nothing in memory.

Batch and automatic reduction run as services, "so leaks and peak memory must be bounded by how the services work, not by users freeing memory" ([users](../../requirements/users.md)).
Users find the results of a batch reduction they started the day before (same page).

[ADR 0002](0002-the-client-is-the-lifetime.md) gave every backend one rule: the client that made a record keeps its values until it releases them or ends.
On a service, this rule fails both requirements:

- The trigger loop submits through one client and never releases anything, so automatic reduction keeps every output it makes.
- A batch client that closes takes its values with it, so the next morning only the records are left.
- Bounding clients that keep by default needs a lease to end a client that vanished and a cap on what one client keeps. Both bound memory by what users release.

Saving was planned as an option per submission (`save=`), so an output submitted without it would still be kept by its client.

The framework does not tell intermediate from final results ([users](../../requirements/users.md)), so a service cannot write only the final outputs.

One case needs a value held across requests.
A spectroscopy volume of up to hundreds of GB grows run by run while users look at cuts through it ([spectroscopy](../../requirements/spectroscopy.md); [tensions](../../requirements/tensions.md), "A large volume held once versus looking at it while it grows").
Cutting a growing volume from disk at the pace of a slider is not expected to work (Simon, 2026-10-05).
A standard VISA machine has 64 GB ([systems](../../requirements/systems.md)), so the user's process cannot always hold such a volume.

## Decision

- **In the user's process**, [ADR 0002](0002-the-client-is-the-lifetime.md) holds.
- **On the service, every output is written to a file when its record completes**, and the record names the file. The service keeps no value for a client: there is no `save=` option and no release of values. `client.output` and references read the file.
- **The one state the service holds between requests is an accumulator.** Each runs as its own job on the cluster, with a memory size and a deadline that its client declares when it opens it. Releasing it or ending its client ends it early; the deadline ends it otherwise. The client may extend the deadline explicitly. A request that references an accumulator runs in the accumulator's job.
- **An accumulator's rows name runs**, which the accumulator reduces and adds inside its job ([ADR 0003](0003-accumulators-add-in-place.md)). So a large element is never an output, and never written.

```python
# a client of the service
records = client.submit(rows, label='night')        # each output written when its record completes
# next day, any process
night = client.records(label='night')
client.output(night[0], 'iofq')                     # read from the file the record names

volume = client.accumulator(Template(VOLUME, params={'grid': grid}, blanks=('runs',)))
volume.push({'runs': {'run': dataset(run=611)}})    # reduced and added in the volume's job
cut = client.submit(CUT, {'data': volume.ref('counts'), 'energy_transfer': 2.0})
                                                    # runs in the volume's job; the cut is written
```

How a client declares the size and deadline is designed with the service.

## Alternatives considered

- **The client keeps values on the service too, with leases and a cap per client.** Memory is then bounded by what users release, or by an error once the cap is reached. The trigger loop keeps everything it makes.
- **Saving as an option per submission (`save=`).** An output submitted without it is kept by its client, so the service still needs leases and caps.
- **Writing only final outputs.** The framework does not tell intermediate from final results.
- **Rows that reference outputs, sent to the accumulator's job without being written** (`client.submit(ANGLE, ..., into=volume)`). The per-run reductions would still spread over nodes. But this adds API and a transfer between jobs, and an element binned onto the volume's grid is as large as the volume.
- **The accumulator in the service's own process.** One process would hold a volume of hundreds of GB per user, and one crash would lose all of them.
- **The volume written to a file after each run, and cuts read from it.** The service would hold nothing. But cuts through a growing volume of hundreds of GB do not follow a slider when read from disk.

## Consequences

- A hosted client keeps nothing, so the service needs no lease and no cap for values. When a client that vanished counts as ended matters only for when its proposal goes idle ([ADR 0004](0004-history-is-append-only-lists.md), scipp/essapps#34).
- The files go to an area per proposal that the framework owns, and are dropped with the proposal's history ([ADR 0004](0004-history-is-append-only-lists.md)). `publish` copies a file into the proposal's upload folder and registers it in SciCat, so a result needed for longer goes to SciCat, as the requirements ask ([tensions](../../requirements/tensions.md)).
- The store of these files is given to the backend, not built into it: each deployment configures its own, and tests use a fake.
- Saving is first-release work (scipp/essapps#23): the file format of each output type and the folder layout within a proposal's area.
- The cluster's scheduler bounds an accumulator. A forgotten one costs at most the memory and time it declared.
- The runs of one accumulator are reduced in its job, on its cores, not spread over nodes. Runs that arrive over hours do not need more.
- A request that references two accumulators needs both states in one job. One accumulator with several tables, such as sample and can runs ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), avoids this; otherwise accumulators opened together share a job, or a small value is copied, which is designed with the service.
- After a scan, a request that copies the volume writes it once ([ADR 0003](0003-accumulators-add-in-place.md)), and the job can end. Cuts then read that file, more slowly.
- Reading part of a volume at the pace of a slider, without making a record (README.md, open question "Views"), is served by the accumulator's job.
