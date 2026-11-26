# Writing a spec

This page is for a workflow author.
After reading it you can write the params and outputs models of a spec, and you know what a request may write at each field.

A *spec* (`ess.spec.WorkflowSpec`) declares what a workflow accepts and what it returns, without the code that computes it.
A workflow package writes one spec per workflow, and a binding that computes it ([bindings.md](bindings.md)).
The workspace (`ess.apps.Workspace`, which runs requests), forms and clients read everything they need from the spec's two pydantic models, the *params* and the *outputs*.
`ess.spec` adds two markers, `Referenced[T]` and `File(...)`, and reads everything else from the field types.
[roles/workflow-authors.ipynb](../../../docs/developer/roles/workflow-authors.ipynb) walks through a spec, its bindings and a driver on a running example.

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

class IofQ(BaseModel, arbitrary_types_allowed=True):  # a group, so that another spec accepts it as Referenced[IofQ]
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

The workflow receives an instance of `IofQParams`: `reduction` as a `Reduction`, `sample` and `can` as local paths, and `transmission` as the `ComputedTransmission` that the record `t611` computed.
It never sees a reference or a file name.

A *parameter* is a field of the params model, such as `reduction`.
A parameter left out has its default, and so does a field of a group left out.
A parameter without a reference may also be given as an instance of its type, such as `'reduction': Reduction(bins=50)`.
pydantic rejects a reference inside an instance, so a group that holds a reference is given as a dict.
At a file field, a `Path` refers to the file by its path, as `file(path=...)` does.

The module that defines a spec imports pydantic, `ess.spec`, and scipp for the types of arrays.
It imports no workflow package, so that a service can load every spec without the workflow code.

Creating a `WorkflowSpec` checks its models against the rules on this page.
It raises `pydantic.ValidationError`, which lists every problem with the model and field it concerns.

## What a field may be

Every field, at any depth, has one of these *field types*.
The type decides what a request writes there and which widget a form shows ([ADR 0001](developer/adr/0001-minimal-workflow-spec.md)):

| Field type | Example | A request writes | A form shows |
|---|---|---|---|
| scalar | `float`, `Annotated[int, Gt(0)]`, an enum, a `Literal` | its JSON value | an input of its type |
| group | `Reduction` | an object of its fields | its fields |
| union of groups | `GivenCentre \| FindCentre` | the fields of one alternative, with its tag | a choice of alternatives |
| file field | `SampleRunFile` | `file(...)` | a picker of files of its kind and role |
| `Referenced[T]` | `Referenced[ComputedTransmission]` | `record.ref(name)` | a picker of records whose output fits `T` |
| union with `Referenced[...]` members | `BeamCentre \| Referenced[BeamCentre]` | what one member accepts | the inline members, plus "From a record" |
| `X \| None` | `MaskFile \| None` | what `X` accepts, or `None` | what `X` shows, plus "None" |
| `list[X]` | `banks: list[int]` | a list of elements, each as for `X` | a list of inputs, or a grid for groups |

A *group* is a pydantic model that is not a `RootModel`.
The params model is a group too, so a rule for the fields of a group holds for the parameters.
A *scalar* is an `int`, `float`, `str`, `bool` or `datetime` (also pydantic's `AwareDatetime` and `NaiveDatetime`), possibly constrained.
An enum or a `Literal` is a scalar if its values are `str`, `int`, `float` or `bool`.
Any other type is rejected, such as `tuple[float, float]` (use a group), `set`, `dict`, `date`, `bytes`, `int | str`, or `sc.Variable` (put it in a group that a reference fills):

```python
class WavelengthRange(BaseModel):
    start: float
    stop: float

class Reduction(BaseModel):
    bins: int = 100
    banks: list[int] = []                         # a list without elements is [], not None
    q_max: float | None = None
    range: WavelengthRange | None = None          # a group in a group

class Binning(BaseModel):
    limits: tuple[float, float]                   # rejected: use WavelengthRange
    groups: list[list[int]]                       # rejected: use a list of a group that holds a list
    edges: sc.Variable                            # rejected: only inside the T of Referenced[T]
```

A list without elements is `[]`, so a list is not typed `list[X] | None`.
Each element is a value, so the element of a list is not typed `X | None`.
A list is not of lists. Its element is a group that holds a list, and the group's class states what each inner list is.
A group does not contain itself, at any depth.

No type in the params or the outputs is a `NewType` or a type alias (`type X = ...`), at any depth, since a class states what a value is.
A name assigned a type, such as `RunFile = Annotated[Path, File()]`, is not an alias: it is that type.
A sample run and a can run are both raw files, distinguished by their roles.
The binding maps each parameter to a key of the workflow, such as esssans' `Filename[SampleRun]`.
The workflow's `NewType`s live in the workflow package and in the binding, never in the spec.

**Parameters are groups.** By convention a parameter is a group, a union of groups, a file field, or a list of one of these.
A group is one tab of a form, and one key of a pipeline binding.
A scalar parameter, such as `bins: int`, is allowed.
Its class names no role, so a `PipelineBinding` needs `keys=` for it, as for a file parameter ([bindings.md](bindings.md#keys)).

Files that vary independently are separate parameters, not fields of one group.
A stage fixes some parameters and leaves the others, the *blanks*, for its calls ([bindings.md](bindings.md#stages)).
On each call, `sciline.Stage` computes again everything that depends on the key of a blank.
A group is one key, so a blank group of sample, can and direct beam computes the can's branch again on every call, though only the sample changes.
A group of files suits files that always change together, such as the two channels of a polarized run.
A provider in `make_pipeline` may split such a group into the workflow's keys.

## References to outputs: `Referenced[T]`

A field typed `Referenced[T]` is filled by an output of another record, such as a transmission computed by a transmission reduction.
`T` is a group, and an output *fits* the field when it is an instance of `T` or of a subclass.
`Referenced[sc.DataArray]` would fit every array output, and a picker would list unrelated records.
So a workflow that computes an array outputs a group that holds it, such as `ComputedTransmission`.
A field that accepts either of two groups is typed `Referenced[A] | Referenced[B]`.
`Referenced[A | B]` is rejected.
`T` holds no `Path` and no `Referenced[...]` at any depth, since no output holds either ([Outputs](#outputs)).
Apart from that, a field of `T` may be of any type, such as `sc.DataArray`.

`Referenced[T]` is `Annotated[T, REFERENCED]`, so pydantic, the binding and any direct use of the params model see `T`.
A value that one user types in and another computes is a union of both, and the request picks one member:

```python
beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()

{'beam_centre': {'x': 0.012, 'y': -0.003}}        # written inline
{'beam_centre': centre.ref('centre')}             # an output of the record centre
```

A reference fills only a field typed `Referenced[...]`, at any depth, or a member of a union there, such as `specimens[0].transmission`.
It never fills a whole list.
`record.ref(name, key)` refers to one element of a list or dict output, which fits by the element type.
A reference never refers to an accumulator.
A request reads an accumulator's outputs through the record that `client.freeze` returns ([accumulators.md](../../../docs/developer/accumulators.md#freeze)).

| Output | Field | Fits |
|---|---|---|
| `BeamCentre` | `Referenced[BeamCentre] \| None` | yes |
| `BeamCentre` | `BeamCentre \| None` | no: the field is not `Referenced[...]` |
| `BeamCentre` | `Annotated[GivenCentre \| FindCentre, ...] \| Referenced[BeamCentre]` | yes |
| `list[TimeRange]`, with a key | an element of `list[TimeRange \| Referenced[TimeRange]]` | yes |
| `sc.DataArray` | `Referenced[ComputedTransmission]` | no: output the group instead |

The *JSON form* of a value is what `model_dump(mode='json')` returns.
The record of a request stores the params in JSON form.
The JSON form of a reference, and of a file, is an object with the key `@ref`, which is reserved at every depth:

```json
{"@ref": "output", "record": "r17", "output": "centre"}
{"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
{"@ref": "file", "file": "run:loki/60339"}
```

The key `@ref` distinguishes a reference or a file from a value written inline.
The tag then distinguishes the alternatives of a union.
A string at a file field is rejected.
The record holds each file by the identity that the workspace resolved, as in the last line.

## Files

A *file field* is typed `Annotated[Path, File(**metadata)]`.
The marker `File` holds the metadata of the files for the field.
Each value is a string, a number or a bool.
`kind` states what the file holds: `'raw'` (the file of a run, the default), `'mask'`, or another kind.

A file fits a file field if its metadata contain each key and value of the marker, such as `kind='mask'` or `instrument='loki'`.
At a raw file field, `role` is not checked.
It states what the field uses the run for: `'sample'`, `'background'` (a SANS can), `'transmission'`, `'empty_beam'`, `'vanadium'`, `'reference'`, after the run types of `ess.reduce` and the technique packages.
One run can have several uses: the transmission run of a can can also be the empty beam.
A picker offers the runs of the field's role and the runs without a role.
At a file field of another kind, `role` states what the file holds, such as `'direct_beam'` of a calibration, and is checked.

`ess.spec` provides the common types, so that each value is spelled once:

```python
RunFile = Annotated[Path, File()]                         # a raw file; a picker offers every run
SampleRunFile = Annotated[Path, File(role='sample')]      # also BackgroundRunFile, TransmissionRunFile, ...
MaskFile = Annotated[Path, File(kind='mask')]
CalibrationFile = Annotated[Path, File(kind='calibration')]  # another kind, by hand

mask: MaskFile | None = None                              # not Annotated[Path | None, File(...)]
```

A run tagged with the wrong role still fills any raw file field through `file(run=...)`.
To change what a picker offers, correct the role in the file source.
A field accepts one kind of file, so a file field is in a union only with `Referenced[...]` members and `None`.

A request writes `file(run=611)` or `file(path=...)`.
The workspace resolves the file when the request is submitted.
It checks that the proposal may read the file and that the file fits the marker, and it records the file's identity.
The workflow receives the file's local path, never the identity.
A program without a workspace passes the path directly: `IofQParams(sample=Path('run-611.nxs'), ...)`.

`File(...)` marks a `Path`, once.
A `Path` without `File(...)` is rejected, and so is `File(...)` on anything else, such as `Annotated[int, File()]`.
A file field accepts no constraint, such as `MinLen(2)`.
It may have a validator, such as `AfterValidator(...)`, which sees the local path.
A check of a file's content belongs to the workflow.

A value that some users read from a file and others compute is a union, such as `normalization: RunFile | Referenced[ComputedNormalization] | None`.
The workflow receives the path or the group, and reads the file itself.

## Choices: unions and `None`

A choice between modes is a discriminated union of groups.
Each alternative has a *tag* field, typed as a `Literal` of one string of its own, without an alias.
The discriminator is the name of the tag field, not a callable, and the request always writes the tag:

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

A union that holds a group holds only groups, besides `None` and `Referenced[...]` members.
`GivenCentre | float` is rejected: make the `float` a field of an alternative.
A `Referenced[T]` member has no tag, so that the workflow that computes `T` needs to know nothing of the specs that accept it.
The discriminator is on the union of the inline members:

```python
beam_centre: (
    Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    | Referenced[BeamCentre]
) = GivenCentre()
```

`X | None` means *without X*: no mask, no direct-beam correction.
A form shows it that way, so a value obtained in one of several ways is a union of alternatives, never `None` for one of them:

```python
mask: MaskFile | None = None                      # Mask: ( ) A file  (•) None
beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
                                                  # Beam centre: (•) Given  ( ) Find
# not: beam_centre: GivenCentre | None = None, whose "None" reads as "no beam centre"
```

An optional part whose fields need values is `X | None`, such as `range: WavelengthRange | None`.
A `bool` field switches an optional part whose fields all have defaults.
In a table, a choice for one field of a row is a union at that field of the row's group, such as `transmission: TransmissionFraction | Referenced[ComputedTransmission]`.
A choice for a whole row is a table whose rows are a union, `list[A | B]`.
A choice for all rows is a parameter of its own.
[ADR 0008](developer/adr/0008-a-choice-between-ways-is-a-union-of-groups.md) states the reasons.

## Defaults

A request that leaves a value out never skips a correction that a correct reduction needs:

```python
reduction: Reduction = Field(default_factory=Reduction)  # a setting: has a default
can: BackgroundRunFile                            # no default: leaving it out is rejected
direct_beam: RunFile | None                       # needed, but a request may go without: it writes None
mask: MaskFile | None = None                      # not needed: may default to None
```

The params of a record show which corrections were applied.
A step whose omission changes what a reader must do with the output, such as its unit or dimensions, makes another output type, and so another spec.

A default is a value that a request could write: never a reference or a file.
A file field, and a field that only a reference fills, has no default.
Exceptions are `None` for a field typed `X | None`, and `[]` for a list.
pydantic does not validate a default, and a binding receives it as it is.
So a default is the value that validating it returns: a group's default is an instance, not a dict, and an enum's default is a member, not its value.
A default meets the field's constraints.
A `default_factory` accepts no other values of the model.
It returns the same value at each call, such as `list` or a group's class, and not `datetime.now` or a counter.
The reason is that a record holds its request with the defaults filled in, and a request is compared with a record's.
Defaults belong in the model only, not in the workflow.

## Tables: several runs

A spec that combines several runs accepts them as *tables*: list parameters named in `WorkflowSpec(..., tables=...)`.
An element of a table is a *row*.
An accumulator receives a table's rows a push at a time ([accumulators.md](../../../docs/developer/accumulators.md)).
A *multi-run spec* shares the other parameters with the *single-run spec* through a base model.
Each table is a list of the single-run type:

```python
class IofQMultiParams(IofQSettings):              # IofQSettings: reduction, beam_centre, transmission
    sample: list[SampleRunFile] = Field(min_length=1)
    can: list[BackgroundRunFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

A request writes every table, `[]` for one without rows.
So a table has no default, and no request leaves out a table by mistake.
A table is validated a row at a time, since an accumulator receives a few rows at a time.
So a table carries only length constraints, such as `Field(min_length=1)`, which the workspace checks on all rows.
The element type checks a row.
The rows of a table are distinct, compared as the record stores them: a file by its identity, a reference by its record and output.
Two references to equal data are two rows.
A value that differs per run, such as a motor position, is in the run's file.
A value that a request writes per run, such as a thickness, makes the runs separate requests.
With one row in each table, the multi-run spec returns what the single-run spec returns.
`ess.spec.testing.check_one_row` checks this ([bindings.md](bindings.md#testing-the-symmetries)).

## Outputs

The outputs model holds the results.
The title and description of a field are its display metadata.
A client shows the outputs in the order of the fields and may select the first, so the primary output comes first.
No name is both a parameter and an output.

An output is typed `X`, `X | None`, `list[X]` or `dict[str, X]` (also `| None`).
`X` is any type except a union, arrays included.
An output that another spec accepts by reference is a group, such as `IofQ` above.
No output holds a `Path` at any depth, so that every file a workflow receives comes from the file source, which checks it.
No output holds `Referenced[...]`, which marks a field of the params.

## When values are validated

Each parameter is validated alone, since a template or a push contains only some values.
An error states the *position* of each problem: the parameter, then field names and list indices, such as `reduction.range.start` or `samples[1].transmission`.

At submission, before anything runs, each value is validated by its type, with files resolved.
A group without a reference is then validated whole, with its validators, which see the local path of each file in it.
A group with a reference, at any depth, is validated when the request runs, with the data read, since the output may not exist at submission.
A failure then fails the record, or stops the accumulator for a push.

A validator raises `ValueError` for a value it rejects.
Any other exception fails the call.
pydantic tries the members of a union in order.
So a validator of an inline member, such as a `model_validator(mode='before')` of `GivenCentre`, also sees a reference to an output, and must reject it with `ValueError`.

The record stores a value written inline in JSON form, and the workflow receives it validated from that form.
So the JSON form of a group is the object of its fields.
A field has no alias, no `exclude`, and no serializer in its type, such as `Annotated[float, PlainSerializer(...)]`.
A group has no serializers and no computed fields.
A group may have field validators and model validators of any mode.
A value written inline must read back from its JSON form as the same value.
So a value whose validator changes an already valid value, such as by converting millimetres to metres, is rejected at submission.

Values that a check compares, such as the start and stop of a range, are fields of one group, which a request always writes whole.
A check across parameters, such as of a Q range that the wavelength band can reach, belongs to the workflow and fails when the request runs.
A stage and a driver never receive a whole params instance.
So the params model has no validators, serializers or computed fields, and no `__init__` of its own.

## The digest of a spec

`spec.digest` is a short hex string computed from the name, type and requiredness of each field of the params and outputs models, and of each group inside them, at any depth.
It distinguishes a record made with other models under the same name and version from a record of the bound spec ([ADR 0009](../../../docs/developer/adr/0009-a-record-of-another-spec-stays-readable.md)).

| Changes the digest | Does not |
|---|---|
| a field renamed, added, or removed | a title, a description, a docstring |
| a field's type, such as `float` to `int`, `X` to `X \| None`, `X` to `Referenced[X]` | a default |
| a field made required or optional | a constraint, such as `Field(gt=0)`, a validator, or a discriminator |
| the values of a `Literal` or an enum | the order of fields, or of the members of a union |
| the metadata of a `File(...)` marker, a role included | the name or module of a group's class |
| the `extra` setting of a group | the names of an enum's members, or an enum replaced by a `Literal` of its values |
| the root type of a `RootModel` | the spec's name and version |
| the module or name of a class that is not a group | |

A type is described so:

```python
class Centre(BaseModel):            # a group: by its fields, and by `extra` unless default
    x: float                        # a builtin, a time, Path, a scipp type: by its name
    unit: Literal['mm', 'm']        # a Literal or an enum: by its values, {'values': ['m', 'mm']}

class Masks(RootModel[dict[str, sc.Variable]]): ...   # by its root type, dict[str, Variable]

class Shapes(RootModel[list[Polygon]]): ...  # any other class, such as Polygon: by module and name, 'mypackage.shapes.Polygon'
```

`Referenced[T]` is described by the fields of `T`, since a reference is checked by class when it is used.
So `T` replaced by another class with the same fields keeps the digest, and a reference made with the old class is rejected when it is checked.
Requiredness counts, although a record stores its params with the defaults filled in, since a parameter that becomes required breaks callers.
Settings of a group's `model_config` other than `extra`, such as `ser_json_inf_nan`, do not change the digest.
A type that is not resolved, such as a forward reference before `model_rebuild()`, has no digest: `spec.digest` raises `ValueError`, naming the field.
The digest does not see a change of what the workflow computes, so the version remains the author's statement of any change a consumer can observe.

A package stores the digests of its specs in a file and checks them in its tests, so that a model changed without a new version fails there:

```python
from ess.spec.testing import check_digests, write_digests

check_digests([IOFQ, BEAM_CENTRE], Path(__file__).with_name('spec_digests.json'))
write_digests([IOFQ, BEAM_CENTRE], path)  # once, to make the file: {"sans-iofq/v1": "9f2c...", ...}
```

The file holds the given specs and nothing else, so a test that finds no specs fails.
A changed interface gets a new version.
Update the file under the same version only for a change that is meant to keep the version.
Records of the old models are then of an unbound spec.
