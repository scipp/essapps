# ESS data-reduction framework: the API

**Status: the in-process backend (`local()`) implements this API, including freeze, `select=`, and persist to a fake store. Not implemented: the service, `client.publish`, and software versions in provenance.**

This document shows by example what workflow authors, app authors, and notebooks write.
It states each rule once.
The [ADRs](adr/index.md) give the reasons, and [the requirements](../requirements/README.md) the goals.
[user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.
[prior-art/](prior-art/README.md) holds studies of other systems.
The first release combines no runs and tunes no parameters interactively.
It needs neither the section Stages and accumulators nor the loop over arrivals in Controllers.

## From a for loop

A notebook without the framework:

```python
curves = {}
for run in (60339, 60340, 60341):
    curves[run] = reduce_iofq(run, can=60338, transmission=transmission, bins=100)
```

The same with the framework:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, params={'reduction': Reduction(bins=100),
                                               'sample': file(run=run),
                                               'can': file(run=60338),
                                               'transmission': t.ref('transmission')})
client.wait(records)
client.output(records[60339], 'iofq')
```

`client` connects to the framework (see Client and backend).
`IOFQ` is the spec of the I(Q) reduction, which names its params and outputs (see Specs and bindings).
`file(run=run)` names the raw data of a run (see Files), here the sample run, and `file(run=60338)` the empty-can run that each reduction subtracts.
`t` is the record of a transmission reduction of the sample, and `t.ref('transmission')` gives its output, the sample's transmission, to each reduction (see Specs and bindings).
`client.submit` returns a *record* at once.
The three reductions run in parallel, possibly on another machine.
`client.wait` blocks until they have finished, and `client.output` reads an output by name.

What the framework adds to the plain loop:

- Each call is kept as a record. The same process finds it later, and on the service, other notebooks and programs too.
- Output values are kept for the client that asked, and for every client of the proposal once persisted.
- Calls can run elsewhere and in parallel.
- Every output can answer where it came from: which spec, which parameter values, which files, which software versions.

## Terms

The framework is essapps (`ess.apps`) and essspec (`ess.spec`), in this repository.

| Term | What it is |
|---|---|
| backend | the process that runs requests and keeps records |
| client | a connection to one backend, for one proposal (an experiment at ESS) |
| spec | a workflow's name, version, params, and outputs |
| params, outputs | the two models of a spec: everything the workflow takes, and its results (see Specs and bindings) |
| parameter | a field of the params model |
| JSON form | what a value dumps to as JSON, `model_dump(mode='json')`. The record of a request stores the JSON form of each value. |
| written inline | given as the value itself, such as `Reduction(bins=100)` or `{'bins': 100}`, not as a reference to an output |
| reference to an output | names an output of a record, `record.ref('centre')`, or of an accumulator. It fills a field typed `Referenced[T]`. Its JSON form is an object with the key `@ref` (see References). |
| `Referenced[T]` | the type of a field that a reference to an output fills, at any depth, with data that fit the group `T`, such as `transmission: Referenced[ComputedTransmission]`. A request writes a field of any other type, inline or as a file. |
| group | a pydantic model, such as `Reduction`, the settings of one reduction step. A request writes a group as an object of its *fields*. A `RootModel` is not a group. |
| position | where a value sits in the params, by field names and list indices, such as `reduction.bins` or `sample[2]`. An error names the position of each problem. |
| scalar | an `int`, `float`, `str`, `bool`, or `datetime`, or an enum or a `Literal` whose values are of these types, possibly with constraints, such as `Annotated[float, Gt(0)]` |
| field type | the type of a field, at any depth: a scalar, a file field, a group, a union of groups, `Referenced[T]`, a union of members written inline and members `Referenced[...]`, `X \| None`, or `list[X]`. Any other type, such as a scipp array, is refused, but inside the `T` of `Referenced[T]`. |
| file field, file | a *file field* is a field that a file fills, typed `Annotated[Path, File(**metadata)]`, such as `RunFile = Annotated[Path, File()]`. A request writes a *file* there as `file(...)`, such as `file(run=611)`. The backend resolves it when the request is submitted, and the params model holds the file's local path, a `Path` (see Files in Specs and bindings). A *file parameter* is a parameter that is a file field, or a list of one. |
| fits | a reference to an output fits a field `Referenced[T]` when the data it names are of the class `T` or a subclass of it (see References) |
| binding | what the backend calls to compute a spec's requests (see What a binding provides) |
| request | a spec and its params, each value written inline, a file, or a reference to an output |
| request phase, call phase | the *request phase* is what the client and the backend do with a request before any data is read: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |
| logged | recorded by the backend in the proposal's *history*. `submit` and the other calls that start work return once logged. |
| record | a request as the backend logged it. A record never changes, only its status. |
| label, member | names under which records are found later |
| template | a spec with *fixed values* for some parameters and *blanks* for the others |
| fixed value | the value a template gives a parameter, as a request does |
| blank | a parameter that a template leaves open, a table among them |
| store, persist | the *store* keeps outputs outside the backend's memory. To *persist* an output is to have it written there. |
| file source | where a backend finds files |
| element, table, row | a list, `list[X]`, holds *elements*. A *table* is a list parameter whose elements an accumulator takes a push at a time; an element of a table is a *row* (see Combining runs: tables). |
| stage | a template the backend keeps for a client, to compute its fixed part once |
| accumulator | a template whose blanks are tables, kept by the backend for a client |
| push | one call that gives an accumulator rows of one or more of its tables |
| driver | the object a binding makes from an accumulator's fixed values, which takes the pushes. The workflow package writes it, and the framework never looks inside it. |
| added | a push is added once the backend has pushed its rows into the driver, in the order logged |
| controller | client code that decides over time what to submit, push, and release, such as the trigger loop (see Controllers) |
| state n | the accumulator after its first n pushes |
| plain request | a request with every value given. A call through a stage and a state of an accumulator have plain requests too. |
| pin | to take an accumulator's state after the pushes logged so far (see Pins) |
| freeze | to pin an accumulator's last state and turn it into a record, without a copy |


## Client and backend

```python
client = local(proposal='p1', files=..., bind=[iofq, ...], store=...)
```

`local` makes a backend in the user's process and a client of it, for interactive work such as a notebook.
`files` is the file source (see Files), `bind` lists a binding for each spec (see Specs and bindings), and `store` is optional (see Persist).
Batch and automatic reduction run on *the service*, a backend that DMSC hosts.
`connect('https://reduce.example', proposal='p1')` makes a client of it.
The service runs only workflows from installed packages.

## Specs and bindings

A *workflow* is a computation that a package offers.

**Spec.** A workflow package declares a spec (`ess.spec.WorkflowSpec`) for each workflow.
A spec has two pydantic models:

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | everything the workflow takes: settings, files, data | each value written inline, a file, or a reference to an output | the values, files with their local paths, and the data the references name |
| outputs | results | (none) | the data the workflow returns |

```python
RunFile = Annotated[Path, File()]                 # types a file field: the file of a run, raw

class Reduction(BaseModel):                       # a group of scalars
    bins: int = 100

class BeamCentre(BaseModel):                      # a group: written inline, or an output of a first reduction
    x: float = 0.0
    y: float = 0.0

class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):  # by a transmission reduction
    transmission: sc.DataArray                    # not a field type: only a reference fills the group

class IofQSettings(BaseModel):                    # the parameters that all runs share
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()  # the nominal centre, or a reference
    transmission: Referenced[ComputedTransmission]  # a reference to an output

class IofQParams(IofQSettings):
    sample: RunFile                               # the sample run
    can: RunFile                                  # the empty-can run

class IofQ(BaseModel, arbitrary_types_allowed=True):  # a group, so another workflow takes it, Referenced[IofQ]
    intensity: sc.DataArray

class IofQOutputs(BaseModel):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

A request for `IOFQ` gives each parameter written inline, as a file, or as a reference to an output:

```python
{'reduction': {'bins': 50},                       # written inline: the group's JSON form
 'sample': file(run=611),                         # a file
 'can': file(run=614),
 'transmission': t611.ref('transmission')}        # a reference to an output of the record t611
```

`TRANSMISSION` is the spec of a transmission reduction, and `t611` the record of a `TRANSMISSION` request for sample run 611.
Its output `transmission` is a `ComputedTransmission`.
A parameter left out takes its default, and so does a field of a group left out.

A list, `list[X]`, holds *elements*, each of type `X`; an element of a table is a *row* (see Combining runs: tables).
The spec marks each field that a reference to an output fills, `Referenced[T]`.
A field that takes a value typed in by one user and computed by another is a union of both, `BeamCentre | Referenced[BeamCentre]`, and the request chooses one member.
The type of a field says what a request writes there, with `| None` removed:

| Type | Example | A request writes |
|---|---|---|
| a scalar | `float`, `Annotated[int, Gt(0)]` | its JSON value |
| a group | `Reduction`, `BeamCentre` | an object of its fields, each as its type says |
| a union of groups | `GivenCentre \| FindCentre` (see below) | an object of one alternative's fields, with its tag |
| the type of a file field | `RunFile` | a file that fits, `file(...)` (see Files) |
| `Referenced[T]` | `Referenced[ComputedTransmission]` | a reference to an output that fits `T` |
| a union of these | `BeamCentre \| Referenced[BeamCentre]` | what one of its members takes |
| a list, `list[X]` | `sample: list[RunFile]` (see Combining runs: tables) | a list of elements, each as for `X` |

`Referenced[T]` is `Annotated[T, REFERENCED]`, so pydantic, a binding, and any direct use of the params model see `T`, and take a `T` inline.

**One language at every depth.** The params model is a group, and the same rules type the fields of every group in it.
Each field, at any depth, is of one of the *field types*: a scalar, a file field, a group, a union of groups, `Referenced[T]`, a union of members written inline and members `Referenced[...]`, `X | None`, or `list[X]`.
A *scalar* is an `int`, `float`, `str`, `bool`, or `datetime`, or an enum or a `Literal` whose values are of these types, possibly constrained, such as `Annotated[float, Gt(0)]`.
Any other type is refused.
The framework does not look inside the `T` of `Referenced[T]`, so an array is a field of a group that a reference fills:

```python
class WavelengthRange(BaseModel):
    start: float
    stop: float

class Reduction(BaseModel):                       # written inline
    bins: int = 100
    banks: list[int] = []                         # a list of scalars; without elements [], not None
    q_max: float | None = None                    # a scalar, or None
    range: WavelengthRange | None = None          # a group in a group, or None

class Specimen(BaseModel):                        # a row of a table, specimens: list[Specimen]
    run: RunFile                                  # a file
    thickness: float = 1.0
    can: RunFile | None = None                    # a file, or None
    transmission: Referenced[ComputedTransmission]  # a reference fills it

class Binning(BaseModel):
    limits: tuple[float, float]                   # refused: not a field type; use a group, WavelengthRange
    edges: sc.Variable                            # refused: not a field type; Referenced[Binning] where an output fills it
```

A request writes each field inside its group, a file or a reference to an output among them:

```python
{'reduction': {'bins': 50, 'range': {'start': 2.0, 'stop': 10.0}},
 'specimens': [{'run': file(run=611), 'thickness': 2.0, 'transmission': t611.ref('transmission')}]}
```

In Python, a value that holds a reference to an output is given as a dict: pydantic refuses a reference in an instance of a group. Any other value may be an instance, whose `Path` at a file field names the file by its path (see `Client.submit`).

A group is validated when the request is submitted, so that an error shows before anything runs.
A group that holds a reference to an output, at any depth, is validated when the request runs: the output may not exist when the request is submitted, and the group's validators see the data read.
A failure then fails the record, or stops the accumulator for a push.
A validator raises `ValueError` for a value it does not take.
No output holds a `Path`, so the `T` of `Referenced[T]` holds none, at any depth.

**References to outputs fill groups.** The `T` of `Referenced[T]` is a group, and a reference to an output fits the field by the class of `T` (see References).
A field typed `Referenced[float]` or `Referenced[sc.DataArray]` would fit every output of that type, so a picker of records would list unrelated outputs.
A group's class says what the value is, such as `BeamCentre` or `ComputedTransmission`.
So an array is a field of a group, such as the field `transmission` of `ComputedTransmission`, and the workflow that computes it outputs the group:

```python
transmission: sc.DataArray                        # refused by WorkflowSpec: not a field type
{'reduction': {'bins': r.ref('bins')}}            # refused at submission: bins is not typed Referenced[...]
{'reduction': r.ref('reduction')}                 # refused at submission: reduction is not typed Referenced[...]
```

**Files.** A field, at any depth, takes a file when it is a *file field*, typed `Annotated[Path, File(**metadata)]`.
The marker `ess.spec.File` holds the metadata that a fitting file has, so that a picker offers only files of its kind; `kind` is `'raw'` unless given.
A spec names each kind of file once:

```python
RunFile = Annotated[Path, File()]                 # a raw file
MaskFile = Annotated[Path, File(kind='mask')]     # a pixel mask, as XML

mask: MaskFile | None = None                      # a file of kind 'mask', or None
```

A request writes a file as `file(...)`, such as `file(run=611)` or `file(path=...)`.
The backend resolves it to the file's identity when the request is submitted, given the marker.
The file source checks then that the file is known, that the request's proposal may read it, and that its metadata fit the marker's.
The record holds the identity.
The params model holds the file's local path, a `Path`, so a workflow sees paths only, never identities.
A program without a backend gives the path, `IofQParams(sample=Path('run-611.nxs'), ...)`.
A `Path` without `File(...)` is refused in the params, and so is `File(...)` on anything but a `Path`, and an output that holds a `Path`: every path a workflow gets has passed the access check.
The marker carries no validators: one that reads the file would read it from a network mount when the request is submitted.
A check of a file's content belongs to the workflow.

A value that is read from a file by some users and computed by others is a union of a file field and a referenced group, such as `normalization: PublishedNormalization | Referenced[ComputedNormalization] | None` ([user-stories.md](user-stories.md), story C2).
The binding gets the file's local path or the group, and reads the file itself.

**Parameters.** By convention a parameter is a group, a union of groups, a file field, or a list of one.
A group is one tab of a form, and one key of a binding.
A file parameter needs `keys=` in a binding.
A number is a field of a group, such as the `bins` of a `Reduction`:

```python
sample: Path                                      # refused by WorkflowSpec: a file field is typed Annotated[Path, File(...)]
sample: RunFile                                   # a file parameter: PipelineBinding needs keys= for it
bins: int = 100                                   # allowed, but PipelineBinding needs keys= for it
```

A UI generated from the params schema shows one widget per type, at any depth ([ADR 0001](../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md)):

| Type | Widget |
|---|---|
| a scalar | an input of its type |
| a group | its fields: a tab at a parameter, a card at a field of a group |
| a file field | a picker of files of its kind |
| `Referenced[T]` | a picker of records whose output fits `T` |
| a union of groups | a choice of alternatives, each with its fields |
| a union with members `Referenced[...]` | the choice of its other members, and "From a record", a picker of records, as one more way |
| `list[X]`, `X` a scalar | a list of inputs |
| any other `list[X]` | a grid of its elements, added and edited; each the widget of `X` |
| `X \| None` | a choice of the ways of `X` and "None" |

No type in the params or the outputs is a `NewType`, at any depth: a class says what a value is.
A sample run and a can run are both raw files, both typed `RunFile`, and the binding sets each at a key of its own, given by `keys=`.
`NewType`s, and other sciline keys such as esssans' `Filename[SampleRun]`, live in the workflow package and in the binding (see What a binding provides), never in the spec.

**Unions.** A field, at any depth, may be a discriminated union of groups, `Annotated[A | B, Field(discriminator='kind')]`, in which each alternative has the *tag* field `kind`, typed as a `Literal` of one string, its own, such as `Literal['find']`.
The request writes the fields of one alternative with its tag, inline, since the tag says which alternative's fields the object has.
It writes the tag even where the alternative has a default for it, and the params schema requires it:

```python
class GivenCentre(BaseModel):                     # an alternative: the centre, given
    kind: Literal['given'] = 'given'
    x: float = 0.0
    y: float = 0.0

class FindCentre(BaseModel):                      # an alternative: find it in the sample run
    kind: Literal['find'] = 'find'
    tolerance: float = 0.1

beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()

{'beam_centre': {'kind': 'given', 'x': 0.012, 'y': -0.003}}
{'beam_centre': {'kind': 'find', 'tolerance': 0.05}}
```

A reference fits a union through a member `Referenced[T]`, which has no tag, so the workflow that computes `T` knows nothing of the specs that take it.
So the discriminator sits on the union of the members written inline:

```python
beam_centre: (
    Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    | Referenced[BeamCentre]                      # centre.ref('centre'), see References
) = GivenCentre()

{'beam_centre': {'kind': 'find', 'tolerance': 0.05}}
{'beam_centre': {'@ref': 'output', 'record': 'r17', 'output': 'centre'}}
```

The key `@ref` tells a reference from a value written inline, then the tag tells the alternatives apart.
An error inside a union names the field without the tag, `beam_centre.tolerance`.

`X | None` means *without X*: apply no mask, no direct-beam correction.
A value obtained in one of several ways that a request writes inline is a union that names each way, not `None` for one of them.
The way "None" of a choice must read as what `None` does:

```python
mask: MaskFile | None = None                      # Mask: ( ) A file  (•) None
direct_beam: RunFile | None                       # Direct beam: (•) A file  ( ) None  [run 60340 ▾]

beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()
# Beam centre: (•) Given: x [0.0] y [0.0]  ( ) From a record
beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
# Beam centre: (•) Given: x [0.0] y [0.0]  ( ) Find: tolerance [0.1]
# not `beam_centre: GivenCentre | None = None  # None: find it`,
# whose way "None" reads as "no beam centre"
```

`direct_beam` has no default (see Defaults below), so a UI shows its choice with no way chosen until the user picks one.
A choice between modes is a union of groups.
An optional part with fields that need values is typed `X | None`, at any depth, such as `range: WavelengthRange | None` in `Reduction`.
A `bool` field switches one optional part of a group whose fields all have defaults, such as `limit_range: bool` next to `start` and `stop`, both with defaults.
The reasons are [ADR 0008](../../packages/essspec/docs/developer/adr/0008-a-choice-between-ways-is-a-union-of-groups.md).

The module that defines a spec imports pydantic, `ess.spec`, and scipp for the types of the arrays in groups that references fill, but no workflow package.
A workflow that takes nothing has the params model `NoParams`.
A request computes every output its spec declares.

**Rules.** The models are ordinary pydantic models with two markers for the framework, `Referenced[T]` and `File(...)`; the rest the framework reads from the field types.
A value *round-trips* when its JSON form reads back as the value.
`WorkflowSpec` checks these rules when it is made:

- The names of params and outputs are distinct.
- Each field, at any depth, is of a field type. A group does not contain itself. A list is not typed `list[X] | None`, since a list without elements is `[]`, its element is not typed `X | None`, since each element is a value, and it is not a list of lists.
- `Referenced[...]` may type any field of the params, but no output. Its `T` is a group, which holds no `Path` and no `Referenced[...]` at any depth.
- A union with members `Referenced[...]` carries its discriminator on the union of the members written inline, since a reference to an output has no tag.
- A file field has no default but `None`, or `[]` for a list.
- `File(...)` marks a `Path` only, and a `Path` without `File(...)` is refused.
- Each output is a type, possibly typed `X | None`, or a list or a dict of one. No output holds a `Path`, at any depth, a file field among them.
- No type in the params or the outputs is a `NewType` or a type alias of a `type` statement, at any depth: a class says what a value is, and a binding maps it to a key of its own. A name assigned a type, such as `RunFile = Annotated[Path, File()]`, is that type.
- A union of groups is discriminated by the name of its tag field, not by a callable. The tag field of each alternative is a `Literal` of one string, its own, without an alias. A union that holds a group holds groups only, not `GivenCentre | float`: a value of a type that is not a group has no tag. A union may also hold members `Referenced[T]`, which need no tag.
- A group has no serializers, computed fields, aliases, or excluded fields, which would make its JSON form differ from the object of its fields. It may have field and model validators of any mode, which run when the group is validated (see Requests and records, Validation). A validator raises `ValueError` for a value it does not take.
- A default is a value, never a reference to an output or a file: what a request may write at the field, which round-trips and holds no `@ref` object at any depth. A field that only a reference fills has no default but `None`, or `[]` for a list. A default is the value that validating it gives, since pydantic does not validate a default: a group's default is an instance, not the object of its fields, and an enum's a member, not its value.
- A table has no default, and carries only length constraints, such as `Field(min_length=2)` (see Combining runs: tables).
- The params model has no field validators, serializers, or computed fields, and no `default_factory` that takes the other values. It may have model validators, but the framework never calls them.

Each parameter is validated alone, since a template, a stage call, or a push gives some values only.
A check of one value belongs to the value's type, such as a field validator of a group, and so does the JSON form of a value, which the framework stores and reads back.
A group written inline is validated whole at submission, with all its fields at hand, unless it holds a reference to an output (see Requests and records, Validation).
A check across parameters, such as a Q range that the wavelength band and the detector can reach, is the binding's or the driver's, and fails when the request runs.
A binding may run the model validators, `IofQParams.model_validate(dict(params))`.

**Defaults.** A request that leaves out a parameter, or a field of a group, so that it takes its default, never skips a correction that a correct reduction needs.
`reduction` has a default, a setting.
The can run has none, so a request or a template that leaves it out is refused, instead of computing I(Q) without the can subtracted.
A correction that a correct reduction needs, but that a request may still go without, is typed `X | None` with no default, such as `direct_beam: RunFile | None`, where `None` means no direct-beam correction: the request writes `None` to go without it, and its record says so.
A mask, which a correct reduction does not need, may default to `None`.
The output is I(Q) either way, and the params of its record say which corrections were applied.
A step whose omission changes what a reader of the output must do with it, such as its unit or its dimensions, makes another output type, and so another spec.

**Binding.** A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec.
The simplest binding is a function that takes an instance of the spec's params model and returns an instance of its outputs model:

```python
def iofq(params: IofQParams) -> IofQOutputs:
    ...                                     # params.reduction.bins; params.sample is a local path
    return IofQOutputs(iofq=result)

draft = function_binding(IOFQ, iofq)        # from ess.spec
```

The binding never sees a reference: the backend reads the data of each reference before it calls the binding.
Each value is its type, whichever form the request gave it in: a group as the model, a group that a reference fills with its data, such as the scipp array at the field `transmission` of a `ComputedTransmission`, and a file as its local path, a `Path`.
The backend passes every parameter, with the defaults of the model filled in.
Defaults belong in the model only.
A binding must not modify the values it gets or return an output that shares memory with one of them.
`ess.spec.testing` checks this for values backed by numpy.
A sciline pipeline, or a driver that accumulates rows, is bound the same way (see What a binding provides).

`local` can also bind a spec to code defined in the notebook, such as `bind=[draft]`, since that code runs in the user's own process.
The service runs only installed workflows.

## Requests and records

A *request* is a spec and its params, given by keyword:

```python
result = client.compute(IOFQ, params={'reduction': Reduction(bins=100), 'sample': file(run=60339),
                                      'can': file(run=60338), 'transmission': t.ref('transmission')})
                                      # submit, then wait
client.output(result, 'iofq')            # raises RecordFailed if the record failed
client.output(result, 'iofq', select={'Q': slice(0, 10)})
```

`params` is a dict by name: each value written inline, a file, or a reference to an output, or a list of these for a list parameter (see Specs and bindings).
A value written inline is the value, such as `Reduction(bins=100)`, or its JSON form, `{'bins': 100}`: both give the same request.
`params` may also be an instance of the params model, if it holds no reference to an output (see `Client.submit`).
It may be left out when every parameter has a default.

Submitting a request returns a *record*.
The record holds the request with every value filled in, defaults included, and the names of its outputs: `record.request.params` holds the JSON form of each value, file, and reference to an output.
It holds no output values.
Running the same request again makes a new record.
Its *status* is `pending`, then `completed`, `failed`, or `cancelled`.

In the user's process, `client.output` of a record returns the output value itself, not a copy.
The framework never changes a record's output value (see open question 7).
`select=` copies the part of an output that it names, by dimension, and makes no record.
Of a group output, such as `IofQ`, it slices each array field that has the dimension, as `sc.DataGroup` slices its items, and copies the other fields whole; a dimension that no array field has raises `ValueError`.
On the service, only that part leaves the backend.

| Call | Does |
|---|---|
| `client.submit(...)` | submits and returns pending records |
| `client.compute(...)` | `submit`, then `wait` |
| `client.status(records)` | the current status of each record |
| `client.wait(records)` | blocks until all have finished, and does not raise failures |
| `client.failure(records)` | why each record failed, or `None` |
| `client.readable(records)` | the names of the outputs of each record that a submission may reference now (see How long records and values are kept) |
| `client.cancel(records)` | cancels unfinished records of the proposal, including those of other clients |
| `client.release(records)` | drops the client's hold on their outputs |

Each takes one request or record, a list, or a dict, and returns the same shape.
`Request(IOFQ, params=...)` makes a request without submitting it, and `request.upstream()` lists the outputs of other records that it reads.
A caller without the workflow package gives `SpecId(name='sans-iofq', version=1)` in place of `IOFQ`.
`client.spec(record.spec)` gives the spec that the backend runs under that `SpecId`, and `client.specs()` every spec it runs, so a form needs no specs of its own.

**Validation.** A request is validated in two phases:

| Phase | What is validated |
|---|---|
| request phase | each value by its *request type*, the params type with each `Referenced[T]` replaced by a reference to an output, from its JSON form; an instance by its dump. Each parameter that holds no reference to an output is validated whole by the params model too. The files are resolved and checked first, so the validators of a group that holds a file see its local path |
| call phase, once the references are read | the params model itself validates what the record stores, with the data read in place of each reference to an output and the local path in place of each file. A group that holds a reference to an output, at any depth, is validated whole here, and its validators see the data read |

In the request phase, at submission, the backend also checks the names and the required parameters, each reference to an output readable and fitting its field (see References), and the table rules (see Combining runs: tables).
Every validator of a group that holds no reference to an output runs then, with all its fields at hand:

```python
{'range': {'start': 10.0, 'stop': 5.0}}           # refused at submission by a validator of WavelengthRange
{'reduction': {'bins': r.ref('bins')}}            # refused at submission: a reference at a field not typed Referenced[...]
```

A value written inline must round-trip, since the log holds its JSON form: a number that is not finite does not.
The record holds the JSON form of the validated group, so `Reduction(bins=100)` and `{'bins': 100}` give one record, and a group whose validator changes a value it has validated already, such as one that converts millimetres to metres, does not round-trip and is refused.
The call phase validates the JSON form that the record stores, so a strict model takes an enum from its string, as the request phase took it from JSON.
It fails only for the data of a reference to an output, or a validator of a group that holds one, which fails the record, or stops the accumulator for a push.
It makes the params instance from these values.
A request that cannot run is refused at submission with a `SubmitError`.
Its `errors` list each reason as a `FieldError`, as pydantic's `ValidationError.errors()` does: the path of the field at fault, `loc`, such as `('reduction', 'bins')` or `('sample', 2)`, the reason, `msg`, and for a call with several requests the request's key or index, `entry`.
If one request of a call is refused, none is submitted.
A request that fails in the call phase makes a failed record.
Its failure, `client.failure(record)`, keeps the framework's own words as they are: a value refused in the call phase, such as `specimens[0]: Value error, ...`, and the backend's reasons, such as `record r17 failed`, `the workflow of sans-iofq/v1 returned None as iofq`, or `the write failed: ...`.
An error that the binding, its driver, the file source, or the store raises is given with its type, such as `KeyError: 'counts'` or `the write failed: OSError: no space left on device`.
Validation in the request phase never proves that a workflow will succeed.

## References

A *reference to an output* names an output of a record, `record.ref(name)`, or of an accumulator, `acc.ref(name)` (see Stages and accumulators).
A reference to a pending record is allowed, so a chain is submitted without waiting.
`BEAM_CENTRE` takes the parameter `direct_beam`, a file field, the file of the direct-beam run, and returns `centre: BeamCentre`.
The same beam centre is typed in by one user and computed by another:

```python
runs = {'sample': file(run=60339), 'can': file(run=60338), 'transmission': t.ref('transmission')}
result = client.submit(IOFQ, params={**runs, 'beam_centre': BeamCentre(x=0.012, y=-0.003)})  # written inline
centre = client.submit(BEAM_CENTRE, params={'direct_beam': file(run=60330)})                # pending
result = client.submit(IOFQ, params={**runs, 'beam_centre': centre.ref('centre')})          # a reference
```

The backend runs the last request once the one before has completed.
A request never references another request of the same `submit` call.
A request writes a file as `file(...)` (see Files): `file(path=...)` names a file by its path, and a string where a file field is expected is refused.
`record.ref(output, key)` names one element of a list or dict output: by its index for a list, `key='2'`, and by its key for a dict.
A key that the output lacks fails in the call phase.

A reference to an output fills only a field typed `Referenced[...]`, at any depth, or a member of a union there, such as `beam_centre`, `time_range[2]`, or `specimens[0].transmission`; never a whole list.
A file is a value, at any file field.
The JSON form of a reference to an output, and of a file, is an object with the key `@ref`, which names its kind:

```json
{"@ref": "file", "file": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "centre"}
{"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
```

The key `@ref` is reserved at every depth: an object with it is a reference to an output or a file.
So the framework finds the references and files of a request without the spec, in every array and every object.
With the spec, it refuses a reference to an output at a field not typed `Referenced[...]`, and a default whose JSON form holds the key `@ref`.

A reference to an output *fits* a field when the data it names are of the class `T` of a `Referenced[T]` there, or of a subclass of it.
`| None` is removed on both sides.
A reference with a key names one element, of the output's element type, and an element of a list takes a reference by the element type.
A file and a reference to an output differ in where they may be and when they are resolved:

| | Reference to an output | File |
|---|---|---|
| Where | a field typed `Referenced[T]`, at any depth, or a member of a union there | a file field, at any depth, or a member of a union there |
| Fills | a whole group, `T`, which holds no `Path` | a file field |
| Fits if | the output's class is `T` or a subclass of it | the file's metadata have the marker's metadata |
| Resolved | in the call phase, to the stored output | in the request phase, to the file's identity; the params model holds its local path |
| Found in a record | by its `@ref` key, without the spec | the same |
| Access check at submission | records the client may read (see How long records and values are kept) | files the proposal may read |

| Data | Type of the field | Fits |
|---|---|---|
| an output `BeamCentre` | `Referenced[BeamCentre] \| None` | yes |
| an output `BeamCentre` | `BeamCentre \| None` | no: the field is not typed `Referenced[...]` |
| an output `BeamCentre` | `GivenCentre \| FindCentre`, a union of groups | no: no member is `Referenced[...]` |
| an output `BeamCentre` | `Annotated[GivenCentre \| FindCentre, ...] \| Referenced[BeamCentre]` | yes |
| an output `list[TimeRange]`, with a key | an element of `time_range: list[TimeRange \| Referenced[TimeRange]]` | yes |
| an output `sc.DataArray` | `Referenced[ComputedTransmission]` | no: a reference stands for a group, so the workflow outputs `ComputedTransmission` |
| a raw file | `RunFile`, the type of a file field of kind `'raw'` | yes |
| a raw file | `MaskFile`, the type of a file field of kind `'mask'` | no |

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment.
A *member* names one part of a label, such as one sample or one temperature.
Both are optional and given at submission, not in the request.

```python
client.compute(IOFQ, params={'reduction': {'bins': 50}, 'sample': sample, 'can': can},
               label='iofq', member='250K')
client.submit({'250K': request_250k, '260K': request_260k}, label='iofq')   # dict keys become the members
client.latest('iofq', member='250K')                  # the newest record of this member
client.records(label='iofq')                          # oldest first; without label=, every record of the proposal
{r.member: r for r in client.records(label='iofq')}   # the newest of each member: later records replace earlier ones
```

## Templates

A *template* is a spec, its fixed values, and its blanks, the names of the parameters left open.
Stages, accumulators, and automatic reduction are built from templates.
A blank is a whole parameter, never a field of a group.
`Template` takes the fixed values as a request does, and all but the spec by keyword:

```python
template = Template(IOFQ, params={'reduction': Reduction(bins=100), 'can': file(run=60338),
                                  'transmission': t.ref('transmission')},
                    blanks=('sample',))
```

`Template` validates each fixed value written inline, and each fixed file, by the request type of its parameter, and checks that each fixed reference to an output is readable and fits its field.
A required parameter may be a blank.

A template can also be made from any request's values.
A name in `blanks` drops the value given for it:

```python
final = client.latest('iofq', member='250K')    # the final reduction of one sample
beamtime = Template(final.request.spec, params=final.request.params,
                    blanks=('sample',))         # its sample left open
```

## How long records and values are kept

Records are the proposal's history ([ADR 0004](adr/0004-history-is-append-only-lists.md)).
They are dropped as a whole once the proposal has been idle for days to weeks.
A proposal is idle while it has no open client, no pending record, and no write to the store in progress.

**Values.** An output value, such as an I(Q) array, lives while something keeps it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)):

| Keeper | Keeps the value until |
|---|---|
| the client that asked for the record (`submit`, `compute`, or `freeze` without `persist=`) | the client releases the record or ends |
| a pending request that reads it | the request's workflow has returned |
| a push, a stage, or an accumulator's template that references it | the binding has taken it, or the push, stage, or accumulator is released, stopped, or dropped |
| a persist request | the value is written |
| the store | the proposal's history is dropped |

Nothing else keeps a value, not a record object, a reference, or a label.
A value that is gone does not come back.

The binding is a black box.
The framework hands it a stage's fixed values at the stage's first call, an accumulator's template when it makes the driver, and a push's rows when the push is added.
The framework does not track what the binding keeps after that.

A submission may reference only

- output values its client keeps,
- persisted outputs,
- files,
- the current state of its client's own accumulators.

Any other reference is refused, even to a record that is still pending.
`client.output` reads only the same values, and raises `NotReadable` for any other.
So another client, even one in the same process, reads a value only once it is persisted.
A submission also refuses a reference to a record that failed or was cancelled, and to an output that a completed record's workflow did not return.
`client.readable(records)` gives, for each record, the names of the outputs that a submission may reference now.
It waits for nothing and reads no value, so a form calls it each time it lists the records to pick from.

```python
centre = client.compute(BEAM_CENTRE, params={'direct_beam': file(run=60330)})
result = client.submit(IOFQ, params={'sample': file(run=60339), 'can': file(run=60338),
                                     'transmission': t.ref('transmission'), 'beam_centre': centre.ref('centre')})
client.release(centre)              # the pending request keeps the centre until it has run
client.output(result, 'iofq')       # allowed: this client asked for the record
client.output(centre, 'centre')     # raises NotReadable: this client released it
```

`client.close()`, or the end of a `with client:` block, releases everything the client keeps.
Closing a client made with `local()` also closes its backend, after the persisted outputs are written.

**Work that nothing keeps is cancelled.** A pending record whose outputs nothing would keep finishes as `cancelled`.
So releasing a pending record that no pending request reads cancels it.
A cancelled request keeps nothing it reads, so cancelling passes along a chain.
A running workflow is not interrupted.
Its record is cancelled at once, and its outputs are dropped when it returns.

**Persist.** Nothing is written unless persisted ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).
An output is *persisted* while a persist request names it and its write has not failed.

```python
client.persist(result, 'iofq')     # the store keeps iofq too; every output if none is named
client.submit(IOFQ, params={'sample': file(run=60341), 'can': file(run=60338),
                            'transmission': t.ref('transmission')},
              label='night', persist=('iofq',))             # only the store keeps iofq
morning = connect(url, proposal='p1')                       # the next day, any process
morning.output(morning.records(label='night')[0], 'iofq')   # read from the store
```

- `client.persist` adds the store as a keeper next to the client.
- `persist=` at submission makes the store the only keeper. `persist=True` names every output, and outputs not named are dropped. A record persisted this way completes once its outputs are written, and fails if a write fails.
- A client reads the values it keeps from memory. It reads other persisted values from the store, after waiting for their write.

**The two deployments.** In the user's process, values live in its memory, and `local(store=...)` sets the store.
The service keeps values in its memory and limits the memory of each client (the *cap*).
It ends a client that makes no call within its *lease*, and gives each proposal a store.

## Files

```python
file(run=60339)
file(path='/home/user/data/run1.h5')
file(pid='20.500.12269/vanadium')     # an output published in a catalogue
```

A request names a file by what a person knows, such as a run number.
The backend resolves the name to the file's identity, which the record holds in place of the name.
The record holds no metadata.
A client reads it from the file source.

**File source.** A backend finds files through its *file source*, which DMSC deploys with it.
A client lists files, waits for new ones, and reads their metadata through `client.files`, from the backend's file source.
A client sees only the files its proposal may read.
A *selector* picks files by metadata.
It matches raw files unless it names another kind, such as calibration.
A file *fits* a file field when the selector of its marker's metadata, `Selector(**File(kind='mask').metadata)`, matches it, so a form lists for a file field only the files that the backend accepts there.

```python
samples = client.files.list(Selector(role='sample'))          # files, usable as values
for run in client.files.watch(Selector(scan='17')): ...      # existing ones first, then new ones
client.files.metadata(run)['sample']                          # the source's current values
```

## Combining runs: tables

Many reductions combine several runs, such as the sample and can runs of SANS.
A spec takes them as *tables*, next to the values that all runs share.
A table is a list parameter whose *rows* an accumulator takes a push at a time, such as the sample runs.
Each row is written inline, a file, or a reference to an output, as the spec types its rows.
The rows of a table are distinct (see Stages and accumulators, Distinct rows).

A *multi-run spec* combines the runs of the *single-run spec* of the same reduction.
Its params model shares the other parameters, `reduction`, `beam_centre`, and `transmission`, with `IofQParams` through the base model `IofQSettings`, and each table is a list of the single-run type:

```python
class IofQMultiParams(IofQSettings):
    sample: list[RunFile] = Field(min_length=1)
    can: list[RunFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

`tables` names the tables.
A table has no default, so a request gives every table, `[]` for one without rows; `Field(min_length=1)` states that it needs a row.
The validators and constraints of the element type check each row.
A request that gives every row is a plain request:

```python
result = client.compute(IOFQ_MULTI, params={'sample': [file(run=611), file(run=612)],
                                             'can': [file(run=614)],
                                             'transmission': t.ref('transmission')})
```

**Values of the runs.** A value that differs per run, such as a motor position, is in the run's file.
A value that differs between runs, such as a thickness, makes the runs separate requests; a value shared by all runs of a request is a parameter, such as one transmission for the summed sample runs.

A multi-run spec may have outputs of its own, such as `STITCH`, a stitch of all angles, or no single-run spec, such as `VOLUME`, which sums counts over runs.
It is written the same way: `runs: list[RunFile] = Field(min_length=1)`, a table of files, with `WorkflowSpec(..., tables=('runs',))`, so the model, its schema, and the backend agree that the table has a row.
A list parameter makes a table only when `tables` names it; otherwise it is a value like any other.

The *table rules* are: the number of rows of a table meets its length constraints, such as a table that needs a row, and the rows of a table are distinct.

**Single-run and multi-run specs.** With one row in each table, a request of `IOFQ_MULTI` is a request of `IOFQ`, and gives the same outputs (the one-row symmetry, see Symmetries).
The specs agree since `IofQMultiParams` shares the other parameters of `IofQParams` and each of its tables is a list of the single-run type; `check_one_row` checks it.
The binding's driver handles structure below a row, such as detector banks, and decides what is summed ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

## Stages and accumulators

A plain request loads the run again for each value of `reduction.bins`.
Adding a run to a sum of a hundred runs reduces all hundred again.
Stages and accumulators keep what such work computes in memory between requests.
Each needs a binding that serves it (see What a binding provides).
A stage needs a binding that makes stages, such as a `PipelineBinding`, which computes the fixed part once.
An accumulator needs a binding that makes drivers, such as a `DriverBinding`, whose driver adds each run without reducing the earlier ones.
Making a stage or an accumulator over a binding that does not serve it is refused.
A client keeps a stage or an accumulator until it releases it or ends.

**Stage.** A *stage* is a template that the backend keeps for a client:

```python
tune = client.stage(Template(IOFQ, params={'sample': file(run=60339), 'can': file(run=60338),
                                           'transmission': t.ref('transmission')},
                             blanks=('reduction',)))
for bins in (50, 100, 200):
    client.compute(tune, params={'reduction': Reduction(bins=bins)}, label='iofq')   # the first call loads the run
```

A call through the stage takes the values of the blanks, as a request does.
It fills the blanks and makes the record of the plain request.
The first call has the binding make its stage from the fixed values.
The binding then computes what depends only on them, and later calls compute only the rest.
A blank is a whole parameter, and a group is one key of the pipeline (see What a binding provides).
If staging fails, the stage *stops*.
The first call and the calls waiting to run fail, and later calls are refused, each with the reason.

**Accumulator.** An *accumulator* is a template whose blanks are tables.
The backend keeps it for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)):

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params={'reduction': Reduction(bins=100),
                                                       'transmission': t.ref('transmission')},
                                   blanks=('sample', 'can')))
iofq.push(sample=[file(run=611)])                                     # one row
iofq.push(sample=[file(run=612), file(run=613)])                      # two rows of one table
iofq.push(sample=[file(run=617)], can=[file(run=614)])                # rows of two tables
client.output(iofq, 'iofq')                                           # the output of state 3
```

A push gives rows of one or more tables, by the name of each, as a list.
Its rows enter one state, so states 1 and 2, which have no can row, are never read here.
The tables of a push stand side by side, as in a request: rows of different tables have nothing to do with each other, and the first sample row is not paired with the first can row.

A push of no rows is refused.
A template fixes a whole table or leaves it blank, and a push into a table the template fixes is refused.
The plain request of state n is the template with each table filled by the rows of the first n pushes, in push order.
State n has the outputs of its plain request.
`client.accumulator` and `push` return once logged.
The backend has the binding make the driver, and pushes each push into it, later.

**Pins.** A call that pins takes state n, where n is the number of pushes logged so far.
Pinning does not wait for these pushes to be added.
`client.output` then waits for state n before it copies.
`EXPORT` writes an I(Q) to a text file; its parameter `result` is typed `Referenced[IofQ]`.

| Call | Returns | Holds back push n+1 |
|---|---|---|
| `client.output(acc, 'iofq')` | a copy of the output of state n, or of the part `select=` names, and makes no record | while it waits for state n and copies |
| `client.submit(EXPORT, params={'result': acc.ref('iofq')})` | a record of the `EXPORT` request. The submission also makes the record of state n, whose outputs the request reads without a copy. | from submission until the request's workflow has returned |
| `client.freeze(acc)` | the record of state n (see Freeze) | no push follows |
| `client.provenance(acc)` | the provenance of the plain request of state n, at once | no |

No client keeps the record of a state that a submission makes ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)), and `client.output` of it raises.
A push or a template may reference no accumulator ([ADR 0003](adr/0003-accumulators-add-in-place.md)).
Only the client that made an accumulator pins it.

**Validation.** Templates, stage calls, pushes, and states are checked in the two phases, as a plain request is (see Requests and records), each on what is new:

| What | Request phase | Call phase |
|---|---|---|
| a template | each fixed value by the request type of its parameter; each fixed reference to an output readable and fitting; the table rules on fixed tables | each fixed value validated by the params model, once, when the stage or the driver is made |
| a stage call | the same, for the values given; the table rules on given tables | each given value validated by the params model |
| a push | each row by the request type of the table's element; each reference readable and fitting; the table rules on the table with the push | each row validated by the table's element type, with the data of each reference read, before the driver gets it |
| a state, at a pin | the number of rows of each table, by its length constraints | (none) |

A value that fails its check at a push stops the accumulator, as any failing push does.

**Distinct rows.** A push refuses a row that the table already holds, since pushing a run twice is usually a mistake, such as running a notebook cell again.
Rows are compared when the request is submitted, as the record stores them: a value written inline by its JSON form, a file by the identity the file source gives it, and a reference to an output by record, output, and key, at each position in the row.
So `file(run=611)` and `file(path=...)` of the same file are one row.
The data that a reference names are not compared, so two references to equal data are two rows, and so are a value written inline and a reference to equal data.
A push checks this against the pushes logged, since a pin that refused the repeat would refuse every later state.
A plain request with a repeated row is refused too, including one in a template.
`SLICES` is a spec like `IOFQ`, with the table `time_range: list[TimeRange | Referenced[TimeRange]] = Field(min_length=1)`:

```python
slicer = client.compute(FIND_SLICES, params={'log': file(run=611)})   # outputs slices: list[TimeRange]
n = len(client.output(slicer, 'slices'))
acc = client.accumulator(Template(SLICES, params={'sample': file(run=611), 'can': file(run=614),
                                                  'transmission': t611.ref('transmission')},
                                  blanks=('time_range',)))
acc.push(time_range=[slicer.ref('slices', key=str(i)) for i in range(n)])
acc.push(time_range=[slicer.ref('slices', key='0')])  # the same reference: refused at submission
acc.push(time_range=[TimeRange(start=0, stop=60)])    # equal to slice 0, but another row: summed twice
```

The client reads how many slices there are, since a reference names one row.
Running again a cell that computes a record and pushes its output pushes a reference to a new record, which is another row too.

The accumulator *stops* if

- adding a push fails,
- a record that a push references does not complete, or
- the binding fails to make the driver.

Later pushes and pins are refused with the reason, and the records of states it did not reach fail.

**Adding waits for readers.** The example uses the toy specs `VOLUME` and `CUT` ([user-stories.md](user-stories.md)).
`VOLUME` sums the counts of the runs of its table `runs` into its output `volume`, a group `Volume` whose field `counts` is an array with the dimension `q`.
`CUT` takes the value at `position.index` of its parameter `volume`, typed `Referenced[Volume]`:

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push(runs=[run])                 # returns once logged; the backend adds it later
    cut = client.submit(CUT, params={'position': Position(index=0),
                                     'volume': volume.ref('volume')})
                                            # pins this state; CUT reads it in place
    show(client.output(volume, 'volume', select={'q': 0}))
                                            # pins this state, waits for it, copies the slice of counts
```

`push` and `submit` return once logged, and the backend adds pushes and runs readers in parallel.
Each reader still sees the state after the push before it in the loop, as if the calls ran one after another.
In Rust's terms, each reader of a state holds a shared borrow of the driver, and a push waits for a mutable borrow.

The rule is that push n+1 waits for the *readers* of state n:

- requests that read the outputs of the record of state n, from their submission until their workflow has returned, even if cancelled while it runs;
- pins of state n, until they have what they asked for (`client.output` until it has copied).

It has two reasons ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

- The driver only moves forward. After push n+1 is added, the outputs of state n cannot be computed again.
- The driver's `push` may change in place the arrays its `outputs()` returned (see What a binding provides).

So no reader sees a value change.

**Freeze.** `client.freeze(acc)` pins the last state and returns its record:

```python
total = client.freeze(iofq)              # no copy, no more pushes
client.output(total, 'iofq')             # as any record's output
client.freeze(volume, persist=True)      # the store keeps every output, not the client
```

The client keeps the record, unless `persist=` hands it to the store.
At the end of a scan, freezing makes the final volume an ordinary record without a copy.
The client can keep it, persist it, or reference it from another template.

**Releasing.** `client.release(acc)`, or ending its client, returns at once:

```python
iofq.push(sample=[file(run=615)])
exported = client.submit(EXPORT, params={'result': iofq.ref('iofq')})   # pins the state after this push
client.release(iofq)                                           # the push is added, and the export runs
```

The pushes up to the last pinned state are still added, and later pushes are never added.
The driver is dropped once the readers of its states are done.

## What a binding provides

A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec:

```python
single = PipelineBinding(IOFQ, make_pipeline)            # from ess.spec.pipeline
multi = DriverBinding(IOFQ_MULTI, IofQDriver)            # from ess.spec
export = function_binding(EXPORT, export_csv)            # from ess.spec
client = local(proposal='p1', files=..., bind=[single, multi, export])
```

Each binding serves some of the client's actions:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline (`PipelineBinding`) | yes | yes | refused |
| a driver (`DriverBinding`) | yes | refused | yes |
| a function (`function_binding`) | yes | refused | refused |

A stage or an accumulator that its binding does not serve is refused when it is made.

**The protocol.** The backend calls a binding through three protocols of `ess.spec.binding`, one per client action:

```python
binding.compute(params)                            # Binding: a plain request
call = binding.make_stage(fixed, blanks)           # StageBinding: at a stage's first call
call({'reduction': Reduction(bins=50)})            # the blanks' values; once per call through the stage
driver = binding.make_accumulator(fixed, blanks)   # AccumulatorBinding: when an accumulator opens
driver.push('sample', [path_611])                  # once per table of a push: its rows
driver.outputs()                                   # once per state that is read
```

`compute` gets an instance of the params model, and returns one of the outputs model.
A stage or an accumulator is made from some values only, so `make_stage` and `make_accumulator` get the values by name, each as its type, whichever form the request gave it in:

```python
{'reduction': Reduction(bins=2), 'beam_centre': BeamCentre(x=0.012, y=-0.003),  # a group as the model
 'transmission': computed,                                     # the output a reference names, a ComputedTransmission
 'sample': [path_611, path_612]}                               # a table as the list of its rows, each a local path
```

`fixed` holds the fixed values, and `blanks` names the others, a table among them.
A parameter in neither takes the spec's default.
`compute`, `make_stage`, and the callable it returns may be called from several threads at once, and none modifies the values it is given.
The callable may return the same object in several calls, so records made through one stage share what does not depend on the blanks.

**A pipeline.** `PipelineBinding` takes a function that makes the sciline pipeline from the fixed values, by name, so that the graph may depend on them.
The function does not get the blanks, so a field that changes the graph cannot be a blank.
The binding copies what the function returns before it sets values in it, so the function may return a pipeline that it holds.
Each parameter sets the sciline key of its class, without `Annotated` metadata, such as `Reduction`.
Each output is computed as the key of its type without `| None`, such as `IofQ`.
A table sets the key of its type, a list, to its rows.
A group is one key, so a provider takes it apart.
A workflow with keys of its own names them with `keys=`, for a value that it takes or computes as the spec types it, such as an output typed `sc.DataArray`.
A file parameter holds a `Path`, whose class names no role, so it needs `keys=`, which names the workflow's own key of the file's path, such as esssans' `Filename[SampleRun]`; esssans' providers then take plain paths.
For a value in another shape, `make_pipeline` inserts an adapter provider, such as one that makes the group `IofQ` from the workflow's array:

```python
def iofq(intensity: BackgroundSubtractedIofQ) -> IofQ:
    return IofQ(intensity=intensity)

def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sans_workflow()
    pipeline.insert(iofq)
    return pipeline

PipelineBinding(IOFQ, make_pipeline,
                keys={'sample': (Filename[SampleRun],), 'can': (Filename[BackgroundRun],)})
```

The spec names its parameters `sample` and `can`, not after esssans' run types `SampleRun` and `BackgroundRun`, which are keys of the workflow.
Files that vary independently are separate parameters, not fields of one group.
A stage fixes or blanks whole parameters, and `sciline.Stage` decides what it computes again on each call by the graph alone: all that depends on the key of a blank.
A group is one key, so a blank group of sample, can, and direct beam would compute the can's branch again on every call, though only the sample changes.
A group of files fits files that always change together; a provider in `make_pipeline` may take it apart into the workflow's keys.

[roles/workflow-authors.ipynb](roles/workflow-authors.ipynb), section 2, shows these routes from a spec's values to such keys, and providers that take the spec's models.
`binding.keys` gives a tuple of keys for each field, and `keys=` takes the same: `(K,)`, or `()` for a parameter that sets no key, a field that `make_pipeline` uses itself.
Two parameters of one class need distinct keys: one gets a subclass, `class QyAxis(Axis): ...`, or `keys=` names a key for it.
An output typed `sc.DataArray` is computed at the key `sc.DataArray` unless `keys=` names another, so two such outputs need `keys=`.
No two fields, parameters or outputs, share a key, over all members of a union: an output computed at a parameter's key would be the value the binding was given, which an output must not share memory with.
An output that reports the centre given or found has a key of its own, such as `UsedCentre`, computed by a provider from `GivenCentre`.

A union of groups has a key for each member, the member's class: `binding.keys['beam_centre'] == (GivenCentre, FindCentre)`, and `keys=` takes one key for each member, in the same order.
`keys=` may give the members one key, `(K, K)`, which then holds the value of either member.
The binding sets the key of the member nearest to the value's class, the first class of `type(value).__mro__` that is a member, so an output of a subclass of `T` at `Referenced[T]` sets the key of `T`.
Here one graph serves both alternatives, since `find_centre` leads from one alternative's key to the other's:

```python
def find_centre(settings: FindCentre, data: SampleData) -> GivenCentre: ...

PipelineBinding(IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq]))
# a GivenCentre -> pipeline[GivenCentre] = value   (replaces find_centre)
# a FindCentre  -> pipeline[FindCentre] = value    (find_centre computes GivenCentre)
```

With `| Referenced[ComputedCentre]`, the keys are `(GivenCentre, FindCentre, ComputedCentre)`.
A union whose members are of one class, such as `BeamCentre | Referenced[BeamCentre]`, is that class to the binding, with one key, and not a union, so it may be a blank.
Where the members need different providers for one key, `make_pipeline` branches on the fixed value.
That always works, since a union is never a blank, even with one key for all members: its key may depend on its value.
A list of unions sets one key, its type, as any list: a provider tells its rows apart.

A parameter typed `X | None`, a union too, sets no key, so `keys=` must give it `()`.
Each value of a union sets one key, but `None` has no key to set: setting `X` to `None` would replace a provider of `X`, and setting nothing would leave the meaning of `None` to whatever the pipeline holds or computes at `X`.
`make_pipeline` says what `None` means instead, here no direct-beam correction:

```python
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sciline.Pipeline(providers)
    if fixed['direct_beam'] is None:
        pipeline[sans.DirectBeam] = None       # esssans' key; its providers take None: no correction
    else:
        pipeline[sans.DirectBeamFilename] = fixed['direct_beam']
    return pipeline

PipelineBinding(SPEC, make_pipeline, keys={'direct_beam': ()})
```

Defaults belong to the spec: `make_pipeline` holds no value at a key that a parameter's key leads to.
Such a value replaces the provider that would read the parameter, so the request's value has no effect, and the binding cannot tell, since the provider is gone from the graph.
A value held at a parameter's own key is harmless, since the binding sets the request's value over it, except at a key of a union whose members have keys of their own, which one member sets and the others do not.
If the pipeline of the beam-centre example held a default `GivenCentre`, as esssans' `LokiAtLarmorTutorialWorkflow` holds a `BeamCenter`, the tag `'find'` would set `FindCentre`, but `iofq` would read the default, and `find_centre` would never run.
The binding refuses a value held at any key of such a union, since it cannot tell which key leads to which: the held value has replaced the provider that would show it.
`make_pipeline` replaces such a value with a provider, `pipeline.insert(...)`, such as an adapter from the spec's group to the workflow's key.
`make_pipeline` sets no value in a pipeline it holds: the binding copies the pipeline only after `make_pipeline` returns it, so a value set for one request would stay for the next.
A stage computes what does not depend on the blanks once, through `sciline.Stage`, and each call computes the rest.
A plain request is a stage with no blanks, called once.
The author of the pipeline promises that it is pure: it modifies no value it is given, and returns the same results for the same values.
`binding.keys` and `binding.pipeline(fixed)` give the keys and the graph of a `PipelineBinding`, so that the driver of the multi-run spec reuses them ([roles/toy_sans.py](roles/toy_sans.py)).
A table whose single-run parameter is a union of several classes has a key per member, so a driver cannot cut the graph at one key for its rows, as it cannot for a blank. A table of `X | Referenced[X]` has one key.
A driver passes `pipeline` the fixed values it is made with: every value that is not a table, defaults filled in.
So a parameter that is a table of the multi-run spec cannot shape the graph, as a blank cannot.

**A driver.** A *driver* is an object with `push(table, rows)` and `outputs()`, as in [sciline ADR 0003](https://github.com/scipp/sciline/pull/245).
The workflow package writes it, for one spec, and it holds everything an accumulator keeps.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
`DriverBinding` makes a driver from the fixed values that are not tables, and pushes the rows of each table in `fixed`, one push per table.
A table without rows is not pushed, so a driver never gets a push of no rows.
A plain request is an accumulator with no blanks, read once.
For the toy SANS reduction, a driver sums each run's numerator and denominator per table, and divides once:

```python
class IofQDriver:
    def __init__(self, fixed):                    # the fixed values that are not tables
        self._bins = fixed['reduction'].bins
        self._sums = {}                           # per table: numerator and denominator

    def push(self, table, rows):
        for row in rows:                          # the local path of a sample or a can run
            parts = numerator_and_denominator(row, self._bins)
            old = self._sums.get(table)
            self._sums[table] = parts if old is None else [a + b for a, b in zip(old, parts)]

    def outputs(self):                            # a state read has a sample row and a can row
        sample_num, sample_den = self._sums['sample']
        can_num, can_den = self._sums['can']
        return IofQOutputs(iofq=IofQ(intensity=sample_num / sample_den - can_num / can_den))
```

A workflow that needs every row at once, such as `STITCH`, writes a driver that keeps its rows and computes over all of them at each read.
The driver promises, and the backend does not check:

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a value it is given. A driver that adds in place copies the first contribution, so that adding never changes a value that another record returned.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec types `X | None` may be `None`.
- A driver may refuse a state, such as one with no sample row yet, by raising in `outputs()`. The read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.

The backend calls `push` and `outputs` from one thread at a time, and pushes only once the readers of the state before are done.
Code that calls a driver itself keeps the same order:

```python
driver = binding.make_accumulator(fixed, blanks=('runs',))
for run in runs:                              # each the local path of a run
    driver.push('runs', [run])
    counts = driver.outputs().volume.counts   # may be the array the next push adds into
    show(counts['q', 0])                      # done with it before the next push
```

`DriverBinding` may call its function that makes a driver from several threads at once, since plain requests run in parallel.

**A function.** `function_binding` serves plain requests only, for drafts in notebooks and for workflows that hold nothing worth keeping between calls.
It has no stage, since a stage over a function would hold nothing.

## Controllers

The backend does not decide what to run.
A *controller* decides over time what to submit, push, and release.
It is ordinary code that uses the client, such as a notebook, an application's UI, or a server that runs automatic reduction.
Controllers never run in the backend.

**A loop over arrivals** waits for new files.
This one adds each run of a rotation scan to a volume as it arrives, and cuts through the volume after each run (story D7):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.files.watch(Selector(scan='17')), 300):   # the scan's 300 runs
    volume.push(runs=[run])                                 # added once the previous cut is copied
    show(client.output(volume, 'volume', select={'q': 0}))
total = client.freeze(volume)
```

The binding of `VOLUME` is a driver that sums, so the loop holds one volume and keeps one record, from `freeze`.

**A loop over records** reduces each file in a request of its own.
`client.output` waits for its record, so the loop shows each result before it takes the next file:

```python
for run in client.files.watch(Selector(role='transmission')):
    record = client.submit(TRANSMISSION, params={'run': run, 'empty_beam': file(run=60336)})
    show(client.output(record, 'transmission'))
```

## Batch and automatic reduction

[automatic-reduction.md](automatic-reduction.md) states these rules in full.
The examples reduce transmission runs with `TRANSMISSION` (see Specs and bindings), whose parameters `run` and `empty_beam` are file fields.
`apply` fills a template once per file: the file is the value of the blank `at`, a file parameter, such as `'run'`.
A *lookup* fills further blanks, each a file parameter, such as the latest empty-beam run before each transmission run at `'empty_beam'`.
The template's fixed values give the other parameters, the same for each file.
`apply` returns the requests keyed by the value of a metadata field (`member_field`):

```python
empty_beams = Lookup({'empty_beam': LastBefore(Selector(role='empty-beam'))})
requests = apply(Template(TRANSMISSION, blanks=('run', 'empty_beam')), runs, client.files,
                 at='run', member_field='sample', lookup=empty_beams)
client.submit(requests, label='transmission', persist=True)
```

A *rule* is plain data: a name, a template, a selector, a label, the blank `at`, and optionally a `member_field`, a `lookup`, a `series`, and `persist`, the outputs it persists.
The *trigger loop* is a controller that fills the template for each new file the selector matches, and submits it under the label, persisted.
A file is new to a rule until a record under its label holds it at `at`.
With a *series*, one request takes all matching files so far with the same value of a metadata field, as the rows of the blank `at`, a table of files, such as `'runs'`.
An example is all angles of one sample at ESTIA, which the toy spec `STITCH` stitches, over its table `runs`, each divided by the reference run of its parameter `reference`, a file field:

```python
reference = file(run=500)                # the reference run
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', at='runs', label='reflectivity')
TriggerLoop(client, rules=[rule]).run()
```

## Provenance and publication

```python
provenance = client.provenance(result)
provenance.files(), provenance.records(), provenance.software
pid = client.publish(result.ref('iofq'), 'scicat')     # the output and its provenance
```

Provenance lists the record's request, every record that supplied a value by reference (directly or through other records), the files they used, and the software versions.
If a value is an output of an accumulator's state, the record listed is the record of that state.
Provenance stops at files.

Publishing puts an output in a catalogue with its provenance.
The catalogue entry stays after the proposal's history is dropped.
`publish` reads the value as `client.output` does, so it needs no persist request.

## Symmetries

Each symmetry names a change that a caller may make without changing the outputs.

| Symmetry | The caller may change | Unchanged | Kept by | Given |
|---|---|---|---|---|
| one-row | the single-run spec ↔ the multi-run spec, with one row in each table | the outputs | the workflow package | that its driver computes what its pipeline computes |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework for the record; the `sciline.Stage` of a `PipelineBinding` for the outputs | a pure pipeline |
| arrival | rows pushed one at a time, or in groups ↔ the same rows in one plain request | the outputs of state n | the driver | the driver's promises |
| order | the order of pushes into different tables | the outputs once the same rows are in | the driver | the driver's promises |
| placement | where a request runs | the record and its outputs | the framework | |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries a workflow package keeps, by comparing two calls with the same values:

- `check_one_row(single, multi, params)`: the binding of the multi-run spec, given the one-row request made from `params`, an instance of the single-run spec's params model, gives the outputs of the binding of the single-run spec. It is the only check against an independent reference.
- `check_caching(binding, fixed, calls)`: each call through a stage gives the plain request. For a `PipelineBinding` it mostly tests `ess.spec`.
- `check_arrival_and_order(make_driver, fixed, tables)`, with `tables` as `{table: rows}`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push per table gives. It takes the function that makes the driver, so a package tests its driver without a spec, and the fixed values: every value that is not a table, defaults included, since the check does not fill them in. Both sides go through the driver, so it does not prove the outputs are right.

## Left to the system

Not visible in the code of notebooks, apps, or workflow packages:

- how history and outputs are stored, moved, and dropped
- how a run number or path becomes a file identity
- when and where a request runs, and where a stage or a driver lives
- the format of persisted outputs and the store's layout
- how the service notices a vanished client and sets the cap per client (scipp/essapps#27)
- how access across proposals is enforced

## Open questions

1. **Grouping.** Spreading one accumulator over several nodes needs a merge of two drivers. No requirement asks for it ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
2. **Removing a row.** Whether an accumulator offers `remove` depends on whether it keeps each row.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Looks that compute.** `select=` only indexes an output. A projection, or a thick slice summed, would run a spec next to the value without making a record. The design of such looks waits for the plotting work. So does reading the latest state already added, without a pin.
5. **Accumulators on the service.** How the service holds a driver, bounds its memory, and drops it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md), scipp/essapps#27).
6. **Persist at submission.** Should `persist=` at submission also keep the values for the client? To be decided when a story needs it.
7. **Values modified in place.** In the user's process, a notebook that modifies a value `client.output` returned changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.
