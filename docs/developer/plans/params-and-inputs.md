# A spec has params and inputs

**Status: proposal**, for the second PR of issue #69.
It rewrites [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), section "Data fields: inputs are parameters", changes [ADR 0007](../adr/0007-packages-split-by-dependencies.md), and the [README](../README.md) sections from Specs and bindings to Stages and accumulators.
The binding amendments that wait for this split, D (model instances in the protocol), F (conversions are providers), G (the pipeline is made from the fixed values), and the spec's types as sciline keys, are in [binding-jobs.md](binding-jobs.md); this note says how they fit.

## In short

A spec declares three models:

| Model | Holds | At submit | When the workflow runs |
|---|---|---|---|
| params | configuration that the whole request shares: literals only | the values | the same values |
| inputs | data, and the literals of one data item, such as a run's thickness | references | the data the references name |
| outputs | the results | (none) | the data the workflow returns |

A *reference* names data that exists elsewhere: a dataset, an output of a record, or an output of an accumulator.
A request gives params by value and inputs by reference.
The backend reads each reference, validates the data, and calls the binding with both instances: `compute(params, inputs) -> outputs`.

Each type is correct wherever it exists.
A params instance is the same at submit and at the call, so the client builds it and a type checker checks it.
An inputs instance exists only at the call, so its data fields are typed as the data the workflow takes: `Path`, `sc.DataArray`.

## Terms

| Term | Meaning |
|---|---|
| data field | A field whose value is a file or an array, annotated with `DataField` (format, `ArraySpec`). In a request it holds a reference, at the call the data. `list[...]` and `dict[str, ...]` of one data type are a *collection*, still one data field. |
| literal | A field that is not a data field: a number, a string, an enum, a list of these. |
| flat model | A model whose fields are literals and data fields, never a model or a list of models. |
| group | A flat model as the value of a parameter or an input. |
| table | A list of a flat model as the value of a parameter or an input; each element is a *row*. |
| parameter | A field of the params model: a group or a table, of literals only. |
| input | A field of the inputs model: a data field, a group, or a table, each holding at least one data field. |
| name | The name of a parameter or an input. A spec's names are distinct across params and inputs. |
| request phase, call phase | The *request phase* is what the client and the backend do with references: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |

## Why

One params model cannot be right in both phases.
In the request phase a data field holds a reference; in the call phase it holds the data.
ADR 0001 types a data field as a reference, so a binding that takes the params model would get references, and the backend passes dicts instead.
A union of reference and data makes the model wrong in both phases, as ADR 0001 records.
A data field typed as the data, with validation switched by phase, keeps one model, but every validator of the params model then runs on references in the request phase, and the client cannot build a typed request.

Only data fields differ between the phases.
So the split is along that line: what the client knows when it asks goes into params, what the backend must fetch goes into inputs.
The models of the user stories ([conftest.py](../../../packages/essdispatch/tests/stories/conftest.py)) already fall on either side: `Run`, `Corrections`, and `Volume` hold only data; `Reduction`, `Scaling`, and `Position` only literals.

A literal of one data item goes with that item.
[users.md](../../requirements/users.md) requires that "parameters may differ per run within one sum", and ISIS batch tables carry a thickness per row ([sans.md](../../requirements/sans.md)).
A row pairs a run with its own literals and its own other runs, such as its transmission run, and arrives in one push.

> Simon: I think this is ok, but concerns about having to constantly restructure models around which params change per-run in an accumulation may arise. This may be void if we supported defining tables/rows as zipped groups, i.e., the table structure might be defined on top of the model, instead of the current `list[RowModel]` approach within the model. But maybe we should down this rabbit hole for now, since in any case the driver needs to support this.

esslivedata splits the same way, with streams for data: its params model holds configuration and never data, which arrive as streams.
The choice among streams is a literal there (`aux_sources`), or one job per stream (`source_names`).
This note does not design streams as inputs.

## The example

```python
class Reduction(BaseModel):                   # a params group
    bins: int = 2

class Run(BaseModel):                         # an input group of IOFQ, a row of IOFQ_MULTI
    file: NexusFile                           # Annotated[Path, DataField(Format.NEXUS)]
    thickness: float                          # a literal of this run, in cm; no default

BeamCentre = NewType('BeamCentre', sc.Variable)

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)

class IofQInputs(BaseModel, arbitrary_types_allowed=True):
    sample: Run
    can: Run | None = None
    beam_centre: Annotated[BeamCentre, DataField(Format.SCIPP, ArraySpec(dims=('xy',), unit='m'))] | None = None

class MultiIofQInputs(BaseModel, arbitrary_types_allowed=True):
    sample: list[Run] = Field(min_length=1)   # a table
    can: list[Run] = []
    beam_centre: ...                          # as in IofQInputs

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: Annotated[IofQ, DataField(Format.SCIPP, ArraySpec(dims=('Q',), unit='dimensionless'))]

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, inputs=IofQInputs, outputs=IofQOutputs)
IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, ..., params=IofQParams, inputs=MultiIofQInputs, outputs=IofQOutputs)
```

> Simon: Frame challenge: Do we still need the complex annotation approach at all? What is the purpose, is it just to auto-detect what needs to be resolved by the framework from references? I'd frame it like this: The workflow author wants to write a spec, with an `Inputs` model, which provides description and validation of data inputs. We should not force them to modify their model with "metadata" that only the framework needs. We can then ask ourselves what we need *on top* of the `Inputs` to be able to setup that mapping/bindings/resolver. In a sense, we can see this as a sort of `Inputs` factory: Given a dataset source (from run numbers, PIDs, ...) and existing workflow outputs in memory, we can setup a factory instance. A user request such as `client.submit(IOFQ, inputs={...})` then creates an `IofQInputs` from the `inputs` dict that contains data references. Also complication like the clumsy `beam_centre` annotation go away -- it could just be a regular `BeamCentre` model with its own validators?

The multi-run spec names each table as the single-run spec names the group, with the same row type.
A workflow without data declares no inputs model and gets `NoInputs`, as one without configuration gets `NoParams`.

## The design

### Rules for the models

Checked when the spec is made:

- The names of params and inputs are distinct.
- Each parameter is a group or a table of literals; the params model has no data field anywhere.
- Each input is a data field, a group, or a table, possibly optional; a group or a row holds at least one data field.
- A data field's default is `None`, an empty list or dict, or absent. A record holds references, and a default that is data has none.

Stated in the docs, not checked:

- A validator of the inputs model, or of its groups and rows, does not read a data field: it runs in the request phase on references.
- A check that reads several parameters, or several inputs, is a model validator, never a field validator reading `info.data`, since a template validates each fixed value by its own field.
- A literal of a data item that the result depends on, such as a thickness, has no default, so that a request that leaves it out is refused.

### Requests and records

A request is a spec, params, and inputs:

```python
record = client.submit(IOFQ, IofQParams(reduction=Reduction(bins=4)),
                       inputs={'sample': {'file': dataset(run=60339), 'thickness': 0.2},
                               'beam_centre': centre.ref('centre')})
```

`params` is an instance or a dict of its fields, and may be left out when every parameter has a default.
`inputs` is a dict by name; a group is a dict of its fields, a table a list of them, and each data field holds a reference.
`record.request` holds both, in JSON's types, with defaults filled in, and every reference of the request is in `record.request.inputs`.
`Request.inputs()`, which today lists the records a request reads, is renamed `upstream()`.

### Templates, stages, and accumulators

A template is a spec, params, inputs, and blanks.
A blank is a whole parameter or a whole input, named by its name.
A template records on which side each blank is, so that filling it needs no spec, also when its spec is only a `SpecId` and after `replace`.
It learns the side from the `WorkflowSpec`, or, for a template made from a request, from the part of the request that holds the name:

```python
tune = client.stage(Template(IOFQ, params=IofQParams(), inputs={'sample': {'file': dataset(run=60339), 'thickness': 0.2}},
                             blanks=('reduction',)))
client.compute(tune, {'reduction': Reduction(bins=50)})                # a stage call fills blanks by name

acc = client.accumulator(Template(IOFQ_MULTI, blanks=('sample', 'can')))
acc.push('sample', [{'file': dataset(run=611), 'thickness': 0.2},
                    {'file': dataset(run=612), 'thickness': 0.2}])
```

A stage may leave open parameters (tuning) or inputs (batch reduction with a fixed can).
An accumulator's blanks are tables.

**Batch and rules build rows.**
`apply`, `Lookup`, and a rule's series fill a blank input from a dataset.
A row holds the dataset and the literals of that run, so they take a function that makes the row from the dataset and its metadata, in place of `dataset_field`:

```python
requests = apply(Template(IOFQ, blanks=('sample',), inputs={'can': ...}), samples, client.datasets,
                 row=lambda ref, metadata: {'file': ref, 'thickness': metadata['thickness']})
```

The default, `lambda ref, metadata: {'file': ref}`, is what `dataset_field='file'` does.

### Validation

Everything that can be checked on references is checked in the request phase, on the whole request.
In the call phase, each value is checked by its own field once, when it is read.

| What | Request phase | Call phase |
|---|---|---|
| a plain request | params model; inputs model in request mode; each reference readable and fitting its field | the inputs model on the read data |
| a template | each fixed value by its own field, params and inputs alike | each fixed input by its own field, once, when the stage or the driver is made |
| a stage call | the plain request it makes, as above | the blank inputs by their own fields |
| a pushed row | the row model in request mode | the row model on the read data, at the push |
| a state, at a pin or a read | its plain request, as above, on references | (none beyond the pushes) |

*Request mode* is one model validated in two ways: the `DataField` annotation wraps each data field's validator, and the validation context chooses.
`IofQInputs.model_validate(inputs, context=REQUEST)` takes references; `model_validate(data)` takes data.
The JSON schema of the inputs model shows references.
A prototype validated a table of runs, an optional group, a `dict[str, ...]` collection, and an optional scipp field both ways, also through `create_model`, which `model_without` uses; it refused a reference in the call phase and data in the request phase, did not copy a `sc.Variable`, and produced the JSON schema of models with `sc.Variable` fields.
The instance from a request-mode check holds a reference where its type says `Path`; only the backend's submit check sees it, and the backend keeps its dump.

**A reference fits a field** when the format and the `ArraySpec` agree, as today, and the Python type of the output is the type of the input, or a subclass of it.
So a `DataArray` output feeding a `Variable` input is refused at submit.

**A repeated row** is one whose data fields equal those of an earlier row; its literals do not count, so the same run pushed twice with a corrected thickness is refused.

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
    def push(self, table: str, rows: Sequence[BaseModel]) -> None: ...
    def outputs(self) -> O: ...
```

`compute` takes the two instances.
`make_stage` and `make_accumulator` take the fixed values by name, each as its field's type, a group as its model and an input's data as the data, since a template is not a valid request.
A function binding is `def iofq(params: IofQParams, inputs: IofQInputs) -> IofQOutputs`.

Each name's type is its sciline key, a parameter's and an input's alike; for `X | None` the key is `X`.
Two names of one type need distinct keys: a `NewType` per name, such as `BeamCentre`, or `keys=` on `PipelineBinding`, which also serves a workflow that takes keys of its own:

```python
PipelineBinding(IOFQ, make_pipeline, keys={'sample': RunInput[SampleRun], 'can': RunInput[CanRun]})
```

`RunInput` is then a type of the workflow side; the spec side needs no `sciline.Scope`.

### Outputs

An output that the spec declares optional is left out by returning `None`.
The backend stores each output value under its record and name, as today; `Record.outputs` holds the names.
The outputs model's JSON schema shows references, through the same `DataField` hook.

### The serialized spec

`SerializedWorkflowSpec` holds `params_schema`, `inputs_schema`, and `outputs_schema`.
A form shows params; a picker, or a table with a picker per data column, shows inputs.

## What changes

- **essspec.**
  - `WorkflowSpec[P, I, O]` gains `inputs`; `NoInputs`; the model rules above.
  - `DataField` gets the request-mode validator, its serializer, and the JSON schema hook.
  - `NexusFile` and `OpaqueFile` become `Annotated[Path, DataField(...)]`; `Array(spec)` gives way to `Annotated[<scipp type>, DataField(Format.SCIPP, spec)]`, with the scipp type always written out.
  - `ref_fields` and literal-or-reference unions go.
  - The binding protocol takes `P`, `I`, and returns `O` (binding-jobs.md, D).
- **essdispatch.**
  - Requests, records, templates, and the log hold `params` and `inputs`; a template records the side of each blank.
  - Reference walking reads `inputs` only. `_check_cells`, `_reads`, and `_typed` take a bare data field as an input, besides groups and tables.
  - The backend validates in the two phases above and calls the binding with instances.
  - `Template(spec, params, inputs, blanks)` replaces `Template.from_spec`.
  - `apply`, `Lookup`, and rules take `row` in place of `dataset_field`.
- **ADR 0007.** Today it says that an instrument's `specs.py` needs only essspec, so that GUIs and remote clients import it without scipp (0007:68, 0007:104). It changes to allow scipp on the spec side, where a data field holds a scipp object, and still keeps scippnexus, sciline, and ess.reduce out. scipp needs only numpy and imports in about 130 ms. The command-line tool and web frontends read `SerializedWorkflowSpec` and need neither.
- **ADR 0001.** Its section "Data fields: inputs are parameters" is rewritten in place as the split, with the reasons above.
- **binding-jobs.md.** Rewritten for the protocol above; its two-run row (`Measurement(sample: Run, can: Run)`) becomes a row of two data fields.
- **Tests.** Every request in the tests is written with `inputs=`. A fake dataset source returns floats, so test specs type their data fields as the fake's values; EXPORT's text output is typed `str`, or returns a path.

## Left for later

- **The form chosen by the field's type.** A dataset source returns one form per dataset. The call phase refuses a form the field does not take, with the field's name; choosing a path or an array by the field's type comes when a workflow needs both.
- **A literal or a reference in one field**, such as a beam centre typed in or computed. The spec offers both, a parameter and an optional input, and a provider chooses; or the package offers two specs.
- **Typed construction of inputs by the client.** Inputs are dicts of references.
- **A reference to a whole `dict[str, ...]` output** given for a dict field. Not expressible today either; a reference names one element.
- **Streams as inputs**, for esslivedata.

## Alternatives considered

- **One params model, data fields typed as data, validation switched by phase.** Keeps one model, and the client passes dicts for everything that holds a reference. Every validator of the params model runs on references in the request phase, and the client never builds a typed request. The split confines request mode to inputs.
- **A request model derived from the inputs model**, each data field retyped as a reference, in place of request mode. A second class per model, rebuilt through `Optional`, `list`, and `dict`, with validators copied. Request mode is the `DataField` annotation alone.
- **Requests as one dict by name**, params and inputs together. The names are distinct, so it is unambiguous, but the client cannot pass a typed `IofQParams`.
- **A table of bare data fields**, `sample: list[NexusFile]`, as a table whose push is `push('sample', [ref])`. `list[NexusFile]` is already a collection, one data field; and adding a literal to the row later would change every push from `ref` to `{'file': ref}`.

## Questions

1. Per-run literals: input groups and rows hold the literals of their data item, such as a thickness, and batch and rules build rows with `row=` from the dataset and its metadata. Agreed?

> Simon:

2. Client API: `submit(spec, params, inputs=...)` and `Template(spec, params, inputs, blanks)`, with blanks, stage calls, and pushes by name. Agreed?

> Simon:

3. A check that reads both a parameter and an input, such as a transmission mode that requires a transmission run (esslivedata, `loki/specs.py`), has no model to live in. Options: a `check(params, inputs)` function on the spec, run in the request phase on references; or design such specs so that the input decides, a transmission run given or not; or leave it to the binding, which fails after the request is accepted. Which?

> Simon:
