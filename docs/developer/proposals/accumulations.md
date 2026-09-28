# Proposal: stages, labels, and accumulations

**Status: proposal for review. Nothing here is implemented.**
It extends the design in [architecture.md](../architecture.md) with sciline's connectors and drivers (scipp/sciline#245, design doc sections 2 and 4).
The code below is pseudocode in the style of the skeleton's API; names are open to change.

## Summary

- `Template` splits in two: a **stage** is plain data, the counterpart of `sciline.Stage`; a **template** is a stored, versioned stage that batches and rules fill.
- A **label** is a name over a sequence of records. Its current record is its value.
- An **accumulation** is a label whose records are successive states of a set of accumulators. Runs push into it; it keeps its value in memory when it can; its states are records.
- A reader names the current record of a label with the stand-in `Current`, resolved at submission like a run number. References do not change.
- Combining and finalizing are separate. The workflow author declares a contribute spec, accumulators, and a finalize spec. The user pushes, the accumulation combines, the user finalizes.
- How an accumulation combines, in memory, flat, or as a tree over many processes, is its policy. The user does not see it.

The rule that keeps this from repeating the stage records that 48e2c17 reverted:

> A cut that crosses a record boundary is made by the workflow author, where the graph is known.
> A cut inside a process may be made by the caller, as a hint.

## The vocabulary

| sciline | essapps, inside one process (a cache) | essapps, between records (durable) |
|---|---|---|
| `Stage` | a held `sciline.Stage`, named by a `Stage` | a spec: a cut the author made |
| forwarder | an output one held stage passes to the next, from the session's memory (C5) | a label: its current record |
| `Accumulator` | held by the accumulation's holder | the state records of an accumulation |
| driver | a workflow's `stage()`; the holder of an accumulation | a group; a rule; the policy of an accumulation |

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
    output: str
    member_key: str | None = None

# at submission, the backend replaces every Current by a reference:
def resolve(value: Current) -> OutputRef:
    record = latest_completed(value.label, value.member_key)   # or pending, see open questions
    return OutputRef(record=record.id, output=value.output)
```

A record under a slot, a batch, a rule, or an accumulation is read the same way.
The reader cannot tell whether the value was forwarded or accumulated, and its record names the exact state it read.

### Accumulators, declared by the author

```python
class AccumulatorDecl(BaseModel):        # in the spec package, beside the specs
    name: str                            # 'sans.numerator'
    value: FieldType                     # the type pushed and the type of the value: they are one type
    factory: EntryPoint                  # builds a sciline.Accumulator
    closed: bool                         # value can be pushed again, grouping does not matter
    commutative: bool                    # order of pushes does not matter
```

`closed` is sciline's condition for "combining combined values" (design doc section 4).
Without it, only the policies `Flat` and `Held` apply.

The author of esssans declares:

```python
SANS_CONTRIBUTE   # spec: one run and the parameters it reads -> numerator, denominator
SANS_NUMERATOR = AccumulatorDecl(name='sans.numerator', value=..., factory=..., closed=True, commutative=True)
SANS_DENOMINATOR = AccumulatorDecl(...)
SANS_FINALIZE     # spec: numerator, denominator, and the parameters finalize reads -> I(Q)
```

`SANS_FINALIZE` is a spec, not a stage the caller cuts from the whole reduction.
Its params hold only what finalize reads, so its record names no value it did not use.

### Accumulation

```python
class Accumulation(BaseModel):           # plain data; its states are the records under `name`
    name: str                            # its label
    version: int = 1                     # a reset is a new version
    accumulators: dict[str, str]         # slot -> AccumulatorDecl name
    policy: Held | Flat | Tree = Held()
    order: str | None = None             # field ordering the members; required unless commutative

class Held(BaseModel): snapshot_every: int | None = None     # one process holds the value
class Flat(BaseModel): pass                                   # one combine over every member
class Tree(BaseModel): fan_in: int = 32                       # combines over chunks, then over chunks of those
```

A **push** is one record whose outputs fill the slots by name, under a **member key**.
A member key is pushed at most once; pushing it again is refused.
Removing or correcting a member means a new version of the accumulation.

A **state** is a run record of the framework's combine:

```python
# built-in spec, typed by the accumulators' declared value types
COMBINE(accumulators: dict[slot, decl], parts: list[dict[slot, OutputRef]]) -> dict[slot, value]
```

A part is a pushed record or another state of the same accumulation, so every policy writes the same kind of record:

```text
Flat:  s = COMBINE([m1, ..., m1000])
Held:  s2 = COMBINE([s1, m4, m5])                 # whenever a state is referenced
Tree:  c0 = COMBINE([m1..m32]) ... c31;  s = COMBINE([c0, ..., c31])
```

The backend refuses a state whose leaves contain a member twice.
Finding the leaves is a walk over references, the same as provenance, and needs no graph.

### The driver

```python
def on_push(acc: Accumulation, member_key: str, record: RunRecord) -> None:
    check_types(record, acc)                        # outputs match the slots' declared types
    refuse_if_pushed(acc, member_key)
    match acc.policy:
        case Held():
            holder(acc).push(record)                # in memory; copy fetched as needed
        case Flat():
            pass                                    # nothing until a state is asked for
        case Tree(fan_in=n):
            submit_missing_chunks(acc, n)           # chunks full under the order field

def current_state(acc: Accumulation) -> RunRecord:  # called when Current(acc.name, ...) resolves
    members = pushed(acc)                           # a query over records: no memory needed
    match acc.policy:
        case Held():
            return holder(acc).record_state()       # records COMBINE([last state, new members])
        case Flat():
            return submit(COMBINE(parts=members))
        case Tree(fan_in=n):
            return submit(COMBINE(parts=chunks(members, n, acc.order)))   # the root; chunks pending
```

`chunks` is a pure function of the pushed members, ordered by `acc.order`.
A trigger loop that restarts finds the same chunks, and submits only the missing ones.
A late member in the middle of the order changes one chunk and the root.

`holder(acc)` is a session in local mode or a long-lived runner the backend owns in shared mode.
It holds the accumulators, writes a state record when `Current` asks for one, and writes a copy of a state's value to disk only when a process elsewhere reads it, on publication, or at `snapshot_every`.
If the holder is lost, a new one replays the pushes, starting from the last snapshot if the accumulators are `closed`.

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

Unchanged: the binding contributes, accumulates, and finalizes inside one run.
It is `Flat` in one process, written as one request.
Whether to keep it beside accumulations is an open question below; the stories S5, S6, D1, and E1 use it.

### B2. Add a run to a sum, then remove one

```python
acc = Accumulation(name='sum', accumulators={'numerator': 'sans.numerator',
                                             'denominator': 'sans.denominator'})
contribute = Stage(spec=SANS_CONTRIBUTE, params={...}, blanks=('run',), outputs=())
for r in (r611, r612):
    client.run(contribute, {'run': r}, label='parts', member_key=str(r), into=acc)
show(client.run(SANS_FINALIZE, {'numerator': Current('sum', 'numerator'),
                                'denominator': Current('sum', 'denominator'), 'q': q}))

client.run(contribute, {'run': r613}, label='parts', member_key='613', into=acc)   # one push
show(client.run(SANS_FINALIZE, {...}))              # from the held value; no run is read again

acc = acc.reset()                                   # version 2, empty
for key in ('611', '613'):
    client.push(acc, client.latest('parts', key), member_key=key)   # the contributions exist already
show(client.run(SANS_FINALIZE, {...}))
```

Adding is one contribution and one push. Removing pushes the kept contributions again, without running them.

### Rotation scan over a thousand angles

```python
acc = Accumulation(name='rotation', accumulators={...}, policy=Tree(fan_in=32), order='angle')
contribute = Stage(spec=SXD_CONTRIBUTE, params={...}, blanks=('run',), outputs=())
for run in angle_runs:                                        # 1000 independent throwaway runs
    client.submit(contribute, {'run': run}, into=acc, member_key=run.id)
volume = client.run(SXD_FINALIZE, {'counts': Current('rotation', 'counts'), ...})
```

The user writes the same code as for B2. `Tree` submits 32 chunk combines as their members complete, and the root when `Current` resolves; `SXD_FINALIZE` waits for the root as a pending output.

### E1. A series grows, the reduction follows

```python
contribute = Rule(name='sans-contribute',
                  template=Template(name='sans-contribute', stage=Stage(spec=SANS_CONTRIBUTE, ...)),
                  selector=Selector(match={'role': Like(pattern='sample')}),
                  into=Into(accumulators={...}, key='sample', policy=Held()))
finalize = Rule(name='sans-finalize',
                template=Template(name='sans-finalize', stage=Stage(spec=SANS_FINALIZE, ...)),
                follows=Follows(label='sans-contribute/acc'))
```

`Into(..., key='sample')` makes one accumulation per value of the dataset field `sample`, all under the label `sans-contribute/acc` with the value of `sample` as member key.
Each arrival pushes one contribution; each new state fires one finalize, which supersedes the previous one under the member key `sample`.
A repeated arrival is refused by "pushed at most once", which is the E1 check against duplicates.
This replaces `Series` for accumulations; a stitch keeps `Series`, see C4.

### A beamtime-long accumulation in memory

```python
acc = Accumulation(name='background-map', accumulators={...},
                   policy=Held(snapshot_every=50))
```

The same as B2 or E1.
A shared backend places the holder in a long-lived runner it owns; the value stays in memory, and a copy is written every 50 pushes and whenever a process elsewhere reads a state.

### Accumulating outputs of one spec into another

A user has spec `A` with output `image` and spec `B` with a parameter `background`, both of the type an installed accumulator declares.
Neither author planned the connection.

```python
acc = Accumulation(name='bg', accumulators={'image': 'ess.image_sum'})
for run in runs:
    client.run(A, {'run': run}, into=acc, member_key=run.id)    # the output 'image' fills the slot 'image'
client.run(B, {'background': Current('bg', 'image'), ...})
```

What the authors decide is only what is reachable: `A` must expose `image` as an output, and `B` must accept a reference for `background`.

### Groups that have meaning

Partial results that are scientific outputs, one per range of angles, are several accumulations, not a tree:

```python
ranges = {f'range-{k}': Accumulation(name=f'range-{k}', accumulators=...) for k in range(4)}
total = Accumulation(name='total', accumulators=...)             # the accumulators must be closed
for name, acc in ranges.items():
    ...                                                           # push the angles of each range
    client.push(total, Current(name, 'counts'), member_key=name)
```

### C4. A stitch is not an accumulation

The reflectometry stitch fits scale factors over all angles at once, so no accumulator is closed or commutative for it.
It stays a spec over a list of references, recomputed over every member, as in [aggregation.md](../aggregation.md#combinations-that-are-not-accumulations), and a rule drives it with `Series`.

## What changes in the current design

- `Template` becomes `Stage` and `Template`; a slider's label is given with the run, since a stage has no name.
- The skeleton's `COMBINE`, which returns `finalize(combine(parts))`, becomes the built-in `COMBINE` and a finalize spec per workflow.
- The adapter's `aggregations={field: MakeAggregation}` stays for the one-request sum; the accumulators it uses become `AccumulatorDecl`s, so the one-request sum and an accumulation share them.
- `Follows` names any label, including an accumulation's; `Into` is new on `Rule`.
- The data store may ask a holder the backend owns for a copy. Today the registry never learns about memory in any process.

## Open questions

1. **Name.** "Accumulation" avoids a near-homonym of sciline's `Aggregation`, which the author's declarations correspond to. Better names are welcome.
2. **Keep the one-request sum?** It is the common case and four stories use it, but it is a second way to write a flat accumulation.
3. **`Current` on a pending state.** Should `Current` resolve only to a completed record, or to the pending root of a `Tree`, so that finalize waits? The rotation scan needs the latter.
4. **What a push is.** Only a run submitted with `into=`, or also every record completed under a label that the accumulation follows? The second makes rules and hand-made runs the same, and lets anyone who submits under that label feed it.
5. **Agreement between members.** Across records, contributions made with different parameter values would be combined without complaint, and each record would still be honest. A check needs no graph: members of one contribute spec agree on every parameter except their member fields. Wanted?
6. **Automatic reduction.** Is an accumulation a stored, versioned object beside rules, or only `Into` on a rule?
7. **A forwarder that fires.** C1 resolves `Current` when a person submits. An interactive chain that reruns I(Q) whenever the beam centre changes needs a follow in a session, as `Follows` does for rules.
8. **A copy from a holder.** Is it acceptable that the backend asks its own long-lived runner to write a copy, given that the registry otherwise never tracks memory?
