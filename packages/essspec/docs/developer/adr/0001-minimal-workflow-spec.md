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

Which form a value takes is the request's choice, per value, since the same
value is typed in by one user and computed by another: a beam centre is typed
in, or found by a first reduction; the time slices of a slicing reduction are
typed in, or found from a log. The parameter's type decides which forms it
accepts, with `| None` removed and, for a list parameter, its element type
taken. A type has a *JSON form* when it is not a file, holds no file, and
pydantic makes a JSON schema of it with the params model's config. A value
*round-trips* when its JSON form reads back as the value.

| A parameter's type | Example | Inline | By reference |
|---|---|---|---|
| a file: `Path`, pydantic's `FilePath` or `DirectoryPath`, or a `NewType` of one | `SampleFile` | no | to a dataset |
| has a JSON form | `Reduction`, `BeamCentre` | yes | to an output that fits |
| a group that holds a file | `Sample` | field by field | to a dataset, for its one required file |
| anything else | `Transmission = NewType('Transmission', sc.DataArray)` | no | to an output that fits |

A file is given by reference to a dataset only, so that the dataset source
checks access and the record holds the dataset's identity; the binding gets a
local path. A *group* is a pydantic model, such as `Reduction`: the usual
type of a parameter given inline. A group that holds a file, such as `Sample`
below, is given *field by field*: each of its fields inline or by reference,
as a parameter is, so its file comes from a dataset. A reference to a dataset
stands for the whole group: its one required file, and its other fields at
their defaults. A *data value* is a value of a type without a JSON form: a
file, or an object such as a scipp array.

A type is a file when pydantic's JSON schema of it is a string with a path
format: `"format": "path"` for `Path` and a `NewType` of it, `"file-path"` for
`FilePath`, `"directory-path"` for `DirectoryPath`. The access check on the
service depends on it, so an author types each file as one of these. A
`NewType` of `str` used for a file is a string: it is given inline, and no
dataset source checks it.

```python
class Reduction(BaseModel):                       # a group
    bins: int = 2

class BeamCentre(BaseModel):                      # a group: inline, or a finder's output
    x: float
    y: float

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)
Transmission = NewType('Transmission', sc.DataArray)
IofQ = NewType('IofQ', sc.DataArray)

class Sample(BaseModel):                          # a group that holds a file
    run: SampleFile
    transmission: Transmission | None = None

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: Sample
    can: CanFile
    beam_centre: BeamCentre | None = None

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, outputs=IofQOutputs)
```

Two requests, written with essdispatch: `client` connects to a backend,
`dataset(...)` names a dataset, and `finder` is the record of a request that
finds the beam centre, so `finder.ref('centre')` names its output `centre`:

```python
client.submit(IOFQ, params={'sample': dataset(run=60339), 'can': dataset(run=60338),
                            'beam_centre': BeamCentre(x=0.012, y=-0.003)})   # inline
client.submit(IOFQ, params={'sample': dataset(run=60339), 'can': dataset(run=60338),
                            'beam_centre': finder.ref('centre')})            # a reference
```

**Rules.** The models carry no annotations for the framework: what the
framework needs, it reads from the field types (`ess.spec.field_types`).
`WorkflowSpec` checks these rules when it is made:

- The names of params and outputs are distinct.
- Each parameter's type is a type, possibly optional, or a list of one; not a
  dict or a union of two types.
- Each output's type is a type, possibly optional, or a list or a dict of one.
- A parameter holds a file only as a file, possibly optional, a list of files,
  or a group that holds a file as a field. A file anywhere else, such as in a
  tuple, a `TypedDict`, a dataclass, or a type alias `type Files = list[Path]`,
  is refused, since no reference to a dataset stands for it.
- A group holds a file only as one of its fields, typed as a file, possibly
  optional. Such a group follows the rules on defaults and on the params model
  below. A field of it that is a list or a dict has a JSON form, since no
  reference stands for a list.
- A default is a value, never a reference: of a type with a JSON form, a value
  that round-trips and whose JSON form is neither an `@ref` object nor a list
  holding one; of any other type, `None` or `[]`.
- A table has no default (see the multi-run spec below).
- The params model has no field validators, serializers, computed fields, or
  aliases, and no `default_factory` that takes the other values. A list
  parameter carries only length constraints, such as `Field(min_length=2)`. The
  params model may have model validators, but the framework never calls them.

Each parameter is validated alone, in every phase: a template, a stage call,
or a push gives some values only, so a check that read several parameters
would hold for some requests of a spec and not for others. Like the params
model, a group given field by field is never validated whole. A check of one
value belongs to the value's type, as `EdgesModel` checks its edges, and so
does the JSON form of a value, which the framework stores and reads back. A
check across parameters, such as a transmission mode that requires a
transmission run, is the binding's or the driver's, and fails when the request
runs. A binding may run the model validators,
`IofQParams.model_validate(dict(params))`, where it has the whole instance.

A default never changes which quantity an output is. The default of
`reduction` is a setting, and the transmission `None` of a `Sample` takes the
one in the run's file: the output is I(Q) either way. The can run has no
default. I(Q) without the can subtracted is another quantity, so it is another
spec, not `IOFQ` with `can=None`, and a request or a template that leaves out
the can is refused instead of computing the other quantity.

**References.** A reference names an output of an earlier record (`OutputRef`:
record, output name, and optionally the key of one element of a list or dict
output), a dataset the framework did not compute (`DatasetRef`: one identity
string), or an output of an accumulator (`AccumulatorRef`: accumulator and
output name). Only a request holds an `AccumulatorRef`: the framework replaces
it, when it accepts the request, by an output of a record of the accumulator's
state. A reference stands for a value, for one row of a list parameter, or
for one field of a group given field by field, such as `sample.run`: never
for a whole list, and never for part of any other group. Its JSON form is an
object with the key `@ref`, which names its kind:

```json
{"@ref": "dataset", "dataset": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "slices", "key": "2"}
```

The framework looks for references at three places only: the top of each
value, the top of each element of a value that is a JSON array, and each field
of an object at one of these two places, such as `sample.run` or
`sample[2].transmission`. At these places it refuses an `@ref` object that the
spec does not allow as a reference: an element of a parameter that is not a
list, such as a `tuple` of groups, whose JSON is an array; a field of a group
given inline whole, such as `reduction.bins`; or the JSON form of an inline
value, such as a group with extra fields that holds the key `@ref`. Deeper
inside a value, `@ref` is plain data. So a framework finds the references of a request without the spec, as a
remote client that holds only the serialized spec must, and an inline value
never reads as a reference.

The identity string of a `DatasetRef` is opaque to the spec and interpreted
only by the framework that minted it, which makes it self-describing where it
has several kinds of identity, as a URI does with its scheme:
`pid:20.500.12269/abc`, `run:dream/1`. It compares as a string, so the
framework normalizes it before a reference enters a record.

**Fit.** A reference *fits* a parameter when the type of the data it names can
be the parameter's value: an `IofQ` output fits a parameter typed
`sc.DataArray`, and does not fit a `Transmission` parameter, another `NewType`
of `sc.DataArray`. A dataset reference fits a file only. A framework
checks fit in the request phase, so that a chain of requests is refused before
anything runs. `ess.spec.field_types.fits` states the rule.

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

"Outputs can be parameters" is a fit check between an output's type and a
parameter's type, whether the parameter's type has a JSON form or not. Where a
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
given field by field, so the request and its record hold it. Such a field takes
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
form for each group, with a choice to give it by reference, and a picker of
references for each data value; `WorkflowSpec.serialize` states its shape.

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
- **A reference to a whole list** filling a list parameter. The table rules,
  such as distinct rows, could then be checked only in the call phase.
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
  value to fill it. A group given field by field holds the values of one run
  in one row.
- **Tables named apart from their parameters.** A second namespace, which
  blanks, pushes, and the driver would mix with the names of parameters. The
  parameter names its table instead.
