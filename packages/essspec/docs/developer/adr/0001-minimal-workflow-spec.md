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
for the types of the data in a group that a reference fills, but never sciline or a workflow package (ADR
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

### Params: values, files, and references to outputs

A request goes through two phases. The *request phase* is what the client and
the backend do with a request before any data is read: validate, log, record,
pin. The *call phase* starts when the backend reads data for the binding.

A request gives each field of the params, at any depth, in one of three
sources:

- *written inline*: the value itself, such as `Reduction(bins=100)` or
  `{'bins': 100}`;
- a *file*, `file(run=60339)`, which the backend resolves to the file's
  identity in the request phase;
- a *reference to an output*, which names an output of an earlier record or
  of an accumulator. In the request phase it is the reference; in the call
  phase it is the data.

The *JSON form* of a value is what it dumps to as JSON,
`model_dump(mode='json')`. The record holds the JSON form of each value, file,
and reference. A value *round-trips* when its JSON form reads back as the
value. The params model types the call phase only. The backend reads each
reference to an output and builds the params instance from the values and the
data, so every field of it holds the type it declares. The binding gets that
instance: `compute(params) -> outputs`.

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | everything the workflow takes: settings, files, data | each value written inline, a file, or a reference to an output | the values, files with their local paths, and the data the references name |
| outputs | results | (none) | the data the workflow returns |

**The spec marks each field that a reference to an output fills.** Such a
field is typed `Referenced[T]`, where the data of the output fit `T`. A request
writes a field of any other type: inline, or as a file. The same value may be
typed in by one user and computed by another: a beam centre is typed in, or
found by a first reduction; the time slices of a slicing reduction are typed
in, or found from a log. Such a field is a union of both,
`BeamCentre | Referenced[BeamCentre]`, and the request chooses one member of
the union that the spec declares. The type of a field, with `| None` removed,
says which sources it takes:

| Type | Example | A request writes |
|---|---|---|
| a scalar | `float`, `Annotated[int, Gt(0)]` | its JSON value |
| a group | `Reduction`, `Sample(run, thickness)` | an object of its fields, each as its type says |
| a union of groups | `GivenCentre \| FindCentre` | an object of one alternative's fields, with its tag |
| the type of a file field | `RunFile`, `MaskFile` | a file that fits |
| `Referenced[T]` | `Referenced[ComputedTransmission]` | a reference to an output that fits `T` |
| a union of these | `BeamCentre \| Referenced[BeamCentre]` | what one of its members takes |
| a list, `list[X]` | `list[RunFile]` | a list of elements, each as for `X` |

`Referenced[T]` is `Annotated[T, REFERENCED]`, so pydantic, a binding, and any
direct use of the params model see `T`, and take a `T` inline.

**The sources are a type.** The framework derives from the params model a
*request type* (`ess.spec.request_types`): the params type with each
`Referenced[T]` replaced by a reference to an output whose data fit `T`, and
each file field by a file, at any depth. At each field, it is the union of the
sources the table above gives it, each with the type that validates it: the
group's own class written inline, a file, or a reference to an output. The
source of a
value is known from its `@ref` key alone, so the request phase validates a
value by the type of its source, the JSON schema of the request type is the
params schema, and a form offers a widget for each source the field takes.
The backend, the schema, and a form thus agree by construction. The params
model stays the type of the call phase; the record stores the JSON form of
the request type.

| | Params model | Request type |
|---|---|---|
| `reduction: Reduction` | `Reduction` | `Reduction` |
| `transmission: Referenced[ComputedTransmission]` | `ComputedTransmission` | `OutputRef \| AccumulatorRef` |
| `beam_centre: BeamCentre \| Referenced[BeamCentre]` | `BeamCentre` | `BeamCentre \| OutputRef \| AccumulatorRef` |
| `sample: RunFile` | the file's local path, a `Path` | a file, `FileRef`, as `{"@ref": "file", ...}` |
| `centre: GivenCentre \| FindCentre` | the alternative | the alternative, with its tag |
| `samples: list[Sample]`, with `Sample.transmission: Referenced[...]` | `list[Sample]` | a list of a model derived from `Sample`, with a reference at `transmission` |

**One language at every depth.** A *group* is a pydantic model, written as an
object of its fields; a `RootModel` is not a group. The params model is a
group, and a field of it is a *parameter*. Each field of a group, at any
depth, is typed from a closed list:

```python
int, float, str, bool, datetime, an enum, a Literal   # a scalar, possibly constrained
Annotated[Path, File(...)]                            # the type of a file field
Reduction                                             # a group
Annotated[A | B, Field(discriminator='kind')]         # a union of groups
Referenced[T]                                         # T a group
Annotated[A | B, ...] | Referenced[C]                 # a union of both
X | None, list[X]                                     # X not a list, not X | None
```

Any other type is refused, such as `tuple[float, float]`, `int | str`, or
`sc.DataArray`. The framework does not look inside the `T` of
`Referenced[T]`, so an array is a field of such a `T`. A group does not
contain itself. A form and the params schema then know what a request writes
at every field, and a form nests one widget per type:

| Type | Widget |
|---|---|
| a scalar | an input of its type |
| a group | its fields: a tab at a parameter, a card at a field of a group |
| a file field | a picker of files of its kind |
| `Referenced[T]` | a picker of records whose output fits `T` |
| a union of groups | a choice of alternatives, each with its fields |
| a union with members `Referenced[...]` | the choice of its other members, and a picker of records as one more way |
| `list[X]` | elements, added and edited, each the widget of `X` |
| `X \| None` | a choice with the way "None" next to the ways of `X` |

**A group is validated when the request arrives, unless it holds a
reference.** A group that a request writes inline, with scalars and files, is
known when the request arrives: values typed in, and files, resolved to their
identity and local path. It is validated whole then, and every validator of the group runs
with all its fields at hand, such as one that the runs of four spin channels
are distinct. A value that another request computes may not exist yet. So a
group that holds a reference to an output, at any depth, is validated when the
request runs, and its validators see the data read:

```python
class Sample(BaseModel):
    run: RunFile                           # a file, its local path known when the request arrives
    thickness: float = Field(1.0, gt=0)    # its type is checked when the request arrives
    transmission: Referenced[ComputedTransmission]  # another request's output

    @model_validator(mode='after')
    def _check(self) -> Self: ...          # runs when the request runs

class IofQParams(BaseModel):
    reduction: Reduction = Reduction()     # validated when the request arrives
    samples: list[Sample]                  # a table, each row with its run
```

A failure then fails the record, or stops the accumulator for a push. This is
the one cost of a reference at a field of a group: a validator of `Sample`
refuses a value only when the request runs.

A value of several parts is a group: `range: WavelengthRange`, not
`limits: tuple[float, float]`. A value that a workflow may go without is typed
`X | None`, at any depth. Why a choice between modes is a union, and what
`X | None` means, is
[ADR 0008](0008-a-choice-between-ways-is-a-union-of-groups.md).

**A file fills a file field.** A field that takes a file, at any depth, is a
*file field*, typed `Annotated[Path, File(**metadata)]`. The marker
`ess.spec.File` holds the metadata that a fitting file has, each a string, a
number, or a bool; `kind` is `'raw'` unless given. `ess.spec` provides the
types of the common kinds, so that each kind is spelled once; a spec writes
another kind by hand:

```python
from ess.spec import MaskFile, RunFile            # RunFile: a raw file; MaskFile: a pixel mask, as XML

CalibrationFile = Annotated[Path, File(kind='calibration')]
```

A request writes a file as `file(...)`, such as `file(run=60339)` or
`file(path=...)`. The backend resolves it to the file's identity in the
request phase, given the marker, so that the file source checks that the file
is known, that the request's proposal may read it, and that its metadata fit
the marker's. The record holds the identity. A file's identity and local path
are therefore known when the request arrives, as a value typed in is, so a
group that holds a file is validated then, and its validators see the path.
The params model holds the local path, a `Path`: a workflow sees paths only,
never identities, and a program without a backend gives the path,
`IofQParams(sample=Path('run-60339.nxs'), ...)`. The marker carries no
validators: one that reads the file would read it from a network mount in the
request phase, and a check of a file's content belongs to the workflow. A
`Path` without `File(...)` is refused in the params, and so is `File(...)` on
anything but a `Path`. No output holds a `Path`, so every path a workflow gets
has passed the access check. The metadata let a picker offer only the files
that fit.

**References to outputs fill groups.** The `T` of `Referenced[T]` is a group,
and a reference to an output fits the field by the class of `T`. A field typed
`Referenced[float]` or `Referenced[sc.DataArray]` would fit every output of that type, so a UI that offers
the outputs that fit would list every number, or every array, that any record
holds. A group's class says what the value is. So a value that another
workflow computes comes as a group, which that workflow outputs, such as a
found beam centre, and an array is a field of a group, such as
`ComputedTransmission.transmission`.

A reference to an output names an output of an earlier record (`OutputRef`:
record, output name, and optionally the key of one element of a list or dict
output), or an output of an accumulator (`AccumulatorRef`: accumulator and
output name). Only a request holds an `AccumulatorRef`: the framework
replaces it, when it accepts the request, by an output of a record of the
accumulator's state. The JSON form of a reference to an output, and of a file
(`FileRef`: one identity string), is an object with the key `@ref`, which
names its kind:

```json
{"@ref": "file", "file": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "slices", "key": "2"}
```

The key `@ref` is reserved at every depth: an object with it is a reference to
an output or a file. So a framework finds the references and files of a
request without the spec, as a remote client that holds only the serialized
spec must, and a value written inline never reads as a reference. With the
spec, the framework refuses a reference to an output at a field not typed
`Referenced[...]`, and a default whose JSON form holds the key.

The identity string of a `FileRef` is opaque to the spec and interpreted
only by the framework that minted it, which makes it self-describing where it
has several kinds of identity, as a URI does with its scheme:
`pid:20.500.12269/abc`, `run:dream/1`. It compares as a string, so the
framework normalizes it before it enters a record.

A file and a reference to an output differ in where they may be and when they
are resolved:

| | Reference to an output | File |
|---|---|---|
| Where | a field typed `Referenced[T]`, at any depth, or a member of a union there | a file field, at any depth, or a member of a union there |
| Fills | a whole group, `T`, which holds no `Path` | a file field |
| Fits if | the output's class is `T` or a subclass of it | the file's metadata have the marker's metadata |
| Resolved | in the call phase, to the stored output | in the request phase, to the file's identity; the params model holds its local path |
| Found in a record | by its `@ref` key, without the spec | the same |
| Access check at submission | records the client may read | files the proposal may read |

A reference to an output fits a union through its members `Referenced[T]`.
Such a member needs no tag, so the workflow that computes `T` knows nothing of
the specs that take it. No reference fills a whole list: each element is
typed `Referenced[X]`, `list[Referenced[X]]`. A subclass fits a field typed by
its base class too, so a field that takes only the subclass is typed by the
subclass. A
framework checks fit in the request phase, so that a chain of requests is
refused before anything runs. `ess.spec.field_types.fits` states the rule.

A value that some users read from a file and others compute is a union of a
file field and a referenced group, such as
`normalization: RunFile | Referenced[ComputedNormalization] | None`.
The binding gets the file's local path or the group, and reads the file itself.

```python
from ess.spec import RunFile                      # types a file field: the file of a run

class Reduction(BaseModel):                       # a group of scalars
    bins: int = 2

class BeamCentre(BaseModel):                      # written inline, or a finder's output
    x: float = 0.0
    y: float = 0.0

class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):  # by a transmission reduction
    transmission: sc.DataArray                    # not a field type: only a reference fills the group

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()  # the nominal centre
    transmission: Referenced[ComputedTransmission]
    sample: RunFile
    can: RunFile

class IofQ(BaseModel, arbitrary_types_allowed=True):  # a group, so another workflow takes it, Referenced[IofQ]
    intensity: sc.DataArray

class IofQOutputs(BaseModel):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, outputs=IofQOutputs)
```

Two requests, written with essapps: `client` connects to a backend,
`file(...)` names a file, `finder` is the record of a request that finds
the beam centre, so `finder.ref('centre')` names its output `centre`, and `t`
is the record of a transmission reduction:

```python
runs = {'sample': file(run=60339), 'can': file(run=60338), 'transmission': t.ref('transmission')}
client.submit(IOFQ, params={**runs, 'beam_centre': BeamCentre(x=0.012, y=-0.003)})  # written inline
client.submit(IOFQ, params={**runs, 'beam_centre': finder.ref('centre')})          # a reference
```

**Rules.** The models carry two markers for the framework: `Referenced[T]`, at
each field that a reference to an output fills, and `File(...)`, at each field
that a file fills. The rest of what the framework needs, it reads from the
field types (`ess.spec.field_types`). `WorkflowSpec` checks these rules when it
is made, and the module docstring of `ess.spec._workflow_spec` states each
with its reason:

- The names of params and outputs are distinct.
- Each field of the params, at any depth, is of a type of the closed list
  above. A group does not contain itself. A list is not typed
  `list[X] | None`, since a list without elements is `[]`, its element is not
  typed `X | None`, since each element is a value, and it is not a list of
  lists.
- `Referenced[T]` may type any field of the params, but no output. Its `T` is
  a group, which holds no `Path` and no `Referenced[...]` at any depth.
- A union with members `Referenced[...]` carries its discriminator on the
  union of the members written inline, since a reference has no tag.
- A file field has no default but `None`, or `[]` for a list.
- `File(...)` marks a `Path` only, and a `Path` without `File(...)` is
  refused. The marker carries no validators: what it would check of a file is
  metadata, which the fit check covers, or the content, the workflow's to
  read.
- Each output's type is a type, possibly typed `X | None`, or a list or a dict
  of one. No output holds a `Path`, at any depth, a file field among them.
- No type in the params or the outputs is a `NewType` or a type alias of a
  `type` statement, at any depth. A class says what a value is. A name
  assigned a type, such as `CalibrationFile = Annotated[Path, File(kind='calibration')]`, is that type.
  A `NewType` of a shared type would say it only if the fit check looked
  through it. A `NewType` that a sciline graph needs as a key belongs to the
  binding (`keys=`, or a provider that takes the value out of the group).
- A union of groups is discriminated by the name of its tag field, not by a
  callable. The tag field of each alternative is a `Literal` of one string,
  its own, such as `Literal['find']`, without an alias. A union that holds a
  group holds groups only: a value of a type that is not a group has no tag.
  A union may also hold members `Referenced[T]`, which need no tag: the key
  `@ref` tells a reference from a value written inline.
- A group has no serializers, computed fields, aliases, or excluded fields,
  which would make its JSON form differ from the object of its fields. It may
  have field and model validators of any mode. A validator raises
  `ValueError` for a value it does not take.
- A default is a value, never a reference to an output or a file: what a
  request may write at the field, which round-trips and holds no `@ref`
  object at any depth. A field that only a reference fills has no default
  but `None`, or `[]` for a list. A default is the value that
  validating it gives, since pydantic does not validate a default: a group's default is an
  instance, not the object of its fields, and an enum's a member, not its
  value.
- A table has no default (see the multi-run spec below), and carries only
  length constraints, such as `Field(min_length=2)`.
- The params model has no field validators, serializers, or computed
  fields, and no `default_factory` that takes the other values. It may have
  model validators, but the framework never calls them.

By convention a parameter is a group, a union of groups, a file field, or a
list of one. A group is a tab of a form, and the key of a binding
(`PipelineBinding` sets the sciline key of a parameter's class). A scalar
parameter, such as `bins: int`, needs `keys=` in the binding. So does a file
parameter, whose type, `Path`, names no role: `keys=` names the workflow's own
key of the file's path, so that providers take plain paths, as esssans' do:

```python
PipelineBinding(IOFQ, make_pipeline,
                keys={'sample': (Filename[SampleRun],), 'can': (Filename[BackgroundRun],)})
```

Files that vary independently are separate parameters, not fields of one
group. A stage fixes or blanks whole parameters, and `sciline.Stage` decides
what it computes again on each call by the graph alone: all that depends on
the key of a blank. A group is one key, so a blank group of sample, can, and
direct beam would compute the can's branch again on every call, though only
the sample changes. A group of files fits files that always change together;
a provider in `make_pipeline` may take it apart into the workflow's keys.

Each parameter is validated alone: a template, a stage call, or a push gives
some values only, so a check that read several parameters would hold for some
requests of a spec and not for others. A check of one value belongs to the
value's type, as `EdgesModel` checks `stop` against `start`, and so does the
JSON form of a value, which the framework stores and reads back. In the
request phase, each value is validated by its request type, and the record
stores its JSON form, which must read back as it. A parameter that holds no
reference to an output is then validated whole by the params model, with the
local path of each file. In the call phase, the params model itself validates
what the record stores, with the data read in place of each reference to an
output and the local path in place of each file. A list is validated whole,
a table a row at a time. A check across parameters, such as a Q range that the
wavelength band and the detector can reach, is the binding's or the driver's,
and fails when the request runs. A binding may run the model validators,
`IofQParams.model_validate(dict(params))`, where it has the whole instance.

A request that leaves out a parameter, or a field of a group, so that it takes
its default, never skips a correction that a correct reduction needs. The
default of `reduction` is a setting. The can run has no default, so a request
or a template that leaves it out is refused. A correction that a correct
reduction needs, but that a request may still go without, is typed `X | None`
with no default, such as `direct_beam: RunFile | None`: the request writes
`None`, and its record says so. The output is I(Q) either way, and the params
of its record say which corrections were applied. A step whose omission changes
what a reader of the output must do with it, such as a normalization without
which I(Q) is in counts, makes another output type, and so another spec. What
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
`T` of a `Referenced[T]` at a field. An output that another workflow takes is a group, such as
`centre: BeamCentre` or `iofq: IofQ`; one that none takes need not be. Where a
framework stores an output is not a spec concept. Output *selection* (choosing
which sciline targets to compute) is not modeled: like parameter slicing, it is
an implementation notion. Livedata-specific output machinery (`OutputView`,
`Temporality`, windowing) stays in esslivedata.

### Groups, and tables

The models of `ess.spec.parameters`, such as `QEdges` and `WavelengthRange`, are
groups that specs share. A field of plain numbers states its unit in its
declaration or in a unit field beside it, as the edges models do; an array
carries its own unit.

A *table* is a list parameter whose *rows* an accumulator takes a push at a
time, such as the sample runs. Each row is written inline, a file, or a
reference to an output, as the spec types its rows. A
*single-run spec* takes one run per parameter. A *multi-run spec* of the same
reduction takes a table in place of such a parameter. Its params model is a
plain model, and the fields the two models have in common are shared by
inheritance:

```python
class IofQSettings(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre | Referenced[BeamCentre] = Field(default_factory=BeamCentre)
    transmission: Referenced[ComputedTransmission]

class IofQParams(IofQSettings):
    sample: RunFile
    can: RunFile

class IofQMultiParams(IofQSettings):
    sample: list[RunFile] = Field(min_length=1)
    can: list[RunFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title=..., description=...,
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

Each table is a `list` of the single-run type, without a default, so a request
gives every table, `[]` for one without rows, and a template that forgets a
table is refused. `Field(min_length=1)` states that a table needs a row. The
model states the length constraints, so that the model, its schema, and the
backend agree that the table has a row. The field `tables` of `WorkflowSpec` is
a tuple of the names of the tables. A list parameter makes a table only when
`tables` names it. A spec whose outputs differ from those of a single-run spec,
such as a stitch of all angles, or that has no single-run spec, such as a sum
of counts over the runs of a scan, is written the same way:
`runs: list[RunFile] = Field(min_length=1)`, with `tables=('runs',)`.

A value that differs per run, such as a motor position, is in the run's file.
A value that differs between runs, such as a thickness, makes the runs
separate requests; a value shared by all runs of a request is a parameter,
such as one transmission for the summed sample runs.

The rows of a table are distinct, compared as the record stores them: a value
written inline by its JSON form, a file by its identity, and a reference to an
output by its record and output. So two references to equal data are two
rows (the essapps README, `docs/developer/README.md`, section Stages and
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
params schema is the JSON schema of the request type, so it gives each
field the sources it takes, and a UI renders the widget of each field, as the
table above has it. The schema of a file field holds the marker's metadata,
`{'$ref': '#/$defs/FileRef', 'metadata': {...}}`, so that a remote picker
offers only the files that fit. `SerializedWorkflowSpec` states the sources
and the widget of each.

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
identity (which spec, parameter values, and files produced an output) similarly
composes spec identity with deployment context; the spec's contribution is
being serializable and versioned.

`code_revision` is provenance, not identity: an optional git commit or package
version of the code the spec describes, so that a record made from a
development branch is honest about what ran. The interface version stays
`version`.

### Shared parameter vocabulary, scipp-free

`ess.spec.parameters` provides constrained unit enums and range/edges models
with cross-field validation (`stop > start`, log-scale positivity), the models
that esslivedata and package code also define. They contain no scipp: conversion of validated values into scipp
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

esslivedata's params model holds settings only, each written inline: its specs
are the special case of this one with no field typed `Referenced[...]`, so a
request may give their params as an instance of the model, and their params
schema holds no reference to an output. Its data arrive as
streams, which this spec does not model. A value that changes during a stream,
such as a motor position, arrives on a stream of its own and is set as a
*context key* of `StreamProcessor`, never taken from the params; here, such a
value comes with the run. esslivedata's outputs are `sc.DataArray` fields with
default-factory templates used for plotter selection. Here each output has a
type of its own, such as the group `IofQ`, with no template.

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
  source per field, as `Referenced[T]` does. A value used both ways,
  such as a beam centre, then needs a client that reads it and sends it again,
  which loses the record of where it came from, or a spec per source; and
  moving a field between the models breaks every client. With one model and a
  marker per field, a value used both ways is a union in one spec,
  `BeamCentre | Referenced[BeamCentre]`, and marking a field changes one type.
- **The sources of a field worked out from its type.** A group of plain fields
  takes its fields or a reference, a group with an array a reference only,
  a group with a file field its fields only, and a union of groups its
  alternatives only. A property of a type, an array, a file field, or a
  union, then changes how a request fills every field of that type: a group
  that gains a file field takes no reference at any of its fields; every group
  of settings, such as `Reduction`, has a picker of records that no request
  needs; and "typed in, or found by a centre finder" cannot be stated as a
  union.
- **Inputs that also take a value inline.** Keeps two models, but the inputs
  then hold values written inline, which removes the reason for two.
- **A constant spec per typed value**, which outputs the value typed in, for a
  request to reference. Needs no framework change, but a submission per typed
  value.
- **A field typed as the reference.** The binding gets references, not data.
- **A union of the reference and the data**, `ComputedTransmission | OutputRef`.
  Wrong in both phases: it needs a validator that accepts anything that is not
  plain data, and it hides that member from the JSON Schema. `Referenced[T]`
  is `T` to the params type, and only the request type puts a reference in its
  place.
- **A field typed as the data, with validation switched by phase.** The field
  holds a reference where its type says a group, and every validator runs on
  references in the request phase.
- **Every value tagged**, `{"value": ...}` and `{"ref": ...}`. Unambiguous
  without a reserved key, but every value written inline in requests, the log,
  and esslivedata's params gets a wrapper.
- **`$ref` as the key of a reference.** It is the JSON Schema keyword for a
  reference within a schema, so a reference held in a schema, as a default or
  an example, would be read as one.
- **A reference to an output at a parameter of any type**, such as a number
  computed by another record. Every output of the type fits, so a picker of the outputs
  that fit lists every number that any record holds; a group that holds the
  number names what it is.
- **`NewType`s to say what a value is**, such as
  `Transmission = NewType('Transmission', sc.DataArray)` or
  `SampleFile = NewType('SampleFile', Path)`, which give the fit check a type
  of its own and each parameter a sciline key of its own. The fit check must
  then look through `NewType`s, both ways for a plain output to fit, and a
  field typed plain `sc.DataArray` still takes every array that any record
  holds. A class says what a value is, and a sciline key that differs from
  the spec's class is the binding's (`keys=`), or an adapter provider that
  takes the value out of the group.
- **Every parameter a group, as a rule of the spec.** It would protect one
  thing: a binding keys a parameter by its class, so `bins: int` would set the
  sciline key `int`. The binding states that itself, by asking for `keys=`
  for a parameter that is not a group. So a group at each parameter is a
  convention, which gives a form one tab per group.
- **A field of a group of any type that pydantic makes a JSON schema of**,
  such as `limits: tuple[float, float]` or `scales: dict[str, float]`, written
  inline as one value. A form has no widget for it, and no spec needs one: its
  parts are the fields of a group. Such a type is refused, and a group that
  holds one is typed `Referenced[...]` where an output fills it.
- **A reference to an output at a parameter or a row only**, never at a field
  of a group, so that every group is validated when the request arrives. The
  language then differs between a parameter and a field: a sample's computed
  transmission cannot sit next to its run and thickness, a group that holds an
  array is allowed only at a marked parameter, and a field of a group holds
  no nested group and no `X | None` of a scalar. One language at every depth
  costs one rule: a group that holds a reference runs its validators when the
  request runs.
- **The rules applied twice**, by a walk over the fields of a request that
  validates it, and by a schema-only copy of each field and group made for the
  JSON schema, as before the request type. Each restated the table above, and
  they drifted: the walk took a reference to an output at a file field, which
  the schema refused, and left it for the fit check to refuse.
- **A file as a plain `Path` field.** The file source does not check the
  path, and a picker cannot tell which kind of file a field takes.
- **A file model**, a pydantic class for each kind of file, which holds the
  file's identity, such as `class Sample(FileModel, kind='raw')`. Its class
  names the role of a file: the binding keys it by its class, the schema and
  messages name it, and a kind of file inherits metadata by subclassing. But a
  workflow then takes the model, not a path, so a sciline workflow whose
  providers take `Filename[SampleRun]`, as esssans' do, needs an adapter
  provider for each file. With the marker, the params model holds a `Path`, a
  workflow keeps its own keys, set by `keys=`, and its providers take plain
  paths. Files lose their class: the schema and messages name no class, and
  no kind of file inherits metadata by subclassing.
- **A reference to a whole list** filling a list. The table rules,
  such as distinct rows, could then be checked only in the call phase.
- **Rows also compared by the data read**, so that a value written inline and
  a reference to equal data are one row. The data of a reference are often
  arrays, which have no JSON form to compare, so the comparison would cover
  few tables.
- **`@ref` reserved only at fields typed `Referenced[...]`**, plain data
  elsewhere. A reference at another field would then be refused by pydantic's
  type error, such as
  "Input should be a valid number", rather than by the rule that says where a
  reference goes.
- **The framework running model validators of the params model.** A stage and a
  driver never make a whole params instance, so such a check would hold for
  some requests of a spec and not for others.
- **Annotations on the fields that hold data**, marking the format of the
  bytes and the array structure. The field types tell the framework the same.
  The two markers the framework reads, `Referenced[T]` and `File(...)`, say
  how a request fills a field, which no field type says.
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
