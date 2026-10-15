# A spec has params and inputs

**Status: proposal**, for the second PR of issue #69.

It rewrites [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec) in place, since that ADR decides what a spec consists of.
It also changes [ADR 0007](../adr/0007-packages-split-by-dependencies.md) and the [README](../README.md) sections from Specs and bindings to Stages and accumulators.
[binding-jobs.md](binding-jobs.md) describes bindings with values passed as dicts; with this split, a binding gets model instances, conversions such as bin edges are providers of the workflow, and a pipeline is made from the fixed values. This note says how these fit.
Terms of binding-jobs.md used here: a *stage* is a template that the backend keeps for a client and calls with values for its blanks; an *accumulator* is a template whose blanks are tables, filled by *pushes*; a *driver* is the object, written by the workflow package, that an accumulator pushes into; a *pin* takes the state of an accumulator after the pushes so far.

## In short

A spec declares three models:

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | configuration: literals only | the values | the same values |
| inputs | data only | a reference per value | the data the references name |
| outputs | results | (none) | the data the workflow returns |

A *reference* names data that exists elsewhere: a dataset, an output of a record, or an output of an accumulator.
A request gives params by value and inputs by reference.
The client builds and validates a params instance; a type checker checks it.
The backend reads each reference and builds the inputs instance from the data, so every field of it holds the type it declares.
The binding gets both instances: `compute(params, inputs) -> outputs`.

The models are ordinary pydantic models.
They carry no annotations for the framework: what the framework needs, it reads from the field types.

A multi-run spec is made from the single-run spec by naming the fields that become tables: `IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))`.

## Terms

| Term | Meaning |
|---|---|
| literal | A value that has a JSON form and that the client knows: a number, a string, an enum, a model of such values. |
| data value | A value that the backend fetches, given by reference: a file, an array, a model holding one, or any other output of a record, such as a fitted scale factor. |
| group | A model of literals as the value of a parameter. |
| parameter, input | A field of the params model, or of the inputs model. A parameter's *side* is params, an input's is inputs. |
| name | The name of a parameter or an input. A spec's names are distinct across params and inputs. |
| table, index, column, row | A *table* is a set of list fields of equal length, pushed together. Its *index* is the field that names it, such as the run; its other *columns* are attached to the index, such as a correction of each run. Index and columns are parameters or inputs. Each position is a *row*. As in a pandas DataFrame, the index values of a table are distinct. |
| blank | A parameter, an input, or a table (named by its index) that a template leaves open. |
| fits | A reference fits a field when the type of the data it names can be the field's value (see Validation). |
| request phase, call phase | The *request phase* is what the client and the backend do with a request before any data is read: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |

## Why

**Two phases.**
In the request phase a data value is a reference; in the call phase it is the data.
One model cannot type both.
A field typed as a reference gives the binding references.
A union of reference and data is wrong in both phases, as [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) records.
A field typed as data, with validation switched by phase, holds a reference where its type says `Path`.
Only data differ between the phases, so the split is along that line.
Params are the same in both phases; inputs exist as a model only in the call phase.

**Values that differ per run come with the run.**
The runs of one sum are reduced with one set of settings ([users.md](../../requirements/users.md)).
A value that differs per run, such as a motor position, is in the run's file.
A value the file lacks or has wrong is given next to the run, as a column of the run's table, so the request, and its record, holds it.
So inputs hold data only, and a literal of one run is a parameter attached to it.

**esslivedata splits the same way.**
Its params model holds configuration and never data, which arrive as streams.
A value that changes during a stream, such as a motor position, arrives on a stream of its own and is set as a *context key* of `StreamProcessor`, never taken from the params.
In a file, such values are part of the run's data.

## The example

The spec side, which needs no workflow package:

```python
class Reduction(BaseModel):                       # a group
    bins: int = 2

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)

class BeamCentre(BaseModel, arbitrary_types_allowed=True):
    value: sc.Variable

    @field_validator('value')
    @classmethod
    def _xy(cls, value: sc.Variable) -> sc.Variable: ...   # checks the data, in the call phase

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)

class IofQInputs(BaseModel):
    sample: SampleFile
    can: CanFile | None = None
    beam_centre: BeamCentre | None = None

IofQ = NewType('IofQ', sc.DataArray)

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, inputs=IofQInputs, outputs=IofQOutputs)
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))
```

`IOFQ_MULTI` takes `sample: list[SampleFile]` with at least one element, `can: list[CanFile | None]` defaulting to empty, and `beam_centre`, `IofQParams`, and `IofQOutputs` as `IOFQ` does.
A workflow without data declares no inputs model and gets `NoInputs`, as one without configuration gets `NoParams`.

## The design

### Rules for the models

Checked when the spec is made:

- The names of params and inputs are distinct, and so are their types: each parameter and each input has a type of its own within the spec, since the type is the sciline key. Two parameters of one model, such as two axes of a Q map, get a `NewType` each: `Axis1 = NewType('Axis1', QAxis)`.
- Each parameter is a group, or a list of groups. Every value in the params model has a JSON form.
- Each input is a data value, possibly optional, a list of data values, or a collection (`dict[str, T]`). A data value is any type but a list or dict: a file (`Path`, or a `NewType` of it) or an object (a scipp type, a model, a `NewType` of either).
- A default of an input is `None`, or an empty list or dict.
- Each output is a data value, possibly optional, or a collection of them.

Stated in the docs:

- A check of one value belongs to the value's type, as `BeamCentre` checks its data. The params and inputs models have no field validators.
- A model validator of the params model runs in the request phase of every request. A model validator of the inputs model runs in the call phase of a plain request only: a stage holds its fixed data in the form its binding made of them, and an accumulator's rows go to the driver one push at a time, so neither makes a whole inputs instance. A state's request, submitted again as a plain request, runs them.
- A check across params and inputs, such as a transmission mode that requires a transmission run, is the binding's: it fails when the request runs. Validation in the request phase never proves that a workflow will succeed.

### Multi-run specs

`tabulate` makes a spec from another one by naming the indexes of its tables, each with the columns attached to it:

```python
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))

SANS_MULTI = SANS.tabulate(name='sans-multi', version=1,
                           tables={'sample': ('sample_geometry',), 'can': ()})   # a parameter attached to each sample run
FREIA_MULTI = FREIA.tabulate(name='freia-multi', version=1,
                             tables={'angle': ('direct_beam',)})                 # a direct beam per angle
SLICES = SANS.tabulate(name='sans-slices', version=1, tables=('time_range',))   # the same runs, sliced by time
```

Each field named becomes a `list` of its type as written, on its own side: `sample: SampleFile` becomes `list[SampleFile]`, `sample_geometry: SampleGeometry | None` becomes `list[SampleGeometry | None]`.
A table whose index is required has at least one row; one whose index is optional is empty by default.
The other fields, the outputs model, and the field validators of the types are as in the spec tabulated; its model validators are not carried over.
`SANS_MULTI` takes a geometry per sample run, given where the file's is missing or wrong, and FREIA a direct beam with the slit settings of each angle ([reflectometry.md](../../requirements/reflectometry.md)).

A push gives the index and its columns by name, as lists of equal length; a column left out is `None` in each row:

```python
acc.push(sample=[dataset(run=611), dataset(run=612)])
acc.push(sample=[dataset(run=613)], sample_geometry=[SampleGeometry(thickness=0.2)])
acc.push(time_range=[TimeRange(start=0, stop=60)])                 # an index on the params side
```

A push names one table, through its index; a push of an attached column without its index is refused, and so is an index value the table already holds.
A blank names a table by its index, and the driver gets the index's name as the table's.

`tabulate` fills the field `tables` of `WorkflowSpec`.
A spec whose outputs differ from those of its single-run form, such as a stitch of all angles, or that has no single-run form, declares its tables itself: `WorkflowSpec(..., tables={'angle': ()})`.
A list field makes a table only when `tables` names it; otherwise it is a value like any other, and the spec has no accumulator over it.

With one row in each table, a request of the tabulated spec is a request of the spec tabulated.
So `ess.spec.testing.check_one_row(single_binding, multi_binding, params, inputs)` turns a request of `IOFQ` into its one-row request of `IOFQ_MULTI`, computes both, and compares the outputs, with `scipp.testing.assert_allclose` for scipp objects.

The models `tabulate` makes have no static type.
A client gives inputs as dicts of references in any case, and a driver gets columns.
If a column is a parameter, the params model is made too, and a client passes a dict for that spec's params.

### Requests and records

A request is a spec, params, and inputs, given by keyword:

```python
record = client.submit(IOFQ, params=IofQParams(reduction=Reduction(bins=4)),
                       inputs={'sample': dataset(run=60339), 'beam_centre': centre.ref('centre')})
```

`dataset(run=...)` names a dataset; `record.ref(name)` names an output of a record.
`params` is an instance or a dict of its fields, and may be left out when every parameter has a default.
`inputs` is a dict by name; each value is a reference, a list a list of references.
`record.request` holds both, in JSON's types, with defaults filled in, and every reference of the request is in `record.request.inputs`.
So `Request.inputs()`, which lists the records a request reads, is renamed `upstream()`.

A file is always given by reference, `dataset(path=...)` for a file named by its path: a string where a file is expected is refused.
The dataset source reads it, so access rules, the identity in the record, and provenance hold for every file.

In the JSON form of a request, a reference carries the key `$ref`, which no field name can be.
The framework finds references in a request, a record, or the log without the spec, as a remote client that holds only the serialized spec must.

### Templates, stages, and accumulators

A template is a spec, params, inputs, and blanks, all but the spec given by keyword.
A template, a stage call, and a fill take values by side, as a request does:

```python
tune = client.stage(Template(IOFQ, inputs={'sample': dataset(run=60339)}, blanks=('reduction',)))
client.compute(tune, params={'reduction': Reduction(bins=50)})   # tuning: a parameter left open

batch = Template(IOFQ, inputs={'can': dataset(run=60340)}, blanks=('sample',))
requests = apply(batch, samples, client.datasets)                # batch: each dataset fills the input 'sample'

acc = client.accumulator(Template(IOFQ_MULTI, params=IofQParams(reduction=Reduction(bins=100)),
                                  blanks=('sample', 'can')))
acc.push(sample=[dataset(run=611), dataset(run=612)])
```

An accumulator's blanks are tables, indexed by a parameter or an input.
`apply`, `Lookup`, and a rule's series fill a blank input with the dataset itself; their `dataset_field` goes.
A batch with parameters that differ per request is a list of requests, each with its own params.

### Validation

The request phase checks what can be checked without data, on what is new.
The call phase checks each data value by its field once, when it is read.

| What | Request phase | Call phase |
|---|---|---|
| a plain request | the params model; names and required inputs; each value a reference or a list of them, readable and fitting its field | the inputs model on the data |
| a template | each fixed value by its own field; each fixed reference readable and fitting | each fixed input by its field, once, when the stage or the driver is made |
| a stage call | the params model on the fixed and the given parameters; each given reference readable and fitting | each given input by its field |
| a push | each literal by its column's type; each reference readable and fitting its column; no index value twice | each data value by its column's type, at the push |
| a state, at a pin or a read | the rules on whole tables, such as a minimum length | (none) |

A data value that fails its check at a push stops the accumulator, as any failing push does.

**Fit.** A reference fits a field when the output's type, with its `NewType`s removed one by one, reaches the field's type or a subclass of it; or when the field's type, with its `NewType`s removed, reaches the output's type or a superclass of it.
So an `IofQ` output fits a field typed `sc.DataArray`, as an export takes it; a `Parts` output fits fields typed `SampleParts = NewType('SampleParts', Parts)` and `CanParts`, as a sum takes them; and an `IofQ` does not fit a `Transmission` field, two `NewType`s of `sc.DataArray`.
A dataset reference fits a file field.

### The inputs factory

The backend turns references into an inputs instance:

```python
factory = InputsFactory(IofQInputs, datasets, store)
factory.check(request_inputs)                  # request phase
inputs = factory.make(request_inputs)          # call phase: an IofQInputs
```

`check` walks the request alongside the model's fields: an inputs value is a reference or a list of references by its position.
`make` reads each reference, as the field's type asks: a local path for a file field, the object for any other.
The fixed inputs of a stage or an accumulator are made field by field, the same way.

### The binding protocol

```python
class Binding(Protocol[P, I, O]):
    spec: WorkflowSpec[P, I, O]
    def compute(self, params: P, inputs: I) -> O: ...

class StageBinding(Binding[P, I, O], Protocol):
    def make_stage(self, fixed: Mapping[str, Any], blanks: Collection[str]
                   ) -> Callable[[Mapping[str, Any]], O]: ...

class AccumulatorBinding(Binding[P, I, O], Protocol):
    def make_accumulator(self, fixed: Mapping[str, Any], blanks: Collection[str]) -> Driver[O]: ...

class Driver(Protocol[O]):
    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None: ...   # index and columns, of equal length
    def outputs(self) -> O: ...
```

`make_stage` and `make_accumulator` take the fixed values by name, a parameter as its group and an input as its data.
A push gives the driver one table by its index's name: a parameter's values as groups, an input's as data.
A function binding is `def iofq(params: IofQParams, inputs: IofQInputs) -> IofQOutputs`.

A field's sciline key is its type as written: `Reduction`, `SampleFile`, `CanFile | None`.
sciline asks for the key `CanFile | None` where a provider takes `CanFile | None`; esssans does the same with types such as `NewType('DirectBeam', sc.DataArray | None)`.
`keys=` on `PipelineBinding` names the key of a parameter, an input, or an output of a workflow that takes keys of its own; `None` sets no key, for a field that `make_pipeline` uses itself, such as a list of runs that it maps over:

```python
PipelineBinding(IOFQ, make_pipeline,
                keys={'sample': Filename[SampleRun], 'can': Filename[BackgroundRun],
                      'iofq': BackgroundSubtractedIofQ})
```

### Outputs

An output that the spec declares optional is left out by returning `None`.
The backend stores each output value under its record and name; `Record.outputs` holds the names.

### The serialized spec

`SerializedWorkflowSpec` holds `params_schema`, `inputs_schema`, `outputs_schema`, and the tables.
The params schema is the params model's JSON schema.
The inputs and outputs schemas are made by the framework from the field types: each value is "a reference to a `<type name>`", a list a list of them.
A form shows params; a picker per input shows inputs.

## What changes

- **essspec.**
  - `WorkflowSpec[P, I, O]` gains `inputs` and `tables`; `NoInputs`; `tabulate`; the model rules above.
  - `ess.spec.testing.check_one_row`, for any tabulated spec.
  - `DataField`, `Format`, `Array()`, `ArraySpec`, `NexusFile`, `OpaqueFile`, `data_fields`, `ref_fields`, and literal-or-reference unions go. A file type is a `NewType` of `Path`; `ess.spec` may offer shared ones.
  - References serialize with `$ref`.
  - The binding protocol takes `P` and `I`, and returns `O`.
- **essdispatch.**
  - Requests, records, templates, and the log hold `params` and `inputs`. Stage calls and fills take `params=` and `inputs=`.
  - `InputsFactory`; the backend validates in the two phases above and calls the binding with instances.
  - `Accumulator.push(**columns)` takes one table by its index and columns; the backend splits them to their sides and refuses an index value the table holds. A table is what the spec's `tables` declares, never a list field alone.
  - A reference fits a field by type (`_agree`).
  - `submit` and `Template(spec, *, params, inputs, blanks)` take all but the spec by keyword; `Template.from_spec` goes.
  - `apply`, `Lookup`, and rules fill a blank input with the dataset; `dataset_field` goes.
  - `Request.inputs()` is renamed `upstream()`.
- **ADR 0007.** Today an instrument's `specs.py` needs only essspec, so that GUIs and remote clients import it without scipp (0007:68, 0007:104). It changes to allow scipp on the spec side, for data types, and still keeps scippnexus, sciline, and ess.reduce out. scipp needs only numpy and imports in about 130 ms. The command-line tool and web frontends read `SerializedWorkflowSpec` and need neither.
- **ADR 0001.** Rewritten in place. "Data fields: inputs are parameters" becomes "Params and inputs", opening with the two phases; "One params model per workflow", "Params are groups and tables of flat models", "Shared parameter vocabulary, scipp-free", and "Convergence with esslivedata" follow it, with `tabulate` and the index in the section on tables.
- **binding-jobs.md.** Rewritten for the protocol above; `RunInput` and its `sciline.Scope` go, since each input has a type of its own.
- **README.** Requests, templates, stages, accumulators, and multi-run specs as above.
- **Requirements.** [users.md](../../requirements/users.md): per-run values come with the run, in its file or next to it; [data.md](../../requirements/data.md): users correct missing or wrong metadata.
- **Tests.** Every request is written with `inputs=`. A fake dataset source gives each test file as a path to a small file the test writes.

## Left for later

- **One spec for one run and for many.** The tables could be declared on the single-run spec itself, as the inputs it may take pushed, with no second spec. Its binding would then make both stages and drivers.
- **A raw run or a reduced result for one input**, such as a direct-beam function from a file or from a record, or a beam centre typed in or computed. A package offers a spec for each form.
- **Loading a dataset as an object.** A dataset reference fits a file field; for an object field, the dataset source loads it when a workflow needs that.
- **Filling params per dataset in `apply`**, such as each sample's thickness in a batch. Until then, such a batch is a list of requests.
- **Corrections held by the dataset source**, so that every reduction of a run sees them. Until then, a correction is a column next to its run.
- **Streams as inputs**, for esslivedata.

## Alternatives considered

- **One model, data fields typed as data, validation switched by phase.** Every validator runs on references in the request phase, and the client cannot build a typed request.
- **`DataField` annotations on data fields**, marking what the framework resolves, its format, and its array structure. Field types tell the framework the same, and the author writes no metadata for the framework.
- **Multi-run models written by hand**, `MultiIofQInputs` next to `IofQInputs`. Repeats the single-run model, and the one-row symmetry holds only by convention.
- **Tables named apart from their fields.** A second namespace, which blanks, pushes, and the driver would mix with the names of fields. The index names its table instead.
- **Groups of inputs**, pairing a run with its direct beam in a model. Columns of a table pair them; a group would make a model a group or a data value depending on its use.
- **Requests as one dict by name**, params and inputs together. Unambiguous, since the names are distinct, but the client cannot pass a typed `IofQParams`.
- **An `ArraySpec` hint on outputs**, the dimensions and units a UI may expect before any data exist. No requirement needs it yet; it can come back as an `Annotated` marker.
