# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values.
The in-process backend implements the history, the clients, the accumulators, and dropping values, and never drops history.
The service is designed and not implemented.**

[README.md](README.md) describes the API: what workflow authors, app authors, and notebooks write, and what they can rely on.
This document describes how the backend keeps what the API promises about records, accumulators, and values.
Other parts of the system, such as where stages run, get sections here when they are designed.
[ADR 0004](adr/0004-history-is-append-only-lists.md) records why history is four lists that a backend stores as it chooses and drops per proposal.
[ADR 0002](adr/0002-the-client-is-the-lifetime.md) and [ADR 0005](adr/0005-the-service-writes-every-output.md) record what keeps a value in the user's process and on the service, and [ADR 0003](adr/0003-accumulators-add-in-place.md) how an accumulator keeps its held state.

This document uses the terms of README.md (see its Terms table), in particular spec, binding, client, record, table, row, plain request, stage, accumulator, push, state, held state, and read.
Story IDs such as D7 refer to [user-stories.md](user-stories.md) and [system-stories.md](system-stories.md).
It adds two terms of its own:

| Term | What it is |
|---|---|
| client entry | what the backend keeps for one client: its proposal, its stages and accumulators, and, in the user's process, the records whose values it keeps |
| finish | how a record ended: its status (completed, failed, or cancelled) and, if it failed, why |

## History and values

The backend keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept until its proposal has been idle for days to weeks (see How long history is kept).
- **Values**: the outputs of records, what a stage computed, the held state of an accumulator. They are large. In the user's process each is kept only while something keeps it (see Values); on the service the outputs of every record are written to a file, and an accumulator's held state lives in its job (see The service).

A record is history; its output values are not.
In the user's process, reading an output whose value is not kept raises an error, and a request that references it is refused at submission; the record stays.

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
- **JSON only.** Everything in history is JSON. Request values already are: references in `ess.reduce.spec` are frozen pydantic models that convert to JSON and back.
- **The same values, live or read back.** The backend converts what it appends to JSON and back before it uses it. A record therefore holds the same values whether it was just made or read from storage. For example, a tuple given as a parameter is a list in the record. The templates of stages and accumulators are held in the same way. A binding gets the values after the spec's params model has validated them again, so it receives the types the model declares.

### Queries

The backend keeps history in memory as maps (`Views` in `views.py`), and adds to them each item it appends:

| Map | Used by |
|---|---|
| records by ID | `client.records`, `client.provenance`, checks of references |
| finishes by record ID; a record without one is pending | `client.status`, `client.wait`, `client.failure`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, the trigger loop |
| accumulators by ID | provenance of a state (see Records) |
| pushes by accumulator | pinning a reference to a state, provenance of a state |

Queries read the maps, never the storage (see Storage).

What the backend keeps besides history is not in the maps either: client entries, output values, the held state of each accumulator and the outputs computed for its current state, which pending records hold which values, which records wait for which, and the queue of work ready to run.
The in-process backend keeps it in memory, apart from the maps, and it is lost when the backend stops.

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
total.request.params['data']             # AccumulatorRef(accumulator=volume.id, output='counts', upto=300)
client.provenance(total).accumulated     # (Request(VOLUME, {'runs': [{'run': ...}, ...]}),): 300 rows
client.provenance(total).datasets()      # the 300 runs
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

This describes the in-process backend, which only the tests start on an existing log file.
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

- Dropping leaves no dangling reference. No other proposal reads the proposal's records (system story G5), and an idle proposal has no open client and no record that waits. The provenance of every kept record is complete.
- The trigger loop knows that it has handled a dataset only from the records under its rule's label ([automatic-reduction.md](automatic-reduction.md)). A running loop keeps a client of its proposal open, so the proposal is not idle and the loop never reduces a handled dataset again. A loop started for a proposal whose history was dropped reduces its datasets again.
- A client whose process ended without closing it is open until the backend ends it. How the service notices such a client is open (scipp/essapps#34).
- A backend that restarts does not know when its earlier clients ended, so it counts idle time from its start.
- A proposal that is never idle, such as one whose automatic reduction runs all year, keeps its history that long. History is small: in story D7, each run appends three items of constant size (see An example).

Publishing writes the provenance, flattened from history, into the catalogue entry, so what is published outlives the history.
A result needed after its proposal's history is dropped is published.

## The in-process backend: clients, values, and accumulators

The backend in the user's process (`local()`) is implemented, and keeps everything but history in memory.
What this section says about clients, values, and accumulators holds for it.
On the service, a client keeps no values, and each accumulator runs as its own job (see The service); the rules of README.md hold there too, and how the job keeps them is designed with the service.

### Clients

A client is one entry in the backend, from `open_client` to `close_client`.
The entry holds the client's proposal and submitter, the IDs of the records whose values it keeps, its stages, and its accumulators.
Every call of the backend names its client, and the backend takes the proposal from the entry.
A call of a client without an entry raises `ClientEnded`; the backend is the only place that checks.
`client.close()` calls `close_client`, which removes the entry and releases everything in it.

### Values

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

How the stories fare, in the user's process and on the service:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's client |
| B1, S2: tuning steps | each step: the notebook's client, until the notebook releases it; the loaded run: the stage |
| B2: a sum read after each run | the sum: the accumulator's held state (`Summing`), to which each push adds one run; each read: a copy, a plain value in the notebook; the request over runs 611 and 613: the notebook's client |
| C5: two stages tuned together, both results read afterwards | the notebook's client |
| C1: a beam centre used by other requests | the client of the notebook that made it, until it releases it or ends |
| D2: overnight batch, laptop closed | the service: each output is written to a file when its record completes |
| D6: a batch's results read weeks later | the service's files, until the proposal's history is dropped |
| D7: a cut after each run | the volume: the accumulator's held state (`Summing`), added in place, in the accumulator's job on the service; each cut and the final copy: written to files. One volume is kept, not one per cut |
| E1: the curve a rule made, read later | the service's files |

### Accumulators

An accumulator lives in its client's entry; the `Accumulator` that `client.accumulator` returns is a handle to it.
`open_held_state(binding, fixed, tables)` in `ess.apps.bindings` makes its held state: the binding's own if the binding provides `held_state(fixed)`, otherwise one that keeps the rows (see below).
README.md (Accumulator, Reads, What a binding provides) states the rules; the backend keeps them like this:

```python
held = open_held_state(binding, read(fixed), tables)   # what depends only on the fixed values, once

def push(rows):                          # {table: row, ...}
    check(rows)                          # names resolved, each row by its table's row model
    wait(rows)                           # until the records they reference have finished
    check_references(rows)
    with lock:                           # the accumulator's
        wait(readers)                    # until the readers of the current state have run
        outputs = None                   # those of the current state, if it was read
        held.push(read(rows))            # in place if the binding does so
        append_push(accumulator, rows)

def pin(reference):                      # a request at submission, client.output, client.provenance
    with lock:                           # so never during an add
        upto = len(pushes[accumulator])
        if checked is None or checked.upto != upto:   # the first read of this state
            checked = (upto, validate(template, pushes[accumulator]))   # the plain request
        refuse(checked.reason)           # if not None
        readers += 1                     # until the request has run, or the copy is made
        return reference with upto

def read_outputs():                      # by a reader, once it runs
    if outputs is None:
        outputs = held.outputs()         # every output, once per state
    return outputs
```

**Opening.** The template is checked as a stage's is, then each fixed value is typed by its own field and that field's validators.
The params model's own validators read whole requests, so they run on the plain request at each read.
Opening waits for the records the template references and reads them once.
The binding gets every fixed value with data fields loaded and defaults filled in; if it fails to make the held state, opening is refused.
History holds the template's values as JSON, and the accumulator uses them as history holds them.

**Push.** A push first resolves the names in its rows and validates each row by its table's row model alone, with no field the model lacks.
It then waits for the records the rows reference to finish, and only then checks those references: completed records of the same proposal, whose outputs fit the fields and are still kept.
So whether a push is refused does not depend on timing.
History holds each row as the request over that one row would hold it, with names resolved and defaults filled in.
Under the accumulator's lock, the push waits for the readers of the current state, drops the outputs computed for it, adds the rows, and appends the push.
A push checks nothing on the whole table, so its cost does not grow with the rows before it.
Adding runs under the accumulator's own lock, not under the backend's lock that submissions also take, so a long add holds up only the pushes and reads of the same accumulator.
The pushes are appended in the order they were added.
A push that does not fit, or whose add fails, appends nothing; after a failed add the accumulator takes no more pushes or reads, and the driver opens a new one.

**Reads.** A read pins the state after the pushes so far: a request when it is submitted, a `client.output` call when it is made, and `client.provenance`.
`client.provenance` reads no output, so it is not a reader.
A read takes the accumulator's lock, then the backend's, as a push does, so it never pins in the middle of an add.
A request reads at most one accumulator, but one submission may hold requests that read different ones; it takes their locks in the order of their IDs.
The record holds the count.

The first read of a state validates its plain request under the accumulator's lock but outside the backend's, and the accumulator keeps the verdict for that state.
A read of a state whose plain request would be refused is refused with that request's reason.
The validation takes time in proportion to the rows, so a driver that reads after every push pays it at every push.

The outputs of a state are computed outside the backend's lock, under a lock of the accumulator's own, all at once, the first time a reader reads them.
The accumulator keeps them until the next push.
A request reads them as the binding returns them; `client.output` copies them.

**The wait.** A push waits until the readers of the current state are done.
A request is done once it has run, not only started, since a running workflow holds the value; this includes a cancelled request whose workflow still runs.
Releasing the accumulator or ending its client wakes a push that waits, which is then refused at once; the held state is dropped once its readers are done.

**The held state that keeps the rows.** For a binding without `held_state(fixed)`, `open_held_state` makes a held state that keeps the rows pushed so far:

```python
call = binding.stage(fixed, tables)        # when it opens; tables: the accumulator's blanks
rows = {table: [] for table in tables}

def push(pushed):                          # {table: row, ...}, data fields loaded
    for table, row in pushed.items():
        rows[table].append(row)

def outputs():                             # the plain request over every row so far
    return call(**rows)                    # every output, which the backend keeps for the state
```

- Its outputs are those of the plain request it computes, so it meets the protocol of README.md (What a binding provides).
- The backend asks for the outputs once per state that is read, so the plain request runs once per such state.
- It keeps the value of every row, with data fields loaded (see Values).

## The service

**Designed in [ADR 0005](adr/0005-the-service-writes-every-output.md); not implemented.**
Batch and automatic reduction run on the service, and a client connects with `connect(url, proposal=...)`.

- **The outputs of every record are written to a file** when the record completes. The store finds a record's file by the record's ID and the output's name; history names no file. `client.output` of a record, and references to its outputs, read the file. The service keeps no value for a client, so it needs no lease and no cap per client for values.
- **The files** lie in an area per proposal that the framework owns, and are dropped with the proposal's history. `publish` copies a file into the proposal's upload folder and registers it in SciCat.
- **The store of the files** is given to the backend, not built into it: each deployment configures its own, and tests use a fake. The file format of each output type and the folder layout within a proposal's area are first-release work (scipp/essapps#23).
- **The held state of an accumulator** is the one value the service holds between requests. Each accumulator runs as its own job on the cluster, with a memory size and a deadline that its client declares when it opens it. Releasing it or ending its client ends the job once its readers have run, and otherwise its deadline ends it. The client may extend the deadline. The cluster's scheduler bounds a forgotten one.
- **The job runs the accumulator's pushes and every request that references it.** So the reduction of one run is never an output, and never written. A request reads at most one accumulator, so it has one job to run in, and values reach a job only as files of records ([ADR 0003](adr/0003-accumulators-add-in-place.md), What may read an accumulator). A state is not a record, so nothing writes it; a request that reads it makes a record, whose outputs are written as any other's. `client.output` of an accumulator returns the value and writes nothing.
- **After a scan**, a request that copies the volume writes it once, and the job can end. Cuts then read that file, more slowly.

## Open

- How the service notices a client whose process ended without closing it (scipp/essapps#34).
- The file format of each output type and the folder layout within a proposal's area (scipp/essapps#23).
- Accumulators on the service, as [ADR 0005](adr/0005-the-service-writes-every-output.md) lists them: how a client declares the memory size and deadline, what the deadline does to readers that still run, and how the declared size covers a held state that keeps the rows.
- How the service stores history: the in-process backend's log, Kafka, or database tables ([ADR 0004](adr/0004-history-is-append-only-lists.md) lists what the in-process backend shows).
- A forwarder: something a client keeps that holds the last value pushed into it, as in sciline. It joins stages and accumulators when a story needs one, for example a driving server that shows the latest curve of each sample.
