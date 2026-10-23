# ADR 0001: Minimal implementation-independent workflow specifications

- Status: proposed
- Deciders: Simon
- Date: 2026-09-14

## Context

Three mechanisms currently describe workflow interfaces to users, with
overlapping purpose and no shared shape:

- `ess.reduce.parameter` / `ess.reduce.workflow`: per-sciline-key `Parameter`
  dataclasses in a global registry, with parameters discovered by walking the
  pipeline graph from selected outputs. Drives the ipywidgets GUI.
- `ess.livedata.config.workflow_spec.WorkflowSpec`: one pydantic params model
  and one pydantic outputs model per workflow, driving the live-data dashboard.
- `ess.nmx.configurations`: standalone pydantic models for batch reduction.

Consolidation was analyzed at length in
[scipp/ess#653](https://github.com/scipp/ess/issues/653) and
[scipp/esslivedata#889](https://github.com/scipp/esslivedata/issues/889). Two
earlier attempts stalled, both for the same reason: scope. A universal
`ess.schemas` catalog ("all workflows for all instruments, imported by
everyone") turned shared conventions into a cross-team release-coordination
problem; a rewrite of the essreduce widget layer
([scipp/ess#689](https://github.com/scipp/ess/pull/689)) kept sciline keys,
workflow factories, and widget concerns inside the spec, so the spec could not
outlive or precede any particular implementation.

The goal is the minimal layer that lets a *generic* user interface —
ipywidgets, a web dashboard, a command-line tool, or a GUI application — be
generated from a workflow description alone. Compute is deliberately abstracted
away: the same spec must make sense whether the workflow runs as a local
sciline pipeline, behind a web service, or as a cluster job. Compute is not
part of this work, but it shapes the design: nothing implementation-bound may
appear in the spec.

## Decision

A new package `essspec`, imported as `ess.spec`, defines the spec layer. It is
developed in scipp/essapps while it changes with the framework (see ADR 0007 of
scipp/essapps, `docs/developer/adr/0007-packages-split-by-dependencies.md`).
Its only dependency beyond the standard library is pydantic. The pieces that
need scipp or sciline are kept in two submodules, `conversions` and `pipeline`,
which the package does not import. A module that defines specs may import scipp
for the types of its data values, but never sciline or a workflow package (ADR
0007 of scipp/essapps).

The package also holds the *bindings*, the other half of the contract with
workflow packages: a binding is what the backend calls to compute a spec's
requests, made by the workflow author from a pipeline, a driver, or a function,
each for one spec (`ess.spec.binding`). `ess.spec.testing` checks the
symmetries that a binding keeps.

### The spec is pure interface: no factory, no keys, no registry

`WorkflowSpec` holds identity (`name`, `version`), display metadata (`title`,
`description`, both mandatory), a params model, an outputs model, its tables,
and an optional `code_revision`. Nothing else. In particular it holds *no*
workflow factory and *no* keys of a workflow: a spec describes *what a user
gives and what they get back*, not how it is computed. A `PipelineBinding`
uses each field's type as its sciline key, or the key that its `keys=` names,
so the spec imports nothing from the workflow. A caller of a remote service
that holds the same spec needs no binding at all. This is what keeps the spec
valid across local, service, and cluster execution.

One requirement follows for workflow packages: the module that defines a spec
must be importable without importing the workflow code. A service then loads
and validates every spec it knows without importing sciline pipelines or
instrument code, and the process that binds a spec to code is the only one that
pays for that import. esslivedata already separates the two for this reason.

How specs are enumerated (module-level tuples, entry points, esslivedata's
per-instrument registration) is out of scope. Any mechanism works against the
same spec type; prescribing one here would recreate the catalog problem that
sank the `ess.schemas` plan. Entry points split by role, specs in one group and
factories in another under the same name, are the natural fit for the
requirement above and are expected to become the convention once the first
service adopts this spec.

### Params inline or by reference

A request goes through two phases. The *request phase* is what the client and
the backend do with a request before any data is read: validate, log, record,
pin. The *call phase* starts when the backend reads data for the binding.

A field of the params model is a *parameter*. A request gives each value in one
of two forms: *inline* or *by reference*. An *inline value* is the value itself
in the request; the record holds its JSON form. A *reference* names data that
exist elsewhere: a dataset, an output of a record, or an output of an
accumulator. In the request phase a value given by reference is the reference;
in the call phase it is the data. The params model types the call phase only.
The backend reads each reference and builds the params instance from the inline
values and the data, so every field of it holds the type it declares. The
binding gets that instance: `compute(params) -> outputs`.

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | everything the workflow takes: settings, files, data | each value given inline or by reference | the values, and the data the references name |
| outputs | results | (none) | the data the workflow returns |

Which form a value takes is the request's choice, at each *place*, since the
same value is typed in by one user and computed by another: a beam centre is
typed in, or found by a first reduction; the time slices of a slicing
reduction are typed in, or found from a log. A place is the value of a
parameter, an element of a list, or a field of a group, at any depth, such as
`sample.run` or `reduction.mask`. The type at the place decides which forms it
accepts, with `| None` removed. A type has a *JSON form* when it is not a
file, holds no file, and pydantic makes a JSON schema of it with its model's
config. A value *round-trips* when its JSON form reads back as the value.

| The type at a place | Example | Inline | By reference |
|---|---|---|---|
| a file: `Path` or a `NewType` of it | `SampleFile` | no | to a dataset |
| a group | `Reduction`, `Sample` | an object of its fields, each a place, unless one is a data value | to an output that fits and holds no file |
| a union of groups | `GivenCentre \| FindCentre` | an object of one alternative's fields, with its tag, as for a group | to an output that fits and holds no file |
| a list, `list[X]` | a list parameter | a list, each element a place of type `X` | no |
| any other type with a JSON form | `int`, an enum, `tuple[float, float]` | yes | no |
| a data value | `sc.DataArray` | no | no: it is a field of a group |

A file is given by reference to a dataset only, so that the dataset source
checks access and the record holds the dataset's identity; the binding gets a
local path. For the same reason, a reference to an output whose type holds a
file is refused. A *group* is a pydantic model, such as `Reduction` or
`Sample` below, written as an object of its fields, so a file in it comes from
a dataset. A `RootModel` is not a group: it is written as its root value. A *data value* is a value of a type without a JSON form that is not a
file, such as a scipp array.

A reference stands for a group or a file only. A number or a scipp array names
no role: every output of its type fits a place of that type, so a UI that
offers the outputs that fit a place would list every number, or every array,
that any record holds. A group names its role by its class. So a value that
another workflow computes comes as a group, which that workflow outputs, such
as a found beam centre, and a data value is a field of a group. No request
writes such a field, so the group has no inline form and is given by
reference only, as `ComputedTransmission` below. A group that holds a file
holds no data value: the file makes it given field by field.

The contents of a tuple, a set, a dict, a
`TypedDict`, or a dataclass are not places: such a value is written whole,
inline. A parameter, or a field of a group, may be a discriminated union of
groups, such as a beam centre that is given, `GivenCentre`, or found from a
run with a tolerance, `FindCentre`; the request writes the fields of one
alternative with its tag, inline, which says what each of the other fields
takes: `{'kind': 'find', 'run': dataset(run=60339), 'tolerance': 0.05}`.
Why a choice is a union, and what `X | None` means, is
[ADR 0008](0008-a-choice-between-ways-is-a-union-of-groups.md).

```python
class Reduction(BaseModel):                       # a group
    bins: int = 2

class BeamCentre(BaseModel):                      # a group: inline, or a finder's output
    x: float = 0.0
    y: float = 0.0

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)
IofQ = NewType('IofQ', sc.DataArray)

class FromFile(BaseModel):                        # the transmission in the run's file
    kind: Literal['file'] = 'file'

class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):  # by a transmission reduction
    kind: Literal['computed'] = 'computed'
    transmission: sc.DataArray                    # a data value: given by reference only

class Sample(BaseModel):                          # a group that holds a file
    run: SampleFile
    transmission: Annotated[FromFile | ComputedTransmission, Field(discriminator='kind')] = FromFile()

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: Sample
    can: CanFile
    beam_centre: BeamCentre = BeamCentre()        # the nominal centre

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, outputs=IofQOutputs)
```

Two requests, written with essdispatch: `client` connects to a backend,
`dataset(...)` names a dataset, and `finder` is the record of a request that
finds the beam centre, so `finder.ref('centre')` names its output `centre`:

```python
client.submit(IOFQ, params={'sample': {'run': dataset(run=60339)}, 'can': dataset(run=60338),
                            'beam_centre': BeamCentre(x=0.012, y=-0.003)})   # inline
client.submit(IOFQ, params={'sample': {'run': dataset(run=60339)}, 'can': dataset(run=60338),
                            'beam_centre': finder.ref('centre')})            # a reference
```

**Rules.** The models carry no annotations for the framework: what the
framework needs, it reads from the field types (`ess.spec.field_types`).
`WorkflowSpec` checks these rules when it is made:

- The names of params and outputs are distinct.
- Each parameter's type is a type, possibly optional, or a list of one; not a
  dict. The type may be a union, such as a union of groups.
- Each output's type is a type, possibly optional, or a list or a dict of one.
- A file is at a place only, possibly optional or in a list: not inside a
  tuple, a dict, or a dataclass, where no dataset can give it.
- A data value is a field of a group, not a parameter or the element of a
  list parameter. A group that holds a file holds no data value.
- A union of groups is discriminated by the name of its tag field, not by a
  callable. The tag field of each alternative is a `Literal` of one string,
  its own, such as `Literal['find']`, without an alias. A union that holds a
  group holds groups only: a value of a type that is not a group has no tag.
- A group has no serializers, computed fields, or aliases: the record holds a
  group as the object of its places, each in the JSON form of its own type,
  and these would make the group's own JSON form differ from it. It may have
  field and model validators of any mode, which run when the group is
  validated whole. It is not at a place inside itself, at any depth, such as
  a tree with `children: list[Node]`; a tree is written whole,
  `children: list[Node] | None`.
- A default is a value, never a reference: what a request may write at the
  place, which round-trips and holds no `@ref` object at any depth; of a type
  without a JSON form, `None` or `[]`. It is the value that validating it
  gives, since pydantic does not validate a default: a group's default is an
  instance, not the object of its fields.
- A table has no default (see the multi-run spec below).
- The params model has no field validators, serializers, computed fields, or
  aliases, and no `default_factory` that takes the other values. A list
  parameter carries only length constraints, such as `Field(min_length=2)`. The
  params model may have model validators, but the framework never calls them.

Each parameter is validated alone: a template, a stage call, or a push gives
some values only, so a check that read several parameters would hold for some
requests of a spec and not for others. A check of one value belongs to the
value's type, as `EdgesModel` checks its edges, and so does the JSON form of a
value, which the framework stores and reads back. In the request phase, each
place given inline is validated alone, from its JSON form. A group with no
reference inside is a value given inline: it is validated whole, and the record
holds the JSON form of the validated group, which must read back as it. Of a
group with a reference inside, each field given inline is validated alone, by
its own type and constraints. In the call phase, each parameter's value is
validated once, whole, from the JSON form the record holds with the data read
in place of each reference, so that every validator of every group runs with
all its fields at hand, as `EdgesModel`'s check of `stop` against `start`
needs. So a validator that widens what a field accepts, such as a `before`
validator that parses a string, works only for a group written without
references, and a value that only a validator of a group with a reference
inside refuses fails the record, or the push, in the call phase. A
check across parameters, such as a Q range that the wavelength band and the
detector can reach, is the binding's or the driver's, and fails when the request
runs. A binding may run the model validators,
`IofQParams.model_validate(dict(params))`, where it has the whole instance.

A request that leaves out a parameter, or a field of a group, never leaves out
a correction that a correct reduction needs. The default of `reduction` is a
setting, and the default transmission of a `Sample`, `FromFile()`, takes the
one in the run's file. The can run
has no default, so a request or a template that leaves it out is refused. A
correction that a correct reduction needs, but that a request may still leave
out, is typed `X | None` with no default, such as
`direct_beam: DirectBeamFile | None`: the request writes `None`, and its
record says so. The output is I(Q) either way, and the params of its record
say which corrections were applied. A step whose omission changes what a
reader of the output must do with it, such as a normalization without which
I(Q) is in counts, makes another output type, and so another spec. What `None` means, and when a union
names the ways instead, is [ADR 0008](0008-a-choice-between-ways-is-a-union-of-groups.md).

**References.** A reference names an output of an earlier record (`OutputRef`:
record, output name, and optionally the key of one element of a list or dict
output), a dataset the framework did not compute (`DatasetRef`: one identity
string), or an output of an accumulator (`AccumulatorRef`: accumulator and
output name). Only a request holds an `AccumulatorRef`: the framework replaces
it, when it accepts the request, by an output of a record of the accumulator's
state. A reference stands for the value at a place whose type is a group or a
file, such as `sample.run` or `beam_centre`: never for a number, a whole list, a tag of a union, or a position inside a value given
inline. Its JSON form is an object with the key `@ref`, which names its kind:

```json
{"@ref": "dataset", "dataset": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "slices", "key": "2"}
```

The key `@ref` is reserved at every depth: an object with it is a reference.
So a framework finds the references of a request without the spec, in every
array and every object, as a remote client that holds only the serialized
spec must, and an inline value never reads as a reference. With the spec, the
framework refuses an `@ref` object that is not at a place, such as an element
of a `tuple`, and an inline value or a default whose JSON form holds the key.

The identity string of a `DatasetRef` is opaque to the spec and interpreted
only by the framework that minted it, which makes it self-describing where it
has several kinds of identity, as a URI does with its scheme:
`pid:20.500.12269/abc`, `run:dream/1`. It compares as a string, so the
framework normalizes it before a reference enters a record.

**Fit.** A reference *fits* a place when the type of the data it names can be
the value there: a `BeamCentre` output fits a place typed `BeamCentre`, and a
`ComputedTransmission` output fits the union `FromFile | ComputedTransmission`,
since data fit a union of groups when they fit one alternative.
A dataset reference fits a file only. A framework checks fit in the request
phase, so that a chain of requests is refused before anything runs.
`ess.spec.field_types.fits` states the rule.

### One params model per workflow

Parameters are a single pydantic model class per workflow (`params:
type[BaseModel]`), not per-key entries in a registry. This gives a JSON Schema
per model and removes the implementation coupling of key-addressed parameters.
sciline sets files and data on a pipeline as parameters too, so one model
holds settings, files, and data alike.
The graph-derived "select outputs, then see only relevant parameters" feature
of `ess.reduce.workflow.get_parameters` does not survive: it treats output
selection as workflow slicing, which only the sciline implementation can
express. If output-dependent parameter sets are needed, they are distinct
workflows (distinct specs).

The field defaults to `NoParams`, a closed model with no fields, so consumers
never branch on its absence, and sending parameters to a workflow that takes
none is a validation error rather than silently ignored.

Value defaults belong to the workflow's params model, set by the workflow
author next to the field's title and description, never to the shared
vocabulary in `ess.spec.parameters`, whose sensible values depend on the
workflow and instrument. A default makes the field optional in a request.

### Outputs are data

Outputs are likewise a pydantic model class (`outputs: type[BaseModel]`,
mandatory). Field title and description are the display metadata; a field may
be optional when the workflow does not always produce it, and the workflow
leaves it out by returning `None`; declaration order is meaningful (consumers
show outputs in order, primary output first). Each output has a type of its
own, such as `iofq: IofQ` or `centre: BeamCentre`, or is a list or a dict of
one, and a reference with a key names one element.

"Outputs can be parameters" is a fit check between an output's type and the
group at a place. An output that another workflow takes is a group, such as
`centre: BeamCentre`; one that none takes, such as `iofq: IofQ`, need not be. Where a
framework stores an output is not a spec concept. Output *selection* (choosing
which sciline targets to compute) is not modeled: like parameter slicing, it is
an implementation notion. Livedata-specific output machinery (`OutputView`,
`Temporality`, windowing) stays in esslivedata.

### Groups, and tables

The models of `ess.spec.parameters`, such as `QEdges` and `WavelengthRange`, are
groups that specs share. A field of plain numbers states its unit in its
declaration or in a unit field beside it, as the edges models do; a data value
carries its own unit.

A *table* is a list parameter whose *rows* an accumulator takes a push at a
time, such as the sample runs. Each row is given inline or by reference, as its
type allows. The rows of a table are distinct. A *single-run spec* takes one
run per parameter. A *multi-run spec* of the same reduction takes a table in
place of such a parameter. Its params model is a plain model, and the fields
the two models have in common are shared by inheritance:

```python
class IofQSettings(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre = Field(default_factory=BeamCentre)

class IofQParams(IofQSettings):
    sample: Sample
    can: CanFile

class IofQMultiParams(IofQSettings):
    sample: list[Sample] = Field(min_length=1)
    can: list[CanFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title=..., description=...,
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

Each table is a `list` of the single-run type, without a default, so a
request gives every table, `[]` for one without rows, and a template that
forgets a table is refused. `Field(min_length=1)` states that a table needs a
row. The model states the length constraints, so that the model, its schema, and the
backend agree that the table has a row. The field `tables` of `WorkflowSpec`
is a tuple of the names of the tables. A list parameter makes a table only when
`tables` names it. A spec whose outputs differ from those of a single-run spec,
such as a stitch of all angles, or that has no single-run spec, such as a sum
of counts over the runs of a scan, is written the same way:
`angle: list[AngleFile] = Field(min_length=1)`, with `tables=('angle',)`.

The runs of one sum are reduced with one set of settings. A value that differs
per run, such as a motor position, is in the run's file. A value that the file
lacks or has wrong is given next to the run, as a field of the row's group,
so the request and its record hold it. Such a field takes
an inline value in one row and a reference to a computed value in another, as a
transmission computed for one run and not another. A blank of a template is a
whole parameter, and a group is one key of a sciline pipeline. So a stage of
the single-run spec that tunes a transmission has `sample` as its blank, and
reads the run again at each call.

The framework compares rows whole, as the record holds them, so the same run
with another transmission is another row. For a table whose element type has a
JSON form, it also compares the rows read, so that a time slice is never summed
twice (the essapps README, `docs/developer/README.md`, section Stages and
accumulators).

With one row in each table, a request of the multi-run spec is a request of the
single-run spec. `ess.spec.testing.check_one_row` checks that a package's
single-run and multi-run bindings agree on such requests.

### Two forms, one-way projection

`WorkflowSpec` is the in-process form: it holds the model *classes*, so
same-process consumers get full pydantic validation including custom
validators. `spec.serialize()` projects onto `SerializedWorkflowSpec`, a
plain-data pydantic model that round-trips through JSON and is what a service
announces to remote consumers. It holds a schema of the params
(`params_schema`), one of the outputs (`outputs_schema`), and the tables. The
params schema gives each parameter the forms it takes, so that a UI renders a
form for each group, with a choice to give it by reference, a picker of
records for each group that holds a data value, and a picker of datasets for
each file; `SerializedWorkflowSpec` states the forms and the widget of each.

There is deliberately no inverse. Validators do not survive JSON Schema, so a
deserialized spec would be a lie about its own validation. Instead, validation
authority sits with the process owning the model classes: in-process UIs
validate directly; remote UIs validate optimistically against the schema and
the owning service accepts or rejects authoritatively. This matches the
announcement-as-contract design adopted for esslivedata in
[scipp/esslivedata#889](https://github.com/scipp/esslivedata/issues/889): the
serialized spec is the entire cross-process surface, and where a model class is
*defined* is invisible to consumers.

### Identity is `name` + `version`; scoping is the enumerator's problem

No `instrument` field and no `WorkflowId` class at this level. Instrument is
meaningless for technique-level batch workflows, and a spec cannot guarantee
global uniqueness of anything — only the context that enumerates or deploys
specs can. esslivedata keeps keying workflows by `(instrument, name, version)`,
supplying the instrument from its registration context. Data-provenance
identity (which spec, parameter values, and datasets produced a dataset) similarly
composes spec identity with deployment context; the spec's contribution is
being serializable and versioned.

`code_revision` is provenance, not identity: an optional git commit or package
version of the code the spec describes, so that a record made from a
development branch is honest about what ran. The interface version stays
`version`.

### Shared parameter vocabulary, scipp-free

`ess.spec.parameters` provides constrained unit enums, range/edges models with
cross-field validation (`stop > start`, log-scale positivity) — the models
that esslivedata and package code also define — and
`Quantity`. They contain no scipp: conversion of validated values into scipp
objects (`edges_to_variable`, `range_to_variables`) lives in
`ess.spec.conversions`, called by providers of the workflow that take the
model. This keeps every parameter JSON-Schema-clean, and `ess.spec` importable
without scipp.

### Convergence with esslivedata

Explicit goal: `ess.livedata.config.workflow_spec.WorkflowSpec` eventually
inherits from this spec, adding its live-data fields (`instrument`, `group`,
`source_names`, `aux_sources`, reset flags). The base spec's field names and
semantics (`name`, `version`, `title`, `description`, `params`, `outputs`) are
those of esslivedata's for exactly this reason, and esslivedata already
declares outputs as a model class with title and description as field metadata.

esslivedata's params model holds settings only, each given inline: its specs
are the special case of this one in which every value is given inline, so a
request may give their params as an instance of the model. Its data arrive as
streams, which this spec does not model. A value that changes during a stream,
such as a motor position, arrives on a stream of its own and is set as a
*context key* of `StreamProcessor`, never taken from the params; here, such a
value comes with the run. esslivedata's outputs are
`sc.DataArray` fields with default-factory templates used for plotter
selection. Here each output is typed by its data, such as `IofQ`, with no
template.

The import edge is small: essspec depends only on pydantic, and the esslivedata
dashboard is decoupled via the serialized-spec announcement, not via imports.
esslivedata adopts it once essspec has moved to scipp/ess (ADR 0007 of
scipp/essapps).

## Consequences

- Generic UIs (including a command-line interface) can be generated from
  `WorkflowSpec` alone, and from `SerializedWorkflowSpec` across process
  boundaries, with no knowledge of the workflow implementation.
- essspec depends on pydantic; installing it pulls in no science stack. A spec
  module with scipp types imports scipp, which needs only numpy.
- `ess.reduce.parameter`, `ess.reduce.workflow`, and the widgets built on them
  are meant to be superseded by this vocabulary; removing them, in a later hard
  break, is essreduce's decision, and they stay until essreduce removes them. The
  graph-derived parameter discovery they provide would be dropped, not ported.
- `ess.nmx.configurations` and esslivedata migrate to the shared vocabulary and
  spec incrementally, per package, with no coordination requirement — a package
  that never migrates costs the others nothing.
- Spec enumeration remains to be designed when a concrete consumer needs it;
  the spec layer does not constrain it beyond being addressable by `(name,
  version)` and importable without workflow code.

Foreseen extensions, each one optional spec field or one field-level
annotation, deliberately not added until a consumer exists: declared failure
reasons, an intermediate flag for retention, declared keys of a collection
output, and dims and units of an output for a UI.

## Alternatives considered

- **Two models, params inline and inputs by reference.** The spec fixes the
  form per field. A value used both ways, such as a beam centre, then needs a
  client that reads it and sends it again, which loses the record of where it
  came from, or a spec per form; and moving a field between the models breaks
  every client.
- **Inputs that also take a value inline.** Keeps two models, but the inputs
  then hold inline values, which removes the reason for two.
- **A constant spec per typed value**, which outputs the value typed in, for a
  request to reference. Needs no framework change, but a submission per typed
  value.
- **A field typed as the reference.** The binding gets references, not data.
- **A union of the reference and the data.** Wrong in both phases: it needs a
  validator that accepts anything that is not plain data, and it hides that
  member from the JSON Schema.
- **A field typed as the data, with validation switched by phase.** The field
  holds a reference where its type says `Path`, and every validator runs on
  references in the request phase.
- **Every value tagged**, `{"value": ...}` and `{"ref": ...}`. Unambiguous
  without a reserved key, but every inline value in requests, the log, and
  esslivedata's params gets a wrapper.
- **`$ref` as the key of a reference.** It is the JSON Schema keyword for a
  reference within a schema, so a reference held in a schema, as a default or
  an example, would be read as one.
- **A reference at every place with a JSON form**, such as a number computed by
  another record. Every output of the type fits, so a picker of the outputs
  that fit lists every number that any record holds; a group that holds the
  number names what it is.
- **A data value as a parameter of a `NewType`**, such as
  `Transmission = NewType('Transmission', sc.DataArray)`, which names the role
  for the fit check. A place typed plain `sc.DataArray` takes every array that
  any record holds, and the fit check must look through `NewType`s both ways.
  A group names the role with a class, and costs a binding an adapter that
  takes the array out.
- **A reference to a whole list** filling a list parameter. The table rules,
  such as distinct rows, could then be checked only in the call phase.
- **References at a few places only**, the top of a value, of an element of a
  list, and of a field of a group that holds a file, so that `@ref` deeper is
  plain data. A setting inside a group, such as a mask, could then never be
  computed by another record. Reserving `@ref` at every depth finds the
  references without the spec as well.
- **A group made from its fields**, each validated alone, without its
  validators. A check such as `stop > start` would then be skipped once a
  request gives one field by reference. Validating the group again from the
  typed values instead runs a validator on its own output, which a
  `BeforeValidator` that parses a string cannot take.
- **The framework running model validators of the params model.** A stage and a
  driver never make a whole params instance, so such a check would hold for
  some requests of a spec and not for others.
- **Annotations on the fields that hold data**, marking what the framework
  resolves, the format of the bytes, and the array structure. The field types
  tell the framework the same, and the author writes no metadata for the
  framework.
- **A `tabulate` that derives the multi-run model** from the single-run spec
  by naming its tables. The model has no static type, drops the model
  validators of the single-run model, and needs framework rules for where the
  metadata of a field goes. The one-row symmetry that it would give by
  construction is checked by `check_one_row`.
- **A table as several list parameters of equal length**, an *index* such as
  the runs, with *columns* attached to it, such as the transmission of each
  run. The values of one run are then spread over several lists and paired by
  position; a blank, a push, and a driver name a table by its index, a name
  that stands for several parameters; and a column left out of a push needs a
  value to fill it. A group holds the values of one run in one row.
- **Tables named apart from their parameters.** A second namespace, which
  blanks, pushes, and the driver would mix with the names of parameters. The
  parameter names its table instead.
