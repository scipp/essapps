# What a binding does: mapping, strategy, protocol

**Status: design note.**
The strategy and the protocol (sections 2 and 3) are agreed; the amendments stay open until the questions at the end are answered, each on its `> Simon:` line.
It amends the binding part of the design: [README, What a binding provides](../README.md#what-a-binding-provides), [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), and [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md).

## In short

A *binding* is the code behind a spec. It does three jobs:

1. **Mapping**: which workflow input each field of the params model sets, and which workflow result each output is.
2. **Strategy**: what the workflow author provides, and how the framework serves each client action from it.
3. **Protocol**: what the backend calls on a binding, and what it passes and gets back.

The workflow author provides one of three things, and each serves some of the client's actions:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline (`PipelineBinding`) | compute everything | a handle to a `sciline.Stage` cut at the blanks | the framework keeps the rows |
| a driver (`DriverBinding`) | make a driver, push every row, read the outputs | refused | a driver that takes the client's pushes |
| a function (`function_binding`) | call it | refused | the framework keeps the rows |

A *driver* is a stateful object with `push(row)` and `outputs()`, in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245): the code that calls `sciline.Stage`s and pushes into accumulators.
The workflow package writes it; `ess.spec` provides none.
A stage exists only over a sciline pipeline, since only there does it hold anything.
"The framework keeps the rows" is a convenience for workflows that take a table whole, as a list: each read computes every row again.

Only the mapping is always specific to a workflow.
This note separates the three jobs, lists what mixing them costs today, and proposes amendments.
A to C settle costs outright; D to G stay open: whether the protocol passes model instances, how data fields reach the workflow, where conversions live, and whether a mapping is a table or code.

## Terms

Defined in the [README](../README.md); repeated here for reading this note alone.

| Term | Meaning |
|---|---|
| group, table, row | A field of a params model is a value, a *group* (a nested model, such as `sample: Run`), or a *table* (a list of a model, such as `sample: list[Run]`); each entry of a table is a *row*. |
| template, fixed, blank | A *template* is a request with some parameters left out, the *blanks*; the others are *fixed*. |
| plain request | A request with every value given. |
| stage | A template the backend keeps for a client, called with values for its blanks. |
| accumulator | A template whose blanks are tables, kept by the backend for a client; each *push* gives it at most one row per table. *State n* is the accumulator after n pushes. |
| symmetries | Changes a caller may make without changing the outputs: *caching* (a call through a stage against the plain request), *arrival* (rows pushed one at a time against all at once), *order* (the order of pushes into different tables), *one-row* (the multi-run spec with one row per table against the single-run spec). |

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
    cut = _Cut(pipeline, outputs, blank_keys)          # 2: a sciline.Stage cut at the blanks
    def call(**values):                                # 3: the blanks by name, as dicts
        results = cut(self._keys.by_key(values))       # 1: the blank values onto their keys
        return {name: results[key] for name, key in self._outputs.items()}  # 1 and 3: a dict
    return call
```

The backend's side of the protocol (`ess/dispatch/backend.py`):

```python
values = _values(IofQParams.model_validate(params))   # validated, then dumped to dicts
call = binding.stage(read(fixed), blanks)             # once per stage, at its first request;
                                                      # read: data in place of references
outputs = dict(call(**read(blank_values)))            # once per request
# the names of `outputs` are checked against IofQOutputs; no IofQOutputs is ever made
```

Every kind of binding mixes the jobs differently.
Today's protocol has `Binding.stage` on every binding, and `HeldStateBinding` adds `held_state(fixed)`, which returns a *held state* with `push` and `outputs`:

| Binding | 1 Mapping | 2 Strategy | 3 Protocol |
|---|---|---|---|
| `PipelineBinding` (`ess.spec.pipeline`) | `params`: paths to keys; `outputs`: names to keys | plain request: one `sciline.Stage`; stage: a `sciline.Stage` cut at the keys of the blanks; no `held_state`, so the backend keeps the rows | dicts by name |
| `AccumulatingPipelineBinding` (`ess.spec.pipeline`) | the same, with `'sample_runs.file'` for each row's field, plus `accumulate`: per table, the keys summed over its rows | one cut per table up to the accumulated keys, sums, one cut from the sums to the outputs; the same for plain requests, stages, and held states | dicts by name |
| a function, wrapped by `FunctionBinding` (`ess.dispatch.bindings`) | the function's signature | each call computes everything; the backend keeps the rows (`open_held_state`, `ess.dispatch.bindings`) | dicts by name |
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
   `ess.spec.testing.check_arrival_and_order` takes only a `HeldStateBinding`, so the keep-the-rows strategy is tested only in essdispatch.
5. **"Kept by the binding's author" covers framework code.**
   The README's Symmetries table says the binding's author keeps caching, arrival, and order.
   For a sciline package, the strategy keeps them (`sciline.Stage`, the sums).
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
   [sciline ADR 0003](https://github.com/scipp/sciline/pull/245) leaves these to a package's own driver; `accumulate` is the generic driver that the ADR defers.
   A held state written by hand is the way out today, but it works on field names and dicts, not on the keys of the package's pipeline.

## The three jobs

### 1. Mapping

The mapping says, for one spec and one workflow, which input each field sets and which result each output is.
It is specific to the workflow.
The workflow author writes it, or the instrument's glue does ([ADR 0007](../adr/0007-packages-split-by-dependencies.md) places facility defaults and the choice of settings outside the workflow package).

If the multi-run spec names each table as the single-run spec names the group, with the same model, one mapping serves both specs (question 11).
With `IOFQ_MULTI` renamed so:

```python
class MultiIofQParams(BaseModel):     # IOFQ_MULTI, tables named as the groups of IOFQ
    sample: list[Run] = Field(min_length=1)
    can: list[Run] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)

keys = {'sample.file': RunData[SampleRun], 'can.file': RunData[CanRun], 'reduction.bins': Bins}
single = PipelineBinding(IOFQ, pipeline, params=keys, outputs={'iofq': IofQ})
multi = DriverBinding(IOFQ_MULTI, make_driver, params=keys, outputs={'iofq': IofQ})
```

The binding knows from its spec whether `'sample.file'` is the field of a group or of each row of a table (amendment B).
How the rows are combined is the driver's job (section 2).
For a function, the mapping is its signature, whose form depends on the protocol (amendment D).

The shared mapping is a convention that saves writing a second one; nothing depends on it.
A package whose specs name their parameters differently, such as `sample` and `sample_runs` in [roles/toy_sans.py](../roles/toy_sans.py), writes two mappings, and each binding checks its own against its own spec.
Either way, the one-row symmetry is the author's promise: the pipeline computes the single-run spec and the driver computes the multi-run spec, so it holds if the driver computes what the pipeline computes.

The author of the pipeline promises that it is pure: it modifies no input, and returns the same results for the same inputs.

### 2. Strategy

The author provides a sciline pipeline, a driver, or a function; the table in In short says which client actions each serves.

**A sciline pipeline** serves a spec without tables, or a spec whose tables the pipeline takes whole, as lists.
A plain request computes the outputs from the pipeline with every value set.
A stage is a handle to a `sciline.Stage` cut at the keys of the blanks: the part that does not depend on the blanks is computed once, and each call computes the rest.
Tuning a parameter over fixed rows, such as the bins over a hundred runs, needs a pipeline that takes the table as a list; such a pipeline is staged as any other.

**A function** serves the same specs as a pipeline, for drafts in notebooks.
It has no stage, since a stage over a function would hold nothing.

**A driver** serves a spec with tables, one row at a time.
It is a driver in the sense of sciline ADR 0003, and holds everything stateful.
The binding makes one from the fixed values, in the keys the mapping names, and pushes one row of one table per call:

```python
driver = make_driver({Bins: 2})                  # the fixed values; no rows yet
driver.push({RunData[SampleRun]: run_101})       # a row of the sample table
driver.push({RunData[CanRun]: can_110})          # a row of the can table
driver.push({RunData[SampleRun]: run_102})
driver.outputs()                                 # -> {IofQ: ...}
```

The keys of a row tell the driver which table it belongs to: rows of different tables set different keys, which the binding checks when it is made.
A row with several fields, such as a run and its own transmission run, is one push with several keys.
A client push that names several tables becomes one driver push per table; the order symmetry makes that split invisible.
A plain request and the tables fixed by an accumulator's template take the same calls: the binding makes a driver and pushes every row it is given.
A changed fixed value means a new driver.

The workflow package writes its driver.
For the toy SANS reduction, it sums each run's numerator and denominator per run type with `sciline.Reduced`, and divides once:

```python
class IofQDriver:
    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[type, Any]) -> None:
        wf = pipeline.copy()
        for key, value in fixed.items():
            wf[key] = value
        self._contribute = {
            RunData[run]: sciline.Stage(wf, inputs=(RunData[run],),
                                        outputs=(Numerator[run], Denominator[run]))
            for run in (SampleRun, CanRun)
        }
        sums = [key[run] for key in (Numerator, Denominator) for run in (SampleRun, CanRun)]
        self._sums = {key: sciline.Reduced(operator.add)() for key in sums}
        self._finalize = sciline.Stage(wf, inputs=sums, outputs=(IofQ,))

    def push(self, row: Mapping[type, Any]) -> None:
        (key,) = row                                       # a row of this spec has one field
        for k, part in self._contribute[key].compute(row).items():
            self._sums[k].push(part)

    def outputs(self) -> dict[type, Any]:
        return self._finalize.compute({k: acc.value for k, acc in self._sums.items()})

make_driver = functools.partial(IofQDriver, pipeline)
```

A driver need not use sciline.
Its keys are whatever its mapping names: sciline keys for a sciline driver, strings for code written by hand.

`ess.spec` provides no driver.
sciline ADR 0003 defers a generic driver until packages repeat the same driver code, and the cases of cost 9 each need code of their own.
`StreamProcessor` of ess.reduce fits the driver protocol, with `accumulate` for `push` and `finalize` for `outputs`; [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md) records why essapps does not import it.
Its context keys, rolling windows, and `clear()` have no counterpart here, since each would break the arrival symmetry.

**The driver's promises.**
The backend does not check them; `check_arrival_and_order` tests the first two.

- `outputs()` gives the outputs of every row pushed so far, whatever the order of pushes into different tables.
- Rows of different tables are kept apart, so the order symmetry holds.
- `push` does not modify the row. A driver that adds in place copies the first contribution, so that adding never changes a value that a `sciline.Stage` holds or that another record returned. `sciline.Reduced` with `operator.add` modifies nothing.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec declares optional may be left out.
- A driver may refuse a state, such as one with no can row yet, by raising in `outputs()`; the read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.
- `push` and `outputs` are called from one thread at a time.

**So the pipeline and the driver are used directly**, in keys, with no spec, params model, or framework:

```python
wf = pipeline.copy()                           # the pipeline, directly
wf[Bins] = 2
wf[RunData[SampleRun]] = run_101
wf[RunData[CanRun]] = can_110
wf.compute(IofQ)

driver = IofQDriver(pipeline, {Bins: 2})       # the driver, directly
driver.push({RunData[SampleRun]: run_101})
driver.push({RunData[CanRun]: can_110})
driver.outputs()[IofQ]
```

The params models, the mapping, and the framework come on top, for use through apps and services:

| Level | Speaks | Used directly by |
|---|---|---|
| pipeline, driver | keys | the workflow package and its tests; notebooks without the framework |
| binding: a mapping over a pipeline, a driver, or a function | the spec's fields and models | the backend |
| framework | requests and records | notebooks, apps, and services, through the client |

Who keeps each symmetry, and what its check proves:

| Symmetry | Kept by | Given | Its check proves |
|---|---|---|---|
| caching | the `sciline.Stage` cut at the blanks | a pure pipeline | `check_caching`: a call through a stage gives the plain request. Mostly tests `ess.spec`. |
| arrival, order | the driver | its promises | `check_arrival_and_order`: pushing rows one at a time, in other orders, and reading between pushes gives what pushing all rows at once gives. Both go through the driver, so it does not prove the outputs are right. |
| arrival, order | the framework, when it keeps the rows | nothing | holds by construction |
| one-row | the author of the two bindings | that the driver computes what the pipeline computes | `check_one_row`: the driver with one row per table gives the pipeline's outputs. The only check against an independent reference; more rows are the package's own tests. |

`check_arrival_and_order` takes a function that makes a driver, the fixed values, and the rows, all in keys, so a package tests its driver without a spec.

### 3. Protocol

The protocol is what the backend calls ([ess.spec.binding](../../../packages/essspec/src/ess/spec/binding.py)).
Every binding computes a plain request; a binding over a sciline pipeline also makes stages, and a binding over a driver also makes drivers:

```python
class Binding(Protocol):
    def compute(self, values) -> Mapping: ...             # a plain request

class StageBinding(Binding, Protocol):                    # implemented by PipelineBinding
    def stage(self, fixed, blanks) -> Callable: ...       # a handle to a sciline.Stage

class AccumulatorBinding(Binding, Protocol):              # implemented by DriverBinding
    def start(self, fixed) -> Driver: ...                 # a driver, in field names

class Driver(Protocol):
    def push(self, row) -> None: ...                      # one row of one table
    def outputs(self) -> Mapping: ...
```

The `Driver` that `start` returns is the package's driver in field names: the binding translates each row and the outputs through its mapping, so a row is `{'sample': {'file': ...}}` where the package's driver gets `{RunData[SampleRun]: ...}`.
`start(fixed)` pushes the rows of any table in `fixed`, so `compute(values)` of a `DriverBinding` is `start(values).outputs()`.

What the backend calls for each client action:

| Client action | `PipelineBinding` | `DriverBinding` | `function_binding` |
|---|---|---|---|
| plain request | `compute(values)` | `compute(values)` | `compute(values)` |
| stage | `stage(fixed, blanks)` at the first call, then the returned callable at each call | refused when the stage is made | refused when the stage is made |
| accumulator | the framework keeps the rows | `start(fixed)`, then `push` per row and `outputs()` per state read | the framework keeps the rows |

When the framework keeps the rows, it wraps the binding in a `Driver` of its own: `push` appends the row, and `outputs` calls `compute` with the fixed values and every row so far.
That wrapper lives in `ess.spec` (amendment C), next to the bindings.

The protocol carries the backend's promises to the binding: every value with defaults filled in, data read, a push never while the outputs of the state before it are in use.
Today it passes groups and rows as dicts by field name, with data in place of each reference, and takes the outputs as a dict.
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

### B. Three bindings, each of which takes its spec

```python
single = PipelineBinding(IOFQ, pipeline, params=keys, outputs={'iofq': IofQ})
multi = DriverBinding(IOFQ_MULTI, make_driver, params=keys, outputs={'iofq': IofQ})
fwd = function_binding(FORWARD, forward)
```

`DriverBinding` replaces `AccumulatingPipelineBinding`, and `accumulate` goes without a replacement: the workflow package writes its driver (section 2).
`PipelineBinding` keeps its form and gets its spec.
The kind of strategy is named by the class, not implied by whether `accumulate` is given.
`HeldStateBinding.held_state` becomes `AccumulatorBinding.start`, and only a `StageBinding` has `stage`.
When a binding is made, it checks that each path names a parameter or a field of a group or row, and that each output name is an output of the spec.
A `DriverBinding` also checks that rows of different tables set different keys.

A binding that knows its spec makes the `bind` mapping redundant:

```python
local(proposal='p1', datasets=datasets, bind=[single, multi, fwd])
```

and the entry point of [ADR 0007](../adr/0007-packages-split-by-dependencies.md) returns a list of bindings.
A bare function, which `bind` accepts today, no longer names its spec, so it is wrapped by `function_binding`.
Settles costs 2, 3, and 9.

### C. All strategies in `ess.spec`

`FunctionBinding` and the keep-the-rows wrapper move from `ess.dispatch.bindings` to `ess.spec`, next to `PipelineBinding` and `DriverBinding`.
`ess.spec.testing` then checks every strategy, and a package tests any binding without `ess.dispatch`.
`combine(op)` (`ess.spec.combine`), a binding that folds the rows of a table with `op`, is a generic driver of the same kind as `accumulate`, and is removed too.
Its readers are tests in essspec and essdispatch (`combine_test.py`, `testing_test.py`, `client_test.py`, `persist_test.py`, `stories/conftest.py`), the README's story S4, and [three-ways-to-run-a-spec.html](../three-ways-to-run-a-spec.html).
The tests use fakes of their own, and story S4 becomes a function over a list.
Settles cost 4.

### D. The protocol passes model instances (open, needs E)

```python
def iofq(params: IofQParams) -> IofQOutputs: ...      # a function binding

call = binding.stage({'sample': Run(...), 'can': Run(...)}, blanks=('reduction',))
call(IofQParams(sample=..., can=..., reduction=Reduction(bins=4)))   # -> IofQOutputs

driver = binding.start({'reduction': Reduction(bins=2)})
driver.push({'sample': Run(...)})                     # the Run the client pushed
driver.outputs()                                      # -> IofQOutputs
```

`stage` gets the fixed groups by name, and a blank is absent, so a strategy that reads a blank in the fixed part fails at once.
The callable gets the full plain request, which the backend has already validated.
So `compute`, and the callable `stage` returns, are `Callable[[IofQParams], IofQOutputs]`, which is what a function binding is.
`start` gets the fixed groups by name too: a state may be an invalid request until it is read, such as one with sample rows and no can row yet, so there is no valid `MultiIofQParams` to pass.

With `WorkflowSpec` generic in its outputs model as well as its params model, a type checker checks `function_binding(IOFQ, iofq)`.
The output names are checked where the author writes them, `IofQOutputs(iofq=...)`, not in the backend.
Settles cost 1 for function bindings.
A package's driver speaks keys, never field names or models (section 2), so D touches only the mappings and the function bindings.

Alternatives considered:

- **Defaults for every parameter that can be a blank**, so that `stage` gets `IofQParams(**fixed)`.
  Required parameters, such as `sample`, become optional: a request without them is no longer refused, and a form no longer marks them required.
  A blank holds a value that is never used, so a strategy that reads it computes a wrong result without an error.
  The params model's own validators run on these values.
- **Staging from the full instance of the first call.**
  `stage` gets a valid instance, but its blank fields hold the first call's values, which the fixed part must not use; `check_caching` catches a strategy that does.
  It does not extend to `start`, for the reason above.
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
A table says which keys each parameter sets, and the strategies need exactly that: a `sciline.Stage` is cut at the keys of the blanks, and a driver's rows are told apart by their keys.
From code alone, the binding cannot know which keys a blank or a row sets.

A middle way gives each parameter a function and the keys it may set or insert:

```python
params={'reduction.bins': Bins,                                          # a key, as today
        'transmission': (configure_transmission, {TransmissionFraction[SampleRun]})}
```

The `sciline.Stage` is then cut at the declared keys.
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
- **The workflow it returns is a driver.** `accumulate` and `finalize` are `push` and `outputs` of section 2, over chunks of a stream where essapps pushes rows of tables.

## Where the jobs live

| Job | Written by | Lives in | Tested by |
|---|---|---|---|
| 1 mapping | the workflow author, or the instrument's glue | the workflow package, or `essdispatchinstruments` (ADR 0007) | the checks when the binding is made; `check_one_row` |
| 2 strategy | the framework: `PipelineBinding`, `DriverBinding`, `function_binding`, the keep-the-rows wrapper; the workflow package: its driver | `ess.spec`; the package | `ess.spec`'s tests, once; the package's own, with `check_arrival_and_order` |
| 3 protocol | the framework | declared in `ess.spec.binding`, called by `ess.dispatch.backend` | essdispatch's tests |

A mapping as a table is not tied to this framework's strategies.
`StreamProcessor` takes part of the same description: dynamic keys (the fields of a row) and target keys (the outputs).
One table could serve both frameworks; a mapping written as code, as esslivedata's are, serves only the strategy it builds.

## What changes for clients

- `bind` is a list of bindings, and a function is bound with `function_binding` (amendment B).
- A stage exists only over a `PipelineBinding`; over a `DriverBinding` or a function binding, it is refused when it is made.
- The README's promise that an empty table matches an optional group left out (`can_runs=[]` matches `can=None`) is dropped, with `test_a_can_left_out_is_an_empty_table_of_cans`. Whether a driver accepts an empty table is the driver's choice.

Records, the other symmetries, and the backend's promises to a binding do not change: when it calls which method, from how many threads, and that a push waits for the readers of the state before it.

## Questions

1. Are mapping, strategy, and protocol the right names? "Binding" would stay the name of the whole: a mapping and a strategy, called through the protocol.

> Simon:

2. B: should a binding take its spec, so that `bind` becomes a list?

> Simon:

3. B: three bindings, `PipelineBinding` (plain requests and stages), `DriverBinding` (plain requests and accumulators, over a function that makes a driver from the fixed values), and `function_binding` (plain requests); no driver in `ess.spec`?

> Simon:

4. C: the bindings and the keep-the-rows wrapper in `ess.spec`, and `combine(op)` removed?

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
