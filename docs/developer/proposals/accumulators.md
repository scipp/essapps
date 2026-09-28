# Proposal: stages, labels, and accumulators

**Status: proposal for review. Nothing here is implemented.**
It extends the design in [architecture.md](../architecture.md) with sciline's connectors and drivers (scipp/sciline#245, design doc sections 2 and 4).
The code below is pseudocode in the style of the skeleton's API; names are open to change.

## Summary

- `Template` splits in two: a **stage** is plain data, the counterpart of `sciline.Stage`; a **template** is a stored, versioned stage that batches and rules fill.
- **Nothing is held unless the user creates a holder.** `client.hold(stage)` builds a stage in a session and keeps it until `client.release`; `client.accumulator(...)` creates an accumulator. Neither changes what a record says.
- A **label** is a name over a sequence of records. Its current record is its value.
- An **accumulator** is a stored object whose states are records under its label, the counterpart of `sciline.Accumulator`. Runs push into it, each push is written to its push log, and it keeps its value in memory when it can.
- An **accumulator spec** names an operation, such as `sum`. A few standard ones ship with ess.reduce, and they serve many workflows. The accumulator binds the value type.
- A reader names the current record of a label with the stand-in `Current`, resolved at submission like a run number. References do not change.
- Combining and finalizing are separate. The workflow author declares a contribute spec and a finalize spec. The user pushes, the accumulator combines, the user finalizes.
- How an accumulator combines, in memory, flat, or as a tree over many processes, is its policy. The user does not see it.
- Adding to a sum in memory and fanning out over processes are one mechanism with two policies. A request over a list of runs stays, computed flat, and holds nothing.

The rule that keeps this from repeating the stage records that 48e2c17 reverted:

> A cut that crosses a record boundary is made by the workflow author, where the graph is known.
> A cut inside a process may be made by the caller, by holding a stage; it never changes a record.

## The vocabulary

| sciline | essapps, inside one process | essapps, between records (durable) |
|---|---|---|
| `Stage` | a held stage: `client.hold(stage)` | a spec: a cut the author made |
| forwarder | an output one held stage passes to the next, from the session's memory (C5) | a label: its current record |
| `Accumulator` | the held value of an accumulator | an accumulator's push log and state records |
| driver | a workflow's `stage()`; the holder of an accumulator | a group; a rule; the policy of an accumulator |

The essapps names mirror the sciline names on purpose.
The process that computes or holds keeps the sciline object; the client and the records see plain data.

| Declared by a package | Plain data a user writes | Created through the client | Held in memory |
|---|---|---|---|
| `WorkflowSpec`: a signature | `Stage` | a held stage | a `sciline.Stage` |
| `AccumulatorSpec`: an operation | | an accumulator | a `sciline.Accumulator` |

The word "accumulation" keeps its current meaning: the accumulated value.

## Explicit holders

Nothing is held unless the user creates a holder.
A plain `client.run(IOFQ, params)`, or a run of a `Stage` that is not held, holds nothing afterwards.

|  | Held stage | Accumulator |
|---|---|---|
| created by | `client.hold(stage)` | `client.accumulator(name, spec, policy=...)` |
| holds | the stage's frontier, which does not change | a value that grows with each push |
| built | at `hold`, which returns when the frontier is computed | at creation, empty |
| stored | nothing; it is a cache | its declaration, per version, and its push log |
| reuse | every call through the handle, while held | every push, while held |
| memory freed | `client.release`, the end of a `with` block, or the end of the session | the same; the push log and state records remain |
| does not fit | `hold` is refused against the session's memory budget; nothing else is evicted | creation is refused the same way |
| holder lost | calls through the handle fail and say so | pushes and reads fail until `client.hold(acc)` replays the push log |

A handle is plain data, an ID and the definition, so it works over HTTP as in process.
A record never names a handle or a session: a call through a held stage records exactly what a plain run with the same values records.

This replaces two implicit mechanisms of the skeleton:
the `vary` hint on submissions and on `Group`, whose omission lost the shared stage without an error (14e2d99), and a session that built a stage on the first call naming it and kept the four most recently used.

A held stage brings back a handle, which 62ec736 removed.
That removal was about duplicated information: a handle and a template held the same values.
The new handle holds a lifetime; `Template` still wraps the plain `Stage`.

## Three needs, one mechanism

Accumulating over runs serves three needs, and techniques differ in which they have:

| Need | Stories | SANS, reflectometry | Spectroscopy |
|---|---|---|---|
| one request over a list, computed flat | S5, S6, a stitch under a rule | yes | yes |
| adding to a sum held in memory | B2, E1, an accumulator over a beamtime | yes | yes |
| fan-out across processes | a rotation scan over a thousand angles | no | yes |

The second and third need are one mechanism: the author's split into a contribute spec and a finalize spec, and an accumulator between them.
Techniques differ only in its policy:

```python
client.accumulator('sum', spec='sum/v1', policy=Held())                  # SANS: one process holds it
client.accumulator('rotation', spec='sum/v1', policy=Tree(fan_in=32))   # spectroscopy: combines fan out
```

The user code is the same for both, and a technique whose data grows changes a policy, not its workflows.

The first need stays a request over a list parameter, computed flat: the binding calls the package's `sciline.Aggregation` and finalizes, and holds nothing between requests.
Adding to a sum always goes through explicit pushes.

What SANS pays for this: the author declares the split, which is the signature of the `sciline.Aggregation` the package already builds, and interactive work writes more records, a contribute record per push and a state record whenever something reads the sum.

### Alternative considered: an accumulating sibling of `Stage`

An object beside `Stage` would hold accumulators at several points of one spec's graph: each call pushes one member, and outputs are computed from the held values.
The record of push n is the spec over the list of the first n members, so no new kind of record is needed.
The skeleton's adapter holds such an object for a stage whose blank is a list, inferring the added members from successive lists.

It is not chosen, for three reasons:

- Where values accumulate changes the result, a sum of ratios against a ratio of sums, so the points cannot be a caller's choice the way a stage's cut is. They belong to the author, which is what the split declares.
- What such an object holds depends on the shape of the graph: which varied parameters the contributions read decides when the held values are dropped. sciline removed this kind of state from `Aggregation` (design doc section 8.4), and sciline keeps connectors out of `Stage` so that a driver can inspect, clear, serialize, or send them (section 8.1).
- Spectroscopy needs the split for fan-out anyway, so the object would be a second mechanism for adding to a sum, beside the accumulator.

## The objects

### Stage, template, and held stage

```python
class Stage(BaseModel):                  # plain data: what to compute
    spec: SpecId
    params: dict[str, Plain]             # the values set
    blanks: tuple[str, ...]              # the values each call fills
    outputs: tuple[str, ...]

class Template(BaseModel):               # stored and versioned; batches and rules fill it
    name: str
    version: int
    stage: Stage
    dataset_field: str | None            # the blank a dataset fills

class HeldStage(BaseModel):              # a handle: plain data
    id: str
    stage: Stage

client.hold(stage: Stage) -> HeldStage   # builds and computes the frontier in a session
client.run(held, values, label=...)      # served by the held stage
client.release(held)                     # frees it; also `with client.hold(stage) as held:`
```

`client.run(stage, values)` with a plain `Stage` fills the blanks and submits a plain run; nothing is held.

### Label and `Current`

```python
class Current(BaseModel):                # a stand-in, never stored in a record
    label: str
    output: str = 'value'                # the output of an accumulator's state
    member_key: str | None = None

# at submission, the backend replaces every Current by a reference:
def resolve(value: Current) -> OutputRef:
    record = latest_completed(value.label, value.member_key)   # or pending, see open questions
    return OutputRef(record=record.id, output=value.output)
```

A record under a slot, a batch, a rule, or an accumulator is read the same way.
The reader cannot tell whether the value was forwarded or accumulated, and its record names the exact state it read.

### Accumulator spec: an operation

```python
class AccumulatorSpec(BaseModel):        # the operation, not the type
    name: str                            # 'sum'
    version: int
    params: dict[str, Plain] = {}        # e.g. {'dim': 'bank'} for concat
    accepts: TypeConstraint              # which value types the operation takes
    closed: bool                         # the value can be pushed again, and grouping does not matter
    commutative: bool                    # the order of pushes does not matter
```

What varies between workflows is mostly the type of the value, not what combining means.
The survey in sciline's design doc (section 1) found these ways of combining in the ESS packages and esslivedata: events by concatenation, dense data by sum, metadata by picking one value, `DataGroup` by key, dicts by union, and one custom function.
ess.reduce ships a standard set that covers them:

| Operation | Combines | closed | commutative |
|---|---|---|---|
| `sum` | dense arrays by addition, binned events by concatenating the bins | yes | yes |
| `same` | keeps the one value; refuses a push that differs | yes | yes |
| `union` | dicts; refuses a key pushed twice | yes | yes |
| `concat(dim)` | arrays along `dim`, in member order | yes | no |

`closed` is sciline's condition for "combining combined values" (design doc section 4).
Without it, only the policies `Flat` and `Held` apply.

The implementations are in ess.reduce, because telling dense data from events is scipp's business, and the framework knows nothing about scipp.
Each is a factory of a `sciline.Accumulator`, bound through the entry-point group `ess.apps.accumulators`, as workflow code is bound to a workflow spec.
The backend loads accumulator specs only, so it checks types without importing code.
Whether `sum` buffers, keeps a running result, or adds in place is the factory's choice and not part of the spec: it changes memory, not the result.
A package may declare an operation of its own, such as esslivedata's combination of banks, in the same way.

An accumulator holds one value.
Values that must accumulate together, such as a numerator and a denominator, are one value whose type is a row model, and an operation over a row applies to each field, as sciline's reduce over a `DataGroup` does by key.
Where fields need different operations, `Row` names one per field:

```python
class NormalizationParts(BaseModel):
    numerator: Array(...)
    denominator: Array(...)

'sum/v1'                                                  # both fields summed
Row(numerator='sum/v1', denominator='sum/v1', wavelength_bands='same/v1')
```

A field accumulated with `same` is also how an author makes members agree: a contribution that carries a value other members do not share is refused when it is pushed.

The author of esssans declares two workflow specs and no accumulator spec:

```python
SANS_CONTRIBUTE   # one run and the parameters it reads -> parts: NormalizationParts
SANS_FINALIZE     # parts: NormalizationParts, and the parameters finalize reads -> I(Q)
```

`SANS_FINALIZE` is a workflow spec, not a stage the caller cuts from the whole reduction.
Its params hold only what finalize reads, so its record names no value it did not use.

### Accumulator

```python
class Accumulator(BaseModel):            # a handle to the stored declaration of one version
    name: str                            # its label
    version: int                         # a reset is a new version
    spec: AccumulatorSpecId | Row        # the operation
    value: FieldType | None              # the type; taken from the first push when not given
    policy: Held | Flat | Tree
    order: str | None                    # field ordering the members; required unless commutative

class Held(BaseModel): snapshot_every: int | None = None     # one process holds the value
class Flat(BaseModel): pass                                   # one combine over every member
class Tree(BaseModel): fan_in: int = 32                       # combines over chunks, then over chunks of those

client.accumulator(name, spec, policy=Held(), value=None, order=None) -> Accumulator   # stores version 1
client.reset(acc) -> Accumulator         # stores the next version, with an empty push log
client.release(acc)                      # frees the held value; the push log and records remain
client.hold(acc)                         # holds it again, replaying the push log
```

No type variable enters the spec vocabulary.
The type of every push, the accumulator's value, and the parameter of every reader must be one type, which the backend checks as it checks an output against a parameter today, and the operation's `accepts` must allow it.

A **push** is one output of one record, under a **member key**.
A run submitted with `into=acc` computes exactly one output, and that output is pushed; `client.push(acc, ref, member_key=...)` pushes an output that exists already.
Each push is an entry in the accumulator's **push log** in the record store:

```python
class Push(BaseModel):
    accumulator: str                     # 'sum/v2'
    member_key: str
    part: OutputRef
    time: datetime
```

The push log is kept apart from the run records: where an output went, like its label, says nothing about what was computed.
A member key is pushed at most once per version; pushing it again is refused.
Removing or correcting a member means a new version.

A **state** is a run record of the framework's combine:

```python
# built-in workflow spec; the parts share one type, which is the type of the value
COMBINE(operation: AccumulatorSpecId | Row, parts: list[OutputRef]) -> value
```

A part is a pushed output or another state of the same accumulator, so every policy writes the same kind of record:

```text
Flat:  s = COMBINE([m1, ..., m1000])
Held:  s2 = COMBINE([s1, m4, m5])                 # whenever a state is referenced
Tree:  c0 = COMBINE([m1..m32]) ... c31;  s = COMBINE([c0, ..., c31])
```

The backend refuses a state whose leaves contain a member twice.
Finding the leaves is a walk over references, the same as provenance, and needs no graph.

### The driver

```python
def on_push(acc: Accumulator, member_key: str, part: OutputRef) -> None:
    check_type(part, acc)                           # binds acc.value on the first push
    append_to_push_log(acc, member_key, part)       # refuses a member key pushed before
    match acc.policy:
        case Held():
            holder(acc).push(part)                  # into the held sciline.Accumulator
        case Flat():
            pass                                    # nothing until a state is asked for
        case Tree(fan_in=n):
            submit_missing_chunks(acc, n)           # chunks full under the order field

def current_state(acc: Accumulator) -> RunRecord:   # called when Current(acc.name) resolves
    members = push_log(acc)
    match acc.policy:
        case Held():
            return holder(acc).record_state()       # records COMBINE([last state, new members])
        case Flat():
            return submit(COMBINE(parts=members))
        case Tree(fan_in=n):
            return submit(COMBINE(parts=chunks(members, n, acc.order)))   # the root; chunks pending
```

`chunks` is a pure function of the push log, ordered by `acc.order`, or by member key where the operation is commutative and no order is given.
A trigger loop that restarts finds the same chunks, and submits only the missing ones.
A late member in the middle of the order changes one chunk and the root.

`holder(acc)` is a session in local mode or a long-lived runner the backend owns in shared mode.
It holds the `sciline.Accumulator`, writes a state record when `Current` asks for one, and writes a copy of a state's value to disk only when a process elsewhere reads it, on publication, or at `snapshot_every`.
`client.hold(acc)` after a loss or a release replays the push log, starting from the last snapshot if the operation is `closed`.

## Examples

### S2, B1. Tune one parameter

```python
with client.hold(Stage(spec=IOFQ, params={'sample_run': run, ...},
                       blanks=('q',), outputs=('iofq',))) as tune:
    for n in (50, 100, 200):
        client.run(tune, {'q': QEdges(start=0.01, stop=0.3, num_bins=n)}, label='iofq')
```

`hold` returns when the session has loaded the data and computed everything that does not depend on `q`; a blank the outputs do not need fails there, not at the first call.
Every call is served by the held stage, and each record is that of a plain run.
The memory is freed when the block ends.

### D1. A batch from a stored template

```python
template = Template(name='temperature-scan', version=1, dataset_field='sample_run',
                    stage=Stage(spec=IOFQ, params={...}, blanks=('sample_run',), outputs=()))
group = apply(client, template, datasets)
client.submit_group(group)                          # throwaway processes; nothing held
```

In a session, members that should share what does not depend on the dataset go through one held stage:

```python
with client.hold(template.stage) as held:
    client.submit_group(group, through=held)
```

### C1. Beam centre feeds a sample reduction

```python
client.run(BEAM_CENTER, {'sample_run': run, ...}, label='beam-centre')     # may run several times
client.run(IOFQ, {'sample_run': run, 'beam_center': Current('beam-centre', 'center'), ...})
```

The record of `IOFQ` names the beam-centre record that was current when it was submitted.
The field is typed `Quantity | OutputRef` as today; `Current` is a stand-in for the reference.

### S5, S6. A sum in one request

```python
client.run(SANS, {'sample_runs': samples, 'background_runs': backgrounds, ...})
```

The binding contributes, accumulates, and finalizes inside one run, with the factory of `sum`, and holds nothing afterwards.
A request whose list extends a previous one reduces every run again; adding a run cheaply is B2.

Across records, S6 is two accumulators with `sum`, one for the sample runs and one for the background runs, and a finalize spec that reads both:

```python
client.run(SANS_SUBTRACT_FINALIZE, {'sample': Current('sample-sum'),
                                    'background': Current('background-sum'), ...})
```

### B2. Add a run to a sum, then remove one

```python
acc = client.accumulator('sum', spec='sum/v1')      # held; its type is NormalizationParts, from the first push
contribute = Stage(spec=SANS_CONTRIBUTE, params={...}, blanks=('run',), outputs=('parts',))
for r in (r611, r612):
    client.run(contribute, {'run': r}, label='parts', member_key=str(r), into=acc)
show(client.run(SANS_FINALIZE, {'parts': Current('sum'), 'q': q}))

client.run(contribute, {'run': r613}, label='parts', member_key='613', into=acc)   # one push
show(client.run(SANS_FINALIZE, {'parts': Current('sum'), 'q': q}))   # from the held value

acc = client.reset(acc)                             # version 2, empty
for key in ('611', '613'):
    client.push(acc, client.latest('parts', key).ref('parts'), member_key=key)   # no rerun
show(client.run(SANS_FINALIZE, {'parts': Current('sum'), 'q': q}))
client.release(acc)
```

Adding is one contribution and one push. Removing pushes the kept contributions again, without running them.
The contribute stage is not held: each run is reduced once, so there is nothing to reuse.

### Rotation scan over a thousand angles

```python
acc = client.accumulator('rotation', spec='sum/v1', policy=Tree(fan_in=32))
contribute = Stage(spec=SXD_CONTRIBUTE, params={...}, blanks=('run',), outputs=('counts',))
for run in angle_runs:                                        # 1000 independent throwaway runs
    client.submit(contribute, {'run': run}, into=acc, member_key=run.id)
volume = client.run(SXD_FINALIZE, {'counts': Current('rotation'), ...})
```

The user writes the same code as for B2. `Tree` submits 32 chunk combines as their members complete, and the root when `Current` resolves; `SXD_FINALIZE` waits for the root as a pending output.

### E1. A series grows, the reduction follows

```python
contribute = Rule(name='sans-contribute',
                  template=Template(name='sans-contribute',
                                    stage=Stage(spec=SANS_CONTRIBUTE, ..., outputs=('parts',))),
                  selector=Selector(match={'role': Like(pattern='sample')}),
                  into=Into(spec='sum/v1', key='sample', policy=Held()))
finalize = Rule(name='sans-finalize',
                template=Template(name='sans-finalize', stage=Stage(spec=SANS_FINALIZE, ...)),
                follows=Follows(label='sans-contribute/acc'))
```

`Into(..., key='sample')` creates one accumulator per value of the dataset field `sample` when its first member arrives, all under the label `sans-contribute/acc` with the value of `sample` as member key.
Each arrival pushes one contribution; each new state fires one finalize, which supersedes the previous one under the member key `sample`.
A repeated arrival is refused by "pushed at most once", which is the E1 check against duplicates.
This replaces `Series` for accumulators; a stitch keeps `Series`, see C4.

### A beamtime-long accumulator in memory

```python
acc = client.accumulator('background-map', spec='sum/v1', policy=Held(snapshot_every=50))
```

The same as B2 or E1.
A shared backend places the holder in a long-lived runner it owns; the value stays in memory, and a copy is written every 50 pushes and whenever a process elsewhere reads a state.

### Accumulating outputs of one spec into another

A user has workflow spec `A` with output `image` and workflow spec `B` with a parameter `background`, both of one array type.
Neither author planned the connection, and neither declared an accumulator spec.

```python
acc = client.accumulator('bg', spec='sum/v1')
image = Stage(spec=A, params={...}, blanks=('run',), outputs=('image',))
for run in runs:
    client.run(image, {'run': run}, into=acc, member_key=run.id)
client.run(B, {'background': Current('bg'), ...})
```

What the authors decide is only what is reachable: `A` must expose `image` as an output, and `B` must accept a reference for `background`.

### Groups that have meaning

Partial results that are scientific outputs, one per range of angles, are several accumulators, not a tree:

```python
ranges = [client.accumulator(f'range-{k}', spec='sum/v1') for k in range(4)]
total = client.accumulator('total', spec='sum/v1')              # the operation must be closed
for acc in ranges:
    ...                                                          # push the angles of each range
    client.push(total, Current(acc.name), member_key=acc.name)
```

### C4. A stitch is not an accumulation

The reflectometry stitch fits scale factors over all angles at once, so no operation that is closed or commutative describes it.
It stays a workflow spec over a list of references, recomputed over every member, as in [aggregation.md](../aggregation.md#combinations-that-are-not-accumulations), and a rule drives it with `Series`.

## What changes in the current design

- `Template` becomes `Stage` and `Template`; a slider's label is given with the run, since a stage has no name.
- `client.hold` and `client.release` replace the `vary` hint, on submissions, on `Group`, and over HTTP. The session keeps a table of held stages by handle instead of building a stage on the first call naming it and keeping the four most recently used.
- Accumulators are the first stored objects besides records: a declaration per version, and a push log, in the record store. The skeleton has no store for templates and rules yet either.
- The skeleton's `COMBINE`, which returns `finalize(combine(parts))`, becomes the built-in `COMBINE` and a finalize spec per workflow.
- A contribute spec outputs one value, a row where several values accumulate together. The adapter's `targets` map the fields of such an output to sciline keys, as `keys` already do for a list of rows.
- Accumulator specs are a second kind of spec: operations, a standard set in ess.reduce, with factories under the entry-point group `ess.apps.accumulators`. The adapter's `aggregations={field: MakeAggregation}` stays for the one-request sum; the accumulators of the package's aggregation should be these factories, so that the one-request sum and an accumulator agree.
- The one-request sum is computed flat, and a stage over a list holds no accumulation. The incremental part of the adapter goes: comparing a list with the previous one, finding the varied parameters the contributions read, rebuilding the aggregation when one changes, and the extra finalize inputs. Story B2's outcome changes to a held accumulator.
- The adapter checks a contribute spec and a finalize spec against the stages of the package's aggregation when it binds them.
- `Follows` names any label, including an accumulator's; `Into` is new on `Rule`.
- The data store may ask a holder the backend owns for a copy. Today the registry never learns about memory in any process.

## Open questions

1. **Does the standard set cover the packages?** The survey lists categories, not functions. Every function passed to `reduce` in the scipp/ess monorepo, and every accumulator in `ess.reduce.streaming`, should be checked against `sum`, `same`, `union`, and `concat`, and the rest listed as package operations.
2. **Three specs for one reduction.** A SANS author writes the whole spec for the one-request sum, a contribute spec, and a finalize spec, and the adapter checks all three against one aggregation. Can the whole spec be derived from the split instead, without a new, composite kind of record?
3. **`Current` on a pending state.** Should `Current` resolve only to a completed record, or to the pending root of a `Tree`, so that finalize waits? The rotation scan needs the latter.
4. **What a push is.** Only a run submitted with `into=` and `client.push`, or also every record completed under a label that the accumulator follows? The second makes rules and hand-made runs the same, and lets anyone who submits under that label feed it.
5. **Agreement between members.** A field accumulated with `same` refuses members that disagree on its value, so an author who wants members to agree on a parameter puts it in the contribution row. Is that enough, or should the backend also compare the parameters of the contributing records, which needs no graph either?
6. **The accumulators a rule makes.** `Into` creates one per series value when its first member arrives. When are they released: never during the beamtime, after a quiet period, or when the rule is paused?
7. **A forwarder that fires.** C1 resolves `Current` when a person submits. An interactive chain that reruns I(Q) whenever the beam centre changes needs a follow in a session, as `Follows` does for rules.
8. **A copy from a holder.** Is it acceptable that the backend asks its own long-lived runner to write a copy, given that the registry otherwise never tracks memory?
9. **A dataset whose bytes changed under a held stage.** The next call fails and says so, and the user holds the stage again; or the stage rebuilds and the record says it was not reused. Failing is explicit; rebuilding is friendlier while files are still written.
10. **A slow `hold`.** Computing a frontier can take minutes. Does `hold` block, or return a pending handle that the first call waits for?
11. **The memory budget.** A `hold` is refused when it does not fit, but the size of a frontier is known only once it is computed. Is the budget checked after building, releasing what was built, or estimated before?
