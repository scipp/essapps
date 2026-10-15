# What a binding is: the workflow author's code, called with the spec's models

**Status: design note.**
It describes the binding part of the design in more depth than [README, What a binding provides](../README.md#what-a-binding-provides), [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), and [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md) do.
[params-and-inputs.md](params-and-inputs.md) says what a spec consists of; this note says how the code behind a spec is called.

## In short

A *binding* pairs a spec with the code that computes its requests.
The backend calls it with instances of the spec's models: `compute(params, inputs) -> outputs`.
A binding never sees a reference: the backend reads the data each reference names before it calls the binding.

The spec defines a type for each parameter, input, and output.
A workflow uses these types as its sciline keys, or the binding maps them to the workflow's own keys.
Either way, the spec imports nothing from the workflow.

The workflow author provides one of three things, the first two as a function that makes them from the fixed values, and each serves some of the client's actions:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline (`PipelineBinding`) | compute everything | a `sciline.Stage` cut at the blanks | refused |
| a driver (`DriverBinding`) | make a driver, push every table, read the outputs | refused | a driver that takes the client's pushes |
| a function (`function_binding`) | call it | refused | refused |

A *driver* is a stateful object with `push(table, columns)` and `outputs()`, in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245): the code that calls `sciline.Stage`s and pushes into sciline Accumulators.
It is written for one spec.
The workflow package chooses it: its own code, or a generic one behind an adapter, such as ess.reduce's `StreamProcessor`; the framework needs no particular driver.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
Of the three bindings, only `PipelineBinding` makes stages, since a stage over a function holds nothing.

## Terms

Defined in the [README](../README.md) and in [params-and-inputs.md](params-and-inputs.md); repeated here for reading this note alone.

| Term | Meaning |
|---|---|
| params, inputs, outputs | The three models of a spec. *Params* hold configuration, literals that the client gives by value. *Inputs* hold data, which a request gives by reference and the binding gets as the data. *Outputs* hold the results. |
| parameter, input, group | A *parameter* is a field of the params model, and an *input* a field of the inputs model. A parameter is a *group*, a model of literals such as `Reduction`, possibly optional, or a list of groups. |
| table, index, column, row | A *table* is a set of list fields of equal length, pushed together. Its *index* is the field that names it, such as `sample`; its other *columns* are attached to the index. Each position is a *row*. |
| template, fixed, blank | A *template* is a request with some parameters, inputs, or tables left out, the *blanks*; the others are *fixed*. A blank is a whole parameter or input, never a field of a group; a table is a blank by its index. |
| plain request | A request with every value given. |
| stage | A template the backend keeps for a client, called with values for its blanks. |
| accumulator, push | An *accumulator* is a template whose blanks are tables, kept by the backend for a client. Each *push* gives it rows of one table. *State n* is the accumulator after n pushes. |
| key | A sciline key, such as `Filename[SampleRun]`. |
| spec modules, workflow code | The *spec modules* define specs and their types: `ess.spec` for types that many specs share, and the module of a spec. They need no workflow package ([ADR 0007](../adr/0007-packages-split-by-dependencies.md)). The *workflow code* is the workflow package and the instrument's glue; it imports the spec modules. |
| symmetries | Changes a caller may make without changing the outputs: *caching* (a call through a stage against the plain request), *arrival* (rows pushed one at a time against all at once), *order* (the order of pushes into different tables), *one-row* (the multi-run spec with one row per table against the single-run spec). |

## The example

A toy SANS reduction, with a single-run spec and the multi-run spec made from it.
The spec modules define the types and the specs:

```python
SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)                # a NeXus file of an empty-can run

class Reduction(BaseModel):                       # a group
    bins: int = 2

IofQ = NewType('IofQ', sc.DataArray)

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)

class IofQInputs(BaseModel):
    sample: SampleFile
    can: CanFile | None = None

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, inputs=IofQInputs, outputs=IofQOutputs)
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))
```

`IOFQ_MULTI` takes `sample: list[SampleFile]` with at least one row and `can: list[CanFile]`, empty by default.
Each table has its index only.

The workflow code imports these types and uses them as keys.
`reduce_run` computes the numerator and denominator of one run (a `Parts`), and `subtract` divides each and subtracts the can, if there is one:

```python
SampleParts = NewType('SampleParts', Parts)
CanParts = NewType('CanParts', Parts)

def sample_parts(file: SampleFile, reduction: Reduction) -> SampleParts:
    return SampleParts(reduce_run(file, reduction.bins))

def can_parts(file: CanFile | None, reduction: Reduction) -> CanParts | None:
    return None if file is None else CanParts(reduce_run(file, reduction.bins))

def iofq(sample: SampleParts, can: CanParts | None) -> IofQ:
    return IofQ(subtract(sample, can))

def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    return sciline.Pipeline([sample_parts, can_parts, iofq])

single = PipelineBinding(IOFQ, make_pipeline)
```

The sample and the can are both NeXus files, but each input has a type of its own, so each is a key of its own.

## The design

### Protocol

The protocol is what the backend calls ([ess.spec.binding](../../../packages/essspec/src/ess/spec/binding.py)).
Every binding computes a plain request; a binding over a sciline pipeline also makes stages, and a binding over a driver also makes drivers:

```python
class Binding(Protocol[P, I, O]):
    spec: WorkflowSpec[P, I, O]
    def compute(self, params: P, inputs: I) -> O: ...                     # a plain request

class StageBinding(Binding[P, I, O], Protocol):                           # PipelineBinding
    def make_stage(self, fixed: Mapping[str, Any], blanks: Collection[str]
                   ) -> Callable[[Mapping[str, Any]], O]: ...             # called with the blanks' values

class AccumulatorBinding(Binding[P, I, O], Protocol):                     # DriverBinding
    def make_accumulator(self, fixed: Mapping[str, Any], blanks: Collection[str]
                         ) -> Driver[O]: ...

class Driver(Protocol[O]):
    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None: ...   # index and columns, of equal length
    def outputs(self) -> O: ...
```

`compute` gets instances of the spec's models.
`make_stage` and `make_accumulator` get values *by name*, params and inputs in one mapping: a parameter as its group, an input as its data, and each column of a table under its own name, as a list:

```python
single.make_stage({'sample': path_611, 'can': None}, blanks=('reduction',))
multi.make_accumulator({'reduction': Reduction(bins=2)}, blanks=('sample', 'can'))
multi.make_accumulator({'reduction': Reduction(bins=2), 'sample': [path_611]},
                       blanks=('can',))                    # the table 'sample' fixed, with one row
```

A stage or an accumulator is made from some values only, so there is no whole instance to pass.
A parameter or an input in neither `fixed` nor `blanks` takes its default from the spec; the binding fills it in.
When the stage or the driver is made, the binding checks that `fixed` and `blanks` do not overlap, that both name only parameters, inputs, and tables of the spec, and that each field without a default is in one of them; `make_accumulator` also checks that each blank is a table.
So a caller, such as a test, names what changes and leaves out what keeps its default.
A binding that reads a blank in the fixed part fails at once, since a blank is not in `fixed`.
The callable that `make_stage` returns gets the values of the blanks only, by name, as a driver's `push` gets only the new rows: the fixed values are in the stage, and nothing passes or checks them again.
`make_accumulator` gets the fixed values by name too, because a state may be an invalid request until it is read, such as one with sample rows and no can row yet.

A push gives the driver one table, by its index's name, with the index and each attached column as lists of equal length, never empty:

```python
driver.push('sample', {'sample': [path_611, path_612]})            # two rows of the table 'sample'
driver.push('sample', {'sample': [path_613], 'sample_geometry': [geometry]})   # SANS_MULTI: an attached column
```

A push never names two tables: rows of different tables have nothing to do with each other.
Values that belong to one run are columns of one table, attached to its index.

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
| plain request | `compute(params, inputs)` | `compute(params, inputs)` | `compute(params, inputs)` |
| stage | `make_stage(fixed, blanks)` at the first call, then the returned callable at each call | refused when the stage is made | refused when the stage is made |
| accumulator | refused when the accumulator is made | `make_accumulator(fixed, blanks)`, then `push` per client push and `outputs()` per state read | refused when the accumulator is made |

The protocol carries the backend's promises to the binding:

- `compute`, `make_stage`, and the callable `make_stage` returns may each be called from several threads at once, and none of them modifies the values it is given. A `PipelineBinding` sets values on a pipeline made for the request or the stage; a function is safe because its author promises it is pure. The callable may return the same object in several calls, so records made through one stage share what does not depend on the blanks.
- A driver's `push` and `outputs` are called from one thread at a time, and a push never while the outputs of the state before it are in use.
- A call through a stage gets the blanks' values of a valid plain request.

The client never sees the protocol.
The author of a function or a driver writes against it, in the spec's models; the author of a sciline pipeline does not.

### From references to data

A request gives each input by reference: a dataset, an output of a record, or an output of an accumulator.
The backend reads each reference in the call phase, when the data is needed, as the field's type asks: a local path for a file field, such as `SampleFile`, and the object for any other, such as a beam centre computed by another record.
It checks each value by its field's type once, when it is read, and builds the inputs instance of a plain request from them.
The fixed inputs of a stage or an accumulator are read field by field, the same way, when the stage or the driver is made, and the inputs of a push when the push is added.
So the form a workflow gets is chosen by the spec's types, not by the dataset source.
Records hold references, so history and provenance stay small.

### Types: the spec defines them, the workflow uses them

A field's type is the sciline key of its value, as written: `reduction: Reduction` sets the key `Reduction`, `sample: SampleFile` sets `SampleFile`, and `can: CanFile | None` sets `CanFile | None`.
sciline asks for the key `CanFile | None` where a provider takes `CanFile | None`; esssans does the same with types such as `NewType('DirectBeam', sc.DataArray | None)`.
An output is computed as the key of its type without `| None`: `iofq: IofQ` is the key `IofQ`.
A table sets the key of each of its columns to its list.
A blank is a whole parameter or input, so a `sciline.Stage` is cut at exactly the keys of the blanks.

Each parameter, input, and output has a type of its own within the spec, which `WorkflowSpec` checks.
Two inputs of one kind of data, such as the sample run and the can run, get a `NewType` each.
Two parameters that share a model, such as two axes of a Q map, do the same: `Axis1 = NewType('Axis1', QAxis)`.
A provider that needs two parameters, such as an axis and its edges, takes both keys.

The types live in the spec modules, and the workflow code imports them.
So the spec stays importable without the workflow package (ADR 0007), and a workflow package that takes these types is usable without the framework: `wf[Reduction] = Reduction(bins=2)`.
Where the types live, in `ess.spec`, in the spec's module, or in an instrument package's spec module such as `ess.loki.spec`, follows from who shares them.

**Conversions are providers.**
A provider takes a model apart or converts it inside the pipeline, as `sample_parts` reads `reduction.bins`.
For a real reduction, `ess.spec.parameters.QEdges` (start, stop, number of bins, scale, unit) is useful to any user of the workflow, with or without the framework.
esssans's key `QBins` holds the bin edges as a `sc.Variable`, and a provider makes them from the model:

```python
from ess.spec.conversions import edges_to_variable
from ess.spec.parameters import QEdges

def q_bins(edges: QEdges) -> QBins:
    return QBins(edges_to_variable(edges, dim='Q'))
```

A user without the framework sets either key: `wf[QEdges] = QEdges(...)`, or `wf[QBins] = edges` directly.
A conversion may instead sit in an adapter, such as a driver that converts before it calls the workflow; that is the workflow author's choice, not the framework's.

**Keys of the workflow's own.**
An existing workflow takes its own keys, such as `Filename[SampleRun]` or `BackgroundSubtractedIofQ`.
`keys=` then names the workflow's key of a parameter, an input, or an output:

```python
PipelineBinding(IOFQ, make_pipeline,
                keys={'sample': Filename[SampleRun], 'can': Filename[BackgroundRun],
                      'iofq': BackgroundSubtractedIofQ})
```

For a parameter or an input, `None` sets no key, for a field that `make_pipeline` uses itself, such as a list of runs that it maps over.
Where a value needs a conversion, the glue inserts a provider that takes the spec's type and returns the workflow's key, such as `q_bins` above.

**A parameter that chooses a source of data**, such as the detector bank or the incident monitor, is a parameter like any other, with its own type.

### Strategies

**A sciline pipeline** serves any spec: a table sets the key of each of its columns to its list, so the pipeline takes a table whole.
`PipelineBinding` takes a function that makes the pipeline from the fixed values, as `DriverBinding` takes one that makes a driver.
A plain request makes the pipeline from all values, sets each field's key to its value, and computes each output's key.
A stage makes the pipeline from the fixed values, and is a handle to a `sciline.Stage` cut at the keys of the blanks: the part that does not depend on the blanks is computed once, and each call computes the rest.
Each request and each stage gets a pipeline of its own, so the copy that makes them safe to run in parallel is in the author's code, where it can be seen.
Tuning a parameter over fixed runs, such as the bins over a hundred runs, needs a pipeline that takes the runs as a list; such a pipeline is staged as any other.
When a `PipelineBinding` is made, it checks that no two fields set one key.
An optional output is computed if the pipeline has a value or a provider for its key, and is `None` otherwise.

The graph may depend on the fixed values.
esslivedata's LOKI factory inserts a provider and adds the output `transmission_fraction` depending on `params.transmission.mode`.
A provider that takes `TransmissionOptions` can choose a value by its mode, but it cannot change which providers the graph has.
So `make_pipeline` gets the fixed values, by name, with defaults filled in:

```python
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    wf = base.copy()
    wf[CorrectForGravity] = CorrectForGravity(False)          # a value users never see
    if fixed['transmission'].mode is TransmissionMode.constant:
        wf[TransmissionFraction[SampleRun]] = sc.scalar(1.0)
    else:
        wf.insert(transmission_from_monitor)                  # the graph depends on a field
    return wf

sans = PipelineBinding(SANS, make_pipeline)
```

A field that changes the graph cannot be a blank: `make_pipeline` does not get the blanks, so it fails when it reads one, and the stage is refused when it is made.
A driver passes `pipeline` the fixed values it is made with: every value that is not a column of a table, defaults filled in.
So a field that is a column of the multi-run spec cannot shape the graph, as a blank cannot.
Over a graph that changes from call to call, a stage would have nothing to cache; such a field is varied by plain requests, or by a driver.
Since the key of each blank is its type, the `sciline.Stage` is cut at the blanks whatever `make_pipeline` inserts.
Values that users never see are set in this function, by the workflow package or by the instrument's glue.
An output that exists only for some values of a field, such as `transmission_fraction`, is an optional output of the spec.

The author of a pipeline promises that it is pure: it modifies no input, and returns the same results for the same inputs.

**A function** serves the same specs as a pipeline, for drafts in notebooks.
It takes the params and inputs instances and returns an outputs instance, so a type checker checks it against the spec.
It has no stage, since a stage over a function would hold nothing.

```python
def iofq_draft(params: IofQParams, inputs: IofQInputs) -> IofQOutputs: ...
draft = function_binding(IOFQ, iofq_draft)          # in place of `single`, while drafting
```

**A driver** serves a spec with tables.
It is a driver in the sense of sciline ADR 0003, holds everything stateful, and is written for one spec.
`DriverBinding` pairs the spec with a function that makes a driver from the fixed values that are not columns of a table:

```python
multi = DriverBinding(IOFQ_MULTI, IofQDriver)

driver = multi.make_accumulator({'reduction': Reduction(bins=2)}, blanks=('sample', 'can'))
driver.push('sample', {'sample': [path_101, path_102]})     # two rows of the table 'sample'
driver.push('can', {'can': [path_110]})
driver.outputs()                                            # -> IofQOutputs
```

`make_accumulator` makes the driver from the values that are not columns of a table, and pushes the rows of each table in `fixed`, one push per table.
A table without rows is not pushed, so a driver never gets a push of no rows.
So `compute(params, inputs)` of a `DriverBinding` is `make_accumulator` with every value fixed and no blanks, read once.
A push may carry several rows of its table, so that a driver can work on them together, such as loading runs in parallel.
A changed fixed value means a new driver.

For the toy SANS reduction, the package writes a driver that sums each run's numerator and denominator per table, and subtracts once at each read.
It calls the functions that the pipeline's providers call, so that one row in each table gives what the pipeline gives:

```python
class IofQDriver:
    def __init__(self, fixed: Mapping[str, Any]) -> None:   # the fixed values that are not columns
        self._bins = fixed['reduction'].bins
        self._sums: dict[str, Parts] = {}                    # per table

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        for file in columns[table]:                          # the index: a file per row
            parts = reduce_run(file, self._bins)
            old = self._sums.get(table)
            self._sums[table] = parts if old is None else old + parts

    def outputs(self) -> IofQOutputs:                        # raises without a sample row
        return IofQOutputs(iofq=IofQ(subtract(self._sums['sample'], self._sums.get('can'))))
```

A workflow that needs every row at once, such as `STITCH`, which fits scale factors over all runs, writes a driver that keeps its rows and computes the pipeline over all of them at each read:

```python
class StitchDriver:
    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[str, Any]) -> None:
        self._wf = pipeline.copy()
        self._wf[ReferenceFile] = fixed['reference']
        self._runs: list[RunFile] = []

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self._runs.extend(columns['runs'])         # STITCH has one table, indexed by 'runs'

    def outputs(self) -> StitchOutputs:
        self._wf[Runs] = Runs(self._runs)
        return StitchOutputs(stitched=self._wf.compute(Stitched))

stitch = DriverBinding(STITCH, functools.partial(StitchDriver, pipeline))
```

A driver over the graph of the single-run spec gets the graph and its keys from the single-run binding, `single.pipeline(fixed)` and `single.keys`, so that it sets no key by hand.
Whether recomputing every row at each read is acceptable is the workflow's choice, not the framework's.

A driver need not use sciline.
The framework calls `push` and `outputs` and nothing else, so it needs no particular driver.
A package writes its own, as above, or uses a generic one.
`StreamProcessor` of ess.reduce is one: `accumulate` is `push`, and `finalize` is `outputs`.
It takes one value per key, by key, so an adapter names the key of each column and pushes the rows one at a time.
Its context keys, rolling windows, and `clear()` stay unused, since each would break the arrival symmetry.
The framework itself does not import it ([ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md)); a package's driver may.

**The driver's promises.**
The backend does not check them; `check_arrival_and_order` tests the first two.

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a value it is given. A driver that adds in place copies the first contribution, so that adding never changes a value that a `sciline.Stage` holds or that another record returned.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec declares optional may be `None`.
- A driver may refuse a state, such as one with no sample row yet, by raising in `outputs()`; the read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.

### Use without the framework

The pipeline and the driver need no spec object, backend, or client:

```python
wf = make_pipeline({})                                     # the pipeline
wf[Reduction] = Reduction(bins=2)
wf[SampleFile] = path_101
wf[CanFile | None] = path_110
wf.compute(IofQ)

driver = IofQDriver({'reduction': Reduction(bins=2)})      # the driver
driver.push('sample', {'sample': [path_101]})
driver.push('can', {'can': [path_110]})
driver.outputs().iofq
```

| Level | Speaks | Used directly by |
|---|---|---|
| pipeline | keys, which are the spec's types or the workflow's own | the workflow package and its tests; notebooks without the framework |
| driver, function | the spec's names and models | the same |
| binding | the protocol | the backend |
| framework | requests and records | notebooks, apps, and services, through the client |

### Symmetries

| Symmetry | Kept by | Given | Its check proves |
|---|---|---|---|
| caching | the `sciline.Stage` cut at the blanks | a pure pipeline | `check_caching(binding, fixed, calls)`: a call through a stage gives the plain request. Mostly tests `ess.spec`. |
| arrival, order | the driver | its promises | `check_arrival_and_order(make_driver, fixed, tables)`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push per table gives. Both go through the driver, so it does not prove the outputs are right. |
| one-row | the package | that the driver computes what the pipeline computes | `check_one_row(single, multi, params, inputs)`: the binding of the multi-run spec, with one row in each table whose index is given and none where it is left out, gives the outputs of the binding of the single-run spec. The only check against an independent reference; more rows are the package's own tests. |

`check_arrival_and_order` takes a function that makes a driver, the fixed values by name, and the tables as `{index: {column: list}}`, so a package tests its driver without a spec.
The fixed values are every value that is not a column of a table, defaults included: the check does not fill them in.
`tabulate` makes the one-row symmetry a property of the two specs: `IOFQ_MULTI` has a table for each field of `IOFQ` it names, whose element type is the field's type, without `| None` for an index, so a pipeline over `IOFQ` and a driver over `IOFQ_MULTI` use the same keys.
A can left out of an `IOFQ` request is a table without rows in `IOFQ_MULTI`.

## Prior art: esslivedata

esslivedata solves the same problem for live data, and `ess.spec` is meant to hold its params models too.
Each instrument has `specs.py`, the params and outputs models, which the dashboard imports, and `factories.py`, the sciline glue, which only the backend loads.
Some factory code works around limits of the base workflows, so it shows what those workflows need, not what a binding must allow.

| | esslivedata |
|---|---|
| names to keys | code: a factory per spec sets each sciline key from the params by hand (`wf[QBins] = params.q_edges.get_edges()`), sets values users never see (`wf[CorrectForGravity] = CorrectForGravity(False)`), and changes the graph and the outputs by an enum field (`config/instruments/loki/factories.py`) |
| strategy | chosen by the same factory: nearly always a `StreamProcessorWorkflow`, given dynamic keys, accumulators, and target keys; other strategies are other implementations of its `Workflow` protocol |
| protocol | `factory(source_name=..., params=SansWorkflowParams(...), aux_source_names=...)`, then `accumulate(data)` and `finalize() -> dict` on the workflow it returns |

What carries over:

- **The factory gets the params model instance.** The backend validates the wire dict into the model and passes it; `attach_factory` checks the factory's `params` type hint against the spec when it is registered. `compute(params, inputs)` does the same with both models.
- **Parameters are read whole.** Every factory reads top-level parameters, `params.q_edges` or `params.beam_center`, and none sets a key from a field of a group on its own.
- **A model is already a sciline key.** The bifrost detector ratemeter sets `wf[DetectorRatemeterRegionParams] = params.region`, and a provider takes the model: the spec's type used as the workflow's key.
- **One model serves several parameters.** The bifrost elastic Q map has two axes, `axis1` and `axis2`, of one model; its custom variant combines `axis1` and `axis1_edges` into one key, `CutAxis1`. With a type per parameter, that is a provider with two inputs.
- **The choice of source is passed beside the params.** `source_name` and `aux_source_names`, the user's choice of detector and monitors, are arguments of the factory, and the factory also routes each stream to its key. Here a choice of source is a parameter, and data are inputs; streams as inputs are left for later.
- **Conversions are called in the factory**, such as `get_edges()`, where this note puts them in providers.
- **Outputs are matched by name, unchecked.** The factory's target keys map output names to keys; nothing checks the names against the outputs model, and no instance of the outputs model is made. Here a binding returns an instance of the outputs model, so pydantic checks the names, and a type checker checks a function binding. The outputs model holds the data itself (`IofQOutputs.i_of_q: sc.DataArray`), as here.
- **The workflow it returns is a driver.** `accumulate` and `finalize` are `push` and `outputs`, over one chunk per stream, by key, where a push here gives the columns of one table. One generic driver serves every sciline factory; only workflows without sciline are written by hand.

## Where things live

| What | Written by | Lives in | Tested by |
|---|---|---|---|
| parameter, input, and output types | shared types: their authors; the rest: the spec's author | the spec modules: `ess.spec`, the spec's module, or an instrument package's spec module | validation tests of the spec |
| pipeline, conversions, driver | the workflow author | the workflow package | the package's own tests, with `check_arrival_and_order`, without the framework |
| `keys=`, values users never see | the instrument's glue, where the workflow does not take the spec's types | `essdispatchinstruments` (ADR 0007) | the checks when a stage or a request makes its pipeline |
| strategies: `PipelineBinding`, `DriverBinding`, `function_binding` | the framework | `ess.spec` | `ess.spec`'s tests, once |
| protocol | the framework | declared in `ess.spec.binding`, called by `ess.dispatch.backend` | essdispatch's tests |
| reading references into data | the framework | `ess.dispatch` | essdispatch's tests |

Who writes a binding for an instrument is a spectrum.
A workflow that takes its own keys needs glue in `essdispatchinstruments`, with `keys=` and providers that convert.
As conversions move into the workflow package's providers, and its keys become the spec's types, the glue shrinks to the values users never see.
