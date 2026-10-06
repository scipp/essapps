# ADR 0005: The service writes the outputs of every record to a file, and holds an accumulator only as a job with a size and a deadline

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05

## Context

The requirements settle where values live ([tensions](../../requirements/tensions.md), "Fast interactive work versus bounded memory"):

> Interactive work, such as a notebook, runs in the user's own process, which keeps results in memory and frees them when it ends; batch and automatic reduction write each result to a file as soon as it is computed and keep nothing in memory, except an accumulator, held in its own job with a declared size and deadline.

Batch and automatic reduction run as services, "so leaks and peak memory must be bounded by how the services work, not by users freeing memory" ([users](../../requirements/users.md)).
Users find the results of a batch reduction they started the day before (same page).

[ADR 0002](0002-the-client-is-the-lifetime.md) decides what keeps a value in the user's process: the client that made a record keeps its values until it releases them or ends.

The framework does not tell intermediate from final results ([users](../../requirements/users.md)), so a service cannot write only the final outputs.

One case needs a value held across requests, the one exception to keeping nothing in memory.
A spectroscopy volume of up to hundreds of GB grows run by run while users look at cuts through it ([spectroscopy](../../requirements/spectroscopy.md); [tensions](../../requirements/tensions.md), "A large volume held once versus looking at it while it grows").
Cutting a growing volume from disk at the pace of a slider is not expected to work (Simon, 2026-10-05).
That users look at slices at that pace is assumed ([users](../../requirements/users.md), Assumed), and how often they look while runs are added is open ([spectroscopy](../../requirements/spectroscopy.md), Open).
A standard VISA machine has 64 GB ([systems](../../requirements/systems.md)), so the user's process cannot always hold such a volume.

## Decision

- **In the user's process**, [ADR 0002](0002-the-client-is-the-lifetime.md) holds.
- **On the service, the outputs of every record are written to a file when the record completes.** The store finds a record's file by the record's ID and the output's name; history names no file, so a record never changes. The service keeps no value for a client: there is no `save=` option and no release of values. `client.output` of a record, and a reference to an output of a record, read its file.
- **The one value the service holds between requests is the held state of an accumulator.** A state is not a record ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)): it changes at every push, and nothing writes it. A request that reads a state makes a record, whose outputs are written like any other's; a request of a copying spec writes the state itself ([ADR 0003](0003-accumulators-add-in-place.md)). `client.output` of an accumulator returns the value and writes nothing, like any read.
- **Each accumulator runs as its own job on the cluster**, with a memory size and a deadline that its client declares when it opens it. A push returns once the service has logged it, and the job adds the pushes in the order logged. The job also runs every request that references the accumulator. A request reads at most one accumulator ([ADR 0003](0003-accumulators-add-in-place.md)), so it runs in that accumulator's job, or anywhere if it reads none. Releasing the accumulator or ending its client ends the job once the pushes logged are added and its readers have run; the deadline ends it otherwise. The client may extend the deadline.
- **The reduction of one run is never an output, and never written** ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)). A binding with a held state of its own adds each run to it at the push. The held state that keeps the rows reduces all of them at each read.

```python
# a client of the service
records = client.submit(requests, label='night')    # each output written when its record completes
# next day, any process
night = client.records(label='night')
client.output(night[0], 'iofq')                     # read from the record's file

volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
volume.push({'runs': {'run': dataset(run=611)}})    # reduced and added in the volume's job
cut = client.submit(CUT, {'data': volume.ref('counts'), 'index': 0})
                                                    # runs in the volume's job; the cut is written
```

Open, to be designed with the service (scipp/essapps#27):

- how a client declares an accumulator's size and deadline;
- what the deadline does to readers that still run, and how it relates to release, which ends the job only once the pushes logged are added and the readers have run;
- how the declared size covers a held state that keeps the rows: each read computes the plain request over every row so far, which needs more memory than the rows (7.5 to 9.9 GB for two plus two LoKI runs, against about 4 GB with a held state of its own; [ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).

## Alternatives considered

- **The client keeps values on the service too, as in the user's process, with leases and a cap per client.** The trigger loop submits through one client and never releases anything, so automatic reduction would keep every output it makes. A batch client that closes would take its values with it, so the next morning only the records would be left. A lease ends a client that vanished, and a cap limits what one client keeps; both bound memory by what users release, or by an error once the cap is reached.
- **Saving as an option per submission (`save=`).** An output submitted without it is kept by its client, so the service still needs leases and caps.
- **Writing only final outputs.** The framework does not tell intermediate from final results.
- **Writing every state of an accumulator.** A volume of hundreds of GB would be written once per run, and most states are never read.
- **Rows that reference outputs, sent to the accumulator's job without being written** (`client.submit(ANGLE, ..., into=volume)`). The per-run reductions would still spread over nodes. But this adds API and a transfer between jobs, and a run's counts binned onto the volume's grid are as large as the volume.
- **Several accumulators of a client in one job, with one size and deadline.** A request could read two of them, and a row of one could reference another without a file. The client would need a way to declare the group. No requirement has two accumulators that grow at once and meet in one request: sample and background runs are one accumulator with two tables ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).
- **The accumulator in the service's own process.** One process would hold a volume of hundreds of GB per user, and one crash would lose all of them.
- **The volume written to a file after each run, and cuts read from it.** The service would hold nothing. But cuts through a growing volume of hundreds of GB do not follow a slider when read from disk.

## Consequences

- A hosted client keeps nothing, so the service needs no lease and no cap for values. When a client that vanished counts as ended matters only for when its proposal goes idle ([ADR 0004](0004-history-is-append-only-lists.md), scipp/essapps#34).
- The files go to an area per proposal that the framework owns, and are dropped with the proposal's history ([ADR 0004](0004-history-is-append-only-lists.md)). `publish` copies a file into the proposal's upload folder and registers it in SciCat, so a result needed for longer goes to SciCat, as the requirements ask ([tensions](../../requirements/tensions.md)).
- The store of these files is given to the backend, not built into it: each deployment configures its own, and tests use a fake.
- Writing outputs is first-release work (scipp/essapps#23): the file format of each output type and the folder layout within a proposal's area.
- The cluster's scheduler bounds an accumulator. A forgotten one costs at most the memory and time it declared.
- The runs of one accumulator are reduced in its job, on its cores, not spread over nodes. Runs that arrive over hours do not need more.
- After a scan, a request that copies the volume writes it once ([ADR 0003](0003-accumulators-add-in-place.md)), and the job can end. Cuts then read that file, more slowly.
- Values move from one accumulator's job to another only as files of records. A finished accumulator is written once, by a record of its last state. Cutting that file is slow, so a large value that a growing accumulator needs, such as a background volume, goes into its template and is read once when it opens ([ADR 0003](0003-accumulators-add-in-place.md)).
- Reading part of a volume at the pace of a slider, without making a record (README.md, open question "Views"), is served by the accumulator's job.
