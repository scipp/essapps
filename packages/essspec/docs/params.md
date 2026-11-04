# Writing a spec

A *spec* (`ess.spec.WorkflowSpec`) declares what a workflow takes and what it returns, without the code that computes it.
Every workflow package writes one spec per workflow.
The framework, its forms, and its clients read everything they need from the spec's two pydantic models:

| Model | Holds | A request gives | The workflow gets |
|---|---|---|---|
| params | everything the workflow takes: settings, files, data | each value written inline, as a file, or as a reference to an output of another record | the values, each file as a local path, and the data each reference names |
| outputs | the results | (nothing) | (it returns them) |

The models are ordinary pydantic models.
The framework adds two markers, `Referenced[T]` and `File(...)`, and reads the rest from the field types.
`WorkflowSpec` checks its rules when it is made; its docstring lists them.
[roles/workflow-authors.ipynb](../../../docs/developer/roles/workflow-authors.ipynb) walks through a spec, its bindings, and a driver on a running example.

**In short:**

- A parameter is a *group* (a pydantic model), a union of groups, a file, or a list of one of these. Numbers are fields of groups.
- A field that another workflow's output fills is typed `Referenced[T]`, where `T` is a group.
- A field that a file fills is typed `Annotated[Path, File(...)]`, usually through a type that `ess.spec` provides, such as `SampleRunFile`.
- A choice between ways of getting a value is a union of groups. `X | None` means "without X".
- A default never skips a correction that a correct reduction needs.
- A check of one value belongs to its type. A check across parameters belongs to the workflow.

## A first spec

```python
from ess.spec import BackgroundRunFile, Referenced, SampleRunFile, WorkflowSpec

class Reduction(BaseModel):                       # a group of scalars
    bins: int = 100

class BeamCentre(BaseModel):                      # written inline, or an output of a first reduction
    x: float = 0.0
    y: float = 0.0

class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):  # an output of a transmission reduction
    transmission: sc.DataArray                    # only a reference fills this group

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()
    transmission: Referenced[ComputedTransmission]
    sample: SampleRunFile                         # the sample run's raw file
    can: BackgroundRunFile                        # the empty-can run's raw file

class IofQ(BaseModel, arbitrary_types_allowed=True):  # a group, so that another spec takes it as Referenced[IofQ]
    intensity: sc.DataArray

class IofQOutputs(BaseModel):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

A client writes a request for `IOFQ` as:

```python
client.submit(IOFQ, params={'reduction': {'bins': 50},            # written inline
                            'sample': file(run=611),              # a file
                            'can': file(run=614),
                            'transmission': t611.ref('transmission')})  # an output of the record t611
```

A parameter left out takes its default, and so does a field of a group left out.
The workflow gets an instance of `IofQParams`: `reduction` as a `Reduction`, `sample` and `can` as local paths, and `transmission` as the `ComputedTransmission` that the record `t611` computed.
It never sees a reference or a file name.

The module that defines a spec imports pydantic, `ess.spec`, and scipp for the types of arrays, but no workflow package, so that a service can load every spec without the workflow code.

## What a field may be

Every field, at any depth, has one of these *field types*.
The type decides what a request writes there and which widget a form generated from the spec shows ([ADR 0001](developer/adr/0001-minimal-workflow-spec.md)):

| Field type | Example | A request writes | A form shows |
|---|---|---|---|
| scalar | `float`, `Annotated[int, Gt(0)]`, an enum, a `Literal` | its JSON value | an input of its type |
| group | `Reduction` | an object of its fields | its fields: a tab for a parameter, a card for a field |
| union of groups | `GivenCentre \| FindCentre` | one alternative's fields, with its tag | a choice of alternatives |
| file field | `SampleRunFile` | `file(...)` | a picker of files of its kind and role |
| `Referenced[T]` | `Referenced[ComputedTransmission]` | `record.ref(name)` | a picker of records whose output fits `T` |
| union with `Referenced[...]` members | `BeamCentre \| Referenced[BeamCentre]` | what one member takes | the other members, plus "From a record" |
| `X \| None` | `MaskFile \| None` | what `X` takes, or `None` | the ways of `X`, plus "None" |
| `list[X]` | `banks: list[int]` | a list of elements, each as `X` takes | a list of inputs, or a grid for groups |

A *scalar* is an `int`, `float`, `str`, `bool`, or `datetime`, or an enum or `Literal` whose values are `str`, `int`, `float`, or `bool`, possibly constrained.
Any other type is refused, such as `tuple[float, float]` (use a group) or `sc.Variable` (put it in a group that a reference fills):

```python
class WavelengthRange(BaseModel):
    start: float
    stop: float

class Reduction(BaseModel):
    bins: int = 100
    banks: list[int] = []                         # without elements [], not None
    q_max: float | None = None
    range: WavelengthRange | None = None          # a group in a group

class Binning(BaseModel):
    limits: tuple[float, float]                   # refused: use WavelengthRange
    edges: sc.Variable                            # refused: only inside the T of Referenced[T]
```

No type in the params or the outputs is a `NewType`: a class says what a value is.
A sample run and a can run are both raw files, typed by their roles, and the binding maps each parameter to a key of the workflow, such as esssans' `Filename[SampleRun]`.
The workflow's `NewType`s live in the workflow package and in the binding, never in the spec.

**Parameters are groups.** By convention a parameter is a group, a union of groups, a file field, or a list of one of these.
A group is one tab of a form, and one key of a pipeline binding.
A scalar parameter, such as `bins: int`, is allowed, but its class names no role, so a `PipelineBinding` needs `keys=` for it, as for a file parameter.

## References to outputs: `Referenced[T]`

A field typed `Referenced[T]` is filled by an output of another record, such as a transmission computed by a transmission reduction.
`T` is a group, and an output *fits* the field when it is an instance of `T` or of a subclass.
A field typed `Referenced[sc.DataArray]` would fit every array output, so a picker would list unrelated records; a group's class says what the value is.
So a workflow that computes an array outputs a group that holds it, such as `ComputedTransmission`.

`Referenced[T]` is `Annotated[T, REFERENCED]`, so pydantic, the binding, and any direct use of the params model see `T`.
A value typed in by one user and computed by another is a union of both, and the request picks one member:

```python
beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()

{'beam_centre': {'x': 0.012, 'y': -0.003}}        # written inline
{'beam_centre': centre.ref('centre')}             # an output of the record centre
```

A reference fills only a field typed `Referenced[...]`, at any depth, or a member of a union there, such as `specimens[0].transmission`; never a whole list.
`record.ref(name, key)` names one element of a list or dict output, which fits by the element type.

| Output | Field | Fits |
|---|---|---|
| `BeamCentre` | `Referenced[BeamCentre] \| None` | yes |
| `BeamCentre` | `BeamCentre \| None` | no: the field is not `Referenced[...]` |
| `BeamCentre` | `Annotated[GivenCentre \| FindCentre, ...] \| Referenced[BeamCentre]` | yes |
| `list[TimeRange]`, with a key | an element of `list[TimeRange \| Referenced[TimeRange]]` | yes |
| `sc.DataArray` | `Referenced[ComputedTransmission]` | no: output the group instead |

`T` holds no `Path` and no `Referenced[...]` at any depth, and no output holds a `Path`: every path a workflow gets has passed the access check of the file source.
In a request, the JSON form of a reference, and of a file, is an object with the key `@ref`, which is reserved at every depth:

```json
{"@ref": "output", "record": "r17", "output": "centre"}
{"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
{"@ref": "file", "file": "run:loki/60339"}
```

## Files

A *file field* is typed `Annotated[Path, File(**metadata)]`.
The marker `File` holds the metadata that a fitting file has, so that a picker offers only files of the right kind and role:

- `kind`, what the file holds: `'raw'` (the file of a run, the default), `'mask'`, or another kind.
- `role`, what a run was measured as: the run types of `ess.reduce` and the technique packages in snake case, such as `'sample'`, `'background'` (a SANS can), `'transmission'`, `'empty_beam'`, `'vanadium'`.

`ess.spec` provides the common types, so that each value is spelled once:

```python
RunFile = Annotated[Path, File()]                         # a raw file of any role
SampleRunFile = Annotated[Path, File(role='sample')]      # also BackgroundRunFile, TransmissionRunFile, ...
MaskFile = Annotated[Path, File(kind='mask')]
CalibrationFile = Annotated[Path, File(kind='calibration')]  # another kind, by hand

mask: MaskFile | None = None
```

A field that takes a run of any role, or of a role the file source does not know, is typed `RunFile`.
A file of another kind or role is refused; a run tagged with the wrong role is corrected in the file source.

A request writes `file(run=611)` or `file(path=...)`.
The backend resolves it when the request is submitted, checks that the proposal may read the file and that its metadata fit the marker, and records the file's identity.
The workflow gets the file's local path.
A program without a backend gives the path directly, `IofQParams(sample=Path('run-611.nxs'), ...)`.

A `Path` without `File(...)` is refused, and so is `File(...)` on anything but a `Path`.
The marker carries no validators: one that read the file would read it over a network mount at submission.
A check of a file's content belongs to the workflow.

A value read from a file by some users and computed by others is a union, such as `normalization: RunFile | Referenced[ComputedNormalization] | None`.
The workflow gets the path or the group, and reads the file itself.

## Choices: unions and `None`

A choice between modes is a discriminated union of groups.
Each alternative has a *tag* field, typed as a `Literal` of one string of its own, and the request always writes the tag:

```python
class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0
    y: float = 0.0

class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
    tolerance: float = 0.1

beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()

{'beam_centre': {'kind': 'find', 'tolerance': 0.05}}
```

A `Referenced[T]` member has no tag, so that the workflow computing `T` needs to know nothing of the specs that take it.
The discriminator sits on the union of the inline members:

```python
beam_centre: (
    Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    | Referenced[BeamCentre]
) = GivenCentre()
```

`X | None` means *without X*: no mask, no direct-beam correction.
It must read that way in a form, so a value obtained in one of several ways is a union naming each way, never `None` for one of them:

```python
mask: MaskFile | None = None                      # Mask: ( ) A file  (•) None
beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
                                                  # Beam centre: (•) Given  ( ) Find
# not: beam_centre: GivenCentre | None = None, whose "None" reads as "no beam centre"
```

An optional part whose fields need values is `X | None`, such as `range: WavelengthRange | None`.
A `bool` field switches an optional part whose fields all have defaults.
[ADR 0008](developer/adr/0008-a-choice-between-ways-is-a-union-of-groups.md) gives the reasons.

## Defaults

A request that leaves a value out never skips a correction that a correct reduction needs:

```python
reduction: Reduction = Field(default_factory=Reduction)  # a setting: has a default
can: BackgroundRunFile                            # no default: leaving it out is refused
direct_beam: RunFile | None                       # needed, but a request may go without: it writes None
mask: MaskFile | None = None                      # not needed: may default to None
```

The params of a record say which corrections were applied.
A step whose omission changes what a reader must do with the output, such as its unit or dimensions, makes another output type, and so another spec.

A default is a value that a request could write: never a reference or a file.
A field that only a reference fills, and a file field, has no default, or `None` if typed `X | None`, or `[]` for a list.
Defaults belong in the model only, not in the workflow.

## Tables: several runs

A spec that combines several runs takes them as *tables*: list parameters that `WorkflowSpec(..., tables=...)` names.
An accumulator takes a table's rows a push at a time ([accumulators.md](../../../docs/developer/accumulators.md)).
A *multi-run spec* shares the other parameters with the *single-run spec* through a base model, and each table is a list of the single-run type:

```python
class IofQMultiParams(IofQSettings):              # IofQSettings: reduction, beam_centre, transmission
    sample: list[SampleRunFile] = Field(min_length=1)
    can: list[BackgroundRunFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

A table has no default and carries only length constraints.
The rows of a table are distinct.
A value that differs per run, such as a motor position, is in the run's file; a value that differs between runs, such as a thickness, makes the runs separate requests.
With one row in each table, the multi-run spec gives what the single-run spec gives; `ess.spec.testing.check_one_row` checks it.

## When values are validated

Each parameter is validated alone, since a template or a push gives some values only.

| When | What |
|---|---|
| at submission, before anything runs | each value by its type, with files resolved; every group that holds no reference is validated whole, with its validators |
| when the request runs, once the references are read | each group that holds a reference, with the data read; a failure fails the record |

A validator raises `ValueError` for a value it does not take.
A value written inline must round-trip through JSON, since the record stores its JSON form: a validator that changes an already valid value, such as converting millimetres to metres, is refused.
A check across parameters, such as a Q range the wavelength band can reach, belongs to the workflow and fails when the request runs.
The params model may have model validators, but the framework never calls them; a binding may, with `IofQParams.model_validate(dict(params))`.
