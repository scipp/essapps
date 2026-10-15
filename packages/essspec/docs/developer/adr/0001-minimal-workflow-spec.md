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
`description`, both mandatory), a params model, an inputs model, an outputs
model, its tables, and an optional `code_revision`. Nothing else. In particular
it holds *no* workflow factory and *no* keys of a workflow: a spec describes
*what a user can configure, which data they give, and what they get back*, not
how it is computed. A `PipelineBinding` uses each field's type as its sciline
key, or the key that its `keys=` names, so the spec imports nothing from the
workflow. A caller of a remote service that holds the same spec needs no
binding at all. This is what keeps the spec valid across
local, service, and cluster execution.

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

### Params and inputs

A request goes through two phases. The *request phase* is what the client and
the backend do with a request before any data is read: validate, log, record,
pin. The *call phase* starts when the backend reads data for the binding. In
the request phase a data value is a *reference*, plain data that names data
existing elsewhere. In the call phase it is the data. One model cannot type
both phases:

- A field typed as a reference gives the binding references.
- A union of the reference and the data is wrong in both phases. It needs a
  validator that accepts anything that is not plain data, and it hides that
  member from the JSON Schema.
- A field typed as the data, with validation switched by phase, holds a
  reference where its type says `Path`.

Only data differ between the phases, so a spec splits its fields there, into
three ordinary pydantic models:

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | configuration: literals only | the values | the same values |
| inputs | data only | a reference per value | the data the references name |
| outputs | results | (none) | the data the workflow returns |

A *literal* is a value that has a JSON form and that the client knows: a
number, a string, an enum, a model of such values. A *data value* is a value
that the backend fetches, given by reference: a file, an array, a model holding
one, or any other output of a record, such as a fitted scale factor. A field of
the params model is a *parameter*, and a field of the inputs model an *input*.

```python
class Reduction(BaseModel):                       # a group: a model of literals
    bins: int = 2

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)
IofQ = NewType('IofQ', sc.DataArray)

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

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, inputs=IofQInputs, outputs=IofQOutputs)
```

The client builds and validates a params instance, and a type checker checks
it. The backend reads each reference and builds the inputs instance from the
data, so every field of it holds the type it declares. The binding gets both
instances: `compute(params, inputs) -> outputs`.

The models carry no annotations for the framework: what the framework needs, it
reads from the field types (`ess.spec.field_types`). A file is `Path` or a
`NewType` of it, and the binding gets a local path; any other data value it
gets as the object. `WorkflowSpec` checks these rules when it is made:

- The names of params, inputs, and outputs are distinct, and so are their
  types, with `| None` and list or dict removed. Two parameters of one model,
  such as two axes of a Q map, get a `NewType` each.
- Each parameter is a *group*, a model of literals, possibly optional, or a
  list of groups. Every value in the params model has a JSON form, and none is
  a file.
- Each input is a data value, possibly optional, or a list of data values. A
  data value is any type but a list, a dict, or a union, once its `NewType`s
  are removed. The default of an input is `None` or an empty list.
- Each output is a data value, possibly optional, or a list or a dict of data
  values.

Inputs hold data only, because a value that differs per run comes with the run.
The runs of one sum are reduced with one set of settings. A value that differs
per run, such as a motor position, is in the run's file. A value that the file
lacks or has wrong is given next to the run, as a column of the run's table
(see the section on tables), so the request and its record hold it.

**References.** A reference names an output of an earlier record (`OutputRef`:
record, output name, and optionally the key of one element of a list or dict
output), a dataset the framework did not compute (`DatasetRef`: one identity
string), or an output of an accumulator (`AccumulatorRef`: accumulator and
output name). Only a request holds an `AccumulatorRef`: the framework replaces
it, when it accepts the request, by an output of a record of the accumulator's
state. Every value in the inputs of a request is a reference or a list of
references, and params hold none. So a framework finds the references of a
request without the spec, as a remote client that holds only the serialized
spec must.

The identity string of a `DatasetRef` is opaque to the spec and interpreted
only by the framework that minted it, which makes it self-describing where it
has several kinds of identity, as a URI does with its scheme:
`pid:20.500.12269/abc`, `run:dream/1`. It compares as a string, so the
framework normalizes it before a reference enters a record.

**Fit.** A reference *fits* a field when the type of the data it names can be
the field's value (`ess.spec.field_types.fits`). `| None` is removed on both
sides. The data's type fits when, with its `NewType`s removed one by one, it
reaches the field's type or a subclass of it; or when the field's type, with
its `NewType`s removed, reaches the data's type or a superclass of it. So an `IofQ` output
fits a field typed `sc.DataArray`, and a plain `sc.DataArray` output fits a
`Transmission` field, since a role narrows a general output. An `IofQ` does not
fit a `Transmission` field, two `NewType`s of `sc.DataArray`. A dataset
reference fits a file field. A framework checks fit in the request phase, so
that a chain of requests is refused before anything runs.

**Checks.** A check of one value belongs to the value's type, as the field
validator of `BeamCentre` checks its data, and so does the JSON form of a
value, which a framework stores and reads back. `WorkflowSpec` refuses every
pydantic decorator of the params and inputs models themselves but a model
validator: field validators, serializers, and computed fields. A check across
params and inputs, such as a transmission mode that requires a transmission
run, is the binding's, and fails when the request runs.

### One params model and one inputs model per workflow

Parameters are a single pydantic model class per workflow (`params:
type[BaseModel]`), and so are inputs (`inputs: type[BaseModel]`), not per-key
entries in a registry. This enables cross-field validation, gives a JSON
Schema per model, and removes the implementation coupling of key-addressed
parameters.
The graph-derived "select outputs, then see only relevant parameters" feature
of `ess.reduce.workflow.get_parameters` does not survive: it treats output
selection as workflow slicing, which only the sciline implementation can
express. If output-dependent parameter sets are needed, they are distinct
workflows (distinct specs).

The fields default to `NoParams` and `NoInputs` (closed models with no fields),
so consumers never branch on either being absent, and sending parameters or
inputs to a workflow that takes none is a validation error rather than silently
ignored.

Value defaults belong to the workflow's params model, set by the workflow
author next to the field's title and description, never to the shared
vocabulary in `ess.spec.parameters`, whose sensible values depend on the
workflow and instrument. A default makes the field optional in a request.

### Outputs are data values

Outputs are likewise a pydantic model class (`outputs: type[BaseModel]`,
mandatory). Field title and description are the display metadata; a field may
be optional when the workflow does not always produce it, and the workflow
leaves it out by returning `None`; declaration order is meaningful (consumers
show outputs in order, primary output first). Each output is a data value with
a type of its own, such as `iofq: IofQ`, or a list or a dict of them, and a
reference with a key names one element.

Outputs and inputs share one kind of type, so "outputs can be inputs" is a fit
check between an output's type and an input's type. Where a framework stores an
output is not a spec concept. Output *selection* (choosing which sciline
targets to compute) is not modeled: like parameter slicing, it is an
implementation notion. Livedata-specific output machinery (`OutputView`,
`Temporality`, windowing) stays in esslivedata.

### Groups, and tables named by their index

The models of `ess.spec.parameters`, such as `QEdges` and `WavelengthRange`, are
groups that specs share. A field of plain numbers states its unit in its
declaration or in a unit field beside it, as the edges models do; a data value
carries its own unit.

A *table* is a set of list fields of equal length, pushed together into an
accumulator. Its *index* is the field that names it, such as the run. Its other
*columns* are attached to the index, such as a correction of each run. Index
and columns are parameters or inputs. Each position is a *row*. As in a pandas
DataFrame, the index values of a table are distinct. A *single-run spec* takes
one run per field. `tabulate` makes a *multi-run spec* from it by naming the
indexes, each with the columns attached to it:

```python
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))
SANS_MULTI = SANS.tabulate(name='sans-multi', version=1,
                           tables={'sample': ('sample_geometry',), 'can': ()})
```

Each field named becomes a `list` on its own side: `sample: SampleFile`
becomes `list[SampleFile]` with at least one row. An index is `X`, or
`X | None = None`, which becomes a table without rows. The validators and
constraints of a field named check each row. The docstring of `WorkflowSpec.tabulate` states the rules for each kind of field.

`tabulate` fills the field `tables` of `WorkflowSpec`, which maps each index to
its attached columns. A spec whose outputs differ from those of a single-run
spec, such as a stitch of all angles, or that has no single-run spec, such as a
sum of counts over the runs of a scan, declares its tables itself:
`WorkflowSpec(..., tables={'angle': ()})`. Its required index carries
`Field(min_length=1)`, as one that `tabulate` makes, so that the model, its
schema, and the backend agree that the table has a row. A list field makes a
table only when `tables` names it.

With one row in each table whose index is given, and none where it is left out,
a request of the multi-run spec is a request of the single-run spec.
`ess.spec.testing.check_one_row` checks that a package's single-run and
multi-run bindings agree on such requests.

### Two forms, one-way projection

`WorkflowSpec` is the in-process form: it holds the model *classes*, so
same-process consumers get full pydantic validation including custom
validators. `spec.serialize()` projects onto `SerializedWorkflowSpec`, a
plain-data pydantic model that round-trips through JSON and is what a service
announces to remote consumers. It holds the params model's JSON Schema
(`params_schema`), the tables, and a schema of the inputs and one of the
outputs (`inputs_schema`, `outputs_schema`). The framework makes the last two
from the field types: each value names the type of its data in `dataType`, and
a list field is an array of them. It suffices to render a form for the params
and a picker per input.

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
identity (which spec, params, and input datasets produced a dataset) similarly
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

esslivedata's params model holds configuration and never data, which arrive as
streams, so a live-data spec takes `NoInputs` until streams are inputs. A value
that changes during a stream, such as a motor position, arrives on a stream of
its own and is set as a *context key* of `StreamProcessor`, never taken from
the params; here, such a value comes with the run. esslivedata's outputs are
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

- **Annotations on the fields that hold data**, marking what the framework
  resolves, the format of the bytes, and the array structure. The field types
  tell the framework the same, and the author writes no metadata for the
  framework.
- **Multi-run models written by hand**, `MultiIofQInputs` next to `IofQInputs`.
  They repeat the single-run model, and the one-row symmetry holds only by
  convention.
- **Rows as models**, pairing a run with its transmission run in a model of two
  fields. The columns of a table pair them; a model would be a group or a data
  value depending on its use.
- **Tables named apart from their fields.** A second namespace, which blanks,
  pushes, and the driver would mix with the names of fields. The index names
  its table instead.
