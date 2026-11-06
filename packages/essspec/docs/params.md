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
`WorkflowSpec` checks its rules when it is made; [the last section](#what-workflowspec-refuses) lists them.
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

A *parameter* is a field of the params model, such as `reduction`.
A parameter left out takes its default, and so does a field of a group left out.
A parameter that holds no reference may also be given as an instance of its type, such as `'reduction': Reduction(bins=50)`.
pydantic refuses a reference inside an instance, so a group that holds one is given as a dict.
At a file field, a `Path` names the file by its path, as `file(path=...)` does.

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
| file field | `SampleRunFile` | `file(...)` | a picker of files of its kind, narrowed by its role |
| `Referenced[T]` | `Referenced[ComputedTransmission]` | `record.ref(name)` | a picker of records whose output fits `T` |
| union with `Referenced[...]` members | `BeamCentre \| Referenced[BeamCentre]` | what one member takes | the other members, plus "From a record" |
| `X \| None` | `MaskFile \| None` | what `X` takes, or `None` | the ways of `X`, plus "None" |
| `list[X]` | `banks: list[int]` | a list of elements, each as `X` takes | a list of inputs, or a grid for groups |

A *group* is a pydantic model, but not a `RootModel`.
The params model is a group too, so a rule for the fields of a group holds for the parameters.
A *scalar* is an `int`, `float`, `str`, `bool`, or `datetime` (also pydantic's `AwareDatetime` and `NaiveDatetime`), or an enum or `Literal` whose values are `str`, `int`, `float`, or `bool`, possibly constrained.
Any other type is refused, such as `tuple[float, float]` (use a group), `set`, `dict`, `date`, `bytes`, `int | str`, or `sc.Variable` (put it in a group that a reference fills):

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
    groups: list[list[int]]                       # refused: a list of a group that holds a list
    edges: sc.Variable                            # refused: only inside the T of Referenced[T]
```

A list without elements is `[]`, so a list is not typed `list[X] | None`.
Each element is a value, so the element of a list is not typed `X | None`.
A list is not of lists: its element is a group that holds a list, whose class says what each inner list is.
A group does not contain itself, at any depth, so that the walk through the field types ends.

No type in the params or the outputs is a `NewType` or a type alias (`type X = ...`), at any depth: a class says what a value is.
A name assigned a type, such as `RunFile = Annotated[Path, File()]`, is that type, not an alias.
A sample run and a can run are both raw files, typed by their roles, and the binding maps each parameter to a key of the workflow, such as esssans' `Filename[SampleRun]`.
The workflow's `NewType`s live in the workflow package and in the binding, never in the spec.

**Parameters are groups.** By convention a parameter is a group, a union of groups, a file field, or a list of one of these.
A group is one tab of a form, and one key of a pipeline binding.
A scalar parameter, such as `bins: int`, is allowed, but its class names no role, so a `PipelineBinding` needs `keys=` for it, as for a file parameter ([bindings.md](bindings.md#keys)).

Files that vary independently are separate parameters, not fields of one group.
A stage fixes or blanks whole parameters, and `sciline.Stage` computes again on each call all that depends on the key of a blank.
A group is one key, so a blank group of sample, can, and direct beam computes the can's branch again on every call, though only the sample changes.
A group of files fits files that always change together, such as the two channels of a polarized run, which a provider of `make_pipeline` may take apart into the workflow's keys.

## References to outputs: `Referenced[T]`

A field typed `Referenced[T]` is filled by an output of another record, such as a transmission computed by a transmission reduction.
`T` is a group, and an output *fits* the field when it is an instance of `T` or of a subclass.
A field typed `Referenced[sc.DataArray]` would fit every array output, so a picker would list unrelated records; a group's class says what the value is.
So a workflow that computes an array outputs a group that holds it, such as `ComputedTransmission`.
`Referenced[A | B]` is refused; a field that takes either is `Referenced[A] | Referenced[B]`.
The framework does not look inside `T` otherwise, so a field of `T` may be of any type, such as `sc.DataArray`.

`Referenced[T]` is `Annotated[T, REFERENCED]`, so pydantic, the binding, and any direct use of the params model see `T`.
It types a field of the params only: the data of an output are not a reference.
A value typed in by one user and computed by another is a union of both, and the request picks one member:

```python
beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()

{'beam_centre': {'x': 0.012, 'y': -0.003}}        # written inline
{'beam_centre': centre.ref('centre')}             # an output of the record centre
```

A reference fills only a field typed `Referenced[...]`, at any depth, or a member of a union there, such as `specimens[0].transmission`; never a whole list.
`record.ref(name, key)` names one element of a list or dict output, which fits by the element type.
`acc.ref(name)` names an output of an accumulator: when the backend accepts the request, it replaces the reference by one to the record of the accumulator's state at that moment.

| Output | Field | Fits |
|---|---|---|
| `BeamCentre` | `Referenced[BeamCentre] \| None` | yes |
| `BeamCentre` | `BeamCentre \| None` | no: the field is not `Referenced[...]` |
| `BeamCentre` | `Annotated[GivenCentre \| FindCentre, ...] \| Referenced[BeamCentre]` | yes |
| `list[TimeRange]`, with a key | an element of `list[TimeRange \| Referenced[TimeRange]]` | yes |
| `sc.DataArray` | `Referenced[ComputedTransmission]` | no: output the group instead |

`T` holds no `Path` and no `Referenced[...]` at any depth, since no output holds either (see [Outputs](#outputs)).
The *JSON form* of a value is what `model_dump(mode='json')` gives, and the record of a request stores the params in JSON form.
The JSON form of a reference, and of a file, is an object with the key `@ref`, which is reserved at every depth:

```json
{"@ref": "output", "record": "r17", "output": "centre"}
{"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
{"@ref": "file", "file": "run:loki/60339"}
```

The key `@ref` tells a reference from a value written inline, and a file from a reference; then the tag tells the alternatives of a union apart.
A string at a file field is refused.
The record holds each file by the identity that the backend resolved, as in the last line.

## Files

A *file field* is typed `Annotated[Path, File(**metadata)]`.
The marker `File` holds the metadata that a fitting file has; each value is a string, a number, or a bool.

- `kind`, what the file holds: `'raw'` (the file of a run, the default), `'mask'`, or another kind. A file of another kind is refused, and so is a file that lacks any other key of the marker, such as `instrument='loki'`.
- `role`, what the field uses a run for: the run types of `ess.reduce` and the technique packages in snake case, such as `'sample'`, `'background'` (a SANS can), `'transmission'`, `'empty_beam'`, `'vanadium'`, `'reference'`. A role is never refused, since the request states the use by the field it fills, and one run can have several uses: the transmission run of a can can also be the empty beam. A picker offers the runs of the field's role and the runs without a role.

`ess.spec` provides the common types, so that each value is spelled once:

```python
RunFile = Annotated[Path, File()]                         # a raw file; a picker offers every run
SampleRunFile = Annotated[Path, File(role='sample')]      # also BackgroundRunFile, TransmissionRunFile, ...
MaskFile = Annotated[Path, File(kind='mask')]
CalibrationFile = Annotated[Path, File(kind='calibration')]  # another kind, by hand

mask: MaskFile | None = None                              # not Annotated[Path | None, File(...)]
```

A run tagged with the wrong role still fills any field of its kind, by `file(run=...)`; to change what a picker offers, correct it in the file source.
A field takes one kind of file, so a file field is in a union only with `Referenced[...]` members and `None`.

A request writes `file(run=611)` or `file(path=...)`.
The backend resolves it when the request is submitted, checks that the proposal may read the file and that it fits the marker, and records the file's identity.
The workflow gets the file's local path, never the identity.
A program without a backend gives the path directly, `IofQParams(sample=Path('run-611.nxs'), ...)`.

`File(...)` marks a `Path`, once: a `Path` without `File(...)` is refused, and so is `File(...)` on anything else, such as `Annotated[int, File()]`.
A file field takes no constraint, such as `MinLen(2)`.
It may take a validator, such as `AfterValidator(...)`, which sees the local path.
The marker itself carries no validators: one that read the file would read it over a network mount at submission.
A check of a file's content belongs to the workflow.

A value read from a file by some users and computed by others is a union, such as `normalization: RunFile | Referenced[ComputedNormalization] | None`.
The workflow gets the path or the group, and reads the file itself.

## Choices: unions and `None`

A choice between modes is a discriminated union of groups.
Each alternative has a *tag* field, typed as a `Literal` of one string of its own, without an alias.
The discriminator names the tag field, not a callable, and the request always writes the tag:

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

A union that holds a group holds only groups, besides `None` and `Referenced[...]` members: `GivenCentre | float` is refused, and the `float` becomes a field of an alternative.
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
pydantic does not validate a default, and a binding gets it as it is, so a default is the value that validating it gives: a group's default is an instance, not a dict, an enum's is a member, not its value, and it meets the field's constraints.
A `default_factory` takes no other values of the model.
Defaults belong in the model only, not in the workflow.

## Tables: several runs

A spec that combines several runs takes them as *tables*: list parameters that `WorkflowSpec(..., tables=...)` names.
An element of a table is a *row*.
An accumulator takes a table's rows a push at a time ([accumulators.md](../../../docs/developer/accumulators.md)).
A *multi-run spec* shares the other parameters with the *single-run spec* through a base model, and each table is a list of the single-run type:

```python
class IofQMultiParams(IofQSettings):              # IofQSettings: reduction, beam_centre, transmission
    sample: list[SampleRunFile] = Field(min_length=1)
    can: list[BackgroundRunFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

A request gives every table, `[]` for one without rows, so a table has no default: no request leaves out a table by mistake.
Any other list is validated whole, but a table is validated a row at a time, since an accumulator takes a few rows at a time.
So a table carries only length constraints, such as `Field(min_length=1)`, which the backend checks on all rows, and the element type checks a row.
The rows of a table are distinct, compared as the record stores them: a file by its identity, a reference by its record and output.
Two references to equal data are two rows.
A value that differs per run, such as a motor position, is in the run's file; a value that differs between runs, such as a thickness, makes the runs separate requests.
With one row in each table, the multi-run spec gives what the single-run spec gives; `ess.spec.testing.check_one_row` checks it.

## Outputs

The outputs model holds the results.
The title and description of a field are its display metadata.
A client shows the outputs in the order of the fields and may select the first, so the primary output comes first.
A name is of a parameter or of an output, not both.

An output is typed `X`, `X | None`, `list[X]`, or `dict[str, X]` (also `| None`), with `X` any type, arrays among them, but not a union of types.
An output that another spec takes by reference is a group, such as `IofQ` above.
No output holds a `Path` at any depth, a file field among them, so that every file a workflow gets comes from the file source, which checks it.
No output holds `Referenced[...]`, which marks a field of the params.

## When values are validated

Each parameter is validated alone, since a template or a push gives some values only.
An error names the *position* of each problem: the parameter, then field names and list indices, such as `reduction.range.start` or `samples[1].transmission`.

| When | What |
|---|---|
| at submission, before anything runs | each value by its type, with files resolved; every group that holds no reference is validated whole, with its validators, which see the local path of each file in it |
| when the request runs, once the references are read | each group that holds a reference, at any depth, with the data read, since the output may not exist at submission; a failure fails the record, or stops the accumulator for a push |

A validator raises `ValueError` for a value it does not take; any other exception fails the call.
pydantic tries the members of a union in order, so a validator of a member written inline, such as a `model_validator(mode='before')` of `GivenCentre`, also sees a reference to an output, and refuses it with `ValueError`.

The record stores a value written inline in JSON form, and the workflow gets it validated from that form.
So the JSON form of a group is the object of its fields: a field has no alias and no `exclude`, and a group has no serializers and no computed fields.
A group may have field validators and model validators of any mode.
A value written inline must round-trip through JSON: a validator that changes an already valid value, such as converting millimetres to metres, is refused.

A check across parameters, such as a Q range the wavelength band can reach, belongs to the workflow and fails when the request runs.
Since each parameter is validated alone, the params model has no field validators, serializers, or computed fields.
It may have model validators, but the framework never calls them, since a stage and a driver never get a whole params instance; a binding may, with `IofQParams.model_validate(dict(params))`.

## What `WorkflowSpec` refuses

Making a spec raises `pydantic.ValidationError`, which lists each problem with the model and field it concerns.
This is every rule that `WorkflowSpec` checks; the sections above give the reasons.
[workflow_spec_examples_test.py](../tests/workflow_spec_examples_test.py) holds a spec that breaks at least one rule of each group below.

Field types ([What a field may be](#what-a-field-may-be)):

- Each field of the params model, and of each group in it at any depth, is of a field type. `tuple`, `set`, `dict`, `date`, `bytes`, a `RootModel`, an array, and `int | str` are refused.
- A group does not contain itself, at any depth.
- A list is not typed `list[X] | None`, its element is not typed `X | None`, and it is not a list of lists.
- No type is a `NewType` or a type alias, at any depth, also inside the `T` of `Referenced[T]` and in the outputs.

Defaults ([Defaults](#defaults)):

- A default is the value that validating it gives: an instance of a group, not a dict; a member of an enum, not its value. It meets the field's constraints.
- A default is not a reference and names no file.
- A file field, and a field typed `Referenced[T]`, has no default, but `None` if typed `X | None`, or `[]` for a list.
- A `default_factory` takes no other values of the model.

Files ([Files](#files)):

- `File(...)` marks a `Path`, once. `Annotated[int, File()]` and `Annotated[Path | None, File()]` are refused; write `Annotated[Path, File()] | None`.
- A `Path` without `File(...)` is refused.
- A file field takes no constraint, such as `MinLen(2)`. A validator, such as `AfterValidator(...)`, is allowed.
- A file field is in a union only with `Referenced[...]` members and `None`.

`Referenced[T]` ([References to outputs](#references-to-outputs-referencedt)):

- It types a field of the params, at any depth, but no output.
- `T` is a group. `Referenced[float]`, `Referenced[sc.DataArray]`, and `Referenced[A | B]` are refused; write `Referenced[A] | Referenced[B]`.
- `T` holds no `Path`, so no file field, and no `Referenced[...]`, at any depth.

Unions of groups ([Choices](#choices-unions-and-none)):

- The discriminator is the name of the tag field, `Field(discriminator='kind')`, not a callable.
- The tag field of each alternative is a `Literal` of one string, its own, without an alias.
- A union that holds a group holds only groups, besides `None` and `Referenced[...]` members.
- In a union with `Referenced[...]` members, the discriminator is on the union of the members written inline.

Tables ([Tables](#tables-several-runs)):

- `tables` names a parameter of the params model, once.
- A table is typed `list[X]` and has no default.
- A table carries only length constraints, and no other constraint or validator.

Outputs ([Outputs](#outputs)):

- A name is of a parameter or of an output, not both.
- An output is typed `X`, `X | None`, `list[X]`, or `dict[str, X]` (also `| None`), not a union of types.
- No output holds a `Path` or `Referenced[...]`, at any depth.

Params model and groups ([When values are validated](#when-values-are-validated)):

- A field has no alias (`alias`, `validation_alias`, `serialization_alias`) and no `exclude` or `exclude_if`.
- A group has no serializers and no computed fields.
- The params model has no field validators, serializers, or computed fields.
