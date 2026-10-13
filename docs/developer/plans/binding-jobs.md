# What a binding does: mapping, strategy, protocol

**Status: design note, open. Nothing here is decided.**
It amends the binding part of the design: [README, What a binding provides](../README.md#what-a-binding-provides), [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), and [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md).
Questions for Simon are at the end, each with a `> Simon:` line.

## In short

A *binding* is the code behind a spec. Today one object does three jobs:

1. **Mapping**: which workflow input each field of the params model sets, and which workflow result each output is.
2. **Strategy**: how the backend serves a plain request, a stage, and an accumulator from what the workflow author provides: a pipeline or a function, which a stage can hold part of, or a *driver* in the sense of sciline, which takes rows one push at a time.
3. **Protocol**: what the backend passes to the binding and gets back. Today that is dicts by name, with data in place of references.

Only the mapping is always specific to a workflow.
The strategy is generic for most workflows (call a function, cut a pipeline, sum rows), and a package supplies its own driver where it is not; the protocol is the backend's interface, with one kind of binding for each of the two things an author provides.
This note separates the three, lists what the mix costs today, and proposes amendments.
Some settle a cost outright.
Three questions stay open: whether the protocol passes model instances, how data fields reach the workflow, and whether a mapping is a table or code.

## The example

The toy SANS reduction of [roles/toy_sans.py](../roles/toy_sans.py): a sciline pipeline of one sample run and one can run, a single-run spec, and a multi-run spec.

```python
pipeline = sciline.Pipeline([load, numerator, denominator, iofq])

class Run(BaseModel):          # a group of IOFQ, and a row of IOFQ_MULTI
    file: NexusFile            # a data field

class Reduction(BaseModel):
    bins: int = 2

class IofQParams(BaseModel):   # IOFQ
    sample: Run
    can: Run
    reduction: Reduction = Field(default_factory=Reduction)

class MultiIofQParams(BaseModel):   # IOFQ_MULTI
    sample_runs: list[Run] = Field(min_length=1)
    can_runs: list[Run] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)

class IofQOutputs(BaseModel):  # both specs
    iofq: Array(ArraySpec(dims=('Q',), unit='dimensionless'))
```

## Today: one object, three jobs

`PipelineBinding.stage` (`ess/spec/pipeline.py`), with the job each line does:

```python
def stage(self, fixed, blanks):
    blank_keys = self._keys.of(blanks)                 # 1: the keys that the blanks set
    pipeline = self._keys.set(self._pipeline, fixed)   # 1: the fixed values onto their keys
    cut = _Cut(pipeline, outputs, blank_keys)          # 2: sciline.Stage, cut at the blanks
    def call(**values):                                # 3: the blanks by name, as dicts
        results = cut(self._keys.by_key(values))       # 1: the blank values onto their keys
        return {name: results[key] for name, key in self._outputs.items()}  # 1 and 3: a dict
    return call
```

The backend's side of the protocol (`ess/dispatch/backend.py`, `_call`, `_typed`, `_read`, `_compute`, `_unfit`):

```python
values = _values(IofQParams.model_validate(params))   # validated, then dumped to dicts
call = binding.stage(read(fixed), blanks)             # once per stage, at its first request;
                                                      # read: data in place of references
outputs = dict(call(**read(blank_values)))            # once per request
# the names of `outputs` are checked against IofQOutputs; no IofQOutputs is ever made
```

Every kind of binding mixes the jobs differently:

| Binding | 1 Mapping | 2 Strategy | 3 Protocol |
|---|---|---|---|
| `PipelineBinding` (`ess.spec.pipeline`) | `params`: paths to keys; `outputs`: names to keys | plain request: one `sciline.Stage`; stage: cut at the keys of the blanks; held state: none, so the backend keeps the rows | dicts by name |
| `AccumulatingPipelineBinding` (`ess.spec.pipeline`) | the same, with `'sample_runs.file'` for each row's field, plus `accumulate`: per table, the keys summed over its rows | one cut per table up to the accumulated keys, sums, one cut from the sums to the outputs; the same for plain requests, stages, and held states | dicts by name |
| a function, wrapped by `FunctionBinding` (`ess.dispatch.bindings`) | the function's signature | each call computes everything; held state: the backend keeps the rows (`open_held_state`, `ess.dispatch.bindings`) | dicts by name |
| `combine(op)` (`ess.spec.combine`) | the fields of a row are the outputs | fold the rows with `op` | dicts by name |
| a held state written by hand | inside the author's code | written by the author | dicts by name |

## What the mix costs

1. **A function's author writes against the protocol, not against the models.**
   The function takes groups as dicts and returns a dict: `def iofq(sample: dict, can: dict, reduction: dict) -> dict`.
   The backend held a validated `IofQParams` and dumped it (`backend.py`, `_values` and `_typed`).
   The models the author wrote appear nowhere in the function.
2. **Nothing pairs a binding with its spec until a request runs.**
   `bind={IOFQ: binding}` is the only link, and `PipelineBinding` never sees the spec.
   A path that names no field fails at the first request (`no sciline key for the parameters`).
   An output the binding does not return fails when the request completes (`_unfit`).
3. **Two sciline classes for one strategy.**
   Their constructors differ in the mapping only: `'sample.file'` against `'sample_runs.file'`, and `accumulate`.
   Which strategy applies follows from whether `accumulate` is given.
4. **Strategies live in two packages.**
   The sciline strategies are in `ess.spec.pipeline`; the function and keep-the-rows strategies are in `ess.dispatch.bindings`.
   `ess.spec.testing.check_arrival_and_order` takes only a binding with `held_state`, so the keep-the-rows strategy is tested only in essdispatch.
5. **"Kept by the binding's author" covers framework code.**
   The README's Symmetries table says the binding's author keeps caching, arrival, and order.
   For a sciline package, the strategy keeps them (`sciline.Stage`, the sums).
   The author promises less: the workflow is pure, the accumulated results add up over runs, and the mappings of the single-run and multi-run spec describe the same workflow.
   So a package's `check_caching` mostly tests `ess.spec.pipeline`.
6. **The protocol fixes the form of data.**
   `DatasetSource.read` returns "the dataset's content, in the form workflows take": one form per dataset source.
   ADR 0001 says the workflow asks for the form it wants, but no part of a binding can say which.
   A sciline loader wants a path (`Filename[SampleRun]`); a workflow that reads another record's output wants the array.
7. **There is no place for conversions.**
   A group such as `QEdges` (start, stop, number of bins) must become a bin-edge array for sciline.
   ADR 0001 has the binding validate the dict again, `QEdges.model_validate(values['q_edges'])`.
   With `PipelineBinding`, that is a provider in the workflow package that takes a dict, so the workflow package knows the protocol's form.
8. **A table of paths to keys cannot say everything a mapping must say.**
   esslivedata's factories (see Prior art) also set values that users never see, such as `CorrectForGravity(False)`, and change the pipeline and its outputs depending on an enum field: one transmission mode inserts a provider and adds the output `transmission_fraction`.
   `PipelineBinding` has no place for either; such values go into the pipeline the workflow package hands over.
9. **Accumulation is one recipe, fixed in the binding.**
   `AccumulatingPipelineBinding` sums the accumulated keys of each row.
   That fits SANS, but not binning each run's events into a held grid (spectroscopy), keeping each run's contribution so that one can be removed (README open question 2), or a loop over banks within each run.
   [sciline ADR 0003](https://github.com/scipp/sciline/pull/245) leaves these to a package's own *driver*, and defers a generic object that wires stages and accumulators together, which is what `accumulate` is.
   A held state written by hand is the way out today, but it works on field names and dicts, not on the keys of the package's pipeline.

## The three jobs

### 1. Mapping

The mapping says, for one spec and one workflow, which input each field sets and which result each output is.
It is specific to the workflow.
The workflow author writes it, or the instrument's glue does ([ADR 0007](../adr/0007-packages-split-by-dependencies.md) places facility defaults and the choice of settings outside the workflow package).
For a sciline pipeline, with amendment B below:

```python
keys = {'sample.file': RunData[SampleRun], 'can.file': RunData[CanRun], 'reduction.bins': Bins}
single = PipelineBinding(IOFQ, pipeline, params=keys, outputs={'iofq': IofQ})
multi = DriverBinding(IOFQ_MULTI,
                      Sums(pipeline, accumulate=(Numerator[SampleRun], Denominator[SampleRun],
                                                 Numerator[CanRun], Denominator[CanRun])),
                      params=keys, outputs={'iofq': IofQ})
```

One mapping serves both specs, because the multi-run spec names each table as the single-run spec names the group, and the row model is the group's model:

```python
class IofQParams(BaseModel):          # IOFQ
    sample: Run
    can: Run
    reduction: Reduction = Field(default_factory=Reduction)

class MultiIofQParams(BaseModel):     # IOFQ_MULTI
    sample: list[Run] = Field(min_length=1)
    can: list[Run] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)
```

The binding knows from its spec whether `'sample.file'` is the field of a group or of each row of a table (amendment B).
How the rows are combined is the driver's job (section 2), here `Sums`.
For a function, the mapping is its signature, whose form depends on the protocol (amendment D).

Naming each table as the group is a convention that saves writing a second mapping; nothing depends on it.
A package whose specs name their parameters differently, such as `sample` and `sample_runs` as in [roles/toy_sans.py](../roles/toy_sans.py), writes two mappings, and each binding checks its own against its own spec.

Either way, the one-row symmetry is the author's promise, tested by `check_one_row`.
A shared mapping does not make it hold: the pipeline computes the single-run spec and the driver computes the multi-run spec, so it holds if the driver computes what the pipeline computes.

The author of the pipeline promises that it is pure: it modifies no input, and returns the same results for the same inputs.

### 2. Strategy

The workflow author provides one of two things, and the backend serves each client action from it:

| The author provides | Plain request | Kept in the backend for a client |
|---|---|---|
| a pipeline or a function | compute everything | a **stage**: the fixed part, computed once |
| a driver | make a held state, push every row, read the outputs | an **accumulator**: a held state that takes the client's pushes |

**A pipeline or a function** serves a spec without tables, or a spec whose tables the workflow takes whole, as lists.
A stage over a pipeline holds a `sciline.Stage` cut at the keys of the blanks, so each call computes only what depends on the blanks.
A stage over a function holds nothing, and each call computes everything.
If the spec has tables, an accumulator over a pipeline or a function still works: the framework wraps the binding so that the held state keeps the rows, and each read computes the plain request over every row so far.
This is a convenience for workflows that take lists: each read computes every row again.

**A driver** serves a spec with tables, one push at a time.
It is a driver in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245): the object that calls stages and pushes into accumulators, and that holds everything stateful.
`ess.spec` provides `Sums(pipeline, accumulate=...)`: one cut per table up to the accumulated keys, their sums over the rows, and one cut from the sums to the outputs, as SANS needs.
A package writes its own driver for the cases of cost 9, for example:

```python
DriverBinding(IOFQ_MULTI, Sums(pipeline, accumulate=(...)), ...)   # sums, as for SANS
DriverBinding(VOLUME, GridDriver(pipeline), ...)   # a package's own (hypothetical): bins each
                                                   # run's events into the held grid
```

A driver is not staged.
Tuning a parameter over fixed rows, such as the bins over a hundred runs, needs a pipeline that takes the table as a list; such a pipeline is staged as any other.

**A driver speaks the keys of its pipeline.**
Its protocol is what a client does with an accumulator, in keys instead of field names:

```python
held = driver.held_state({Bins: 2})                                  # the fixed values
held.push({RunData[SampleRun]: run_612, RunData[CanRun]: can_614})   # a row of each of two tables
held.push({RunData[SampleRun]: run_613})                             # a row of one table
held.outputs()                                                       # -> {IofQ: ...}
```

The keys of a push tell the driver which table each value belongs to.
Rows of two tables set different keys, as the single-run pipeline needs `RunData[SampleRun]` and `RunData[CanRun]` apart; the binding checks this when it is made.
A table that the accumulator's template fixes, such as one can run for every state, is pushed by the binding right after it makes the held state, so the driver's `held_state` gets only the values that are not tables.
A plain request over a spec with tables takes the same calls: the binding makes a held state, pushes every row, and reads the outputs.

This protocol is the part of `StreamProcessor` that [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md) says an accumulator needs.
The fixed values are its static part, the keys of a push are its dynamic keys, and `outputs` is its finalize.
Context keys, rolling windows, and `clear()` are not part of it, since each would break the arrival symmetry.

**So the pipeline and the driver are used directly**, in keys, with no spec, params model, or framework:

```python
wf = pipeline.copy()                           # the pipeline, directly
wf[Bins] = 2
wf[RunData[SampleRun]] = run_101
wf[RunData[CanRun]] = can_110
wf.compute(IofQ)

held = Sums(pipeline, accumulate=(...)).held_state({Bins: 2})   # the driver, directly
held.push({RunData[SampleRun]: run_101, RunData[CanRun]: can_110})
held.push({RunData[SampleRun]: run_102})
held.outputs()[IofQ]
```

The params models, the mapping, and the framework come on top, for use through apps and services:

| Level | Speaks | Used directly by |
|---|---|---|
| pipeline | keys | the workflow package and its tests; notebooks without the framework |
| driver | keys | the same, for sums over runs |
| binding: a mapping and a pipeline, function, or driver | the spec's fields and models | the backend |
| framework | requests and records | notebooks, apps, and services, through the client |

Who keeps each symmetry:

| Symmetry | Kept by | Given |
|---|---|---|
| caching | the stage, by a cut at the blanks | a pure pipeline |
| arrival, order | the driver | for `Sums`: accumulated keys that add up over runs, such as a numerator and a denominator, and that each depend on one table only (checked when it is made); for a package's own driver: its author's promise |
| arrival, order | the framework, for a held state that keeps the rows | nothing |
| one-row | the author of the two bindings | tested by `check_one_row` |

### 3. Protocol

The protocol is what the backend calls ([ess.spec.binding](../../../packages/essspec/src/ess/spec/binding.py)).
A binding is one of two kinds, by what the author provides, and has only the methods its kind serves:

```python
class StageBinding(Protocol):      # over a pipeline or a function
    def stage(self, fixed, blanks) -> Function: ...

class HeldStateBinding(Protocol):  # over a driver
    def held_state(self, fixed) -> HeldState: ...

class HeldState(Protocol):
    def push(self, rows) -> None: ...
    def outputs(self) -> Mapping: ...
```

What the backend calls for each client action:

| Client action | `StageBinding` | `HeldStateBinding` |
|---|---|---|
| plain request | `stage(values, ())()` | `held_state(fixed)`, a `push` per row, `outputs()` |
| call through a stage | `stage(fixed, blanks)` at the first call, then the returned callable at each call | refused when the stage is made |
| accumulator (spec with tables) | the rows kept by the framework; each read calls `stage(fixed, tables)` with the rows so far | `held_state(fixed)`, then `push` and `outputs()` |

The backend knows the kind of binding when a client makes a stage, so a stage over a `HeldStateBinding` is refused then, not at its first call.

It carries the backend's promises to the binding: every value with defaults filled in, data read, a push never while the outputs of the state before it are in use.
Today it passes groups and rows as dicts by field name, with data in place of each reference, and takes the outputs as a dict.
A `DriverBinding` translates these calls into the driver's, in keys, through its mapping.
The client never sees the protocol, and neither does the author of a sciline workflow or of a driver.
The author of a function sees it, through the function's signature.

## Amendments

They are independent of each other.
A to C settle costs now; D to G are open.

### A. Name the jobs in the docs

The README's "What a binding provides", ADR 0006, and [roles/workflow-authors.ipynb](../roles/workflow-authors.ipynb) explain a binding as a mapping and a strategy, called through the protocol.
The Symmetries table splits "Kept by" into the strategy and what it is given, as in the table above.
The README's section Drivers calls the client code that decides what to submit and push a *driver*, which clashes with sciline's driver.
The two are the halves of one loop: the client's code decides which rows arrive, and the package's driver adds them.
One of them needs another name (question 10).
Settles cost 5.

### B. Two kinds of binding, each of which takes its spec

A binding is a `StageBinding` or a `HeldStateBinding` (section 3), by what the author provides:

```python
single = PipelineBinding(IOFQ, pipeline, params=keys, outputs={'iofq': IofQ})           # StageBinding
multi = DriverBinding(IOFQ_MULTI, Sums(pipeline, accumulate=(...)), params=keys,
                      outputs={'iofq': IofQ})                                            # HeldStateBinding
fwd = function_binding(FORWARD, forward)                                                 # StageBinding
```

`DriverBinding` replaces `AccumulatingPipelineBinding`, and `accumulate` moves into `Sums`; `PipelineBinding` keeps its form and gets its spec.
The kind of strategy is named by the class and its argument, not implied by whether `accumulate` is given.
The two protocols are disjoint, where today `HeldStateBinding` extends the protocol with `stage`.
When a binding is made, it checks that each path names a parameter or a field of a group or row, and that each output name is an output of the spec.
A `DriverBinding` also checks that rows of different tables set different keys.
The driver checks its own structure; `Sums` checks that each accumulated key depends on the rows of one table.
A `DriverBinding` takes no pipeline next to its driver: the driver is built over the pipeline and owns it, and two arguments could disagree.

A stage over fixed rows, which `AccumulatingPipelineBinding` serves today (`test_a_stage_over_fixed_rows_loads_each_run_once`), has no counterpart: a driver is not staged (section 2).

A binding that knows its spec makes the `bind` mapping redundant:

```python
local(proposal='p1', datasets=datasets, bind=[single, multi, fwd])
```

and the entry point of [ADR 0007](../adr/0007-packages-split-by-dependencies.md) returns a list of bindings.
Settles costs 2, 3, and 9.

### C. All strategies in `ess.spec`

`FunctionBinding` and the keep-the-rows held state move from `ess.dispatch.bindings` to `ess.spec`, next to `PipelineBinding`, `DriverBinding`, and `Sums`.
`ess.spec.testing` then checks every strategy, and a package tests any binding without `ess.dispatch`.
sciline ADR 0003 defers a generic driver until packages repeat the same driver code; `Sums` could move to sciline then.
Settles cost 4.

### D. The protocol passes model instances (open, needs E)

```python
def iofq(params: IofQParams) -> IofQOutputs: ...      # a function binding

call = binding.stage({'sample': Run(...), 'can': Run(...)}, blanks=('reduction',))
call(IofQParams(sample=..., can=..., reduction=Reduction(bins=4)))   # -> IofQOutputs

held = binding.held_state({'reduction': Reduction(bins=2)})
held.push({'sample_runs': Run(...)})                  # the Run the client pushed
held.outputs()                                        # -> IofQOutputs
```

`stage` gets the fixed groups by name, and a blank is absent, so a strategy that reads a blank in the fixed part fails at once.
The callable gets the full plain request, which the backend has already validated.
So `stage` returns what a function binding is, `Callable[[IofQParams], IofQOutputs]`, and a function binding is a binding staged with nothing fixed.
`held_state` gets the fixed groups by name too: a state may be an invalid request until it is read, such as one with sample rows and no can row yet, so there is no valid `MultiIofQParams` to pass.

With `WorkflowSpec` generic in its outputs model as well as its params model, a type checker checks `function_binding(IOFQ, iofq)`.
The output names are checked where the author writes them, `IofQOutputs(iofq=...)`, not in the backend.
Settles cost 1 for function bindings.
A driver never sees field names or models (section 2), so D touches only the mappings and the function bindings.

Alternatives considered:

- **Defaults for every parameter that can be a blank**, so that `stage` gets `IofQParams(**fixed)`.
  Required parameters, such as `sample`, become optional: a request without them is no longer refused, and a form no longer marks them required.
  A blank holds a value that is never used, so a strategy that reads it computes a wrong result without an error.
  The params model's own validators run on these values.
- **Staging from the full instance of the first call.**
  `stage` gets a valid instance, but its blank fields hold the first call's values, which the fixed part must not use; `check_caching` catches a strategy that does.
  It does not extend to held states, for the reason above.
- **Dicts, as today.**

### E. How data fields reach the workflow (open)

A data field is declared as a reference (`NexusFile = Annotated[Ref, DataField(...)]`), and the workflow needs the data.
Today the backend replaces each reference by the data in the dicts it passes, which only works because dicts carry no types.
Outputs are the harder case: nearly every output is a data field.

Options:

- **E1. Dicts, data in place of references.** Today. Rules out D.
- **E2. A handle type.** `NexusFile` and `Array` are classes that hold a reference, and inside the backend also a way to the data: a path, or the value. A request and a call use the same model class. The mapping says which form the workflow wants (cost 6).
- **E3. Two forms of each model**, generic over the type of its data fields: `Run[Ref]` in requests, `Run[Path]` in calls. Every model with a data field gets a type parameter.
- **E4. `model_construct` with the data in fields typed as references.** No copy, but the static types are wrong, and dumping such an instance fails.
- **E5. Data apart from the params instance**, as esslivedata passes streams apart from its params. The workflow gets `IofQParams` without its data fields, and the data by field path. A group such as `Run` is then split in two for the workflow, and the mapping joins the halves.

What a solution must give:

- one model class per group, for requests and for calls;
- static types that hold where the author reads them;
- the form the workflow takes chosen by the mapping, not by the dataset source;
- no copy of a large array, in inputs or in outputs;
- references in records, so that history and provenance stay small.

### F. Conversions in the mapping (open)

A conversion turns a field's value into what the workflow takes, such as a `QEdges` group into a bin-edge array.
It could be part of the mapping:

```python
params={'reduction.q_edges': (QBins, edges_to_variable), ...}
```

or stay in the workflow, as a provider that takes the group.
In the mapping, the workflow package needs no provider that knows the protocol's form (cost 7); in the workflow, the mapping stays a table of names.
esslivedata puts the conversion on the group's model, `QEdges.get_edges()`, and its factory calls it; `ess.spec.conversions.edges_to_variable` does the same for `EdgesModel`.
With model instances in the protocol (D), a mapping could name such a method: `'reduction.q_edges': (QBins, QEdges.get_edges)`.

### G. A table or code (open)

A mapping can be a table, as in B, or code that configures the pipeline from the params, as esslivedata's factories are:

```python
def configure(pipeline: sciline.Pipeline, params: SansWorkflowParams) -> sciline.Pipeline:
    pipeline = pipeline.copy()
    pipeline[Bins] = params.reduction.bins
    pipeline[CorrectForGravity] = CorrectForGravity(False)   # a value users never see
    if params.transmission.mode is TransmissionMode.constant:
        pipeline[TransmissionFraction[SampleRun]] = sc.scalar(1.0)
    else:
        pipeline.insert(transmission_from_monitor)          # the graph depends on a field
    return pipeline
```

Code can say everything (cost 8).
A table tells the strategy which keys each parameter sets, and the sciline strategy needs exactly that: a stage cuts at the keys of the blanks, and an accumulator checks that each sum depends on the rows of one table.
From code alone, the strategy cannot know which keys a blank touches, so it could not cut a stage, or check that an accumulator's sums each depend on one table.

A middle way gives each parameter a function and the keys it may set or insert:

```python
params={'reduction.bins': Bins,                                          # a key, as today
        'transmission': (configure_transmission, {TransmissionFraction[SampleRun]})}
```

The strategy then cuts at the declared keys.
Values that users never see belong in the pipeline that the workflow package hands over, as today.
An output that exists only for some values of a field, such as `transmission_fraction`, is an optional output of the spec.

## Prior art: esslivedata

esslivedata solves the same problem for live data.
Each instrument is a package with `specs.py`, the params and outputs models, which the dashboard imports, and `factories.py`, the sciline glue, which only the backend loads.
In terms of the three jobs:

| Job | esslivedata |
|---|---|
| 1 mapping | code: a factory per spec sets each sciline key from the params by hand (`wf[QBins] = params.q_edges.get_edges()`), sets values users never see (`wf[CorrectForGravity] = CorrectForGravity(False)`), and changes the graph and the outputs by an enum field (`config/instruments/loki/factories.py`) |
| 2 strategy | chosen by the same factory: it returns a `StreamProcessorWorkflow`, given dynamic keys, accumulators, and target keys; other strategies are other implementations of its `Workflow` protocol |
| 3 protocol | `factory(source_name=..., params=SansWorkflowParams(...), aux_source_names=...)`, then `accumulate(data)` and `finalize() -> dict` on the workflow it returns |

What carries over:

- **The factory gets the params model instance.** The backend validates the wire dict into the model and passes it; `attach_factory` checks the factory's `params` type hint against the spec when it is registered. This is amendment D, in use.
- **It works there because data are not params.** A workflow's data arrive as streams, named by source; no field of the params model holds a reference. In essapps, runs are fields of the params model, which is why E is open.
- **The mapping is code** (cost 8, amendment G), and the factory mixes it with the choice of strategy.
- **Outputs are matched by name, unchecked.** The factory's target keys map output names to keys; nothing checks the names against the outputs model, and no instance of the outputs model is made. The same holds in essapps today (cost 2).

## Where the jobs live

| Job | Written by | Lives in | Tested by |
|---|---|---|---|
| 1 mapping | the workflow author, or the instrument's glue | the workflow package, or `essdispatchinstruments` (ADR 0007) | `check_one_row`, the checks of the strategy when the binding is made |
| 2 strategy | the framework: the stage cut, the function strategy, the keep-the-rows held state, `Sums`; a package: its own driver | `ess.spec`; the package | `ess.spec`'s tests, once; the package's own, with `check_arrival_and_order` |
| 3 protocol | the framework | declared in `ess.spec.binding`, called by `ess.dispatch.backend` | essdispatch's tests |

A mapping as a table is not tied to this framework's strategies.
`StreamProcessor`, through which esslivedata accumulates, takes the same description: dynamic keys (the fields of a row), accumulated keys, and target keys (the outputs).
One table could serve both frameworks; a mapping written as code, as esslivedata's are, serves only the strategy it builds.
sciline ADR 0003 makes `StreamProcessor` a driver too, over chunks of a stream within a context.
Without context keys, rolling windows, and `clear()`, it fits the driver protocol of section 2, with rows in place of chunks.

## What does not change

The client API, except that a stage over a spec bound to a driver is refused; records, the symmetries, and the backend's promises to a binding: when it calls which method, from how many threads, and that a push waits for the readers of the state before it.

## Questions

1. Are mapping, strategy, and protocol the right names? "Binding" would stay the name of the whole: a mapping and a strategy, called through the protocol.

> Simon:

2. B: should a binding take its spec, so that `bind` becomes a list?

> Simon:

3. B: two kinds of binding, `StageBinding` and `HeldStateBinding`, each with only the methods it serves; `accumulate` moved into a `Sums` driver; no stage over a driver?

> Simon:

4. C: the function and keep-the-rows strategies, `PipelineBinding`, `DriverBinding`, and `Sums` in `ess.spec`, with `Sums` moving to sciline once packages repeat it?

> Simon:

5. D: given a solution to E, should the protocol pass model instances?

> Simon:

6. E: which option to try first? I would try E2, the handle, on the toy SANS example: a `NexusFile` that gives a path, and an `Array` that gives the value.

> Simon:

7. F: conversions in the mapping, or in the workflow?

> Simon:

8. Who writes the mapping for an instrument: the workflow package, or the instrument's glue in `essdispatchinstruments`?

> Simon:

9. G: a table, code, or a function per parameter with the keys it declares? Do you know of a real reduction, other than esslivedata's transmission mode, whose graph depends on a field?

> Simon:

10. A: "driver" means the client code that submits and pushes in the README, and the package's object that adds rows in sciline. Keep "driver" for sciline's meaning, as one vocabulary across sciline, ess.reduce, and essapps, and rename the README's (for example to *client loop*)?

> Simon:

11. Mapping: recommend, as a convention, that the multi-run spec names each table as the single-run spec names the group, with the same model, so that one mapping serves both? Nothing depends on it. Going further, by deriving the single-run spec (`IOFQ_MULTI.single_run()`) or by letting the backend accept a group where a table is expected, as a table of one row, would be the first thing that depends on the one-row symmetry. Worth it, or a step too far?

> Simon:
