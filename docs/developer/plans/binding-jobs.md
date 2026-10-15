# What a binding does: mapping, strategy, protocol

**Status: design note.**
It amends the binding part of the design: [README, What a binding provides](../README.md#what-a-binding-provides), [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), and [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md).
It assumes that the protocol passes model instances (amendment D).
It writes every data field as the value a workflow takes, a path or an array, and leaves open how a request's references become these values (amendment E), so that E is decided against this interface.

## In short

A *binding* is the code behind a spec. It does three jobs:

1. **Mapping**: which workflow input each parameter sets, and which workflow result each output is.
2. **Strategy**: what the workflow author provides, and how the framework serves each client action from it.
3. **Protocol**: what the backend calls on a binding, and what it passes and gets back.

The mapping names whole parameters only: a parameter `sample: Run` sets one key to a `Run`, and the workflow takes the `Run` apart in its own providers.

The workflow author provides one of three things, and each serves some of the client's actions:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline (`PipelineBinding`) | compute everything | a handle to a `sciline.Stage` cut at the blanks | refused |
| a driver (`DriverBinding`) | make a driver, push every row, read the outputs | refused | a driver that takes the client's pushes |
| a function (`function_binding`) | call it | refused | refused |

A *driver* is a stateful object with `push(rows)` and `outputs()`, in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245): the code that calls `sciline.Stage`s and pushes into accumulators.
The workflow package writes it; `ess.spec` provides none.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
A stage exists only over a sciline pipeline, since only there does it hold anything.

This note separates the three jobs, lists what mixing them costs today, and proposes amendments.
A to D and F settle costs; E is decided after this interface, and G stays open.

## Terms

Defined in the [README](../README.md); repeated here for reading this note alone.

| Term | Meaning |
|---|---|
| parameter, group, table, row | A *parameter* is a field of a params model. It is a value, a *group* (a nested model, such as `sample: Run`), or a *table* (a list of a model, such as `sample: list[Run]`); each entry of a table is a *row*. |
| template, fixed, blank | A *template* is a request with some parameters left out, the *blanks*; the others are *fixed*. A blank is a whole parameter, never a field of a group. |
| plain request | A request with every value given. |
| stage | A template the backend keeps for a client, called with values for its blanks. |
| accumulator | A template whose blanks are tables, kept by the backend for a client; each *push* gives it at most one row per table. *State n* is the accumulator after n pushes. |
| key | A sciline key, such as `Filename[SampleRun]`, or whatever names an input or result of a driver that does not use sciline. |
| symmetries | Changes a caller may make without changing the outputs: *caching* (a call through a stage against the plain request), *arrival* (rows pushed one at a time against all at once), *order* (the order of pushes into different tables), *one-row* (the multi-run spec with one row per table against the single-run spec). |

## The example

The toy SANS reduction of [roles/toy_sans.py](../roles/toy_sans.py), with a single-run spec and a multi-run spec.
The workflow package writes the sciline pipeline and the models it takes as inputs:

```python
class Run(BaseModel):                  # a group of IOFQ, and a row of IOFQ_MULTI
    file: Path                         # the workflow loads it

class RunInput(sciline.Scope[RunType, Run], Run):
    """A Run, as the input for one run type."""

class Reduction(BaseModel):
    bins: int = 2

def filename(run: RunInput[RunType]) -> Filename[RunType]:
    return Filename[RunType](run.file)

def bins(reduction: Reduction) -> Bins:
    return Bins(reduction.bins)

pipeline = sciline.Pipeline([filename, bins, read, load, numerator, denominator, iofq])
```

`read` turns a `Filename` into the run's counts and monitor count; the other providers are those of toy_sans.py.

The specs are built from the same models:

```python
class IofQParams(BaseModel):           # IOFQ
    sample: Run
    can: Run
    reduction: Reduction = Field(default_factory=Reduction)

class MultiIofQParams(BaseModel):      # IOFQ_MULTI
    sample: list[Run] = Field(min_length=1)
    can: list[Run] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):   # both specs
    iofq: Annotated[sc.Variable, ArraySpec(dims=('Q',), unit='dimensionless')]
```

A workflow that cannot load its inputs takes the data.
`PARTS_SUM` of the [user stories](../user-stories.md) sums the outputs of other records:

```python
class Parts(BaseModel, arbitrary_types_allowed=True):   # a row of PARTS_SUM
    numerator: sc.Variable
    denominator: sc.Variable
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

The mapping today names the fields of groups and rows by path: `{'sample.file': RunData[SampleRun], 'reduction.bins': Bins}`.

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
   A sciline loader wants a path; a workflow that reads another record's output wants the array.
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
10. **Paths into groups are reassembled by convention.**
    A row with two fields, such as a run and its own transmission run, maps to two keys, and the driver must get both in one push.
    The binding checks that every field of a row arrives together and that the rows of different tables set different keys.
    Both checks exist only because the mapping split the row.

## The three jobs

### 1. Mapping

The mapping says, for one spec and one workflow, which key each parameter sets and which key each output is.
It names whole parameters, never the fields of a group or a row:

```python
single = PipelineBinding(
    IOFQ, pipeline,
    params={'sample': RunInput[SampleRun], 'can': RunInput[CanRun], 'reduction': Reduction},
    outputs={'iofq': IofQ},
)
```

What each key gets:

| Parameter | Its key gets |
|---|---|
| a value, such as `bins: int` | the value |
| a group, such as `sample: Run` | the model instance |
| a table, such as `sample: list[Run]` | for a pipeline, the list of rows; for a driver, each row, pushed under the key |

For each output, the binding takes the result at its key, and builds the outputs model from them.

A key gets exactly what the params model holds, so the mapping translates names only.
A row is one value under one key, so a driver's push needs no reassembly (cost 10).
A blank is a whole parameter, so a `sciline.Stage` is cut at exactly the keys of the blanks.

**The workflow takes the models as inputs.**
A provider takes a model apart, or converts it, inside the pipeline: `filename` turns a `RunInput[SampleRun]` into a `Filename[SampleRun]`, and `bins` a `Reduction` into `Bins`.
For a real reduction, a model such as `QBins` (start, stop, number of bins) is useful to any user of the workflow, with or without the framework:

```python
QEdges = NewType('QEdges', sc.Variable)    # the bin edges that workflows take today

def setup_qedges(bins: QBins) -> QEdges:
    return QEdges(sc.linspace('Q', bins.start, bins.stop, bins.num + 1, unit='1/angstrom'))
```

The conversion is a provider of the workflow, so the mapping needs no functions (amendment F), and a user without the framework sets either key: `wf[QBins] = QBins(...)`, or `wf[QEdges] = edges` directly.

A model that serves several run types gets a key per run type, as `RunInput[RunType]` does.
A generic pydantic model, `class Run(BaseModel, Generic[RunType])` with keys `Run[SampleRun]`, would also do, and sciline supports it, but not with run types made by `NewType`, as ess.reduce makes them.
pydantic names both `Run[SampleRun]` and `Run[CanRun]` as `Run[NewType]`, and sciline's dask scheduler names its tasks by the key's name, so both keys become one task and get one value, without an error ([scipp/sciline#250](https://github.com/scipp/sciline/issues/250)).
`RunInput` with `sciline.Scope` as its first base avoids this (pydantic warns about the order of bases).

When a binding is made, it checks that every parameter of the spec has a key, that the keys are distinct, and that every output of the spec has a key; optional outputs may have none.

**One mapping for both specs.**
`IOFQ_MULTI` names each table as `IOFQ` names the group, with the same model, so the same `params` serves both:

```python
multi = DriverBinding(IOFQ_MULTI, make_driver, params=single.params, outputs=single.outputs)
```

This is a convention that saves writing a second mapping; the framework knows of no link between the two specs.
A package whose specs name their parameters differently, such as `sample` and `sample_runs` in [roles/toy_sans.py](../roles/toy_sans.py), writes two mappings.
The one-row symmetry is the package's own: `check_one_row` is a test the package may write, comparing the driver with one row per table against the pipeline.

**A function** takes the params model whole and returns the outputs model (amendment D), so it needs no mapping:

```python
def iofq(params: IofQParams) -> IofQOutputs: ...
draft = function_binding(IOFQ, iofq)            # in place of `single`, while drafting
```

The author of a pipeline or a function promises that it is pure: it modifies no input, and returns the same results for the same inputs.

### 2. Strategy

**A sciline pipeline** serves a spec without tables, or a spec whose tables the pipeline takes whole, as lists.
A plain request computes the outputs from the pipeline with every value set.
A stage is a handle to a `sciline.Stage` cut at the keys of the blanks: the part that does not depend on the blanks is computed once, and each call computes the rest.
Tuning a parameter over fixed rows, such as the bins over a hundred runs, needs a pipeline that takes the table as a list; such a pipeline is staged as any other.

**A function** serves the same specs as a pipeline, for drafts in notebooks.
It has no stage, since a stage over a function would hold nothing.

**A driver** serves a spec with tables.
It is a driver in the sense of sciline ADR 0003, and holds everything stateful.
The binding makes one from the fixed values, in the keys of the mapping, and pushes rows, as lists per key:

```python
driver = make_driver({Reduction: Reduction(bins=2)})      # the fixed values; no rows yet
driver.push({RunInput[SampleRun]: [run_101, run_102]})    # two rows of the sample table
driver.push({RunInput[CanRun]: [can_110], RunInput[SampleRun]: [run_103]})
driver.outputs()                                          # -> {IofQ: ...}
```

A push may carry several rows and several tables, so that a driver can work on them together, such as loading runs in parallel.
A plain request, and the tables fixed by an accumulator's template, are one push of every row.
A client's push is one driver push.
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
            RunInput[run]: sciline.Stage(wf, inputs=(RunInput[run],),
                                         outputs=(Numerator[run], Denominator[run]))
            for run in (SampleRun, CanRun)
        }
        sums = [key[run] for key in (Numerator, Denominator) for run in (SampleRun, CanRun)]
        self._sums = {key: sciline.Reduced(operator.add)() for key in sums}
        self._finalize = sciline.Stage(wf, inputs=sums, outputs=(IofQ,))

    def push(self, rows: Mapping[type, Sequence[Run]]) -> None:
        for key, runs in rows.items():
            for run in runs:
                for k, part in self._contribute[key].compute({key: run}).items():
                    self._sums[k].push(part)

    def outputs(self) -> dict[type, Any]:
        return self._finalize.compute({k: acc.value for k, acc in self._sums.items()})

make_driver = functools.partial(IofQDriver, pipeline)
```

A workflow that needs every row at once, such as `STITCH`, which fits scale factors over all angles, writes a driver that keeps its rows and computes the pipeline over all of them at each read:

```python
class StitchDriver:
    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[type, Any]) -> None:
        self._wf = pipeline.copy()
        for key, value in fixed.items():
            self._wf[key] = value
        self._runs: list[Run] = []

    def push(self, rows: Mapping[type, Sequence[Run]]) -> None:
        self._runs.extend(rows[Runs])

    def outputs(self) -> dict[type, Any]:
        self._wf[Runs] = list(self._runs)
        return self._wf.compute((Stitched,))
```

Whether recomputing every row at each read is acceptable is the workflow's choice, not the framework's.

A driver need not use sciline.
Its keys are whatever its mapping names: sciline keys for a sciline driver, strings for code written by hand.

`ess.spec` provides no driver.
sciline ADR 0003 defers a generic driver until packages repeat the same driver code, and the cases of cost 9 each need code of their own.
`StreamProcessor` of ess.reduce is close to the driver protocol, with `accumulate` for `push` and `finalize` for `outputs`, though its `accumulate` takes one chunk, not lists of rows; [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md) records why essapps does not import it.
Its context keys, rolling windows, and `clear()` have no counterpart here, since each would break the arrival symmetry.

**The driver's promises.**
The backend does not check them; `check_arrival_and_order` tests the first two.

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a row. A driver that adds in place copies the first contribution, so that adding never changes a value that a `sciline.Stage` holds or that another record returned. `sciline.Reduced` with `operator.add` modifies nothing.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec declares optional may be left out.
- A driver may refuse a state, such as one with no can row yet, by raising in `outputs()`; the read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.
- `push` and `outputs` are called from one thread at a time.

**So the pipeline and the driver are used directly**, in keys, with no spec or framework:

```python
wf = pipeline.copy()                           # the pipeline, directly
wf[Reduction] = Reduction(bins=2)
wf[RunInput[SampleRun]] = Run(file=path_101)
wf[Filename[CanRun]] = path_110                # a key past the model, set directly
wf.compute(IofQ)

driver = IofQDriver(pipeline, {Reduction: Reduction(bins=2)})   # the driver, directly
driver.push({RunInput[SampleRun]: [Run(file=path_101)], RunInput[CanRun]: [Run(file=path_110)]})
driver.outputs()[IofQ]
```

The specs, the mapping, and the framework come on top, for use through apps and services:

| Level | Speaks | Used directly by |
|---|---|---|
| pipeline, driver | keys, and the workflow's models as values | the workflow package and its tests; notebooks without the framework |
| binding: a mapping over a pipeline, a driver, or a function | the spec's parameter names and models | the backend |
| framework | requests and records | notebooks, apps, and services, through the client |

Who keeps each symmetry, and what its check proves:

| Symmetry | Kept by | Given | Its check proves |
|---|---|---|---|
| caching | the `sciline.Stage` cut at the blanks | a pure pipeline | `check_caching`: a call through a stage gives the plain request. Mostly tests `ess.spec`. |
| arrival, order | the driver | its promises | `check_arrival_and_order`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push of all rows gives. Both go through the driver, so it does not prove the outputs are right. |
| one-row | the package | that the driver computes what the pipeline computes | `check_one_row`, if the package writes one: the driver with one row per table gives the pipeline's outputs. The only check against an independent reference; more rows are the package's own tests. |

`check_arrival_and_order` takes a function that makes a driver, the fixed values, and the rows, all in keys, so a package tests its driver without a spec.

### 3. Protocol

The protocol is what the backend calls ([ess.spec.binding](../../../packages/essspec/src/ess/spec/binding.py)).
Every binding computes a plain request; a binding over a sciline pipeline also makes stages, and a binding over a driver also makes drivers.
It passes and returns model instances (amendment D):

```python
class Binding(Protocol[P, O]):
    spec: WorkflowSpec[P, O]
    def compute(self, params: P) -> O: ...                               # a plain request

class StageBinding(Binding[P, O], Protocol):                            # PipelineBinding
    def stage(self, fixed: Mapping[str, Any], blanks: Collection[str]) -> Callable[[P], O]: ...

class AccumulatorBinding(Binding[P, O], Protocol):                      # DriverBinding
    def start(self, fixed: Mapping[str, Any]) -> Driver[O]: ...

class Driver(Protocol[O]):
    def push(self, rows: Mapping[str, Sequence[BaseModel]]) -> None: ...  # rows by table name
    def outputs(self) -> O: ...
```

`stage` gets the fixed parameters by name, and a blank is absent, so a strategy that reads a blank in the fixed part fails at once.
The callable it returns gets the full plain request, which the backend has already validated.
`start` gets the fixed parameters by name too: a state may be an invalid request until it is read, such as one with sample rows and no can row yet, so there is no valid `MultiIofQParams` to pass.

The `Driver` that `start` returns is the package's driver in parameter names: the binding renames each table to its key and leaves the rows as they are, so `{'sample': [run_101]}` reaches the package's driver as `{RunInput[SampleRun]: [run_101]}`.
`start(fixed)` pushes the rows of any table in `fixed`, so `compute(params)` of a `DriverBinding` is `start(dict(params)).outputs()`.

What the backend calls for each client action:

| Client action | `PipelineBinding` | `DriverBinding` | `function_binding` |
|---|---|---|---|
| plain request | `compute(params)` | `compute(params)` | `compute(params)` |
| stage | `stage(fixed, blanks)` at the first call, then the returned callable at each call | refused when the stage is made | refused when the stage is made |
| accumulator | refused when the accumulator is made | `start(fixed)`, then `push` per client push and `outputs()` per state read | refused when the accumulator is made |

The protocol carries the backend's promises to the binding: every value with defaults filled in, a push never while the outputs of the state before it are in use.
The client never sees the protocol, and neither does the author of a sciline workflow or of a driver.
The author of a function sees it, through the function's signature, which is the spec's own models.

## Amendments

They are independent of each other, apart from D, which every other amendment assumes.

### A. Name the jobs in the docs

The README's "What a binding provides", ADR 0006, and [roles/workflow-authors.ipynb](../roles/workflow-authors.ipynb) explain a binding as a mapping and a strategy, called through the protocol.
The Symmetries table splits "Kept by" into the strategy and what it is given, as in the table above.
The README's section Drivers calls the client code that decides what to submit and push a *driver*, which clashes with sciline's driver.
The two are the halves of one loop: the client's code decides which rows arrive, and the package's driver adds them.
One of them needs another name (question 9).
Settles cost 5.

### B. Three bindings, each of which takes its spec and maps whole parameters

```python
single = PipelineBinding(IOFQ, pipeline, params=keys, outputs={'iofq': IofQ})
multi = DriverBinding(IOFQ_MULTI, make_driver, params=keys, outputs={'iofq': IofQ})
exp = function_binding(EXPORT, export)
```

`DriverBinding` replaces `AccumulatingPipelineBinding`, and `accumulate` goes without a replacement: the workflow package writes its driver (section 2).
`PipelineBinding` gets its spec, and its mapping names whole parameters (section 1).
The kind of strategy is named by the class, not implied by whether `accumulate` is given.
`HeldStateBinding.held_state` becomes `AccumulatorBinding.start`, and only a `StageBinding` has `stage`.

A binding that knows its spec makes the `bind` mapping redundant:

```python
local(proposal='p1', datasets=datasets, bind=[single, multi, exp])
```

and the entry point of [ADR 0007](../adr/0007-packages-split-by-dependencies.md) returns a list of bindings.
A bare function, which `bind` accepts today, no longer names its spec, so it is wrapped by `function_binding`.
Settles costs 2, 3, 9, and 10.

### C. All strategies in `ess.spec`, and only drivers accumulate

`FunctionBinding` moves from `ess.dispatch.bindings` to `ess.spec`, next to `PipelineBinding` and `DriverBinding`.
`ess.spec.testing` then checks every strategy, and a package tests any binding without `ess.dispatch`.

Two generic drivers are removed, since how rows combine is the workflow's choice:

- The keep-the-rows held state (`open_held_state`, `ess.dispatch.bindings`), which the backend uses today for any binding without a held state of its own. A workflow that needs every row at once writes a driver that keeps them, as `StitchDriver` does.
- `combine(op)` (`ess.spec.combine`), a binding that folds the rows of a table with `op`. Its readers are tests in essspec and essdispatch (`combine_test.py`, `testing_test.py`, `client_test.py`, `persist_test.py`, `stories/conftest.py`), the README's story S4, and [three-ways-to-run-a-spec.html](../three-ways-to-run-a-spec.html). The tests use fakes of their own, and story S4 becomes a function over a list.

Settles cost 4.

### D. The protocol passes model instances (assumed)

```python
def iofq(params: IofQParams) -> IofQOutputs: ...      # a function binding

call = binding.stage({'sample': Run(...), 'can': Run(...)}, blanks=('reduction',))
call(IofQParams(sample=..., can=..., reduction=Reduction(bins=4)))   # -> IofQOutputs

driver = binding.start({'reduction': Reduction(bins=2)})
driver.push({'sample': [Run(...)]})                   # the Run the client pushed
driver.outputs()                                      # -> IofQOutputs
```

So `compute`, and the callable `stage` returns, are `Callable[[IofQParams], IofQOutputs]`, which is what a function binding is.
With `WorkflowSpec` generic in its outputs model as well as its params model, a type checker checks `function_binding(IOFQ, iofq)`.
The output names are checked where the author writes them, `IofQOutputs(iofq=...)`, not in the backend.
A pipeline's providers take the same models (section 1), so a conversion such as `QBins` to `QEdges` never sees the protocol's form.
Settles costs 1 and 7.

Alternatives considered:

- **Defaults for every parameter that can be a blank**, so that `stage` gets `IofQParams(**fixed)`.
  Required parameters, such as `sample`, become optional: a request without them is no longer refused, and a form no longer marks them required.
  A blank holds a value that is never used, so a strategy that reads it computes a wrong result without an error.
  The params model's own validators run on these values.
- **Staging from the full instance of the first call.**
  `stage` gets a valid instance, but its blank fields hold the first call's values, which the fixed part must not use; `check_caching` catches a strategy that does.
  It does not extend to `start`, for the reason in section 3.
- **Dicts, as today.** Every provider that takes a group would take a dict.

### E. From references to the values a workflow takes (open, decided after this interface)

This note writes a data field as the value a workflow takes: `Run.file: Path` for a workflow that loads, `Parts.numerator: sc.Variable` for one that does not.
A request and a record hold references instead: a dataset name, or another record's output.
E is how the backend gets from a request's references to the workflow's values, and from the workflow's outputs to the record's references.
Outputs are the harder case: nearly every output is a data field.

What a solution must give:

- the interface of this note, unchanged, for the workflow author;
- static types that hold where the author reads them;
- the form the workflow takes chosen by the workflow's model, not by the dataset source (cost 6);
- no copy of a large array, in inputs or in outputs;
- references in records, so that history and provenance stay small.

Options to weigh against it, listed only: a handle type that holds a reference and, in the backend, the value; two forms of each model, generic over the type of its data fields; `model_construct` with values in fields typed as references; data passed apart from the params instance, as esslivedata passes streams.

### F. Conversions are providers of the workflow

A conversion, such as `QBins` to a bin-edge array, is a provider that takes the model (section 1).
The mapping holds no functions; esslivedata's `QEdges.get_edges()` becomes the body of such a provider.
Settles cost 7, with D.

### G. A table or code (open)

A mapping can be a table, as in B, or code that configures the pipeline from the params, as esslivedata's factories are:

```python
def configure(pipeline: sciline.Pipeline, params: SansWorkflowParams) -> sciline.Pipeline:
    pipeline = pipeline.copy()
    pipeline[QBins] = params.q_bins
    pipeline[CorrectForGravity] = CorrectForGravity(False)   # a value users never see
    if params.transmission.mode is TransmissionMode.constant:
        pipeline[TransmissionFraction[SampleRun]] = sc.scalar(1.0)
    else:
        pipeline.insert(transmission_from_monitor)          # the graph depends on a field
    return pipeline
```

Code can say everything (cost 8).
A table says which key each parameter sets, and the strategies need exactly that: a `sciline.Stage` is cut at the keys of the blanks, and a driver's rows arrive under their table's key.
From code alone, the binding cannot know which keys a blank or a row sets.

With models as inputs, a provider that takes `Transmission` can choose a value by its mode, but it cannot change which providers the graph has.
A middle way gives a parameter a function and the keys it may set or insert:

```python
params={'q_bins': QBins,                                              # a key, as above
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
- **The mapping is code** (cost 8, amendment G), and the factory mixes it with the choice of strategy. Its conversions, such as `get_edges()`, are called in the factory, where amendment F puts them in providers.
- **Outputs are matched by name, unchecked.** The factory's target keys map output names to keys; nothing checks the names against the outputs model, and no instance of the outputs model is made. The same holds in essapps today (cost 2).
- **The workflow it returns is a driver.** `accumulate` and `finalize` are `push` and `outputs` of section 2, over chunks of a stream where essapps pushes rows of tables.

## Where the jobs live

| Job | Written by | Lives in | Tested by |
|---|---|---|---|
| 1 mapping | the workflow author, or the instrument's glue | the workflow package, or `essdispatchinstruments` (ADR 0007) | the checks when the binding is made |
| models and conversions | the workflow author | the workflow package (question 5) | the package's own tests, without the framework |
| 2 strategy | the framework: `PipelineBinding`, `DriverBinding`, `function_binding`; the workflow package: its driver | `ess.spec`; the package | `ess.spec`'s tests, once; the package's own, with `check_arrival_and_order` |
| 3 protocol | the framework | declared in `ess.spec.binding`, called by `ess.dispatch.backend` | essdispatch's tests |

A mapping as a table is not tied to this framework's strategies.
`StreamProcessor` takes part of the same description: dynamic keys (the tables) and target keys (the outputs).
One table could serve both frameworks; a mapping written as code, as esslivedata's are, serves only the strategy it builds.

## What changes for clients

- `bind` is a list of bindings, and a function is bound with `function_binding` (amendment B).
- A stage exists only over a `PipelineBinding`; an accumulator only over a `DriverBinding`. Either is refused, when it is made, over any other binding. An accumulator over `STITCH` needs its package's `StitchDriver`, and `STITCH` then has no stage.
- The README's promise that an empty table matches an optional group left out (`can_runs=[]` matches `can=None`) is dropped, with `test_a_can_left_out_is_an_empty_table_of_cans`. Whether a driver accepts an empty table is the driver's choice.

Records, the other symmetries, and the backend's promises to a binding do not change: when it calls which method, from how many threads, and that a push waits for the readers of the state before it.

## Questions

1. Are mapping, strategy, and protocol the right names? "Binding" would stay the name of the whole: a mapping and a strategy, called through the protocol.

> Simon:

2. B: should a binding take its spec, so that `bind` becomes a list?

> Simon:

3. B: three bindings, `PipelineBinding` (plain requests and stages), `DriverBinding` (plain requests and accumulators, over a function that makes a driver from the fixed values), and `function_binding` (plain requests); no driver in `ess.spec`?

> Simon:

4. C: the bindings in `ess.spec`, and both keep-the-rows and `combine(op)` removed, so that only a package's driver accumulates? A spec bound to a driver has no stage, so a workflow that needs every row at once, such as `STITCH`, chooses between accumulating and tuning over fixed rows.

> Simon:

5. Section 1: the specs are built from the workflow package's models, such as `Run` and `QBins`. ADR 0007 says an instrument's `specs.py` needs only essspec, so that GUIs and remote clients import it without the science stack. Importing the models from a workflow package runs its `__init__.py`, and a model with an `sc.Variable` field needs scipp. Do the models move to a light module the workflow package and the specs share, or does ADR 0007's promise go, with remote clients reading `SerializedWorkflowSpec` only?

> Simon:

6. Section 2: a driver push carries lists of rows for several tables. Is `Mapping[Key, Sequence[Row]]` the form, or do you prefer one table per push?

> Simon:

7. Who writes the mapping for an instrument: the workflow package, or the instrument's glue in `essdispatchinstruments`?

> Simon:

8. G: a table, code, or a function per parameter with the keys it declares? Do you know of a real reduction, other than esslivedata's transmission mode, whose graph depends on a field?

> Simon:

9. A: "driver" means the client code that submits and pushes in the README, and the package's object that adds rows in sciline. Keep "driver" for sciline's meaning, as one vocabulary across sciline, ess.reduce, and essapps, and rename the README's (for example to *client loop*)?

> Simon:

10. Section 1: should the one-row convention, the multi-run spec naming each table as the single-run spec names the group, be recommended in the docs? Going further, deriving the single-run spec (`IOFQ_MULTI.single_run()`), or letting the backend accept a group where a table is expected, as a table of one row, would be the first thing that depends on the one-row symmetry. Worth it, or a step too far?

> Simon:
