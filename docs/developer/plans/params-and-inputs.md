# A spec has params and inputs

**Status: proposal**, for the second PR of issue #69.
It rewrites [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), section "Data fields: inputs are parameters", changes [ADR 0007](../adr/0007-packages-split-by-dependencies.md), and the [README](../README.md) sections from Specs and bindings to Stages and accumulators.
The binding amendments that wait for this split, D (model instances in the protocol), F (conversions are providers), G (the pipeline is made from the fixed values), and the spec's types as sciline keys, are in [binding-jobs.md](binding-jobs.md); this note says how they fit.

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

A multi-run spec is made from the single-run spec by naming the fields that become tables: `IOFQ.tabulate(tables={'sample': ('sample',), 'can': ('can',)})`.

## Terms

| Term | Meaning |
|---|---|
| literal | A value that has a JSON form and that the client knows: a number, a string, an enum, a model of such values. |
| data | A value given by reference, which the backend fetches: a file, an array, a model holding one, or any other output of a record, such as a fitted scale factor. |
| group | A model of literals as the value of a parameter. |
| parameter, input | A field of the params model, or of the inputs model. |
| name | The name of a parameter or an input. A spec's names are distinct across params and inputs. |
| table, column, row | A *table* is a set of list fields, its *columns*, of equal length, pushed together; each index is a *row*. A column is a parameter or an input. A table of one column is a list field. |
| request phase, call phase | The *request phase* is what the client and the backend do with a request before any data is read: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |

## Why

**Two phases.**
In the request phase a data value is a reference; in the call phase it is the data.
One model cannot type both: a data field typed as a reference gives the binding references, a union of reference and data is wrong in both phases ([ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) records this), and a field typed as data, with validation switched by phase, holds a reference where its type says `Path`.
Only data differ between the phases, so the split is along that line.
Params are the same in both phases; inputs exist as a model only in the call phase.

**Per-run values come with the data.**
The runs of one sum are reduced with one set of settings; a value that differs per run, such as a motor position, comes with the run's data or from an earlier result ([users.md](../../requirements/users.md)).
A sample's thickness is one value per sample, for every run summed for it ([sans.md](../../requirements/sans.md)).
A correction to a run's metadata applies to the run, for every reduction that reads it ([data.md](../../requirements/data.md)).
So inputs hold data only, and a literal never has to travel with a run.

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

class IofQInputs(BaseModel, arbitrary_types_allowed=True):
    sample: SampleFile
    can: CanFile | None = None
    beam_centre: BeamCentre | None = None

IofQ = NewType('IofQ', sc.DataArray)

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: Annotated[IofQ, ArraySpec(dims=('Q',), unit='dimensionless')]   # a hint, see Outputs

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, inputs=IofQInputs, outputs=IofQOutputs)
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables={'sample': ('sample',), 'can': ('can',)})
```

`IOFQ_MULTI` takes `sample: list[SampleFile]` with at least one element, `can: list[CanFile]` defaulting to empty, and `beam_centre`, `IofQParams`, and `IofQOutputs` as `IOFQ` does.
A workflow without data declares no inputs model and gets `NoInputs`, as one without configuration gets `NoParams`.

## The design

### Rules for the models

Checked when the spec is made:

- The names of params and inputs are distinct.
- Each parameter is a group, or a list of groups. Every value in the params model has a JSON form.
- Each input is a data value, possibly optional, a list of data values, or a collection (`dict[str, T]`). A data value is any type but a list or dict: a file (`Path`, or a `NewType` of it) or an object (a scipp type, a `NewType` of one, a model).
- A default of an input is `None`, or an empty list or dict.
- Each output is a data value, possibly optional, or a collection of them.

Stated in the docs:

- A model validator of the params or inputs model runs for plain requests and stage calls, not for an accumulator, which never makes a whole instance: its rows go to the driver one push at a time. A check of one value belongs to the value's type, as `BeamCentre` checks its data.
- A check across params and inputs, such as a transmission mode that requires a transmission run, is the binding's: it fails when the request runs. Validation in the request phase never proves that a workflow will succeed.

### Multi-run specs

`tabulate` makes a spec from another one by naming its tables, each a tuple of columns:

```python
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1,
                           tables={'sample': ('sample',), 'can': ('can',)})
```

Each column, a parameter or an input of type `T`, becomes a `list[T]` on its own side: with at least one element if it is required, and empty by default if it is optional.
The other fields are as in the spec tabulated.
A column belongs to one table.
The columns of a table have equal length; a push gives one row of each column at a time:

```python
SANS_MULTI = SANS.tabulate(name='sans-multi', version=1,
                           tables={'sample': ('sample', 'sample_transmission')})   # two columns
acc.push('sample', [{'sample': dataset(run=611), 'sample_transmission': dataset(run=601)}])

SLICES = SANS.tabulate(name='sans-slices', version=1, tables={'slices': ('time_range',)})
acc.push('slices', [TimeRange(start=0, stop=60)])   # a column of params: the same runs, sliced by time
```

A push into a table of one column gives values; into a table of several columns, dicts by column.
A list field that a spec declares itself is a table of one column.

With one row in each table, a request of the tabulated spec is a request of the spec tabulated.
So `ess.spec.testing.check_one_row(single, multi, params, inputs)` turns a request of `IOFQ` into its one-row request of `IOFQ_MULTI` and compares the outputs of the two bindings, for any package.

The models `tabulate` makes have no static type.
A client gives inputs as dicts of references in any case, and a driver gets values.
If a column is a parameter, the params model is made too, and a client passes a dict for that spec's params.

### Requests and records

A request is a spec, params, and inputs, given by keyword:

```python
record = client.submit(IOFQ, params=IofQParams(reduction=Reduction(bins=4)),
                       inputs={'sample': dataset(run=60339), 'beam_centre': centre.ref('centre')})
```

`params` is an instance or a dict of its fields, and may be left out when every parameter has a default.
`inputs` is a dict by name; each value is a reference, a list a list of references.
`record.request` holds both, in JSON's types, with defaults filled in.
Every reference of the request is in `record.request.inputs`.
`Request.inputs()`, which lists the records a request reads, is renamed `upstream()`.

A file is always given by reference, `dataset(path=...)` for a file named by its path: a string where a file is expected is refused.
The dataset source reads it, so access rules, corrections of the run's metadata, the identity in the record, and provenance hold for every file.

### Templates, stages, and accumulators

A template is a spec, params, inputs, and blanks, all but the spec given by keyword.
A blank is a whole parameter or a whole input, or a table, named by its name.
A template records on which side each blank is, from the `WorkflowSpec`, or, for a template made from a request, from the part of the request that holds the name; filling it then needs no spec.

```python
tune = client.stage(Template(IOFQ, inputs={'sample': dataset(run=60339)}, blanks=('reduction',)))
client.compute(tune, {'reduction': Reduction(bins=50)})          # tuning: a parameter left open

batch = Template(IOFQ, inputs={'can': dataset(run=60340)}, blanks=('sample',))
requests = apply(batch, samples, client.datasets)                # batch: each dataset fills 'sample'

acc = client.accumulator(Template(IOFQ_MULTI, params=IofQParams(reduction=Reduction(bins=100)),
                                  blanks=('sample', 'can')))
acc.push('sample', [dataset(run=611), dataset(run=612)])
```

An accumulator's blanks are tables, of params or of inputs.
`apply`, `Lookup`, and a rule's series fill a blank input with the dataset itself, `{blank: ref}`; their `dataset_field` goes.

### Validation

The request phase checks everything that can be checked without data, on the whole request.
The call phase checks each data value by its field once, when it is read.

| What | Request phase | Call phase |
|---|---|---|
| params | the params model, by the client when it builds the instance, and by the backend at submit | the values from the record |
| inputs of a plain request | names, required inputs, each value a reference or a list of them; each reference readable and fitting its field | `IofQInputs.model_validate` on the data |
| a template | each fixed parameter by its own field; fixed inputs as above | each fixed input by its field, once, when the stage or the driver is made |
| a stage call | the plain request it makes, as above | the inputs model on the fixed and the blank data |
| a push | literals by their column's type; each reference readable and fitting its column | each value by its column's type, at the push |
| a state, at a pin or a read | its plain request, as above | (none beyond the pushes) |

A reference *fits* a field when the type of the output it names is the field's type, or a subclass of it; a `NewType` fits only itself.
A dataset reference fits a file field.
So feeding an I(Q) into a beam-centre input is refused at submit, although both hold scipp objects.

### The inputs factory

The backend turns references into an inputs instance:

```python
factory = InputsFactory(IofQInputs, datasets, store)
factory.check(request_inputs)                  # request phase
inputs = factory.make(request_inputs)          # call phase: an IofQInputs
```

`check` walks the request alongside the model's fields: an inputs value is a reference or a list of references by its position, so the framework never decides from a value's shape whether it is a reference.
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
    def push(self, table: str, rows: Sequence[Any]) -> None: ...   # values, or dicts by column
    def outputs(self) -> O: ...
```

`make_stage` and `make_accumulator` take the fixed values by name, a parameter as its group and an input as its data.
A push gives the driver a parameter's rows as groups and an input's as data.
A function binding is `def iofq(params: IofQParams, inputs: IofQInputs) -> IofQOutputs`.

Each name's type is its sciline key: `Reduction`, `SampleFile`, `BeamCentre`; for `X | None` the key is `X`.
A type per input, such as `SampleFile` and `CanFile`, gives each input its own key.
`keys=` on `PipelineBinding` names the key of a workflow that takes keys of its own, such as `Filename[SampleRun]`:

```python
PipelineBinding(IOFQ, make_pipeline, keys={'sample': Filename[SampleRun], 'can': Filename[CanRun]})
```

### Outputs

An output that the spec declares optional is left out by returning `None`.
The backend stores each output value under its record and name, as today; `Record.outputs` holds the names.

An `ArraySpec` on an output is a *hint*: the dimensions and units a UI may expect before any data exist, to choose a plotter.
Nothing checks the data against it, and no reference is refused by it.
It is an `Annotated` marker that pydantic ignores; an instance of a pydantic model as `Annotated` metadata replaces the field's validation, so `ArraySpec` becomes a frozen dataclass.

### The serialized spec

`SerializedWorkflowSpec` holds `params_schema`, `inputs_schema`, `outputs_schema`, and the tables.
The params schema is the params model's JSON schema.
The inputs and outputs schemas are made by the framework from the field types: each value is "a reference to a `<type name>`", a list a list of them, with an output's hint.
A form shows params; a picker per input shows inputs.

### Values that differ per run

They come with the data:

- **In the run's file.** The workflow reads them as it reads the run, such as a motor position from the NeXus logs.
- **Corrected.** A correction of a run's metadata is held apart from the file, by the dataset source or the catalogue, and `dataset(run=611)` names the run with its corrections. Every reduction that reads the run sees them.
- **From an earlier result.** A record whose output maps runs to values, such as a table of thicknesses that a person filled in, is one input of the reduction. The driver looks up each run's value by the run's identity, which it reads from the file.

## What changes

- **essspec.**
  - `WorkflowSpec[P, I, O]` gains `inputs` and `tables`; `NoInputs`; `tabulate`; the model rules above.
  - `ess.spec.testing.check_one_row`, for any tabulated spec.
  - `DataField`, `Format`, `Array()`, `NexusFile`, `OpaqueFile`, `data_fields`, `ref_fields`, and literal-or-reference unions go. A file type is a `NewType` of `Path`; `ess.spec` may offer shared ones.
  - `ArraySpec` becomes a frozen dataclass, an output hint.
  - The binding protocol takes `P`, `I`, and returns `O` (binding-jobs.md, D).
- **essdispatch.**
  - Requests, records, templates, and the log hold `params` and `inputs`; a template records the side of each blank.
  - Reference walking reads `inputs` only, by position.
  - `InputsFactory`; the backend validates in the two phases above and calls the binding with instances.
  - A push into a table of several columns gives dicts by column; the backend splits each row to its sides.
  - A reference fits a field by type (`_agree`).
  - `submit` and `Template(spec, *, params, inputs, blanks)` take all but the spec by keyword; `Template.from_spec` goes.
  - `apply`, `Lookup`, and rules fill a blank input with the dataset; `dataset_field` goes.
  - `Request.inputs()` is renamed `upstream()`.
  - Dataset sources hold corrections of a run's metadata: not designed here.
- **ADR 0007.** Today an instrument's `specs.py` needs only essspec, so that GUIs and remote clients import it without scipp (0007:68, 0007:104). It changes to allow scipp on the spec side, for data types, and still keeps scippnexus, sciline, and ess.reduce out. scipp needs only numpy and imports in about 130 ms. The command-line tool and web frontends read `SerializedWorkflowSpec` and need neither.
- **ADR 0001.** Its section "Data fields: inputs are parameters" is rewritten in place as this split, with `tabulate`.
- **binding-jobs.md.** Rewritten for the protocol above; `RunInput` and its `sciline.Scope` go, since each input has a type of its own.
- **README.** Requests, templates, stages, accumulators, and multi-run specs as above.
- **Requirements.** [users.md](../../requirements/users.md) and [sans.md](../../requirements/sans.md): per-run values come with the data; [data.md](../../requirements/data.md): corrections of a run's metadata apply to the run.
- **Tests.** Every request is written with `inputs=`. A fake dataset source returns floats, so test specs type their inputs as the fake's values.

## Left for later

- **One spec for one run and for many.** The tables could be declared on the single-run spec itself, as the inputs it may take pushed, with no second spec. Its binding would then make both stages and drivers.
- **Loading a dataset as an object.** A dataset reference fits a file field; for an object field, the dataset source loads it when a workflow needs that.
- **A literal or a reference for one value**, such as a beam centre typed in or computed. The spec offers a parameter and an optional input, and a provider chooses; or the package offers two specs.
- **A param per dataset in a batch**, such as each sample's thickness in a batch of single-run requests. It comes with the run's data, or `apply` gains a function that makes the params of each request.
- **How a dataset source holds corrections**, and how a record names them.
- **Streams as inputs**, for esslivedata.

## Alternatives considered

- **One model, data fields typed as data, validation switched by phase.** Every validator runs on references in the request phase, and the client cannot build a typed request.
- **`DataField` annotations on data fields**, marking what the framework resolves, its format, and its array structure. Field types tell the framework the same, and the author writes no metadata for the framework.
- **Multi-run models written by hand**, `MultiIofQInputs` next to `IofQInputs`. Repeats the single-run model, and the one-row symmetry holds only by convention.
- **A literal per run, typed in with the sum**, such as a thickness per row. A correction of a run belongs to the run, not to one request that reads it: each request would repeat it, and two could disagree.
- **Groups of inputs**, pairing a run with its transmission run in a model. Columns of a table pair them; a group would make a model a group or a data value depending on its use.
- **Requests as one dict by name**, params and inputs together. Unambiguous, since the names are distinct, but the client cannot pass a typed `IofQParams`.

## Questions

1. Push shape: a table of one column takes values, `push('sample', [ref])`, and a table of several columns dicts by column. Or dicts always, so that a push keeps its shape when a column is added (which changes the spec's version anyway)?

> Simon:

2. A list field that a spec declares itself, without `tabulate`, is a table of one column. Keep this, for specs without a single-run form, or require `tabulate` for every table?

> Simon:
