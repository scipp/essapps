# Proposal: stages, labels, and accumulators

**Status: proposal for review. Nothing here is implemented.**
It extends the design in [architecture.md](../architecture.md) with sciline's connectors and drivers (scipp/sciline#245, design doc sections 2 and 4).
The code below is pseudocode in the style of the skeleton's API; names are open to change.

## Summary

- `Template` splits in two: a **stage** is plain data, the counterpart of `sciline.Stage`; a **template** is a stored, versioned stage that batches and rules fill.
- A **label** is a name over a sequence of records. Its current record is its value.
- An **accumulator** is a label whose records are successive states of one accumulated value, the counterpart of `sciline.Accumulator`. Runs push into it; it keeps its value in memory when it can; its states are records.
- An **accumulator spec** names an operation, such as `sum`. A few standard ones ship with ess.reduce, and they serve many workflows. The accumulator binds the value type.
- A reader names the current record of a label with the stand-in `Current`, resolved at submission like a run number. References do not change.
- Combining and finalizing are separate. The workflow author declares a contribute spec and a finalize spec. The user pushes, the accumulator combines, the user finalizes.
- How an accumulator combines, in memory, flat, or as a tree over many processes, is its policy. The user does not see it.

The rule that keeps this from repeating the stage records that 48e2c17 reverted:

> A cut that crosses a record boundary is made by the workflow author, where the graph is known.
> A cut inside a process may be made by the caller, as a hint.

## The vocabulary

| sciline | essapps, inside one process (a cache) | essapps, between records (durable) |
|---|---|---|
| `Stage` | a held `sciline.Stage`, named by a `Stage` | a spec: a cut the author made |
| forwarder | an output one held stage passes to the next, from the session's memory (C5) | a label: its current record |
| `Accumulator` | a held `sciline.Accumulator`, named by an `Accumulator` | an `Accumulator`'s state records |
| driver | a workflow's `stage()`; the holder of an accumulator | a group; a rule; the policy of an accumulator |

The essapps names mirror the sciline names on purpose.
An essapps `Stage` or `Accumulator` is plain data that names what to compute or hold; the process that computes or holds it keeps the sciline object.

| Declared by a package | User creates | Held in memory |
|---|---|---|
| `WorkflowSpec`: a signature | a run request; its record is its instance | a `sciline.Stage` |
| `AccumulatorSpec`: an operation | an `Accumulator` of one value type; its states are records | a `sciline.Accumulator` |

The word "accumulation" keeps its current meaning: the accumulated value.

## The objects

### Stage and template

```python
class Stage(BaseModel):                  # plain data; the session holds a sciline.Stage for it
    spec: SpecId
    params: dict[str, Plain]             # the values set
    blanks: tuple[str, ...]              # the values each call fills
    outputs: tuple[str, ...]

class Template(BaseModel):               # stored and versioned; batches and rules fill it
    name: str
    version: int
    stage: Stage
    dataset_field: str | None            # the blank a dataset fills
```

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
class Accumulator(BaseModel):            # plain data; its states are the records under `name`
    name: str                            # its label
    version: int = 1                     # a reset is a new version
    spec: AccumulatorSpecId | Row        # the operation
    value: FieldType | None = None       # the type; taken from the first push when not given
    policy: Held | Flat | Tree = Held()
    order: str | None = None             # field ordering the members; required unless commutative

class Held(BaseModel): snapshot_every: int | None = None     # one process holds the value
class Flat(BaseModel): pass                                   # one combine over every member
class Tree(BaseModel): fan_in: int = 32                       # combines over chunks, then over chunks of those
```

No type variable enters the spec vocabulary.
The type of every push, the accumulator's value, and the parameter of every reader must be one type, which the backend checks as it checks an output against a parameter today, and the operation's `accepts` must allow it.

A **push** is one output of one record, under a **member key**.
A run submitted with `into=acc` computes exactly one output, and that output is pushed.
A member key is pushed at most once; pushing it again is refused.
Removing or correcting a member means a new version of the accumulator.

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
    refuse_if_pushed(acc, member_key)
    match acc.policy:
        case Held():
            holder(acc).push(part)                  # into the held sciline.Accumulator
        case Flat():
            pass                                    # nothing until a state is asked for
        case Tree(fan_in=n):
            submit_missing_chunks(acc, n)           # chunks full under the order field

def current_state(acc: Accumulator) -> RunRecord:   # called when Current(acc.name) resolves
    members = pushed(acc)                           # a query over records: no memory needed
    match acc.policy:
        case Held():
            return holder(acc).record_state()       # records COMBINE([last state, new members])
        case Flat():
            return submit(COMBINE(parts=members))
        case Tree(fan_in=n):
            return submit(COMBINE(parts=chunks(members, n, acc.order)))   # the root; chunks pending
```

`chunks` is a pure function of the pushed members, ordered by `acc.order`, or by member key where the operation is commutative and no order is given.
A trigger loop that restarts finds the same chunks, and submits only the missing ones.
A late member in the middle of the order changes one chunk and the root.

`holder(acc)` is a session in local mode or a long-lived runner the backend owns in shared mode.
It holds the `sciline.Accumulator`, writes a state record when `Current` asks for one, and writes a copy of a state's value to disk only when a process elsewhere reads it, on publication, or at `snapshot_every`.
If the holder is lost, a new one replays the pushes, starting from the last snapshot if the operation is `closed`.

## Examples

### S2, B1. Tune one parameter

```python
tune = Stage(spec=IOFQ, params={'sample_run': run, ...}, blanks=('q',), outputs=('iofq',))
for n in (50, 100, 200):
    client.run(tune, {'q': QEdges(start=0.01, stop=0.3, num_bins=n)}, label='iofq')
```

As today, except that the label is given with the run: a stage has no name.

### D1. A batch from a stored template

```python
template = Template(name='temperature-scan', version=1, dataset_field='sample_run',
                    stage=Stage(spec=IOFQ, params={...}, blanks=('sample_run',), outputs=()))
group = apply(client, template, datasets)          # as today
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

Unchanged: the binding contributes, accumulates, and finalizes inside one run, with the factory of `sum`.
It is `Flat` in one process, written as one request.
Whether to keep it beside accumulators is an open question below; the stories S5, S6, D1, and E1 use it.

Across records, S6 is two accumulators with `sum`, one for the sample runs and one for the background runs, and a finalize spec that reads both:

```python
client.run(SANS_SUBTRACT_FINALIZE, {'sample': Current('sample-sum'),
                                    'background': Current('background-sum'), ...})
```

### B2. Add a run to a sum, then remove one

```python
acc = Accumulator(name='sum', spec='sum/v1')        # its type is NormalizationParts, from the first push
contribute = Stage(spec=SANS_CONTRIBUTE, params={...}, blanks=('run',), outputs=('parts',))
for r in (r611, r612):
    client.run(contribute, {'run': r}, label='parts', member_key=str(r), into=acc)
show(client.run(SANS_FINALIZE, {'parts': Current('sum'), 'q': q}))

client.run(contribute, {'run': r613}, label='parts', member_key='613', into=acc)   # one push
show(client.run(SANS_FINALIZE, {'parts': Current('sum'), 'q': q}))   # from the held value

acc = acc.reset()                                   # version 2, empty
for key in ('611', '613'):
    client.push(acc, client.latest('parts', key).ref('parts'), member_key=key)   # no rerun
show(client.run(SANS_FINALIZE, {'parts': Current('sum'), 'q': q}))
```

Adding is one contribution and one push. Removing pushes the kept contributions again, without running them.

### Rotation scan over a thousand angles

```python
acc = Accumulator(name='rotation', spec='sum/v1', policy=Tree(fan_in=32))
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

`Into(..., key='sample')` makes one accumulator per value of the dataset field `sample`, all under the label `sans-contribute/acc` with the value of `sample` as member key.
Each arrival pushes one contribution; each new state fires one finalize, which supersedes the previous one under the member key `sample`.
A repeated arrival is refused by "pushed at most once", which is the E1 check against duplicates.
This replaces `Series` for accumulators; a stitch keeps `Series`, see C4.

### A beamtime-long accumulator in memory

```python
acc = Accumulator(name='background-map', spec='sum/v1', policy=Held(snapshot_every=50))
```

The same as B2 or E1.
A shared backend places the holder in a long-lived runner it owns; the value stays in memory, and a copy is written every 50 pushes and whenever a process elsewhere reads a state.

### Accumulating outputs of one spec into another

A user has workflow spec `A` with output `image` and workflow spec `B` with a parameter `background`, both of one array type.
Neither author planned the connection, and neither declared an accumulator spec.

```python
acc = Accumulator(name='bg', spec='sum/v1')
image = Stage(spec=A, params={...}, blanks=('run',), outputs=('image',))
for run in runs:
    client.run(image, {'run': run}, into=acc, member_key=run.id)
client.run(B, {'background': Current('bg'), ...})
```

What the authors decide is only what is reachable: `A` must expose `image` as an output, and `B` must accept a reference for `background`.

### Groups that have meaning

Partial results that are scientific outputs, one per range of angles, are several accumulators, not a tree:

```python
ranges = [Accumulator(name=f'range-{k}', spec='sum/v1') for k in range(4)]
total = Accumulator(name='total', spec='sum/v1')                # the operation must be closed
for acc in ranges:
    ...                                                          # push the angles of each range
    client.push(total, Current(acc.name), member_key=acc.name)
```

### C4. A stitch is not an accumulation

The reflectometry stitch fits scale factors over all angles at once, so no operation that is closed or commutative describes it.
It stays a workflow spec over a list of references, recomputed over every member, as in [aggregation.md](../aggregation.md#combinations-that-are-not-accumulations), and a rule drives it with `Series`.

## What changes in the current design

- `Template` becomes `Stage` and `Template`; a slider's label is given with the run, since a stage has no name.
- The skeleton's `COMBINE`, which returns `finalize(combine(parts))`, becomes the built-in `COMBINE` and a finalize spec per workflow.
- A contribute spec outputs one value, a row where several values accumulate together. The adapter's `targets` map the fields of such an output to sciline keys, as `keys` already do for a list of rows.
- Accumulator specs are a second kind of spec: operations, a standard set in ess.reduce, with factories under the entry-point group `ess.apps.accumulators`. The adapter's `aggregations={field: MakeAggregation}` stays for the one-request sum; the accumulators of the package's aggregation should be these factories, so that the one-request sum and an accumulator agree.
- `Follows` names any label, including an accumulator's; `Into` is new on `Rule`.
- The data store may ask a holder the backend owns for a copy. Today the registry never learns about memory in any process.

## Open questions

1. **Does the standard set cover the packages?** The survey lists categories, not functions. Every function passed to `reduce` in the scipp/ess monorepo, and every accumulator in `ess.reduce.streaming`, should be checked against `sum`, `same`, `union`, and `concat`, and the rest listed as package operations.
2. **Keep the one-request sum?** It is the common case and four stories use it, but it is a second way to write a flat accumulation.
3. **`Current` on a pending state.** Should `Current` resolve only to a completed record, or to the pending root of a `Tree`, so that finalize waits? The rotation scan needs the latter.
4. **What a push is.** Only a run submitted with `into=`, or also every record completed under a label that the accumulator follows? The second makes rules and hand-made runs the same, and lets anyone who submits under that label feed it.
5. **Agreement between members.** A field accumulated with `same` refuses members that disagree on its value, so an author who wants members to agree on a parameter puts it in the contribution row. Is that enough, or should the backend also compare the parameters of the contributing records, which needs no graph either?
6. **Automatic reduction.** Is an accumulator a stored, versioned object beside rules, or only `Into` on a rule?
7. **A forwarder that fires.** C1 resolves `Current` when a person submits. An interactive chain that reruns I(Q) whenever the beam centre changes needs a follow in a session, as `Follows` does for rules.
8. **A copy from a holder.** Is it acceptable that the backend asks its own long-lived runner to write a copy, given that the registry otherwise never tracks memory?
