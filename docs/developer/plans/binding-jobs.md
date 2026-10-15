# What a binding is: a strategy, called in the spec's terms

**Status: design note.**
It amends the binding part of the design: [README, What a binding provides](../README.md#what-a-binding-provides), [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), and [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md).
It assumes that the protocol passes model instances (amendment D).
It writes every data field as the value a workflow takes, a path or an array, and leaves open how a request's references become these values (amendment E), so that E is decided against this interface.

## In short

A *binding* pairs a spec with a *strategy*: what the workflow author provides, and how the framework serves each client action from it.
The backend calls a binding in the spec's own terms: params model instances, parameter names, row models, and outputs model instances.

The spec defines the types of its parameters and outputs.
A workflow uses these types as its sciline keys, or an adapter in the instrument's glue translates them to the workflow's own keys.
Either way, the spec imports nothing from the workflow.

The workflow author provides one of three things, and each serves some of the client's actions:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline (`PipelineBinding`) | compute everything | a handle to a `sciline.Stage` cut at the blanks | refused |
| a driver (`DriverBinding`) | make a driver, push every row, read the outputs | refused | a driver that takes the client's pushes |
| a function (`function_binding`) | call it | refused | refused |

A *driver* is a stateful object with `push(table, rows)` and `outputs()`, in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245): the code that calls `sciline.Stage`s and pushes into accumulators.
It is written for one spec, in that spec's terms.
The workflow package chooses it: its own code, or a generic one behind an adapter, such as ess.reduce's `StreamProcessor`; the framework needs no particular driver.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
A stage exists only over a sciline pipeline, since only there does it hold anything.

This note lists what today's bindings cost, describes the design, and proposes amendments.
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
| key | A sciline key, such as `Filename[SampleRun]`. |
| spec side, workflow side | The *spec side* is the modules that define specs and their types: `ess.spec` for types that many specs share, and the module of a spec. It needs no workflow package ([ADR 0007](../adr/0007-packages-split-by-dependencies.md)). The *workflow side* is the workflow package and the instrument's glue; it imports the spec side. |
| symmetries | Changes a caller may make without changing the outputs: *caching* (a call through a stage against the plain request), *arrival* (rows pushed one at a time against all at once), *order* (the order of pushes into different tables), *one-row* (the multi-run spec with one row per table against the single-run spec). |

## The example

The toy SANS reduction of [roles/toy_sans.py](../roles/toy_sans.py), with a single-run spec and a multi-run spec.

The spec side defines the types, the specs, and the run types that tell the sample from the can:

```python
SampleRun = NewType('SampleRun', int)       # run types (question 5)
CanRun = NewType('CanRun', int)
RunType = TypeVar('RunType', SampleRun, CanRun)

class Run(BaseModel):                        # a group of IOFQ, and a row of IOFQ_MULTI
    file: Path                               # the workflow loads it

class RunInput(sciline.Scope[RunType, Run], Run):
    """A Run for one run type: the type of a parameter, and a sciline key."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return handler(Run)                  # validated, and shown in forms, as a Run

class Reduction(BaseModel):
    bins: int = 2

IofQ = NewType('IofQ', sc.Variable)

class IofQParams(BaseModel):                 # IOFQ
    sample: RunInput[SampleRun]
    can: RunInput[CanRun]
    reduction: Reduction = Field(default_factory=Reduction)

class MultiIofQParams(BaseModel):            # IOFQ_MULTI
    sample: list[RunInput[SampleRun]] = Field(min_length=1)
    can: list[RunInput[CanRun]] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):   # both specs
    iofq: Annotated[IofQ, ArraySpec(dims=('Q',), unit='dimensionless')]
```

The workflow side imports these types and uses them as keys:

```python
def filename(run: RunInput[RunType]) -> Filename[RunType]:
    return Filename[RunType](run.file)

def bins(reduction: Reduction) -> Bins:
    return Bins(reduction.bins)

pipeline = sciline.Pipeline([filename, bins, read, load, numerator, denominator, iofq])
```

`read` turns a `Filename` into the run's counts and monitor count; the other providers are those of toy_sans.py, with `iofq` returning the spec's `IofQ`.

A workflow that cannot load its inputs takes the data.
`PARTS_SUM` of the [user stories](../user-stories.md) sums the outputs of other records:

```python
class Parts(BaseModel, arbitrary_types_allowed=True):   # a row of PARTS_SUM
    numerator: sc.Variable
    denominator: sc.Variable
```

## Today

`PipelineBinding.stage` (`ess/spec/pipeline.py`):

```python
def stage(self, fixed, blanks):
    blank_keys = self._keys.of(blanks)                 # the keys that the blanks set
    pipeline = self._keys.set(self._pipeline, fixed)   # the fixed values onto their keys
    cut = _Cut(pipeline, outputs, blank_keys)          # a sciline.Stage cut at the blanks
    def call(**values):                                # the blanks by name, as dicts
        results = cut(self._keys.by_key(values))       # the blank values onto their keys
        return {name: results[key] for name, key in self._outputs.items()}  # a dict
    return call
```

A `PipelineBinding` maps the fields of groups and rows by path to keys: `params={'sample.file': RunData[SampleRun], 'reduction.bins': Bins}`, `outputs={'iofq': IofQ}`.

The backend's side (`ess/dispatch/backend.py`):

```python
values = _values(IofQParams.model_validate(params))   # validated, then dumped to dicts
call = binding.stage(read(fixed), blanks)             # once per stage, at its first request;
                                                      # read: data in place of references
outputs = dict(call(**read(blank_values)))            # once per request
# the names of `outputs` are checked against IofQOutputs; no IofQOutputs is ever made
```

Today's protocol has `Binding.stage` on every binding, and `HeldStateBinding` adds `held_state(fixed)`, which returns a *held state* with `push` and `outputs`:

| Binding | Names to keys | Strategy | Passes |
|---|---|---|---|
| `PipelineBinding` (`ess.spec.pipeline`) | `params`: paths to keys; `outputs`: names to keys | plain request: one `sciline.Stage`; stage: a `sciline.Stage` cut at the keys of the blanks; no `held_state`, so the backend keeps the rows | dicts by name |
| `AccumulatingPipelineBinding` (`ess.spec.pipeline`) | the same, with `'sample_runs.file'` for each row's field, plus `accumulate`: per table, the keys summed over its rows | one cut per table up to the accumulated keys, sums, one cut from the sums to the outputs; the same for plain requests, stages, and held states | dicts by name |
| a function, wrapped by `FunctionBinding` (`ess.dispatch.bindings`) | none | each call computes everything; the backend keeps the rows (`open_held_state`, `ess.dispatch.bindings`) | dicts by name |
| `combine(op)` (`ess.spec.combine`) | none | fold the rows with `op` | dicts by name |
| a held state written by hand | inside the author's code | written by the author | dicts by name |

## What today's bindings cost

1. **A function's author writes against the protocol, not against the models.**
   The function takes groups as dicts and returns a dict: `def iofq(sample: dict, can: dict, reduction: dict) -> dict`.
   The backend held a validated `IofQParams` and dumped it (`backend.py`, `_values` and `_typed`).
   The models the author wrote appear nowhere in the function.
2. **Nothing pairs a binding with its spec until a request runs.**
   `bind={IOFQ: binding}` is the only link, and `PipelineBinding` never sees the spec.
   A path that names no field fails at the first request (`no sciline key for the parameters`).
   An output the binding does not return fails when the request completes (`_unfit`).
3. **Two sciline classes for one strategy.**
   Their constructors differ only in the paths, `'sample.file'` against `'sample_runs.file'`, and in `accumulate`.
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
8. **Paths to keys cannot say everything.**
   esslivedata's factories (see Prior art) also set values that users never see, such as `CorrectForGravity(False)`, and change the pipeline and its outputs depending on an enum field: one transmission mode inserts a provider and adds the output `transmission_fraction`.
   `PipelineBinding` has no place for either; such values go into the pipeline the binding is given.
9. **Accumulation is one recipe, fixed in the binding.**
   `AccumulatingPipelineBinding` sums the accumulated keys of each row.
   That fits SANS, but not binning each run's events into a held grid (spectroscopy), keeping each run's contribution so that one can be removed (README open question 2), or a loop over banks within each run.
   [sciline ADR 0003](https://github.com/scipp/sciline/pull/245) leaves these to a package's own driver.
   A held state written by hand is the way out today, but it gets dicts, not the models.
10. **Paths into groups are reassembled by convention.**
    A row with two fields, such as a run and its own transmission run, maps to two keys, and the driver must get both in one push.
    The binding checks that every field of a row arrives together and that the rows of different tables set different keys.
    Both checks exist only because the paths split the row.

## The design

### Protocol: the spec's terms

The protocol is what the backend calls ([ess.spec.binding](../../../packages/essspec/src/ess/spec/binding.py)).
Every binding computes a plain request; a binding over a sciline pipeline also makes stages, and a binding over a driver also makes drivers:

```python
class Binding(Protocol[P, O]):
    spec: WorkflowSpec[P, O]
    def compute(self, params: P) -> O: ...                               # a plain request

class StageBinding(Binding[P, O], Protocol):                            # PipelineBinding
    def make_stage(self, fixed: Mapping[str, Any], blanks: Collection[str]
                   ) -> Callable[[Mapping[str, Any]], O]: ...   # called with the blanks' values

class AccumulatorBinding(Binding[P, O], Protocol):                      # DriverBinding
    def make_accumulator(self, fixed: Mapping[str, Any], blanks: Collection[str]
                         ) -> Driver[O]: ...

class Driver(Protocol[O]):
    def push(self, table: str, rows: Sequence[BaseModel]) -> None: ...   # rows of one table
    def outputs(self) -> O: ...
```

`make_stage` and `make_accumulator` get the fixed parameters by name and the names of the blanks.
A parameter that is in neither takes its default from the spec; the binding fills it in.
When the stage or the driver is made, the binding checks that `fixed` and `blanks` do not overlap, that both name only parameters of the spec, and that each parameter without a default is in one of them; `make_accumulator` also checks that each blank is a table.
So a caller, such as a test, names what changes and leaves out what keeps its default: `binding.make_stage({'sample': run}, blanks=('reduction',))`.
A strategy that reads a blank in the fixed part fails at once, since a blank is not in `fixed`.
The callable it returns gets the values of the blanks only, by name, as a driver's `push` gets only the new rows: the fixed values are in the stage, and nothing passes or checks them again.
The backend validates the full plain request, and makes its record, before each call.
`make_accumulator` gets the fixed parameters by name too: a state may be an invalid request until it is read, such as one with sample rows and no can row yet, so there is no valid `MultiIofQParams` to pass.
A push names one table by its parameter name, and gives its rows as the models the client pushed.
It never names two tables: rows of different tables have nothing to do with each other, and a push of two would read as if they did.
Rows that belong together are one row of a model with several fields, such as `class Measurement(BaseModel): sample: Run; can: Run`.

The names follow one scheme per kind of thing:

| Kind | Named for | Names |
|---|---|---|
| protocols, which the backend calls, and their methods | the client object they serve | `Binding.compute` (a plain request), `StageBinding.make_stage`, `AccumulatorBinding.make_accumulator` |
| classes, which the author uses | what the author provides | `PipelineBinding`, `DriverBinding`, `function_binding` |
| what a protocol method returns | what it is | a stage callable, a `Driver` |

`make_stage` and `make_accumulator` are factories for the computing half of a client's stage or accumulator.
The other half, the template, its log, states, pins, and records, is the backend's.
So a client's accumulator has one driver behind it, and a client's stage one callable.
A plain request has no client object that lasts beyond it, so there is nothing to make: `compute` is the computation itself.
It is on every binding because it is the one action all three serve; for a `PipelineBinding` it is a stage with no blanks, called once, and for a `DriverBinding` an accumulator with no blanks, read once.

What the backend calls for each client action:

| Client action | `PipelineBinding` | `DriverBinding` | `function_binding` |
|---|---|---|---|
| plain request | `compute(params)` | `compute(params)` | `compute(params)` |
| stage | `make_stage(fixed, blanks)` at the first call, then the returned callable at each call | refused when the stage is made | refused when the stage is made |
| accumulator | refused when the accumulator is made | `make_accumulator(fixed, blanks)`, then `push` per client push and `outputs()` per state read | refused when the accumulator is made |

The protocol carries the backend's promises to the binding:

- `compute`, `make_stage`, and the callable `make_stage` returns may each be called from several threads at once, and none of them modifies the values it is given. A `PipelineBinding` sets values on a copy of its pipeline; a function is safe because its author promises it is pure. The callable may return the same object in several calls, so records made through one stage share what does not depend on the blanks.
- A driver's `push` and `outputs` are called from one thread at a time, and a push never while the outputs of the state before it are in use.
- A call through a stage gets the blanks' values of a valid plain request.
The client never sees the protocol.
The author of a function or a driver writes against it, in the spec's models; the author of a sciline pipeline does not.

### Types: the spec defines them, the workflow uses them

A parameter's type, or an output's, is the sciline key of that value: `reduction: Reduction` sets the key `Reduction` to the `Reduction` instance, and `iofq: IofQ` is computed as the key `IofQ`.
A parameter with a plain type gets one that names it, `bins: Bins` with `Bins = NewType('Bins', int)`, as any sciline input does.
A blank is a whole parameter, so a `sciline.Stage` is cut at exactly the keys of the blanks.

The types live on the spec side, and the workflow side imports them.
So the spec stays importable without the science stack of the workflow package (ADR 0007), and a workflow package that takes these types is usable without the framework: `wf[Reduction] = Reduction(bins=2)`.

**Conversions are providers.**
A provider takes a model apart or converts it inside the pipeline: `filename` turns a `RunInput[SampleRun]` into a `Filename[SampleRun]`.
For a real reduction, `ess.spec.parameters.QEdges` (start, stop, number of bins, scale, unit) is useful to any user of the workflow, with or without the framework.
esssans's key `QBins` holds the bin edges as a `sc.Variable`, and a provider makes them from the model:

```python
from ess.spec.conversions import edges_to_variable
from ess.spec.parameters import QEdges

def q_bins(edges: QEdges) -> QBins:
    return QBins(edges_to_variable(edges, dim='Q'))
```

A user without the framework sets either key: `wf[QEdges] = QEdges(...)`, or `wf[QBins] = edges` directly.

**One model, several parameters.**
`sample` and `can` are both a `Run`, but need different keys, so each parameter's type carries its role: `RunInput[SampleRun]` and `RunInput[CanRun]`.
Two axes of a Q map that share a model are the same case.
A provider that needs two parameters, such as an axis and its edges, takes both keys.
`RunInput` is a `sciline.Scope` with `__get_pydantic_core_schema__`, so that pydantic validates it as a `Run`; `ess.spec` can provide that hook once.
A generic pydantic model, `class Run(BaseModel, Generic[RunType])` with keys `Run[SampleRun]`, would also do, and sciline supports it, but not with run types made by `NewType`, as ess.reduce makes them.
pydantic names both `Run[SampleRun]` and `Run[CanRun]` as `Run[NewType]`, and sciline's dask scheduler names its tasks by the key's name, so both keys become one task and get one value, without an error ([scipp/sciline#250](https://github.com/scipp/sciline/issues/250)).

**An adapter, where a workflow does not take the spec's types.**
An existing workflow takes its own keys, such as `Filename[SampleRun]` or a `QBins` variable.
The instrument's glue then translates: with providers that take the spec's types and return the workflow's keys, such as `filename` and `q_bins` above, inserted into the pipeline; or, where a value needs no conversion, with a `keys` argument that names the workflow's key for a parameter:

```python
PipelineBinding(IOFQ, pipeline, keys={'reduction': ReductionSettings})
```

**A parameter that chooses a source of data**, such as the detector bank or the incident monitor, is a value like any other, with its own type.
Which dataset or stream then supplies a data field, the *routing*, is not part of the params model or of a binding (amendment E).

**The one-row symmetry.**
`IOFQ_MULTI` names each table as `IOFQ` names the group, with the same row type, so a pipeline over `IOFQ` and a driver over `IOFQ_MULTI` use the same keys.
This is a convention; the framework knows of no link between the two specs.
The one-row symmetry is the package's own: `check_one_row` is a test the package may write, comparing the driver with one row per table against the pipeline.

### Strategies

**A sciline pipeline** serves a spec without tables, or a spec whose tables the pipeline takes whole, as lists.
A plain request sets each parameter's key to its value and computes each output's key.
A stage is a handle to a `sciline.Stage` cut at the keys of the blanks: the part that does not depend on the blanks is computed once, and each call computes the rest.
Tuning a parameter over fixed rows, such as the bins over a hundred runs, needs a pipeline that takes the table as a list; such a pipeline is staged as any other.
What key a table that a pipeline takes whole sets, the field's `list[...]` type or a type that names the list, is open (question 5).
When a `PipelineBinding` is made, it checks that the parameters have distinct keys, that each is a key of the pipeline, and that each output's key can be computed.

```python
single = PipelineBinding(IOFQ, pipeline)
```

The author of a pipeline promises that it is pure: it modifies no input, and returns the same results for the same inputs.

**A function** serves the same specs as a pipeline, for drafts in notebooks.
It takes the params model and returns the outputs model, so a type checker checks it against the spec.
It has no stage, since a stage over a function would hold nothing.

```python
def iofq(params: IofQParams) -> IofQOutputs: ...
draft = function_binding(IOFQ, iofq)            # in place of `single`, while drafting
```

**A driver** serves a spec with tables.
It is a driver in the sense of sciline ADR 0003, holds everything stateful, and is written for one spec.
`DriverBinding` pairs the spec with a function that makes a driver from the fixed values that are not tables:

```python
multi = DriverBinding(IOFQ_MULTI, functools.partial(IofQDriver, pipeline))

driver = binding.make_accumulator({'reduction': Reduction(bins=2)}, blanks=('sample', 'can'))
driver.push('sample', [run_101, run_102])                   # two rows of the sample table
driver.push('can', [can_110])
driver.outputs()                                            # -> IofQOutputs
```

`make_accumulator` makes the driver from the values that are not tables, and pushes the rows of any table in `fixed`, so `compute(params)` of a `DriverBinding` is `make_accumulator(dict(params), blanks=()).outputs()`.
A push may carry several rows of its table, so that a driver can work on them together, such as loading runs in parallel.
A plain request, and the tables fixed by an accumulator's template, are one push per table, of all its rows.
A client's push may name several tables, so that their rows enter one state; the backend makes it one driver push per table, and reads the outputs only after the last, so the driver need not know.
A changed fixed value means a new driver.

For the toy SANS reduction, the package writes a driver that sums each run's numerator and denominator per run type with `sciline.Reduced`, and divides once.
It knows which key each table feeds; that is ordinary code in the one place that needs it:

```python
class IofQDriver:
    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[str, Any]) -> None:
        wf = pipeline.copy()
        wf[Reduction] = fixed['reduction']
        self._contribute = {
            table: (RunInput[run], sciline.Stage(wf, inputs=(RunInput[run],),
                                                 outputs=(Numerator[run], Denominator[run])))
            for table, run in (('sample', SampleRun), ('can', CanRun))
        }
        sums = [key[run] for key in (Numerator, Denominator) for run in (SampleRun, CanRun)]
        self._sums = {key: sciline.Reduced(operator.add)() for key in sums}
        self._finalize = sciline.Stage(wf, inputs=sums, outputs=(IofQ,))

    def push(self, table: str, rows: Sequence[Run]) -> None:
        key, stage = self._contribute[table]
        for run in rows:
            for k, part in stage.compute({key: run}).items():
                self._sums[k].push(part)

    def outputs(self) -> IofQOutputs:
        sums = {k: acc.value for k, acc in self._sums.items()}
        return IofQOutputs(iofq=self._finalize.compute(sums)[IofQ])
```

A workflow that needs every row at once, such as `STITCH`, which fits scale factors over all angles, writes a driver that keeps its rows and computes the pipeline over all of them at each read:

```python
class StitchDriver:
    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[str, Any]) -> None:
        self._wf = pipeline.copy()
        self._wf[Reference] = fixed['reference']
        self._runs: list[Run] = []

    def push(self, table: str, rows: Sequence[Run]) -> None:
        self._runs.extend(rows)                    # STITCH has one table, 'runs'

    def outputs(self) -> StitchOutputs:
        self._wf[Runs] = list(self._runs)
        return StitchOutputs(stitched=self._wf.compute(Stitched))
```

Whether recomputing every row at each read is acceptable is the workflow's choice, not the framework's.

A driver need not use sciline.
The framework calls `push` and `outputs` and nothing else, so it needs no particular driver.
A package writes its own, as above, or uses a generic one, and generic drivers may in time be collected in a library module; the design does not depend on which.
`StreamProcessor` of ess.reduce is one: `accumulate` is `push`, and `finalize` is `outputs`.
It takes one value per key, by key, so an adapter names the key of each table and pushes the rows one at a time.
Its context keys, rolling windows, and `clear()` stay unused, since each would break the arrival symmetry.
The framework itself does not import it ([ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md)); a package's driver may.

**The driver's promises.**
The backend does not check them; `check_arrival_and_order` tests the first two.

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a row. A driver that adds in place copies the first contribution, so that adding never changes a value that a `sciline.Stage` holds or that another record returned. `sciline.Reduced` with `operator.add` modifies nothing.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec declares optional may be left out.
- A driver may refuse a state, such as one with no can row yet, by raising in `outputs()`; the read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.

### Use without the framework

The pipeline and the driver need no spec object, backend, or client:

```python
wf = pipeline.copy()                                       # the pipeline
wf[Reduction] = Reduction(bins=2)
wf[RunInput[SampleRun]] = Run(file=path_101)
wf[Filename[CanRun]] = path_110                            # a key past the model, set directly
wf.compute(IofQ)

driver = IofQDriver(pipeline, {'reduction': Reduction(bins=2)})   # the driver
driver.push('sample', [Run(file=path_101)])
driver.push('can', [Run(file=path_110)])
driver.outputs().iofq
```

| Level | Speaks | Used directly by |
|---|---|---|
| pipeline | keys, which are the spec's types or the workflow's own | the workflow package and its tests; notebooks without the framework |
| driver, function | the spec's parameter names and models | the same |
| binding | the protocol | the backend |
| framework | requests and records | notebooks, apps, and services, through the client |

### Symmetries

| Symmetry | Kept by | Given | Its check proves |
|---|---|---|---|
| caching | the `sciline.Stage` cut at the blanks | a pure pipeline | `check_caching`: a call through a stage gives the plain request. Mostly tests `ess.spec`. |
| arrival, order | the driver | its promises | `check_arrival_and_order`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push of all rows gives. Both go through the driver, so it does not prove the outputs are right. |
| one-row | the package | that the driver computes what the pipeline computes | `check_one_row`, if the package writes one: the driver with one row per table gives the pipeline's outputs. The only check against an independent reference; more rows are the package's own tests. |

`check_arrival_and_order` takes a function that makes a driver, the fixed values, and the rows, so a package tests its driver without a spec.

## Amendments

They are independent of each other, apart from D, which every other amendment assumes.

### A. Describe a binding as a strategy, called in the spec's terms

The README's "What a binding provides", ADR 0006, and [roles/workflow-authors.ipynb](../roles/workflow-authors.ipynb) describe a binding as above.
The Symmetries table splits "Kept by" into the strategy and what it is given.
The README's section Drivers calls the client code that decides what to submit and push a *driver*, which clashes with sciline's driver.
The two are the halves of one loop: the client's code decides which rows arrive, and the package's driver adds them.
One of them needs another name (question 9).
Settles cost 5.

### B. Three bindings, each of which takes its spec

```python
single = PipelineBinding(IOFQ, pipeline)
multi = DriverBinding(IOFQ_MULTI, make_driver)
exp = function_binding(EXPORT, export)
```

`DriverBinding` replaces `AccumulatingPipelineBinding`, and `accumulate` goes without a replacement: the workflow package chooses its driver.
`PipelineBinding` gets its spec, and takes the keys from the spec's types, with `keys` only for an adapter.
The kind of strategy is named by the class, not implied by whether `accumulate` is given.
The protocols in `ess.spec.binding` change with them: `Binding` keeps only `compute`; the protocol `StageBinding` adds `make_stage`, implemented by `PipelineBinding`; and the protocol `AccumulatorBinding`, implemented by `DriverBinding`, replaces `HeldStateBinding`, with `make_accumulator` for its `held_state`.
The backend calls the protocols, not the classes, so that essdispatch imports no sciline, and its tests use fakes of their own.

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

The framework keeps no driver of its own, since how rows combine is the workflow's choice; two go:

- The keep-the-rows held state (`open_held_state`, `ess.dispatch.bindings`), which the backend uses today for any binding without a held state of its own. A workflow that needs every row at once writes a driver that keeps them, as `StitchDriver` does.
- `combine(op)` (`ess.spec.combine`), a binding that folds the rows of a table with `op`. Its readers are tests in essspec and essdispatch (`combine_test.py`, `testing_test.py`, `client_test.py`, `persist_test.py`, `stories/conftest.py`), the README's story S4, and [three-ways-to-run-a-spec.html](../three-ways-to-run-a-spec.html). The tests use fakes of their own, and story S4 becomes a function over a list.

Settles cost 4.

### D. The protocol passes model instances (assumed)

`compute` is `Callable[[IofQParams], IofQOutputs]`, which is what a function binding is; the callable `make_stage` returns takes the blanks' values and returns `IofQOutputs` too.
With `WorkflowSpec` generic in its outputs model as well as its params model, a type checker checks `function_binding(IOFQ, iofq)`.
The output names are checked where the author writes them, `IofQOutputs(iofq=...)`, not in the backend.
A pipeline's providers take the same models, so a conversion such as `QEdges` to `QBins` never sees the protocol's form.
Settles costs 1 and 7.

Alternatives considered:

- **Defaults for every parameter that can be a blank**, so that `make_stage` gets `IofQParams(**fixed)`.
  Required parameters, such as `sample`, become optional: a request without them is no longer refused, and a form no longer marks them required.
  A blank holds a value that is never used, so a strategy that reads it computes a wrong result without an error.
  The params model's own validators run on these values.
- **Staging from the full instance of the first call.**
  `make_stage` gets a valid instance, but its blank fields hold the first call's values, which the fixed part must not use; `check_caching` catches a strategy that does.
  It does not extend to `make_accumulator`, for the reason in Protocol.
- **Dicts, as today.** Every provider that takes a group would take a dict.

### E. From references to the values a workflow takes (open, decided after this interface)

This note writes a data field as the value a workflow takes: `Run.file: Path` for a workflow that loads, `Parts.numerator: sc.Variable` for one that does not.
A request and a record hold references instead: a dataset name, or another record's output.
The params name the source of each data field; the routing, which dataset or stream supplies it, stays apart from the params model and the binding.
E is how the backend gets from a request's references to the workflow's values, and from the workflow's outputs to the record's references.
Outputs are the harder case: nearly every output is a data field.

What a solution must give:

- the interface of this note, unchanged, for the workflow author;
- static types that hold where the author reads them;
- the form the workflow takes chosen by the spec's types, not by the dataset source (cost 6);
- no copy of a large array, in inputs or in outputs;
- references in records, so that history and provenance stay small.

Options to weigh against it, listed only: a handle type that holds a reference and, in the backend, the value; two forms of each model, generic over the type of its data fields; `model_construct` with values in fields typed as references.

### F. Conversions are providers of the workflow

A conversion, such as `QEdges` to the bin edges of `QBins`, is a provider that takes the model (Types).
`edges_to_variable`, or esslivedata's `QEdges.get_edges()`, becomes the body of such a provider.
Settles cost 7, with D.

### G. A graph that depends on a field (open)

esslivedata's LOKI factory inserts a provider and adds the output `transmission_fraction` depending on `params.transmission.mode`; its wavelength lookup-table workflow sets `PulseStride`, or leaves it to another provider, depending on `pulse.auto_stride`.
With types as keys, a provider that takes `TransmissionOptions` can choose a value by its mode, but it cannot change which providers the graph has.

A driver or a function does what it likes, so only a `PipelineBinding` is affected.
It could take a function that configures the pipeline from the fixed values, with the keys it may set or insert declared, so that a `sciline.Stage` can still be cut at the blanks:

```python
PipelineBinding(SANS, pipeline, configure=(configure_transmission, {TransmissionFraction[SampleRun]}))
```

Values that users never see are set in the pipeline the binding is given, by the workflow package or by the instrument's glue.
An output that exists only for some values of a field, such as `transmission_fraction`, is an optional output of the spec.

## Prior art: esslivedata

esslivedata solves the same problem for live data, and `ess.spec` is meant to hold its params models too.
Each instrument has `specs.py`, the params and outputs models, which the dashboard imports, and `factories.py`, the sciline glue, which only the backend loads.
Some factory code works around limits of today's base workflows and is meant to go, so it shows what is needed now, not what a binding must allow.

| | esslivedata |
|---|---|
| names to keys | code: a factory per spec sets each sciline key from the params by hand (`wf[QBins] = params.q_edges.get_edges()`), sets values users never see (`wf[CorrectForGravity] = CorrectForGravity(False)`), and changes the graph and the outputs by an enum field (`config/instruments/loki/factories.py`) |
| strategy | chosen by the same factory: nearly always a `StreamProcessorWorkflow`, given dynamic keys, accumulators, and target keys; other strategies are other implementations of its `Workflow` protocol |
| protocol | `factory(source_name=..., params=SansWorkflowParams(...), aux_source_names=...)`, then `accumulate(data)` and `finalize() -> dict` on the workflow it returns |

What carries over:

- **The factory gets the params model instance.** The backend validates the wire dict into the model and passes it; `attach_factory` checks the factory's `params` type hint against the spec when it is registered. This is amendment D, in use.
- **Parameters are read whole.** Every factory reads top-level parameters, `params.q_edges` or `params.beam_center`, and none sets a key from a field of a group on its own.
- **A model is already a sciline key.** The bifrost detector ratemeter sets `wf[DetectorRatemeterRegionParams] = params.region`, and a provider takes the model: the spec's type used as the workflow's key.
- **One model serves several parameters.** The bifrost elastic Q map has two axes, `axis1` and `axis2`, of one model; its custom variant combines `axis1` and `axis1_edges` into one key, `CutAxis1`. With a type per parameter, that is a provider with two inputs.
- **The choice of source is passed beside the params.** `source_name` and `aux_source_names`, the user's choice of detector and monitors, are arguments of the factory, and the factory also routes each stream to its key. In `ess.spec` they become parameters, and the routing stays apart from the params (amendment E).
- **Conversions are called in the factory**, such as `get_edges()`, where amendment F puts them in providers.
- **Outputs are matched by name, unchecked.** The factory's target keys map output names to keys; nothing checks the names against the outputs model, and no instance of the outputs model is made. The same holds in essapps today (cost 2). The outputs model holds the data itself (`IofQOutputs.i_of_q: sc.DataArray`), as this note writes outputs.
- **The workflow it returns is a driver.** `accumulate` and `finalize` are `push` and `outputs`, over one chunk per stream, by key, where essapps pushes a list of rows of one table. One generic driver serves every sciline factory; only workflows without sciline are written by hand.

## Where things live

| What | Written by | Lives in | Tested by |
|---|---|---|---|
| parameter and output types | shared types: their authors; the rest: the spec's author | the spec side: `ess.spec`, the spec's module (question 5) | validation tests of the spec |
| pipeline, conversions, driver | the workflow author | the workflow package | the package's own tests, with `check_arrival_and_order`, without the framework |
| adapter, values users never see | the instrument's glue, where the workflow does not take the spec's types | `essdispatchinstruments` (ADR 0007) | the checks when the binding is made |
| strategies: `PipelineBinding`, `DriverBinding`, `function_binding` | the framework | `ess.spec` | `ess.spec`'s tests, once |
| protocol | the framework | declared in `ess.spec.binding`, called by `ess.dispatch.backend` | essdispatch's tests |

## What changes for clients

- `bind` is a list of bindings, and a function is bound with `function_binding` (amendment B).
- A stage exists only over a `PipelineBinding`; an accumulator only over a `DriverBinding`. Either is refused, when it is made, over any other binding. An accumulator over `STITCH` needs its package's `StitchDriver`, and `STITCH` then has no stage.
- The README's promise that an empty table matches an optional group left out (`can_runs=[]` matches `can=None`) is dropped, with `test_a_can_left_out_is_an_empty_table_of_cans`. Whether a driver accepts an empty table is the driver's choice.

Records, the other symmetries, and the backend's promises to a binding do not change: when it calls which method, from how many threads, and that a push waits for the readers of the state before it.

## Questions

1. A: is "a binding is a strategy, called in the spec's terms" the right description, with "binding" the name of the whole?

> Simon:

2. B: should a binding take its spec, so that `bind` becomes a list?

> Simon:

3. B: three bindings, `PipelineBinding` (plain requests and stages), `DriverBinding` (plain requests and accumulators, over a function that makes a driver from the fixed values), and `function_binding` (plain requests); the framework needs no particular driver?

> Simon:

4. C: the bindings in `ess.spec`, and both keep-the-rows and `combine(op)` removed, so that only a package's driver accumulates? A spec bound to a driver has no stage, so a workflow that needs every row at once, such as `STITCH`, chooses between accumulating and tuning over fixed rows.

> Simon:

5. Types: the run types (`SampleRun`, `CanRun`) are shared by the spec's types and the workflow's keys. Today ess.reduce defines them, in a module whose import runs `ess/reduce/__init__.py` and loads the science stack. Do they move to the spec side, `ess.spec` or a light module of ess.reduce, with ess.reduce importing them; or do specs use run types of their own, and the instrument's glue adapts? And for a table that a pipeline takes whole: is its key the field's `list[...]` type, or a type that names the list?

> Simon:

6. Strategies: a driver push names one table and carries a list of its rows, `push(table, rows)`. The client's push may still name several tables, meaning that their rows enter one state; it reads, as the driver push did, as if the rows belonged together. Keep it, or make each client push name one table too, with states then visible between the pushes of a sample and its can?

> Simon:

7. Who writes the binding for an instrument: the workflow package, or the instrument's glue in `essdispatchinstruments`? With types as keys, a workflow package that takes the spec's types needs no glue but values users never see.

> Simon:

8. G: does `PipelineBinding` take a configuring function with declared keys, or does a workflow whose graph depends on a field use a driver or a function? Do you know of a real reduction, other than esslivedata's transmission mode and pulse stride, whose graph depends on a field?

> Simon:

9. A: "driver" means the client code that submits and pushes in the README, and the package's object that adds rows in sciline. Keep "driver" for sciline's meaning, as one vocabulary across sciline, ess.reduce, and essapps, and rename the README's (for example to *client loop*)?

> Simon:

10. One-row: should the convention, the multi-run spec naming each table as the single-run spec names the group, with the same row type, be recommended in the docs? Going further, deriving the single-run spec (`IOFQ_MULTI.single_run()`), or letting the backend accept a group where a table is expected, as a table of one row, would be the first thing that depends on the one-row symmetry. Worth it, or a step too far?

> Simon:
