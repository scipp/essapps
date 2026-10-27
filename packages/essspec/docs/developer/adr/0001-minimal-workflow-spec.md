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

Which form a value takes is the request's choice, at each *root place*, since
the same value is typed in by one user and computed by another: a beam centre
is typed in, or found by a first reduction; the time slices of a slicing
reduction are typed in, or found from a log. A root place is the value of a
parameter, or an element of a list parameter. The type at a root place decides
which forms it accepts, with `| None` removed. A type has a *JSON form* when
pydantic makes a JSON schema of it with its model's config. A value
*round-trips* when its JSON form reads back as the value.

| The type at a root place | Example | Inline | By reference |
|---|---|---|---|
| a dataset model | `Sample`, `MaskFile` | no | to a dataset that fits |
| a group without a data value | `Reduction`, `BeamCentre` | an object of its fields | to an output of its class |
| a group with a data value | `ComputedTransmission` | no | to an output of its class |
| a union of groups | `GivenCentre \| FindCentre` | an object of one alternative's fields, with its tag | no |
| a list, `list[X]` | a list parameter | a list, each element a root place of type `X` | no |

**Only the first level is interpreted.** A *group* is a pydantic model, written
as an object of its fields; a `RootModel` is not a group. Each parameter is a
group, a union of groups, or a list of one, possibly typed `X | None`. A field
of a group is a *scalar*, `int`, `float`, `str`, `bool`, `datetime`, an enum,
or a `Literal`, possibly constrained, such as `Annotated[float, Gt(0)]`, or a
list of scalars. A field is never typed `X | None`. A *data value* is a field
without a JSON form, such as a scipp array. No request can write it, so a group
with a data value is given by reference to an output only, and its other fields
may be of any type that an output may hold. The framework looks no deeper.

This buys three things:

- No group written inline holds a reference, so each is validated whole when
  the request arrives, and every validator of the group runs then, with all
  its fields at hand. A reference only has to fit its type, and the call phase
  only resolves the references at root places.
- A UI needs one widget per root place and one input per scalar, with no
  recursion:

  | Root place | Widget |
  |---|---|
  | a group without a data value | a form of its fields, and a picker of records whose output is of its class |
  | a group with a data value | a picker of records whose output is of its class |
  | a dataset model | a picker of datasets of its kind |
  | a union of groups | a choice of alternatives, each a form of its fields |
  | a list | rows, added and edited; each row the widget of its element |
  | `X \| None` | a toggle over the widget of `X` |

- A `PipelineBinding` keys each parameter by its class, so a group is one
  sciline key.

A value of several parts is a group, a parameter of its own:
`range: WavelengthRange`, not `limits: tuple[float, float]`. A value that a
request may leave out is a parameter typed `X | None`, or a `bool` field of a
group, such as `limit_range: bool` next to `start` and `stop`. Why a choice
between ways is a union, and what `X | None` means, is
[ADR 0008](0008-a-choice-between-ways-is-a-union-of-groups.md).

**Files are dataset models.** A file is a parameter, or the element of a list
parameter, whose type is a *dataset model*: a subclass of
`ess.spec.DatasetModel`, which holds only `path: Path`. Its class keywords name
the metadata that a fitting dataset has:

```python
class MaskFile(DatasetModel, kind='mask'):
    """A pixel mask, as XML."""
```

A dataset model is given by reference to a dataset only, so that the dataset
source checks access and the record holds the dataset's identity; the binding
gets the model with a local path. No output is or holds a dataset model or a
`Path`, so every path a workflow gets has passed the access check. The metadata
let a picker offer only the datasets that fit. A field of the model for a
metadata value, such as the run number, waits for a workflow that needs it.

**References stand for whole groups.** A number or a scipp array names no role:
every output of its type fits a place of that type, so a UI that offers the
outputs that fit would list every number, or every array, that any record
holds. A group names its role by its class. So a value that another workflow
computes comes as a group, which that workflow outputs, such as a found beam
centre, and a data value is a field of a group, such as
`ComputedTransmission.transmission`.

A reference names an output of an earlier record (`OutputRef`: record, output
name, and optionally the key of one element of a list or dict output), a
dataset the framework did not compute (`DatasetRef`: one identity string), or
an output of an accumulator (`AccumulatorRef`: accumulator and output name).
Only a request holds an `AccumulatorRef`: the framework replaces it, when it
accepts the request, by an output of a record of the accumulator's state. Its
JSON form is an object with the key `@ref`, which names its kind:

```json
{"@ref": "dataset", "dataset": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "slices", "key": "2"}
```

The key `@ref` is reserved at every depth: an object with it is a reference.
So a framework finds the references of a request without the spec, as a remote
client that holds only the serialized spec must, and an inline value never
reads as a reference. With the spec, the framework refuses a reference inside
a group written inline, and a default whose JSON form holds the key.

The identity string of a `DatasetRef` is opaque to the spec and interpreted
only by the framework that minted it, which makes it self-describing where it
has several kinds of identity, as a URI does with its scheme:
`pid:20.500.12269/abc`, `run:dream/1`. It compares as a string, so the
framework normalizes it before a reference enters a record.

Output references and dataset references work the same way:

| | Reference to an output | Reference to a dataset |
|---|---|---|
| Root place | a parameter, or an element of a list parameter | the same |
| Stands for | a whole group, not a dataset model | a whole dataset model |
| Fits if | the output's class is the group's class or a subclass of it | the dataset's metadata has the model's metadata |
| Resolved in the call phase to | the stored output | the dataset model with the local path |
| Found in a record | by its `@ref` key, without the spec | the same |
| Access check at submission | records the client may read | datasets the proposal may read |

No reference fits a union, and no single reference fills a list parameter. A
subclass that names a role fits a root place typed by its base class too, so
a root place that takes one role only is typed by that role's class. A framework
checks fit in the request phase, so that a chain of requests is refused before
anything runs. `ess.spec.field_types.fits` states the rule.

A root place takes a file or an output of another workflow, not either. A value
that some users read from a file and others compute is a group given by
reference, and a workflow that reads the file outputs the group.

```python
class Sample(DatasetModel, kind='raw'): ...       # the file of a sample run
class Can(DatasetModel, kind='raw'): ...

class Reduction(BaseModel):                       # a group of scalars
    bins: int = 2

class BeamCentre(BaseModel):                      # inline, or a finder's output
    x: float = 0.0
    y: float = 0.0

class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):  # by a transmission reduction
    transmission: sc.DataArray                    # a data value: given by reference only

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre = BeamCentre()        # the nominal centre
    transmission: ComputedTransmission
    sample: Sample
    can: Can

class IofQ(BaseModel, arbitrary_types_allowed=True):  # a group, so another workflow takes it by reference
    intensity: sc.DataArray

class IofQOutputs(BaseModel):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, outputs=IofQOutputs)
```

Two requests, written with essapps: `client` connects to a backend,
`dataset(...)` names a dataset, `finder` is the record of a request that finds
the beam centre, so `finder.ref('centre')` names its output `centre`, and `t`
is the record of a transmission reduction:

```python
runs = {'sample': dataset(run=60339), 'can': dataset(run=60338), 'transmission': t.ref('transmission')}
client.submit(IOFQ, params={**runs, 'beam_centre': BeamCentre(x=0.012, y=-0.003)})  # inline
client.submit(IOFQ, params={**runs, 'beam_centre': finder.ref('centre')})          # a reference
```

**Rules.** The models carry no annotations for the framework: what the
framework needs, it reads from the field types (`ess.spec.field_types`).
`WorkflowSpec` checks these rules when it is made:

- The names of params and outputs are distinct.
- Each parameter is a group, a union of groups, or a list of one, possibly
  typed `X | None`. A list parameter is not typed `list[X] | None`, since a
  list without rows is `[]`, and its element is not typed `X | None`, since
  each row is a value.
- A field of a group without a data value is a scalar or a list of scalars,
  never typed `X | None`.
- A dataset model holds only `path`, as `DatasetModel` declares it; a field
  validator of `path` is allowed. A `Path` outside a dataset model is refused.
- Each output's type is a type, possibly typed `X | None`, or a list or a dict
  of one. No output is or holds a dataset model or a `Path`, at any depth.
- No type in the params or the outputs is a `NewType` or a type alias, at any
  depth. A class names a role. A `NewType` of a shared type would name one only
  if the fit check looked through it. A `NewType` that a sciline graph needs
  as a key belongs to the binding (`keys=`, or a provider that takes the value
  out of the group).
- A union of groups is discriminated by the name of its tag field, not by a
  callable. The tag field of each alternative is a `Literal` of one string,
  its own, such as `Literal['find']`, without an alias. A union that holds a
  group holds groups only: a value of a type that is not a group has no tag.
  No alternative has a data value: a request writes a union inline.
- A group has no serializers, computed fields, or aliases, which would make
  its JSON form differ from the object of its fields. It may have field and
  model validators of any mode.
- A default is a value, never a reference: what a request may write at the
  place, which round-trips and holds no `@ref` object at any depth; of a type
  without a JSON form, `None` or `[]`. It is the value that validating it
  gives, since pydantic does not validate a default: a group's default is an
  instance, not the object of its fields, and an enum's a member, not its
  value.
- A table has no default (see the multi-run spec below).
- The params model has no field validators, serializers, computed fields, or
  aliases, and no `default_factory` that takes the other values. A list
  parameter carries only length constraints, such as `Field(min_length=2)`. The
  params model may have model validators, but the framework never calls them.

Each parameter is validated alone: a template, a stage call, or a push gives
some values only, so a check that read several parameters would hold for some
requests of a spec and not for others. A check of one value belongs to the
value's type, as `EdgesModel` checks `stop` against `start`, and so does the
JSON form of a value, which the framework stores and reads back. In the request
phase, each group, or row, written inline is validated whole, from its JSON
form, and the record holds the JSON form of the validated group, which must
read back as it. The call phase makes each value of the JSON form the record
holds, with the data read in place of each reference; it fails only for the
data of a reference, such as a dataset model whose validator refuses the local
path. A check across parameters, such as a Q range that the wavelength band
and the detector can reach, is the binding's or the driver's, and fails when
the request runs. A binding may run the model validators,
`IofQParams.model_validate(dict(params))`, where it has the whole instance.

A request that leaves out a parameter, or a field of a group, never leaves out
a correction that a correct reduction needs. The default of `reduction` is a
setting. The can run has no default, so a request or a template that leaves it
out is refused. A correction that a correct reduction needs, but that a
request may still leave out, is typed `X | None` with no default, such as
`direct_beam: DirectBeam | None`: the request writes `None`, and its record
says so. The output is I(Q) either way, and the params of its record say
which corrections were applied. A step whose omission changes what a
reader of the output must do with it, such as a normalization without which
I(Q) is in counts, makes another output type, and so another spec. What
`None` means, and when a union names the ways instead, is
[ADR 0008](0008-a-choice-between-ways-is-a-union-of-groups.md).

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
group at a root place. An output that another workflow takes is a group, such as
`centre: BeamCentre` or `iofq: IofQ`; one that none takes need not be. Where a
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
time, such as the sample runs. Each row is a root place, given inline or by
reference, as its type allows. The rows of a table are distinct. A *single-run spec* takes one
run per parameter. A *multi-run spec* of the same reduction takes a table in
place of such a parameter. Its params model is a plain model, and the fields
the two models have in common are shared by inheritance:

```python
class IofQSettings(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre = Field(default_factory=BeamCentre)
    transmission: ComputedTransmission

class IofQParams(IofQSettings):
    sample: Sample
    can: Can

class IofQMultiParams(IofQSettings):
    sample: list[Sample] = Field(min_length=1)
    can: list[Can] = Field(min_length=1)

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
`runs: list[Run] = Field(min_length=1)`, with `tables=('runs',)` and `Run` a
dataset model.

The runs of one sum are reduced with one set of settings, the parameters next
to the tables, such as one transmission for the summed sample runs. A value
that differs per run, such as a motor position, is in the run's file. A row of
a table of a dataset model is a dataset reference only, so runs that need
different values that their files lack are different requests.

The framework compares rows whole, as the record holds them. For a table whose
element type has a JSON form and is not a dataset model, it also compares the rows read, so that a time slice is never summed
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
params schema gives each parameter the forms it takes, so that a UI renders
the widget of each root place, as the table above has it; at a dataset model,
the schema names the model's metadata, so that a remote picker offers only the
datasets that fit. `SerializedWorkflowSpec` states the forms and the widget of
each.

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
selection. Here each output has a type of its own, such as the group `IofQ`,
with no template.

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
  holds a reference where its type says a group, and every validator runs on
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
- **`NewType`s as roles**, such as
  `Transmission = NewType('Transmission', sc.DataArray)` or
  `SampleFile = NewType('SampleFile', Path)`, which name the role for the fit
  check and give each parameter a sciline key of its own. The fit check must
  then look through `NewType`s, both ways for a plain output to fit a role,
  and a place typed plain `sc.DataArray` still takes every array that any
  record holds. A class names the role, and a sciline key that differs from
  the spec's class is the binding's (`keys=`), or an adapter provider that
  takes the value out of the group.
- **Parameters of any type**, such as `bins: int` at the top level. A
  generated UI shows a parameter that is not a group outside the sections of
  the groups, and two parameters of one type need `keys=` for distinct sciline
  keys.
- **A field of a group of any type with a JSON form**, such as
  `limits: tuple[float, float]` or `scales: dict[str, float]`, given inline
  as one value. A form has no widget for it, and no spec needs one: its parts
  are the fields of a group, a parameter of its own.
- **Any place at any depth inline or by reference**: groups inside groups,
  unions and `X | None` at fields, each field a place of its own. A group with
  a reference inside cannot be validated whole when the request arrives, so it
  is checked field by field, and its validators run only once the data are
  read, in the call phase. The framework recurses over places, with cycle
  detection, and a form nests widgets. The cases found in the requirements
  and the ess reduction packages are written with parameters of their own, or
  as separate requests, at the cost of prefixed names, such as for the
  settings of each of several He3 cells.
- **A file as a `Path` field of a group**, given by reference to a dataset.
  A group that holds a file then has a reference inside, and a picker cannot
  tell which kind of dataset a field takes.
- **A dataset model at a field of a group**, such as
  `Sample(run: SampleRun, thickness: float)`, so that a table holds runs each
  with settings of its own. The group could be validated at submission with an
  unvalidated stand-in for the dataset, but a validator of the group that
  reads the path would check the stand-in, never the local path, and batch and
  rules would name places inside groups. An output reference cannot be handled
  so, since its record may be pending at submission. Left out until a workflow
  needs it; adding it later breaks nothing.
- **A reference to a whole list** filling a list parameter. The table rules,
  such as distinct rows, could then be checked only in the call phase.
- **`@ref` reserved at root places only**, plain data deeper. A reference
  inside a group would then be refused by pydantic's type error, such as
  "Input should be a valid number", rather than by the rule that says where a
  reference goes.
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
  value to fill it.
- **Tables named apart from their parameters.** A second namespace, which
  blanks, pushes, and the driver would mix with the names of parameters. The
  parameter names its table instead.
