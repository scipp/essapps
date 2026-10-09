# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values.
The in-process backend implements the history, the clients, the accumulators, dropping values, and cancelling work that nothing keeps, and never drops history.
It logs the record of a state with its whole request, not yet as the accumulator and its number of pushes.
Persisting, the store, freezing, and selections are designed and not yet implemented.
The service is designed and not implemented.**

[README.md](README.md) describes the API: what workflow authors, app authors, and notebooks write, and what they can rely on.
This document describes how the backend keeps what the API promises about records, accumulators, and values.
Other parts of the system, such as where stages run, get sections here when they are designed.
[ADR 0004](adr/0004-history-is-append-only-lists.md) records why history is six lists that a backend stores as it chooses and drops per proposal.
[ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md) and [ADR 0005](adr/0005-nothing-is-written-unless-persisted.md) record what keeps a value and when it is written, and [ADR 0003](adr/0003-accumulators-add-in-place.md) how an accumulator keeps its held state.

This document uses the terms of README.md (see its Terms table), in particular spec, binding, client, record, table, row, plain request, stage, accumulator, push, state, held state, and read.
Story IDs such as D7 refer to [user-stories.md](user-stories.md) and [system-stories.md](system-stories.md).
It adds two terms of its own:

| Term | What it is |
|---|---|
| client entry | what the backend keeps for one client: its proposal, its stages and accumulators, and the records whose values it keeps |
| finish | how a record ended: its status (completed, failed, or cancelled) and, if it failed, why |

## History and values

The backend keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept until its proposal has been idle for days to weeks (see How long history is kept).
- **Values**: the outputs of records, what a stage computed, the held state of an accumulator. They are large. Each is kept only while something keeps it (see Values), and an output is written only when a client persists it.

A record is history; its output values are not.
Reading an output whose value nothing keeps for the caller raises an error, and a request that references it is refused at submission; the record stays.

## History

History is six lists, each only appended to:

| List | One item per | Holds | Appended when |
|---|---|---|---|
| records | record | ID, time, proposal, submitter, request, output names, label, member; for the record of a state, the accumulator and its number of pushes in place of the request | a submission or a freeze is accepted |
| accumulators | opened accumulator | ID, proposal, template | an accumulator is accepted, before its held state opens |
| finishes | finished record | record ID, status, failure message | a record completes, fails, or is cancelled |
| pushes | push into an accumulator | accumulator ID, one row per table, each as a request over it holds it | a push is accepted, before its rows are added |
| persist requests | request to persist outputs of a record | record ID, output names | `client.persist`, or a submission or freeze with `persist=`, is accepted |
| writes | write that ended | record ID, the outputs written, or why the write failed | a write ends |

Client entries are not history: opening or ending a client, making a stage, releasing anything, and `client.output` append nothing of their own, and no client entry survives a restart.
A release that leaves a pending record with no keeper appends its finish, `cancelled`.
A record does not say which stage it went through.
A record never names an accumulator: a submission that reads one appends a record of the state's plain request before the records that read it ([ADR 0008](adr/0008-a-read-of-a-state-is-a-record.md)).

### An example

Story D7 is the loop over arrivals in README.md: each run of a rotation scan is pushed into an accumulator `volume` as it arrives, a cut is copied from the volume after each push, and the volume is frozen once the scan ends.
With two runs, and a cut kept as a record after the first, these items are appended, in this order (`#n` is a record, `a` the accumulator, JSON abbreviated):

```text
accumulators      a   volume/v1  {}  blanks=(runs)
pushes            a   {runs: {run: uuid:run-1}}
records           #1  volume/v1  a after 1 push
records           #2  cut/v1  {data: #1.counts, index: 0}  label=cut member=17
finishes          #1  completed
finishes          #2  completed
pushes            a   {runs: {run: uuid:run-2}}
records           #3  volume/v1  a after 2 pushes
persist requests  #3  all outputs
finishes          #3  completed
writes            #3  counts
```

`uuid:run-1` is the dataset identity the backend resolved `dataset(run=1)` to.
`#1` is the record of the state after the first push, the plain request over the rows pushed so far, appended in one submission with the cut `#2` that reads it.
History stores it as the accumulator and its number of pushes, and lists the rows when it is read ([ADR 0004](adr/0004-history-is-append-only-lists.md)).
`#1.counts` is the reference `{record: #1, output: counts}`.
The second push is added once cut `#2` has run, since `#2` reads the volume that the push adds to (see Accumulators).
It is appended when the driver makes it, which may be before `#2` finishes.
The cuts that `client.output(volume, 'counts', select=...)` copies append nothing.
`#3` is the record that `client.freeze(volume, persist=True)` returns; its values are the volume itself, written once.
Each push appends one item, and each read one record of constant size, so history grows in proportion to the number of runs.

### What history records

- **What the backend accepted, not what the client called.** Dataset names are resolved and defaults filled in. Resolving `dataset(run=4711)` again later could give another dataset; the recorded identity cannot change.
- **Only accepted changes.** A change is checked before anything is appended. A refused call appends nothing.
- **A submission whole or not at all.** A submission of 500 requests appends its 500 records together, so a backend that stops half-way has stored all of them or none.
- **No values.** Output values, what a stage computed, and an accumulator's held state are not history. History logs that a value was asked to be persisted and whether the write succeeded, not the value.
- **JSON only.** Everything in history is JSON. Request values already are: references in `ess.spec` are frozen pydantic models that convert to JSON and back.
- **The same values, live or read back.** The backend converts what it appends to JSON and back before it uses it. A record therefore holds the same values whether it was just made or read from storage. For example, a tuple given as a parameter is a list in the record. The templates of stages and accumulators are held in the same way. A binding gets the values after the spec's params model has validated them again, so it receives the types the model declares.

### Queries

The backend keeps history in memory as maps (`Views` in `views.py`), and adds to them each item it appends:

| Map | Used by |
|---|---|
| records by ID | `client.records`, `client.provenance`, checks of references |
| finishes by record ID; a record without one is pending | `client.status`, `client.wait`, `client.failure`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, the trigger loop |
| accumulators by ID | the template of an accumulator, as history holds it |
| pushes by accumulator | pinning a state, the plain request of a state (see Records) |
| persist requests and writes by record ID | `client.output` and checks of references, for values kept by the store |

Queries read the maps, never the storage (see Storage).

What the backend keeps besides history is not in the maps either: client entries, output values, the held state of each accumulator and the outputs computed for its current state, the steps each accumulator has yet to do (see Accumulators), which pending records and steps hold which values, which records wait for which, and the queue of work ready to run.
The in-process backend keeps it in memory, apart from the maps, and it is lost when the backend stops.

The maps depend on two orders:

- the records under one label are in the order they were submitted, since `client.latest` returns the newest;
- the pushes into one accumulator are in the order they were accepted, which is the order they are added, since the state after `n` pushes is the plain request over the first `n`.

A finish always names a record appended before it.

### Records

Every record is a request.
Its status is its finish, so a record never changes and a client's copy of it is never out of date.
A record holds references to outputs of records and to datasets, and to nothing else.
A request that reads an accumulator reads the record of the state it pinned, made at its submission: the accumulator's template with each table filled by the rows pushed into it, in push order, and defaults filled in ([ADR 0008](adr/0008-a-read-of-a-state-is-a-record.md)).
The record that `freeze` returns is such a record too.
History stores either as the accumulator and its number of pushes, and lists the rows when it is read.
Provenance is a walk over records and datasets.

```python
total = client.freeze(volume)
total.request                                # Request(VOLUME, {'runs': [{'run': ...}, ...]}): 300 rows
client.provenance(total).datasets()          # the 300 runs
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
| `persist` | one persist request |
| `written` | one write that ended |

The backend appends an event to the log, then applies it to its maps (`Views.apply`).
A refused call appends nothing, so every event in the log can be applied when the log is read again.
A submission is one event, so it is stored whole or not at all.

The log lives in memory, or in a file of JSON lines.
A write that fails, as on a full disk (system story H1), leaves the file as it was.
If a crash cuts the last line short while it is being written, that line is dropped when the file is read; any other line that is not an event is an error.

Each log has exactly one backend, and each backend one log.
A backend holds an exclusive lock (`flock`) on its log file from start to close, and a second backend on the same file, in any process, is refused at start.
The operating system releases the lock when the process ends, so a backend started after a crash or for an upgrade (system story H2) takes the file over.
Two notebooks that share results are clients of one backend with a store, and share what they persist; a backend in each notebook shares nothing.

Event formats must stay readable for as long as the log is kept.
The maps may change between versions.

### Restart

This describes the in-process backend, which only the tests start on an existing log file, with the same store.
A backend given a log that already has events applies them as it applies new ones, so it has the maps of the backend that wrote the log.
No client survives a restart, so the backend keeps only what the store keeps and what persisted work needs:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')), store=store)   # applies the events in the file
```

[ADR 0005](adr/0005-nothing-is-written-unless-persisted.md) (Restart) gives the rule: a pending record that no persist request needs is cancelled (`the backend restarted`); one that a persist request needs runs if each value it reads is a dataset, is written, or is an output of a pending record that runs, and fails otherwise.
A pending record runs from scratch, without its stage, since client entries do not survive a restart.
A pending record that reads a held state fails, since the held state did not survive the restart.
A write that had not ended has failed, since its value is gone.

This is what system story H2 (backend upgrade with runs in flight) needs from history.
H2 also needs every event format to stay readable across versions, and the store, so that a pending record whose input had completed still runs.

### On the service

The service may store history as the same log in a file, in Kafka, or as one database table per list.
It may split history by proposal.
This keeps the orders the maps depend on (see Queries), since each lies within one proposal, and it drops a proposal's history by dropping its part.

## How long history is kept

Users find the records of their work while they consider it ongoing, as with an application they leave open: for batch and automatic reduction, days to weeks ([requirements](../requirements/tensions.md)).
The lasting history of what ran belongs in SciCat.

A proposal is *idle* while none of its clients is open, none of its records is pending, and none of its writes is pending.
Once a proposal has been idle for the retention period, which the deployment sets, its history is dropped as a whole, and the values in its store with it.
With a retention period R:

```text
day 0     a batch of 500 runs is submitted; the laptop closes, so its client ends
day 1     the last record of the batch is written and finishes; the proposal is idle from now
day 3     the user opens a notebook: the records are there, and the proposal is no longer idle
day 3     the notebook closes; the proposal is idle from now
day 3+R   the proposal's history is dropped
```

- Dropping leaves no dangling reference. No other proposal reads the proposal's records (system story G5), and an idle proposal has no open client and no record that waits. The provenance of every kept record is complete.
- The trigger loop knows that it has handled a dataset only from the records under its rule's label ([automatic-reduction.md](automatic-reduction.md)). A running loop keeps a client of its proposal open, so the proposal is not idle and the loop never reduces a handled dataset again. A loop started for a proposal whose history was dropped reduces its datasets again.
- A client whose process ended without closing it is open until the backend ends it. How the service notices such a client is open (scipp/essapps#34).
- A backend that restarts does not know when its earlier clients ended, so it counts idle time from its start.
- A proposal that is never idle, such as one whose automatic reduction runs all year, keeps its history that long. History is small: in story D7, each push appends one item, and each read one record, of constant size (see An example).

Publishing writes the provenance, flattened from history, into the catalogue entry, so what is published outlives the history.
A result needed after its proposal's history is dropped is published.

## The in-process backend: clients, values, and accumulators

The backend in the user's process (`local()`) is implemented, and keeps everything but history and the store in memory.
What this section says about clients, values, and accumulators holds for it.
The same rules hold on the service; what differs there is in The service.

### Clients

A client is one entry in the backend, from `open_client` to `close_client`.
The entry holds the client's proposal and submitter, the IDs of the records whose values it keeps, its stages, and its accumulators.
Every call of the backend names its client, and the backend takes the proposal from the entry.
A call of a client without an entry raises `ClientEnded`; the backend is the only place that checks.
`client.close()` calls `close_client`, which removes the entry and releases everything in it.

### Values

README.md states what keeps an output value (How long records and values are kept; [ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).
The backend keeps three things for that rule:

- in each client entry, the IDs of the records the client asked for and has not released;
- for each output, how many pending records, pending reads of accumulators, and steps of accumulators reference it and have not yet run, and how many stages that have not yet staged reference it;
- for each output, whether a persist request names it, and whether it has been written.

A pending record holds its inputs from submission until its workflow returns, even if the record is cancelled meanwhile, since the workflow still reads them.
A step of an accumulator, opening its held state or adding a push, holds the outputs it references from the call that made it until it is done or dropped.
An output value is dropped from memory once no client entry keeps its record, no stage that has not yet staged references it, no pending record or step that reads it has yet to run, and no persist request waits for its write.
A written value is read from the store.
The backend checks this when a client releases a record or a stage or ends, when a stage has staged or stops, when a workflow returns or a record finishes without running, when a step is done or dropped, when a write ends, and when a record completes, since a record released while pending drops its outputs as soon as it completes.

**What a submission reads.** A submission, a stage, an opening, and a push check each record they reference against what is kept for their client: a record the client entry keeps, or a persisted output.
Any other reference is refused, also to a record that is still pending and that only another pending request reads.
A call through a stage that has not yet staged may also reference what the stage keeps; once the stage has staged, a call reads only the values of its blanks, and its record's edges to the template's values are provenance.
`client.output` reads the same values, waiting for a pending record or write; any other raises.

**Cancelling work that nothing keeps.** When a pending record loses its last keeper, the backend finishes it as `cancelled`, and lets go of what it reads, which may cancel the records that only it read.
A workflow that is already running cannot be interrupted: its record is cancelled at once, and the workflow runs to its end while its outputs are dropped.
The backend checks this at the same points at which it drops values.

A stage keeps the values its template references until its first call has staged the binding, and an accumulator those of its template until its held state has opened.
From then on, what the binding holds is its own, and the backend keeps nothing for it: the stage holds the callable the binding returned, and the accumulator its held state, until the client releases them or ends.
In the user's process, a value the binding still references stays in memory, such as each row of a held state that keeps the rows (see Accumulators), also an output of a record that the client has released.
If staging fails, the stage stops: that call fails with the reason, so do the calls through it that wait to run, and later calls are refused with `the stage stopped: staging failed: <reason>`.

A label names records and keeps no values.

How the stories fare, in the user's process and on the service:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's client |
| B1, S2: tuning steps | each step: the notebook's client, until the notebook releases it; the loaded run: the stage's binding, once staged |
| B2: a sum read after each run | the sum: the accumulator's held state (`Summing`), to which each push adds one run; each read: a copy, a plain value in the notebook; the request over runs 611 and 613: the notebook's client |
| B4: cuts through a volume | the volume: the notebook's client; each cut: a copy of the selection, a plain value in the notebook |
| C5: two stages tuned together, both results read afterwards | the notebook's client |
| C1: a beam centre used by other requests | the client of the notebook that made it, until it releases it or ends; for other clients, the store once persisted |
| D2: overnight batch, laptop closed | the store: the batch is persisted at submission, and each record's outputs are written before it completes |
| D6: a batch's results read weeks later | the store, until the proposal's history is dropped |
| D7: a cut after each run | the volume: the accumulator's held state (`Summing`), added in place; each cut: a copy of the selection; the last state: the record that `freeze` returns, written to the store. One volume is kept, not one per cut |
| E1: the curve a rule made, read later | the store: the rule persists what it submits |

### Accumulators

An accumulator lives in its client's entry; the `Accumulator` that `client.accumulator` returns is a handle to it.
`open_held_state(binding, fixed, tables)` in `ess.dispatch.bindings` makes its held state: the binding's own if the binding provides `held_state(fixed)`, otherwise one that keeps the rows (see below).
README.md (Accumulator, Reads, Adding waits for readers, Freeze, What a binding provides) states the rules; the backend keeps them like this:

```python
held = open_held_state(binding, read(fixed), tables)   # the first step: what depends only on the fixed values, once

def push(rows):                          # {table: row, ...}
    check(rows)                          # names resolved, each row by its table's row model
    check_references(rows)               # against what the backend knows at the call; a pending record is accepted
    append_push(accumulator, rows)
    steps.append(rows)                   # then the call returns

def advance():                           # under the backend's lock, whenever a reader is done or a record finishes
    if busy or not steps or readers[added]:   # the readers of the state before the next push
        return
    if a record that steps[0] references is pending:
        return
    if one failed or was cancelled:
        return stop()
    busy, outputs = True, None           # outputs: those of the current state, if it was read
    on_worker(add)

def add():                               # outside the backend's lock
    held.push(read(steps.popleft()))     # in place if the binding does so; stop() if it fails
    added += 1                           # starts the records that wait for this state
    busy = False
    advance()

def pin(accumulator):                    # a submission, client.output, client.freeze, client.provenance
    with lock:                           # the backend's
        upto = len(pushes[accumulator])  # the pushes logged, added or not
        readers[upto] += 1               # until the submission or freeze is appended, or the copy is made
    if checked is None or checked.upto != upto:   # the first read of this state
        checked = (upto, validate(template, pushes[accumulator][:upto]))   # the plain request
    refuse(checked.reason)               # if not None
    return checked.request               # a submission or freeze appends a record of it, read once added == upto

def read_outputs():                      # by a reader, once the state is reached
    if outputs is None:
        outputs = held.outputs()         # every output, once per state
    return outputs
```

**Opening.** The template is checked as a stage's is, then each fixed value is typed by its own field and that field's validators.
The params model's own validators read whole requests, so they run on the plain request at each read.
The records the template references are checked as a request's are, against what the backend knows at the call: one that failed or was cancelled is refused, and a pending one is accepted.
The accumulator is then appended to history, and `client.accumulator` returns.
Its first step opens the held state once these records have completed, and reads them once.
The binding gets every fixed value with data fields loaded and defaults filled in; if it fails to make the held state, the accumulator stops (see Adding).
History holds the template's values as JSON, and the accumulator uses them as history holds them.

**Push.** A push first resolves the names in its rows and validates each row by its table's row model alone, with no field the model lacks.
It then checks the records the rows reference as a submission checks them, against what the backend knows at the call: records of the same proposal, not failed or cancelled, whose outputs fit the fields and, if completed, are still kept.
A pending record is accepted.
If it fails or is cancelled later, the accumulator stops (see Adding).
History holds each row as the request over that one row would hold it, with names resolved and defaults filled in.
The push is then appended and queued as a step of the accumulator, and the call returns.
A push checks nothing on the whole table, so its cost does not grow with the rows before it.
The pushes are appended in the order they are accepted, and added in that order.
A push that does not fit appends nothing.

**Reads.** A read pins the state after the pushes appended so far, added or not: a request when it is submitted, a `client.output` call when it is made, `client.freeze`, and `client.provenance`.
`client.provenance` reads no output, so it is not a reader.
Pinning never waits for a push to be added.
Under the backend's lock, a submission pins the state of each accumulator its requests read, and counts as a reader of these states, so the next push into each is not added while the submission is checked.
It validates their plain requests outside the lock, then, under the lock, checks the requests, appends a record of each state's plain request and then the records of the requests, and stops counting as a reader.
The records of the requests reference the outputs of the records of the states.
A record of a state is computed from the held state: its outputs are what the held state returns, not a copy, as a call through a stage is computed from what the stage holds.
No client asked for it, so no client entry keeps it: its outputs are kept by the requests of its submission, and dropped once they have run.
The record is a reader of its state until then.
A later request, stage call, row, or template that references it is refused by the general check (see Values), so every reader of a state is logged before the next push.
A record of a state the accumulator has yet to reach waits for it as for an input record.
`client.output` of the accumulator waits for the state as `client.wait` waits for a pending record, and copies the output or the selection; `client.output` of the record of a state raises.

The first read of a state validates its plain request outside the backend's lock, and the accumulator keeps the verdict for that state.
A read of a state whose plain request would be refused is refused with that request's reason.
The validation takes time in proportion to the rows, so a driver that reads after every push pays it at every push.

The outputs of a state are computed outside the backend's lock, under a lock of their own, all at once, the first time a reader reads them.
The accumulator keeps them until the next push is added.
The record of the state holds them as the binding returns them, and the requests that read it read them in place; `client.output` of the accumulator copies them, or the selection.

**Freeze.** `client.freeze` pins the state after the pushes appended so far, validates its plain request as a read does, and appends the record of that state, with a persist request if `persist=` is given.
The accumulator then takes no pushes.
The record is a reader of its state until it has finished.
Its outputs are those the held state returns, not copied; once the record has completed, the backend drops the held state, the outputs keep any memory they share with it, and a read of the accumulator is refused and names the record.
If the record fails or is cancelled, the accumulator can still be read and frozen again, unless it has stopped.

**Adding.** Each accumulator has a queue of steps, in the order they were appended: opening its held state, then adding each push.
The backend runs one step of an accumulator at a time, on a worker, outside its lock.
A step starts once the records it references have completed.
Adding a push also waits until the readers of the state before it are done: the held state only moves forward, and the outputs of a state may share memory with it, so the next push may change them in place (README.md, Adding waits for readers).
A record of a state is done once the requests that read it have run, not only started, since a running workflow holds the value; this includes a cancelled request whose workflow still runs.
Before a push is added, the outputs computed for the state before it are dropped.
Once it is added, the records that wait for the state after it start.
A long step holds up only the later steps of the same accumulator and the reads of its later states.
Submissions and other accumulators go on.

If a step fails, or a record it references fails or is cancelled, the accumulator stops.
Its later steps are dropped, later pushes and reads are refused with the reason, and the records that wait for a state it did not reach fail with it.
The reason names the step, such as `the accumulator stopped: push 1 failed: <reason>` or `the accumulator stopped: push 0: input <id> cancelled`.
The driver then opens a new accumulator.

Releasing the accumulator or ending its client removes it from the client entry, so it takes no more pushes or reads.
The steps up to the last pinned state still run, and the later ones are dropped; the held state is dropped once its readers have run.
A pending record that nothing keeps any more, such as a cut whose client released it, is cancelled, and so is a record of a state that only it read.
Closing the backend waits until no record with a persist request is pending and no write is pending.

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

**Designed in [ADR 0005](adr/0005-nothing-is-written-unless-persisted.md); not implemented.**
Batch and automatic reduction run on the service, and a client connects with `connect(url, proposal=...)`.

- **The rules are those of the user's process.** A client keeps the values of the records it asks for until it releases them or ends; nothing is written unless persisted.
- **A cap per client** counts the values a client keeps, those its pending requests keep, and its held states. Values with a persist request count against the proposal until written, and the cap never drops them. At the cap, a completed value that does not fit is dropped, as for a released record, and reading it raises with the reason. A freeze moves the held state's outputs into its record, so memory they share counts once. If a frozen value does not fit, the freeze record fails, and the accumulator stays readable.
- **A lease** ends a client that vanished. Every call renews it, and a call in progress keeps it alive, such as a `client.output` that waits for a long reader (scipp/essapps#34).
- **The store** is an area per proposal that the framework owns. It finds a value by the record's ID and the output's name; history names no file. Values are dropped with the proposal's history at the latest. A value dropped earlier, for example to free disk space (system story H1), is read as a value that nothing keeps. `publish` copies a value into the proposal's upload folder and registers it in SciCat.
- **The store is given to the backend**, not built into it: each deployment configures its own, and tests use a fake. The format of each output type and the layout within a proposal's area are first-release work (scipp/essapps#23).
- **Values reach other nodes** as the system decides, for example through a scratch file deleted once nothing keeps the value.
- **Besides history and the store**, the service holds the values its clients keep, what its stages hold, the held states of accumulators, and caches that it may drop at any time, such as a copy of a value it has read. The outputs of the record of a state are held where the held state is. How it holds a held state, bounds its memory, and ends it is open ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md), Open). A request reads a held state in place, so it runs where that held state is ([ADR 0003](adr/0003-accumulators-add-in-place.md), What may read an accumulator).
- **An upgrade** ends every client of the old instance. Whether persisted work running at the upgrade is finished by the old instance or run again by the new one is open ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md), Open).

## Open

- How the service notices a client whose process ended without closing it (scipp/essapps#34), and how it sets the cap per client (scipp/essapps#27).
- The format of each persisted output type and the layout of the store within a proposal's area (scipp/essapps#23).
- Accumulators on the service: how the service holds a held state, bounds its memory, and ends it ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md), Open).
- What a finish records besides the status, such as the software environment that ran the request ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md), Open).
- An upgrade with persisted work running: the old instance finishes it, or the new one runs it again ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md), Open).
- How the service stores history: the in-process backend's log, Kafka, or database tables ([ADR 0004](adr/0004-history-is-append-only-lists.md) lists what the in-process backend shows).
- A forwarder: something a client keeps that holds the last value pushed into it, as in sciline. It joins stages and accumulators when a story needs one, for example a driving server that shows the latest curve of each sample.
