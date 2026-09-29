# Array records

**Status: a proposal for a decision. README.md does not change until it is adopted.**

## Summary

An array is the durable form of a holder: the holder's template, stored once, and one record per call through the holder.
A record stays what it is today, one request with one status, and names its array and its position in it.
A new reference, `Rows(array, start, stop, output)`, names one output of consecutive records of one array.

Why: an accumulator read can then name its elements in O(1), and its record still means the flat request of way 2.
D7 stops being quadratic, and "ways 2 and 3 make the same records" still holds.
A holder's template is also stored once, not once per call.

Main cost: a second level of identity (array and row) that the check, the scheduler, the store, the wire format, and provenance all handle.
An array is a durable object that grows while its holder lives.
The store keeps holders' templates and records name them, which reopens a settled decision.
The O(1) read holds only when the pushed elements are consecutive records of one holder, so D7 must route its angles through a stage.

Verdict: the chain of totals is better on concepts, cost, generality, and implementation size.
Arrays are better at one thing: an accumulator read's record is the flat request, whatever the read cadence, also in published provenance.
Arrays fix D7 only for elements from one holder.
Their other gains are constant factors that the store can take without an API change, or groupings that no story needs.
The two combine: the chain for accumulator reads, and "a holder's records share one stored template" as a layout of the store, invisible in the API.

## The design

### Records are rows of arrays

Every call through a holder makes a record in the holder's array.
A request outside a holder makes an array of one record.

```python
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
    a = client.compute(tune, {'bins': 50}, label='iofq')
    b = client.compute(tune, {'bins': 100}, label='iofq')

a.request.params                       # every value, defaults included, as today
(a.array == b.array, a.row, b.row)     # (True, 0, 1)
a.ref('iofq')                          # OutputRef(record=a.id, output='iofq'), as today

plain = client.compute(IOFQ, a.request.params)
(plain.array == a.array, plain.row)    # (False, 0): an array of one record, no blanks
plain.request == a.request             # the record of the plain request, as today
```

What the store keeps:

```text
array a41c   template  sans-iofq/v1 {run: uuid:run-1, threshold: 0.0, can: None,
                                     beam_centre: None, normalization: None}
             blanks    (bins,)
             proposal p1, submitter anna, holder stage, growing until the session ends
  row 0      {bins: 50}    completed   label iofq   created 10:02
  row 1      {bins: 100}   completed   label iofq   created 10:03
```

`record.request` is the array's template filled with the row's values.
A user never reads a template from the store: every record carries its full request, as today.
`client.submit` returns records in the shape given, as today; a call through a holder returns the new row's record.

The array's template holds values as resolved at the first call: defaults filled, dataset names resolved to identities.
A later call whose fixed values resolve differently, such as a run name that now names another dataset, starts a new array of the same holder.
Otherwise a record through a stage would differ from the plain request.

### Which calls form one array

Only holders form arrays: every call through a stage, and every read of an accumulator.
A batch from `apply`, a rule's submissions, and a plain `for` loop make one array per request.

The alternatives, and why not:

- A batch as one array. Its requests are complete at submission, so a list of its outputs costs O(k) once, not per read. What it saves is storage, which the store can take without an array. `apply` would also have to return its template next to the requests.
- A rule's arrivals as one array. The trigger loop runs for months, so the array needs a bound (per scan, per day), and it outlives any session. It is then a durable accumulator log, and "holders live in a session" reopens. An index by label gives the trigger loop the same speed.
- A session's plain requests of one spec as one array. D7 would then work without a stage. But the array would depend on the order in which a session submits, and two scans in one session would break each other's ranges.

### Growth and end

An array gains records while its holder lives, and never changes after that.
There is no call to close an array: ending the session ends it, including when the system ends a session whose client died (B5).
A finished record never changes, as today.

### References

A record's ID names its array and row, so `OutputRef(record=..., output=...)` is unchanged for one record.
A range of records of one array is a new reference:

```python
Rows(array='a41c', start=0, stop=3, output='counts')
# means
[OutputRef(record=<row 0>, output='counts'), OutputRef(record=<row 1>, ...), OutputRef(record=<row 2>, ...)]
```

`Rows` is an item of a list of output references and stands for the references it covers.
A list may mix `Rows` and single references; it means the list with every `Rows` expanded.
Request equality compares lists expanded, at O(k); otherwise only provenance, and the backend when it reads values, expand them.
Users do not construct `Rows`; an accumulator does.
Plain requests keep their lists as given (S4 is unchanged).

### Accumulator reads

A read is the record that computing an accumulator makes, `client.submit(volume)`.
An accumulator keeps its elements as a list of ranges and merges a push into the last range when it is the next row of the same array.

```python
with client.session() as session:
    angle = session.stage(Template(ANGLE, blanks=('run',)))
    volume = session.accumulator(SUM.of(Counts))
    for run in runs:                                      # 1000 runs
        volume.push(client.submit(angle, {'run': run}))
    total = client.compute(volume)

total.request.params['counts']       # [Rows(array=<angle's array>, start=0, stop=1000, output='counts')]
angles = client.records(spec=ANGLE)
plain = Request(SUM.of(Counts), {'counts': [a.ref('counts') for a in angles]})
total.request == plain               # True: the range is expanded to compare
```

The reads are records of the accumulator's own array, whose template is `SUM.of(Counts)` with the blank `counts`.

Elements from several arrays, such as a record made outside the stage, or a generic `SUM` over outputs of different specs, give a list of pieces:

```python
volume.push(client.submit(angle, {'run': r1}))    # angle's array, row 0
volume.push(client.submit(ANGLE, {'run': r2}))    # outside the stage: an array of its own
volume.push(client.submit(angle, {'run': r3}))    # angle's array, row 1
client.submit(volume).request.params['counts']
# [Rows(angle, 0, 1, 'counts'), OutputRef(<r2's record>, 'counts'), Rows(angle, 1, 2, 'counts')]
```

A read costs O(number of pieces).
D7 written as in user-stories.md, with plain `client.submit(ANGLE, ...)`, gives 1000 pieces: correct, and quadratic as today.

### Status, failure, cancel, rerun

Each record has its own status, failure, and outputs, as today.
A failed record affects no other record of its array.
`client.cancel(records)` cancels records; ending a holder does not cancel its pending records (B5).
A rerun is a new record: through the holder, a new row of its array; outside it, a new array.
Under a label and member, the latest record wins, as today.

A range over a failed record fails, as a list with a failed reference does.
An accumulator leaves a failed element out of its next read by splitting its range, `[Rows(a, 0, j, ...), Rows(a, j + 1, k, ...)]`.
Reads submitted before the failure was known stay failed.
Skipping failed elements without failed reads is a separate decision (below); it needs the same addition with or without arrays.

### Labels, members, queries

Label and member stay on the record: B3 puts two labels on records of one stage.
The array has no label.
`client.records`, `latest`, and `members` return records, as today.
No call lists arrays; no story needs one.

### Provenance and publication

`client.provenance(record)` expands `Rows` into the records it names.
The provenance of D7's total lists 1000 angle records and no reads: the flat form.
A published entry carries each record's full request, so it does not depend on the store's templates.

### What the backend checks

Each record is a complete request.
The backend checks the template filled with the row's values against the spec's params model, as it checks a plain request.
It never checks a template alone, since a template with blanks is not a valid request, and it never checks that records of one array agree with each other.

No record is a piece of another: an array's records are independent requests that share stored values.
Records that held caller-cut pieces of one pipeline would make the graph-blind backend check that the pieces fit together; arrays do not ask for that.

A `Rows` reference is checked once per reference, not once per element:

| Check | For a list of k references | For `Rows(array, start, stop, output)` |
|---|---|---|
| proposal | each record | the array's |
| output exists and fits the field | each record's spec | the array's spec |
| no failed or cancelled input | each record | the array's failed rows, by lookup |
| wait for inputs to finish | k waits | the array's unfinished rows, by lookup |

### Backend and store

```text
arrays   id, spec, template values, blanks, proposal, submitter, holder kind, created, growing
records  array, row, row values, status, failure, label, member, created, output locations
per array, derived: sorted unfinished rows, failed rows, waiting reads by range
```

A hosted backend opens an array at a holder's first call, appends a record per call, and ends the array when the session ends.
A call through a stage sends the array ID and the row's values, not the full request.
`Rows` is part of the wire format and of the reference vocabulary in `ess.reduce.spec`, since a params model must accept it where it accepts a list of references.

### One concept or two

The unit with one status is what user code holds: `result.status`, `client.output(result, ...)`, `result.ref(...)`, `latest`, `members`, rerun.
Whatever it is called, it is today's record.

- Two concepts, "an array record with rows": a call returns a row, and every story that holds a result holds a row. "Record" then names the container, and every use of "record" in README.md and the stories changes meaning.
- One concept in the sense "every record has rows; a plain request is a record of one row": a stage call must still return something with one status, which is the row. This is the two-concept design under one name, and "record" becomes ambiguous.
- Rows are records, and the array is where a holder's template is kept (this design). User code keeps "record" for what it holds. The array is visible only as `record.array`, `record.row`, and in `Rows`.

The third adds one durable term, array, and one reference form, `Rows`.
Of the three, it adds the fewest terms that still give an accumulator read an O(1) name with a flat meaning.

### Not part of the design: replacing records

Keeping only the latest read of an accumulator, by replacing its record, breaks references: D7's `CUT_k` names read k.
Labels already give "the latest".
Not proposed.

## Stories and guarantees

user-stories.md has 45 stories. A3, G3, H1, H2, and H3 are system stories only.

| Story | Array records | Chain of totals |
|---|---|---|
| S1 | unchanged | unchanged |
| S2 | unchanged: the three records are rows 0 to 2 of the stage's array | unchanged |
| S3 | unchanged | unchanged |
| S4 | unchanged: plain requests keep their lists | unchanged |
| S5 | unchanged | unchanged |
| S6 | unchanged | unchanged |
| S7 | unchanged: a batch is one array per request | unchanged |
| S8 | unchanged | unchanged |
| A1 | unchanged | unchanged |
| A2 | unchanged | unchanged |
| A3 | system only; unchanged | unchanged |
| A4 | unchanged; gap stays | unchanged |
| A5 | unchanged | unchanged |
| B1 | unchanged: the four tuning records are one array | unchanged |
| B2 | unchanged code and checks; the reads name `Rows(contribute, 0, 2)` and `Rows(contribute, 0, 3)`; the removal is a plain request, as today | unchanged code and checks; the second read is `PARTS_SUM([t1, c613])`, and README.md's sentence under the example changes (below) |
| B3 | unchanged: two labels in one array | unchanged |
| B4 | unchanged | unchanged |
| B5 | unchanged: the dead session's array stops growing, the new stage makes a new array | unchanged |
| B6 | unchanged | unchanged |
| C1 | unchanged | unchanged |
| C2 | unchanged | unchanged |
| C4 | unchanged | unchanged |
| C5 | unchanged: two stages, two arrays | unchanged |
| D1 | unchanged | unchanged |
| D2 | unchanged | unchanged |
| D3 | unchanged: `client.cancel(first)` cancels 500 records | unchanged |
| D4 | unchanged: refused before any array exists | unchanged |
| D5 | unchanged | unchanged |
| D6 | unchanged | unchanged |
| D7 | **changed code**, checks unchanged (below) | **changed check**, code unchanged (below) |
| E1 | unchanged | unchanged |
| E2 | unchanged | unchanged |
| E3 | unchanged | unchanged |
| E4 | unchanged | unchanged |
| F1 | unchanged: the stage's record and the plain one have equal requests, so equal provenance | unchanged |
| F2 | unchanged | unchanged |
| F4 | unchanged | unchanged |
| G1 | unchanged | unchanged |
| G2 | unchanged | unchanged |
| G3 | system only: the laptop session's arrays live in the cluster's store | unchanged |
| G4 | unchanged | unchanged |
| G5 | unchanged | unchanged |
| H1 | system only: outputs are dropped per record; templates stay with their arrays | unchanged |
| H2 | system only: arrays of sessions ended by the upgrade stop growing | unchanged |
| H3 | system only: an array's template is kept while any of its records is; a `Rows` over expired records loses those steps of provenance, as a list does | system only: an expired read cuts the provenance of the reads after it, as expired angles cut a list's |

Neither design breaks a story. Each changes one: D7.

D7 with array records. The angles go through a stage so that they form one array:

```python
with client.session() as session:
    angle = session.stage(Template(ANGLE, blanks=('run',)))           # added
    volume = session.accumulator(SUM.of(Counts))
    for run in islice(datasets.watch(Selector(scan='17')), 1000):
        volume.push(client.submit(angle, {'run': run}))                 # through the stage
        client.submit(CUT, {'data': client.submit(volume).ref('counts'), 'index': 0},
                      label='cut', member='17')
    total = client.compute(volume)
# checks as in user-stories.md, including total.request == plain.request
```

The stage computes nothing ahead, since ANGLE has no fixed values; it is there only to group the records.
Without it, the story passes and stays quadratic.

D7 with the chain. The code is as in user-stories.md; one check changes:

```python
assert sc.identical(client.output(total, 'counts'), client.output(plain, 'counts'))
assert set(client.provenance(total).datasets()) == set(client.provenance(plain).datasets())
# replaces: assert total.request == plain.request
```

| README guarantee | Array records | Chain of totals |
|---|---|---|
| A record holds the spec, every parameter value including defaults, and its inputs by reference. A finished record never changes. | changed, a sentence added: "A record made through a holder is a row of the holder's array, which keeps the template once. An array gains records while its holder lives." | unchanged |
| A holder never changes what a record says. A record made through a stage or an accumulator is the record of the plain request. | unchanged: array and row are part of the record's ID, which differs between any two records already | changed: "... A record made through a stage is the record of the plain request. A read of an accumulator is the record of its accumulator spec over the previous read and the elements pushed since." |
| Every connection between requests is a reference. A value passed in memory is the referenced output itself, so a workflow must not modify its inputs. | unchanged; `Rows` is a reference to several outputs | unchanged |
| A record's outputs do not depend on how they were computed ... | unchanged | unchanged; for a left fold such as `SUM` the chained value is bit-identical to the flat one |
| The provenance of a record reaches every dataset it read ... | unchanged, through `Rows` | unchanged, through 2k − 1 records |
| Records are kept for the medium term. A published entry answers what produced it without access to the records. | unchanged | unchanged |
| Only holders keep memory on a user's behalf, and ending their session releases them once the requests made through them have run. | unchanged | unchanged |

Other sentences that change:

| Where | Array records | Chain of totals |
|---|---|---|
| README, accumulator: "Computing an accumulator makes a record of its accumulator spec over the elements pushed so far." | unchanged | "... over the record of the previous read and the elements pushed since." |
| README, under the B2 example: "It is the record that way 2 makes" | unchanged | "The second read makes a record of `PARTS_SUM(numerator=[t1.numerator, c613.numerator], ...)`, where `t1` is the first read." |
| README, one sum three ways: "Ways 2 and 3 make the same records" | unchanged | "Way 3 makes the records of way 2 written as a chain, with the same values." |
| README, accumulator specs: "An author may declare that the result does not depend on how the elements are grouped." | unchanged | every accumulator spec has this property; open question 1 loses its second half |
| README, left to the system: "how records that share most of their references are stored without repeating them" | replaced by arrays, which the API shows | "how the records of one holder are stored without repeating its template" |
| automatic-reduction.md: "The core keeps no store of them, and records do not name them" | "... except a holder's template, which its array keeps; a record names its array." | unchanged |
| system story D7: "Each read of the volume makes a record over every angle pushed so far." | holds when the angles go through one stage | "Each read makes a record over the previous read and the angles pushed since." |

## Against the chain of totals

The chain: the first read is `SPEC([e_1, ..., e_m])`; each later read is `SPEC([previous read, elements pushed since])`; a read with nothing pushed since returns the previous read's record.

| | Array records | Chain of totals |
|---|---|---|
| New concepts | array (durable), `Rows` (reference) | none |
| What an accumulator read's record says | what the value is: the flat request over the elements | how the value was computed: the previous read plus the new elements; same value, same datasets |
| Request equality of two reads over the same element records | equal, at any read cadence | equal only if read at the same points |
| D7, per push: client | O(1), O(pieces) in general | O(1) |
| D7, per push: check | O(1) per `Rows` | O(1), two references |
| D7, per push: schedule | O(log k), lookup in the unfinished rows | O(1) |
| D7, per push: compute | O(1) with the value held in a session; O(k) without, unless the backend reuses the previous read as a part | O(1), with or without a session: it reads the previous read's output |
| D7, per push: store and wire | O(1) | O(1) |
| D7, provenance per query | O(k): k records | O(k): 2k − 1 records |
| D7 as written (angles not through a stage) | O(k) per push, quadratic | O(1) per push |
| Per call through a stage | template stored and sent once; check unchanged, O(request) | as today; the store may keep the template once |
| Failed element | reads over it fail; the accumulator splits its range for the next read | reads chained on it fail; the accumulator chains the next read on the last completed read |
| Skipping failed elements without failed reads | needs a backend rule that leaves failed elements out (decision 4) | the same rule |
| Cancel | per record, as today | per record, as today |
| B2 removal | a plain request over the kept records, as today; a future `remove` is O(pieces) to record | a plain request over the kept records, as today; a future `remove` restarts the chain from a flat list, O(k) once |
| Trigger-loop restart, accumulating rule | O(1) reads only with a session owned by the loop and a stage (README open question 2); a restart makes a new array, so each read names one piece per restart, and the value is recomputed over all elements once | continues from `client.latest(label, member)` in O(1); needs no session |
| Hosted backend | open, append to, and end arrays; `Rows` on the wire and in the store's schema | nothing new |
| Published provenance | flat: the total over its angles | the chain, unless `publish` flattens a chain of one accumulator spec (O(k), about 30 lines) |
| Requirement on accumulator specs | none; a spec whose result depends on grouping is recomputed over all elements | the result must not depend on grouping, as sciline's `Accumulator` protocol already requires ("so that combining can proceed in groups or as a chain") |
| Implementation, in-process | about 270 lines: `Record.array`, `Record.row`, `Rows`, and request equality that expands it (records.py, 50); params models that accept `Rows` (accumulators.py, 15); arrays, appends, `Rows` in resolve, check, schedule, finish, and read, end at session end (backend.py, 150); holders open arrays, the accumulator keeps pieces (sessions.py, 40); provenance expands `Rows` (client.py, 10); plus `walk_refs` and `as_ref` in ess.reduce | about 20 lines in the accumulator holder (remember the last read, build the next one); about 15 more to chain past a failed read |
| Forecloses | self-contained records in the store (reading one needs its array); "the core keeps no store of templates"; a fast accumulator for elements not made through one holder, unless user code knows to use a stage | "ways 2 and 3 make the same records"; request equality as a test that two reads combine the same elements; accumulator specs whose result depends on grouping |

Array records do not help the trigger loop unless a rule's arrivals form one array, which needs the bound and the durable log rejected above.
They do not help E1, whose series records list the runs in run order, not arrival order.

The analogies that make arrays look natural are batches: `sciline.compute_members(pipeline, members=, key=, table=)` takes a complete table, and a Slurm job array has a fixed range of indices at submission.
A stage's array grows, which neither does.
The batch case is the one where arrays save nothing but storage.

Where arrays win: an accumulator read's record keeps its flat meaning, including in published provenance, and it does not depend on when reads happened.
That is worth a new concept only if the flat meaning is a requirement.
Records are medium-term working state, and what lasts is the published entry, whose provenance the chain can flatten when it is published.

Per-call failures are not where the problems are.
Each record keeps its own status, failure, and rerun, as today, because each record is a complete request.
The problems are elsewhere: the O(1) read depends on how the elements were submitted, every layer handles a range reference, arrays grow, and the store keeps templates.

The chain wins.
The two combine: the chain for accumulator reads, and the array as the store's layout for a holder's records, stated under "Left to the system" and invisible in the API.

## Decisions

### 1. Must an accumulator read's record be the flat request?

D7 reads the volume after every push, and each read makes a record.
The record of the read after push k:

```python
SUM.of(Counts)(counts=[a_1.counts, ..., a_k.counts])     # flat, today: O(k) in every layer
SUM.of(Counts)(counts=[t_{k-1}.counts, a_k.counts])      # chain: O(1); t_{k-1} is the previous read
SUM.of(Counts)(counts=[Rows(angles, 0, k, 'counts')])    # arrays: O(1); means the flat list
```

Flat is quadratic over the scan: 6.66 s for 1000 angles against 0.55 s for the chain, most of it in checking and scheduling references.
All three give the same value and reach the same 1000 datasets.
The chain changes D7's check `total.request == plain.request` into equal values and equal datasets, and README.md's "ways 2 and 3 make the same records" goes.

Options:

- (a) No: the chain. About 20 lines, no new concept, D7 code unchanged.
- (b) Yes, with array records (this proposal). One durable term and one reference form, about 270 lines plus ess.reduce, and D7 routes its angles through a stage.
- (c) Yes, with structural sharing of reference lists in the store and every layer. No API change, and it works for elements from anywhere, but it is the largest implementation.

Recommendation: (a).

> Simon:

### 2. If the flat meaning is required: arrays or structural sharing, and which calls form an array?

Only if decision 1 is (b) or (c).
Arrays give an O(1) read only for consecutive records of one holder; D7 as written (plain `client.submit(ANGLE, ...)`) stays quadratic until its angles go through a stage:

```python
angle = session.stage(Template(ANGLE, blanks=('run',)))     # computes nothing ahead; groups the records
volume.push(client.submit(angle, {'run': run}))
```

Structural sharing works for any elements, with no API change, at the cost of a persistent list type in the client, the wire format, validation, scheduling, the store, and equality.

If arrays: which calls form one?

- (i) Holders only: calls through a stage and reads of an accumulator. Arrays end with the session.
- (ii) Also a batch: one submission's requests of one spec. `apply` returns its template with the requests. Saves storage only.
- (iii) Also a rule's arrivals across steps. Needs a bound such as per scan or per day, and outlives sessions, so it is a durable accumulator log.

And whether rows are records (user code keeps "record" for the unit with one status; the array is a stored template) or an array record has rows (every story's result becomes a row).

Recommendation: arrays over holders only (i), with rows being records.

> Simon:

### 3. Storing a holder's records without repeating its template: API or store?

1000 calls through an IOFQ stage take 372 kB as records and 159 kB with the template stored once (toy spec; a real template has more fields and saves more).
Both are linear in the number of calls.
README.md already leaves "how records that share most of their references are stored without repeating them" to the system.

Options:

- (a) Leave it to the store. Reword that item as "how the records of one holder are stored without repeating its template". No API change.
- (b) Make arrays part of the API for this reason alone.

Recommendation: (a).

> Simon:

### 4. A failed element in an accumulator

Today, and with the chain or arrays alike, a failed pushed record makes every later read fail, and nothing drops it.
In D7, a corrupt angle j fails every read submitted before the failure is known, and the CUT that reads each of them.
D1's goal, "one corrupt file affects nothing else", does not hold for D7.

Options:

- (a) The accumulator leaves a failed element out of its next read: the chain continues from the last completed read with the elements since, minus the failed one; arrays split the range. Reads submitted before the failure was known stay failed. No backend change.

  ```python
  # angle j failed; t_c is the last completed read, c < j
  SUM.of(Counts)(counts=[t_c.counts, a_{c+1}.counts, ..., a_k.counts])     # chain, without a_j
  SUM.of(Counts)(counts=[Rows(angles, 0, j, 'counts'), Rows(angles, j + 1, k, 'counts')])   # arrays
  ```

- (b) An accumulator spec leaves failed elements out. A read waits until its elements have finished and combines those that completed; its record lists the elements left out. No read fails for a failed element. Needs a backend rule for accumulator specs, and a record field set when the record finishes.

Recommendation: (a) now; (b) when a story shows that the failed reads between a failure and the next read matter.

> Simon:

### 5. Flatten a chain in provenance and publication?

Only if decision 1 is (a).
The provenance of D7's total then lists 999 reads and 1000 angles, and a published entry would carry the chain.
A chain of one accumulator spec can be presented as that spec over the chain's leaves, at O(k) when asked, about 30 lines.

Options:

- (a) No: provenance and published entries show the chain as recorded.
- (b) `publish` flattens; `client.provenance` shows the records as they are.
- (c) Both flatten.

Recommendation: (a) until an accumulated result is published; then (b).

> Simon:
