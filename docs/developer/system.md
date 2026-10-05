# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values.
The in-process backend implements the history, the clients, the accumulators, and dropping values, and never drops history.
The service is designed and not implemented.**

[README.md](README.md) describes the API: what workflow authors, app authors, and notebooks write, and what they can rely on.
This document describes how the backend keeps what the API promises about records, accumulators, and values.
Other parts of the system, such as where stages run, get sections here when they are designed.
[ADR 0004](adr/0004-history-is-append-only-lists.md) records why history is four lists that a backend stores as it chooses and drops per proposal.
[ADR 0002](adr/0002-the-client-is-the-lifetime.md) and [ADR 0005](adr/0005-the-service-writes-every-output.md) record what keeps a value in the user's process and on the service, and [ADR 0003](adr/0003-accumulators-add-in-place.md) how an accumulator holds its state.

This document uses the terms of README.md (see its Terms table), in particular spec, binding, client, record, table, row, stage, accumulator, held state, and state.
Story IDs such as D7 refer to [user-stories.md](user-stories.md) and [system-stories.md](system-stories.md).
It adds two terms of its own:

| Term | What it is |
|---|---|
| client entry | what the backend keeps for one client: its proposal, the records whose values it keeps, its stages, and its accumulators |
| finish | how a record ended: its status (completed, failed, or cancelled) and, if it failed, why |

## History and values

The backend keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept until its proposal has been idle for days to weeks (see How long history is kept).
- **Values**: the outputs of records, what a stage computed, the held state of an accumulator. They are large. In the user's process each is kept only while a client keeps it or a pending request that reads it has yet to run (see Values); on the service every output is written to a file (see The service).

A record is history; its output values are not.
Reading an output whose value is not kept raises an error, and a request that references it is refused at submission; the record stays.

## History

History is four lists, each only appended to:

| List | One item per | Holds | Appended when |
|---|---|---|---|
| records | record | ID, time, proposal, submitter, request, output names, label, member | a submission is accepted |
| accumulators | opened accumulator | ID, proposal, template | an accumulator opens |
| finishes | finished record | record ID, status, failure message | a record completes, fails, or is cancelled |
| pushes | push into an accumulator | accumulator ID, one row per table, each as a request over it holds it | rows are added to an accumulator |

Client entries are not history: opening or ending a client, making a stage, and releasing anything append nothing, and no client entry survives a restart.
A record does not say which stage it went through.
A reference to an accumulator in a record names the accumulator and how many pushes it covers, so that the rows it read can be found.

### An example

Story D7 is the loop over arrivals in README.md: each run of a rotation scan is pushed into an accumulator `volume` as it arrives, a cut reads the volume after each push, and a copy of the volume is made once the scan ends.
With two runs, these items are appended, in this order (`#n` is a record, `a` the accumulator, JSON abbreviated):

```text
accumulators  a   volume/v1  {}  blanks=(runs)
pushes        a   {runs: {run: uuid:run-1}}
records       #1  cut/v1  {data: a[:1].counts, index: 0}  label=cut member=17
finishes      #1  completed
pushes        a   {runs: {run: uuid:run-2}}
records       #2  cut/v1  {data: a[:2].counts, index: 0}  label=cut member=17
finishes      #2  completed
records       #3  copy/v1  {data: a[:2].counts}
finishes      #3  completed
```

`uuid:run-1` is the dataset identity the backend resolved `dataset(run=1)` to.
`a[:2].counts` is the reference `{accumulator: a, output: counts, upto: 2}`: the output `counts` of `a` after its first two pushes.
The second push waits until cut `#1` has run, since `#1` reads the volume that the push adds to (see Accumulators).
Each run appends three items of constant size, however many runs came before.

### What history records

- **What the backend accepted, not what the client called.** Dataset names are resolved and defaults filled in. Resolving `dataset(run=4711)` again later could give another dataset; the recorded identity cannot change.
- **Only accepted changes.** A change is checked before anything is appended. A refused call appends nothing.
- **A submission whole or not at all.** A submission of 500 requests appends its 500 records together, so a backend that stops half-way has stored all of them or none.
- **No values.** Output values, what a stage computed, and an accumulator's held state are not history.
- **JSON only.** Everything in history is JSON. Request values already are, since references in `ess.reduce.spec` are plain dicts.
- **The same values, live or read back.** The backend converts what it appends to JSON and back before it uses it. A record therefore holds the same values whether it was just made or read from storage. For example, a tuple given as a parameter is a list in the record. A binding gets the values after the spec's params model has validated them again, so it receives the types the model declares.

### Queries

The backend keeps history in memory as maps (`Views` in `views.py`), and adds to them each item it appends:

| Map | Used by |
|---|---|
| records by ID | `client.records`, `client.provenance`, checks of references |
| finishes by record ID; a record without one is pending | `client.status`, `client.wait`, `client.failure`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, the trigger loop |
| accumulators by ID | provenance of a state (see Records) |
| pushes by accumulator | binding a reference to a state, provenance of a state |

Queries read the maps, never the storage (see Storage).

Live state that is not history is not in the maps either: client entries, output values, the held state of each accumulator and the outputs computed for its current state, which pending records hold which values, which records wait for which, and the queue of work ready to run.
The backend keeps it apart from the maps, and it is lost when the backend stops.

The maps depend on two orders:

- the records under one label are in the order they were submitted, since `client.latest` returns the newest;
- the pushes into one accumulator are in the order they were added, since a reference to a state covers the first `upto`.

A finish always names a record appended before it.

### Records

Every record is a request.
Its status is its finish, so a record never changes and a client's copy of it is never out of date.
A reference to a state of an accumulator holds the accumulator's ID and a count, `upto`, not the rows.
Provenance expands it into the plain request over the rows of the first `upto` pushes (`Backend.accumulated`): the accumulator's template with each table filled by the rows pushed into it, in push order, and defaults filled in.

```python
total = client.compute(COPY, {'data': volume.ref('counts')})
total.request.params['data']             # AccumulatorRef(accumulator=volume.id, output='counts', upto=1000)
client.provenance(total).accumulated     # (Request(VOLUME, {'runs': [{'run': ...}, ...]}),): 1000 rows
client.provenance(total).datasets()      # the 1000 runs
```

## Storage

The design needs three things from how a backend stores history, and nothing more:

- the lists only grow and keep the orders above, until a proposal's part is dropped as a whole;
- a submission is stored whole or not at all;
- one backend writes them.

[ADR 0004](adr/0004-history-is-append-only-lists.md) leaves the rest to the backend.

### The in-process backend: an event log

The in-process backend stores history as one log of events, each appended once and never changed (`log.py`).
Each event appends to history:

| Event | Appends |
|---|---|
| `submitted` | the records of one submission, with the time, proposal, and submitter they share |
| `opened` | one accumulator |
| `finished` | one finish |
| `pushed` | one push |

The backend appends an event to the log, then applies it to its maps (`Views.apply`).
A refused call appends nothing, so every event in the log can be applied when the log is read again.
A submission is one event, so it is stored whole or not at all.

The log lives in memory, or in a file of JSON lines.
A write that fails, as on a full disk (system story H1), leaves the file as it was.
If a crash cuts the last line short while it is being written, that line is dropped when the file is read; any other line that is not an event is an error.

Each log has exactly one backend, and each backend one log.
A backend holds an exclusive lock (`flock`) on its log file from start to close, and a second backend on the same file, in any process, is refused at start.
The operating system releases the lock when the process ends, so a backend started after a crash or for an upgrade (system story H2) takes the file over.
Two notebooks that share results are clients of one backend; a backend in each notebook shares nothing.

Event formats must stay readable for as long as the log is kept.
The maps may change between versions.

### Restart

A backend given a log that already has events applies them as it applies new ones, so it has the maps of the backend that wrote the log.
It then runs the records that are still pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

- A pending record runs from scratch, without its stage, since client entries do not survive a restart.
- A pending record that reads an accumulator fails, since the accumulator, with its held state, did not survive the restart.
- A pending record fails if the value of one of its inputs is not kept. The in-process backend keeps values only in memory, so after a restart this happens to every pending record with an input that had already completed.
- No client keeps the outputs of a record that completes after a restart, since the client that made it is gone. Its values are dropped once the pending records that read them have run.

This is what system story H2 (backend upgrade with runs in flight) needs from history.
H2 also needs every event format to stay readable across versions, and the files that the service writes, so that a pending record whose input had completed still runs.

### On the service

The service may store history as the same log in a file, in Kafka, or as one database table per list.
It may split history by proposal.
This keeps the orders the maps depend on (see Queries), since each lies within one proposal, and it drops a proposal's history by dropping its part.

## Accumulators

An accumulator lives in its client's entry in the backend; the `Accumulator` that `client.accumulator` returns is a handle to it.
If its binding provides `accumulator(fixed)`, its held state is the binding's own.
Otherwise it is a held state that keeps the rows (see The held state that keeps the rows below; [ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
The backend does this:

```python
held = held_state(binding, read(fixed), tables)   # when it opens; what depends only on the fixed values is computed once
unreadable = 'nothing has been pushed into the accumulator'

def push(rows):                          # {table: row, ...}
    wait(rows)                           # until their records have finished
    check(rows)                          # each by its table's row model
    with lock:                           # the accumulator's
        reason = validate(pushes[accumulator] + [rows])   # the plain request; refused if a fixed value changes
        wait(readers)                    # until the readers of the current state have run
        held.push(read(rows))            # in place if the binding does so
        append_push(accumulator, rows)
        unreadable = reason              # None if the plain request is accepted

def bind(reference):                     # a request at submission, or a client.output call
    with lock:                           # so never during an add
        refuse(unreadable)               # if not None
        upto = len(pushes[accumulator])
        readers += 1                     # until the request has run, or the copy is made
        return reference with upto
```

**Opening.** The template's blanks must be one or more tables of the spec; a template with no blank, or with a blank that is not a table, is refused.
Any binding can be accumulated.
The template is checked as a stage's template is: what a request would refuse is refused, with the tables left out.
Opening waits for the records the template references, refuses them unless they have completed, and reads them once.
The binding gets every value but the tables, with data read and defaults filled in.
Each value is typed by its field alone, since the params model's own validators may need the tables.

**Push.** A push names one row for each of one or more of the accumulator's tables, and its rows enter one state; a key that is not one of them is refused.
Each row gets the check that the request over that one row gets, by its table's row model alone: it is valid for the row model, with no field the model lacks, its values can be stored, and its references name completed records of the same proposal whose outputs fit the fields and are still kept.
History holds each row as that request would hold it, with names resolved and defaults filled in.
A push waits for the records the rows reference to finish before it is checked, so whether it is refused does not depend on timing.
Rules on a whole table, such as its length, and the params model's own validators apply to the plain request over all rows pushed so far, which each push validates once.
If that request would be refused, the rows are still added, and the accumulator keeps the reason as the reason its state cannot be read.
If it is accepted but gives a fixed value other than the one the binding was opened with, as a `field_validator` that changes a value may, the push is refused and nothing is added.
A row that references an accumulator is refused: it would read the other accumulator's state while it adds, without holding back that accumulator's next push.
A stage or an accumulator whose template references an accumulator is refused too, since it would hold that state, and so hold back every push, for as long as it lives.
A push that does not fit, or whose add fails, appends nothing.
After a failed add the accumulator takes no more pushes or reads, since the binding may hold part of the rows; the driver opens a new accumulator.
Adding can take long.
It runs under the accumulator's own lock, not under the backend's lock that submissions also take, so a long add holds up only the pushes and reads of the same accumulator.
The pushes are appended in the order they were added.

**Reads.** A reader binds to the state after the pushes so far: a request when it is submitted, a `client.output` call when it is made.
`client.provenance` binds in the same way, but reads no output, so it is not a reader.
It takes the accumulator's lock, then the backend's, as a push does, so it never binds in the middle of an add.
A submission that references several accumulators takes their locks in the order of their IDs.
The record holds the count; a reference that names another count than the current one is refused, since the accumulator holds no earlier state.
A state whose plain request would be refused, such as one with nothing pushed, is refused with that reason, and so is an accumulator of another client.

The outputs of a state are computed outside the backend's lock, once while the state has readers: the first reader computes the outputs it reads, and later readers of the same state compute only those not yet computed.
They are dropped when the state's last reader is done.
A request reads them as the binding returns them, not a copy, so its outputs must not share memory with them, such as a slice of them; this is documented, not enforced.
`client.output` copies them.

**The wait.** A push waits until the readers of the current state are done: the requests that reference it and were accepted before the push, and `client.output` calls in progress.
A request is done once it has run, not only started, since a running workflow holds the value; this includes a cancelled request whose workflow still runs.
No reader waits for a push, so the wait ends; a long reader holds back the next push.
A reader that binds while a push waits or adds blocks until the push is done, and binds to the state after it.
Releasing the accumulator or ending its client waits for nothing: a push that waits is refused, and the held state is dropped once its readers are done.

**Binding.** The protocol is `accumulator(fixed)`, which returns a held state with `push(rows)` and `outputs(names)` ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

```python
held = binding.accumulator({'scale': 2.0})     # nothing pushed yet
held.push({'runs': {'run': counts_611}})       # values, not references
held.push({'runs': {'run': counts_612}})
held.outputs(['normalized'])                   # {'normalized': ...}, computed from the held state
```

- After rows are pushed in order, `outputs` gives what the plain request over those rows gives, with the same other values.
- `push` takes the rows of one push, one per table, so that a binding such as `StreamProcessor` can add them at once. It may modify the held state in place, but not the rows. A binding whose held state starts from the first row's values copies them, so that adding in place never changes the output they came from.
- `outputs` may return part of the held state, not a copy. It may leave out an output that the spec declares optional, and `client.output` without a name then leaves it out, as for a record. It must not modify what an earlier call for the same state returned, since running readers still use it while later names are computed.
- The backend calls `push` and `outputs` from one thread at a time, and never `outputs` while a push adds.
- The stories bind `NORMALIZE` and `VOLUME` to `Summing`, a toy `StreamProcessor`: it sums the counts of each table's runs and computes the outputs from the sums.

**The held state that keeps the rows.** For a binding without `accumulator(fixed)`, `held_state` in `ess.apps.bindings` makes a held state that keeps the rows pushed so far and computes the plain request over them at each read:

```python
call = binding.stage(fixed, tables)        # when it opens; tables: the accumulator's blanks
rows = {table: [] for table in tables}

def push(pushed):                          # {table: row, ...}, data read
    for table, row in pushed.items():
        rows[table].append(row)

def outputs(names):                        # the plain request over every row so far
    return {name: value for name, value in call(**rows).items() if name in names}
```

- It meets the protocol above, since its outputs are those of the plain request it computes.
- What depends only on the fixed values is computed once if the binding's stage keeps it, as a stage of `PipelineBinding` does through `sciline.Stage`. A function binding computes everything at each read.
- Each read repeats the work per run for every row so far. A driver that reads after every push pays work that grows with the number of rows.
- It keeps the value of every row, with data read. A row that references an output of a record keeps that value in memory while the accumulator lives, even after the client releases the record.
- A binding avoids both costs by providing `accumulator(fixed)`.

## Clients

A client is one entry in the backend, from `open_client` to `close_client`.
The entry holds the client's proposal and submitter, the IDs of the records whose values it keeps, its stages, and its accumulators.
Every call of the backend names its client, and the backend takes the proposal from the entry.
A call of a client without an entry raises `ClientEnded`; the backend is the only place that checks.
`client.close()` calls `close_client`, which removes the entry and releases everything in it.

## Values

README.md states which output values are kept in the user's process (How long records and values are kept; [ADR 0002](adr/0002-the-client-is-the-lifetime.md)).
The backend keeps two things for that rule:

- in each client entry, the IDs of the records the client made and has not released;
- for each output, how many pending records reference it and have not yet run.

A pending record holds its inputs from submission until its workflow returns, even if the record is cancelled meanwhile, since the workflow still reads them.
An output value is dropped once no client entry keeps its record and no pending record that reads it has yet to run.
The backend checks this when a client releases a record or ends, when a workflow returns or a record finishes without running, and when a record completes, since a record released while pending drops its outputs as soon as it completes.

A stage keeps what it computed from its fixed values, and an accumulator its held state, until the client releases them or ends.
A held state that keeps the rows (see Accumulators) holds the value of each row, also an output of a record that the client has released.
What a stage computed is a cache: the backend may drop it at any time, and the next call through the stage computes it again and makes the same record.
The in-process backend never drops it.

A label names records and keeps no values.
On the service, every output is written to a file instead (see The service).

How the stories fare:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's client |
| B1, S2: tuning steps | each step: the notebook's client, until the notebook releases it; the loaded run: the stage |
| B2: a sum read after each run | the sum: the accumulator's held state, to which each push adds one run; each read: a copy, a plain value in the notebook; the request over runs 611 and 613: the notebook's client |
| C5: two stages tuned together, both results read afterwards | the notebook's client |
| C1: a beam centre used by other requests | the client of the notebook that made it, until it releases it or ends |
| D2: overnight batch, laptop closed | the service: each output is written to a file when its record completes |
| D6: a batch's results read weeks later | the service's files, until the proposal's history is dropped |
| D7: a cut after each run | the volume: the accumulator's held state, added in place, in the accumulator's job on the service; each cut and the final copy: written to files. One volume is kept, not one per cut |
| E1: the curve a rule made, read later | the service's files |

## How long history is kept

Users find the records of their work while they consider it ongoing, as with an application they leave open: for batch and automatic reduction, days to weeks ([requirements](../requirements/tensions.md)).
The lasting history of what ran belongs in SciCat.

A proposal is *idle* while none of its clients is open and none of its records is pending.
Once a proposal has been idle for the retention period, which the deployment sets, its history is dropped as a whole, and on the service the files of its outputs with it.
With a retention period R:

```text
day 0     a batch of 500 runs is submitted; the laptop closes, so its client ends
day 1     the last record of the batch finishes; the proposal is idle from now
day 3     the user opens a notebook: the records are there, and the proposal is no longer idle
day 3     the notebook closes; the proposal is idle from now
day 3+R   the proposal's history is dropped
```

- Dropping leaves no dangling reference. No other proposal reads the proposal's records (system story G5), and an idle proposal has no client that keeps a value and no record that waits. The provenance of every kept record is complete.
- The trigger loop knows that it has handled a dataset only from the records under its rule's label ([automatic-reduction.md](automatic-reduction.md)). A running loop keeps a client of its proposal open, so the proposal is not idle and the loop never reduces a handled dataset again. A loop started for a proposal whose history was dropped reduces its datasets again.
- A client whose process ended without closing it is open until the backend ends it. How the service notices such a client is open (scipp/essapps#34).
- A backend that restarts does not know when its earlier clients ended, so it counts idle time from its start.
- A proposal that is never idle, such as one whose automatic reduction runs all year, keeps its history that long. History is small: in story D7, each run appends three items of constant size (see An example).

Publishing writes the provenance, flattened from history, into the catalogue entry, so what is published outlives the history.
A result needed after its proposal's history is dropped is published.

## The service

**Designed in [ADR 0005](adr/0005-the-service-writes-every-output.md); not implemented.**
Batch and automatic reduction run on the service, and a client connects with `connect(url, proposal=...)`.

- **Every output is written to a file** when its record completes, and the record names the file. `client.output` and references read the file. The service keeps no value for a client, so it needs no lease and no cap per client for values.
- **The files** lie in an area per proposal that the framework owns, and are dropped with the proposal's history. `publish` copies a file into the proposal's upload folder and registers it in SciCat.
- **The store of the files** is given to the backend, not built into it: each deployment configures its own, and tests use a fake. The file format of each output type and the folder layout within a proposal's area are first-release work (scipp/essapps#23).
- **An accumulator** is the one state the service holds between requests. Each runs as its own job on the cluster, with a memory size and a deadline that its client declares when it opens it. Releasing it or ending its client ends it early, and otherwise its deadline ends it. The client may extend the deadline. The cluster's scheduler bounds a forgotten one.
- **The rows of an accumulator name runs**, which it reduces and adds inside its job. So a large per-run value is never an output, and never written. A request that references the accumulator runs in its job, and its outputs are written as any other.
- **After a scan**, a request that copies the volume writes it once, and the job can end. Cuts then read that file, more slowly.

## Open

- How the service notices a client whose process ended without closing it (scipp/essapps#34).
- The file format of each output type and the folder layout within a proposal's area (scipp/essapps#23).
- How a client declares an accumulator's memory size and deadline, and how a request that reads two accumulators gets both states in one job. One accumulator with several tables, such as sample and can runs, avoids the second.
- How the service stores history: the in-process backend's log, Kafka, or database tables ([ADR 0004](adr/0004-history-is-append-only-lists.md) lists what the in-process backend shows).
- A forwarder: something a client keeps that holds the last value pushed into it, as in sciline. It joins stages and accumulators when a story needs one, for example a driving server that shows the latest curve of each sample.
