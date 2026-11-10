# ADR 0008: A choice between ways of obtaining a value is a union of groups; `X | None` means without X

- Status: proposed
- Deciders: Simon
- Date: 2026-10-26

## Context

A workflow often takes a value that the user obtains in one of several ways.
A beam centre is typed in, or found from a run with a tolerance.
A transmission is the one in the sample run's file, typed in, or computed from a transmission run.
A workflow also often has a step that a request may go without, such as a mask or a direct-beam correction.

The spec must state these choices so that:

- a generic UI shows them, from the params schema alone ([ADR 0001](0001-minimal-workflow-spec.md));
- a request holds only values that mean something for the choice it makes;
- a workflow outputs a value without knowing which specs take it, and the output fits each field `Referenced[T]` of its class, such as a found centre at `beam_centre: BeamCentre | Referenced[BeamCentre]`;
- a `PipelineBinding` sets sciline keys from which sciline picks the providers, without a provider that takes the choice apart.

Terms from [ADR 0001](0001-minimal-workflow-spec.md) and the developer README:

- A *group* is a pydantic model, written as a JSON object of its fields. The params model is a group, and a field of it is a *parameter*.
- Each field, at any depth, is typed from a closed list, the *field types*: a *scalar*, such as a `float` or a `Literal`; a file field; a group; a union of groups; `Referenced[T]`; a union of members written inline and members `Referenced[...]`; `X | None`; or `list[X]`. Any other type, such as `sc.DataArray` or `tuple[float, float]`, is refused.
- A *table* is a list parameter whose elements an accumulator takes a push at a time; an element of a table is a *row*.
- A *reference to an output* names an output of a record, such as `finder.ref('centre')`.
- `Referenced[T]` types a field that a reference to an output fills, with data that fit the group `T`. A reference *fits* the field when the output's class is `T` or a subclass of it. A request writes a field of any other type, inline or as a file. The framework does not look inside `T`, so a group that holds an `sc.DataArray` is a `T`.
- A *file field* is a field that a file fills, typed `Annotated[Path, File(**metadata)]`, such as `RunFile`, which `ess.spec` provides. A request writes a *file* there as `file(...)`, which the workspace (`ess.apps.Workspace`, which runs requests) resolves when the request is submitted. The params model, and so the binding, holds the file's local path, a `Path`.
- A *template* is a request with some parameters left open, its *blanks*; the values it gives are its *fixed values*. A *plain request* gives every value.
- A *stage* is a template that the workspace keeps: it computes what does not depend on the blanks once, and the rest at each call.

Terms of `PipelineBinding`, which computes a spec with a sciline pipeline:

- `make_pipeline` is the function given to `PipelineBinding`. It gets the fixed values by name, never a blank, and returns a pipeline.
- A *key* is the sciline key that the binding sets from a parameter's value.
- A pipeline *holds a value* at a key when `pipeline[K] = v` was set, rather than a provider of `K` inserted. A *workflow factory*, such as esssans' `LokiAtLarmorTutorialWorkflow()`, returns a pipeline that holds some values as defaults.
- A key *leads to* another when a chain of providers computes the second from the first.

Terms of this ADR:

- A *tag* is the field that names an alternative of a union, such as `kind: Literal['find']`.
- "Typed `X | None`" means a type that admits `None`; it is not the same as *optional* in ADR 0001, which means "has a default".

## Decision

**A choice between ways is a discriminated union of groups.**
A field, at any depth, may be a union whose alternatives are groups, each with a tag:

```python
class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0
    y: float = 0.0

class FindCentre(BaseModel):              # find it in the sample run
    kind: Literal['find'] = 'find'
    tolerance: float = 0.1

class IofQParams(BaseModel):
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
    ...

# the params of two requests
{'beam_centre': {'kind': 'given', 'x': 0.012, 'y': -0.003}}
{'beam_centre': {'kind': 'find', 'tolerance': 0.05}}
```

`BeamCentre` is a group of the same fields as `GivenCentre`, without a tag:

```python
class BeamCentre(BaseModel):
    x: float = 0.0
    y: float = 0.0
```

- The tag is a `Literal` of one string, its own, without an alias, and the union is discriminated by the tag's name, not by a callable. The params schema maps each tag to its alternative, a form labels each alternative by its tag, and the position of an error inside the union leaves the tag out: each reads the tag by its field's name, without running code of the spec.
- A request writes the tag inline, even where the alternative has a default for it: the tag says which alternative's fields the object has.
- A union that holds a group holds groups only: a value of another type has no tag.

**A reference fits a union through an untagged member `Referenced[T]`.**
If an output could fit a tagged alternative, it would carry the tag of one alternative.
The workflow that computes it would then name an alternative of each spec that takes it, so a producer would know its consumers.
A member `Referenced[T]` needs no tag: a centre finder outputs a plain `BeamCentre`, and knows nothing of the specs that take it.
A request tells a reference from a value written inline by the key `@ref`, then the alternatives written inline by their tag:

```json
{"kind": "given", "x": 0.012, "y": -0.003}
{"@ref": "output", "record": "r17", "output": "centre"}
```

An alternative written inline is a group that a request writes.
A group that holds data no request writes, such as an `sc.DataArray`, is a member `Referenced[...]`, which needs no tag.
So the discriminator sits on the union of the members written inline, `Annotated[A | B, Field(discriminator='kind')] | Referenced[C]`, since a reference has no tag.

At each field, the spec author picks the ways it takes:

```python
beam_centre: BeamCentre = BeamCentre()                            # typed in
beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()   # typed in, or from a record: finder.ref('centre')
beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
                                                                  # typed in, or found in this graph
beam_centre: (                                                    # typed in, found in this graph, or from a record
    Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    | Referenced[BeamCentre]
) = GivenCentre()
```

A transmission is the one in the sample run's file, typed in, computed from a transmission run in this graph, or computed by another workflow, such as a transmission reduction that outputs `ComputedTransmission`.
One declaration states all four ways:

```python
class FromFile(BaseModel):                # an alternative without fields
    kind: Literal['file'] = Field('file', title="From the sample's file")

class TransmissionFraction(BaseModel):
    kind: Literal['given'] = 'given'
    value: float = Field(gt=0, le=1)

class FromRun(BaseModel):                 # computed from a transmission run in this graph
    kind: Literal['run'] = 'run'
    run: RunFile                          # a file field

transmission: (
    Annotated[FromFile | TransmissionFraction | FromRun, Field(discriminator='kind')]
    | Referenced[ComputedTransmission]    # holds an sc.DataArray: t611.ref('transmission')
) = FromFile()
```

A field may also take a file or a reference to an output, such as a normalization that some users read from the catalogue and others compute:
`normalization: RunFile | Referenced[ComputedNormalization] | None`, with `RunFile` the type of a file field.
The binding gets the file's local path or the group, and reads the file itself.

**`X | None` means *without X*.**
`mask: Referenced[PixelMask] | None = None` applies no mask for `None`; `direct_beam: RunFile | None`, the file of the direct-beam run, applies no direct-beam correction.
A value obtained in one of several ways that a request writes inline is a union that names each way, not `None` for one of them:

```python
transmission: Annotated[FromFile | TransmissionFraction, Field(discriminator='kind')] = FromFile()
# not: transmission: TransmissionFraction | None = None   # None: the one in the run's file
```

A UI shows a group as its fields; a field `Referenced[T]` as a picker of records; a union as one choice between its members, each alternative with its fields and a member `Referenced[...]` as "From a record"; and a type `X | None` as a choice that adds the way "None" to the ways of `X`, at any depth:

```
Beam centre: (•) Given  ( ) From a record      x [0.012] y [-0.003]
Transmission: (•) From the sample's file  ( ) Given  ( ) From a run  ( ) From a record
Direct beam: (•) A file  ( ) None              [run 60340 ▾]
Mask: ( ) From a record  (•) None
```

The title of an alternative's tag labels it in the choice, and the tag's description explains it; the schema titles a tag without a title by its value, `'given'` as "Given".
A choice offers "From a record" at each field that a reference fills, also while no output fits.
A choice without a default starts with no way chosen, as any field without a default starts empty; so does a required `BeamCentre | Referenced[BeamCentre]`.
A type `U | None`, for a union `U`, is one choice of the ways of `U` and "None".
The way "None" reads as "without X", so `None` must mean that.
That `None` means *without X* is a guideline, not a rule the framework checks.
It has no exception for a group that only a reference fills: `None` in `transmission: Referenced[ComputedTransmission] | None` does not mean "the one in the run's file".

**`X | None` at any depth.**
A field of a group may be typed `X | None`, such as `q_max: float | None` or `range: WavelengthRange | None` in `Reduction`.
The form shows it as a choice with the way "None", so a text input never has to tell an empty text from `None`.
A choice between modes is a union of groups.
An optional part with fields that need values is typed `X | None`: `direct_beam: RunFile | None` for an optional file, and `mask: Referenced[PixelMask] | None` for an optional mask, with `PixelMask` a group that holds the array.
A `bool` field switches one optional part of a group whose fields all have defaults, such as `limit_range: bool` next to `start` and `stop`, both with defaults; the form shows those fields either way.
A list that may be empty is `[]`, not `None`: a list is not typed `list[X] | None`, and its element is not typed `X | None`.

**A default never skips a correction that a correct reduction needs.**
A request that forgets a parameter, or a field of a group, must not get a reduction with a step silently skipped.
So a correction that a correct reduction needs, but that a request may still go without, is typed `X | None` with no default, `direct_beam: RunFile | None`, and the request writes `None`.
The output is I(Q) either way; the params of its record say which corrections were applied.
A correction that a correct reduction does not need, such as a mask, may default to `None`.
A step whose omission changes what a reader of the output must do with it, such as a normalization without which I(Q) is in counts, makes another output type, and so another spec.

**`PipelineBinding` sets one key per member of a union parameter, chosen by the value's class.**
A union parameter has a key for each member, the member's class: `binding.keys['beam_centre'] == (GivenCentre, FindCentre)`.
The binding sets the key of the member nearest to the value's class, the first class of `type(value).__mro__` that is a member, so sciline picks the providers.
The workspace makes each value by the type of its source, so a value written inline is of the alternative its tag names, and the data of a reference are of a referenced member or a subclass of one:

```python
def find_centre(settings: FindCentre, sample: SampleData) -> GivenCentre: ...

PipelineBinding(IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq]))
# a GivenCentre -> pipeline[GivenCentre] = value   (replaces find_centre)
# a FindCentre  -> pipeline[FindCentre] = value    (find_centre computes GivenCentre)
```

With `| Referenced[ComputedCentre]`, the keys are `(GivenCentre, FindCentre, ComputedCentre)`.
A union whose members are of one class, such as `BeamCentre | Referenced[BeamCentre]`, is that class to the binding, with one key, and not a union.
`keys=` may give the members one key, `(K, K)`, which every value then sets.
A union in a list parameter sets no key of its own: the list's key holds it, and a provider tells its elements apart.

**`PipelineBinding` sets no key for a parameter typed `X | None`; `make_pipeline` says what `None` means.**
Every value of a union sets the key of a member that the request chose; `None` names no key.
Setting `X` to `None` would replace a provider of `X`.
Setting nothing would leave the meaning of `None` to whatever the pipeline does without a value at `X`, which in a workflow factory is typically "compute X", not "without X".
So `keys=` gives such a parameter `()`, and the binding refuses it otherwise, with a message that says so:

```python
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sciline.Pipeline(providers)     # made for this call, never one it keeps
    if fixed['direct_beam'] is None:
        pipeline[sans.DirectBeam] = None       # esssans' key; its providers take None: no correction
    else:
        pipeline[sans.DirectBeamFilename] = fixed['direct_beam']
    return pipeline

PipelineBinding(IOFQ, make_pipeline, keys={'direct_beam': ()})
```

**Neither a union nor a parameter typed `X | None` is a blank of a `PipelineBinding` stage.**
A union's key may depend on each value's class, and `make_pipeline`, which gets no blank, may branch on it; this holds for `(K, K)` too, so that the rule has no exception.
A parameter typed `X | None` has no key, since `make_pipeline` decides what `None` means.
`BeamCentre | Referenced[BeamCentre]` is not a union to the binding, so it may be a blank.
A client that varies a union or a parameter typed `X | None` submits plain requests, which are correct but compute everything each time.

**`PipelineBinding` refuses a value that `make_pipeline` holds at a key of a union.**
A value held at a parameter's own key is harmless: the binding sets the request's value over it.
A union whose members have keys of their own is different: one member sets its key, and the others do not.
If `make_pipeline` held a default `GivenCentre`, as `LokiAtLarmorTutorialWorkflow()` holds a `BeamCenter`, the tag `'find'` would set `FindCentre`, `iofq` would read the held value, and `find_centre` would never run.
The binding refuses a value held at any key of such a union, for any tag, since it cannot tell which key leads to which: the held value has replaced the provider that would show it.
So a value held at `FindCentre`, which would be harmless, is refused too.
A test that runs each alternative but does not compare the results passes; the binding refuses the held value at the first request instead.
A value held at a key that a parameter's key leads to has the same effect, but the binding cannot see it, for the same reason: the spec's defaults belong in the spec.

## Consequences

- A UI generator needs one component beyond a plain group and a picker of records: a choice between ways, for a union and for a type `X | None`, at any depth. "None" is one more way, without fields.
- An output carries no tag. A workflow's output fits each field `Referenced[T]` of its class, in any spec, without a class per consumer.
- One parameter states every way of obtaining its value, a record's output among them, so one spec serves a user who types the value in, one who computes it in this graph, and one who takes it from a record.
- An author who wraps a workflow factory that holds a value at a key of a union replaces it with a provider, `pipeline.insert(adapter)`, such as an adapter from `GivenCentre` to esssans' `BeamCenter`. Mapping the alternatives to other keys with `keys=` does not help while the held value stays further down the graph.
- An author tests that each alternative has its effect, and that a request with `None` after one with a value gives the result without X. The second fails if `make_pipeline` sets the value in a pipeline it keeps.
- A spec that tunes a value in a stage, such as a scan of the beam centre, types that parameter as a group or as `X | Referenced[X]`, or its client submits plain requests.
- A table whose single-run parameter is a union of several classes has a key per member, so the driver of the multi-run spec cannot cut the single-run graph at one key for its rows. A table of `X | Referenced[X]` has one key.
- A choice for one field of a row of a table is a union at that field of the row's group, such as `transmission: TransmissionFraction | Referenced[ComputedTransmission]`. A choice of way for a whole row is a table whose rows are a union, `list[A | B]` or `list[A | B | Referenced[C]]`, and a choice of way for all rows is a parameter.

## Alternatives considered

- **A selector field next to the fields of every mode**, `enabled: bool` or `mode: Literal[...]`, as esslivedata's detector and monitor views do. Used to choose between modes, it lets a request hold values that mean nothing, `{'enabled': False, 'tolerance': 0.05}`, and a required field must be given even for a mode that does not use it.
- **A union held by a wrapper group.** The binding sets the wrapper's key, so a provider takes the union apart instead of sciline choosing providers by key.
- **A reference that fits a union when the output fits a tagged alternative.** An output fits only if its class is an alternative, with its tag, so the workflow that computes it knows the specs that take it. A centre finder outputs a tagged `GivenCentre`, a second class next to the `BeamCentre` that other fields take, and an alternative given only by reference, such as a computed transmission, carries a tag that no request writes. A member `Referenced[T]` gives the same choice with an untagged output.
- **A tag owned by the framework**, through a pydantic callable discriminator: `Annotated[BeamCentre | FindCentre, Choice()]`, where a request names the alternative by its class name under a reserved key, `{'@kind': 'FindCentre', 'tolerance': 0.05}`, and no group has a tag field. An untagged `BeamCentre` output then fits the union, and plain pydantic validates and dumps it. But it adds an annotation that the framework owns, makes class names part of a spec's interface, and differs from the pydantic-native `kind` fields that esslivedata uses. A reference fits through an untagged member `Referenced[T]`, so a tag field that the author writes serves.
- **An exception for groups that only a reference fills**, whose `None` could mean something else when the field's description says what, such as `transmission: Referenced[ComputedTransmission] | None` with `None` for the one in the run's file. A UI shows the description, but the way "None" still reads "without X", and a guideline with an exception is one more thing to teach.
- **The discriminator on the whole union**, `Annotated[A | B | Referenced[C], Field(discriminator='kind')]`. Pydantic would then need a tag on `C`, and a reference to an output carries none.
- **No type `X | None` in the spec**, with an empty list, an `enabled` flag, or an empty alternative such as `NoMask` in its place. Every parameter that may go without a value would be a union, and a request that goes without it would write a tagged object. It puts a concern of sciline bindings into the spec, which `function_binding` and `DriverBinding` do not share: they get `None` as a plain value.
- **`X | None` only at a parameter**, since a text input at a field of a group cannot tell an empty text from `None`. A form that shows every `X | None` as a choice with the way "None" removes that reason, and `range: WavelengthRange | None` in `Reduction` then needs no parameter of its own. A list that may be `None` has two empty values, `None` and `[]`, so a list is still not typed `list[X] | None`.
- **`X | None` keyed like a union**, with `None` setting nothing. `make_pipeline` would not set the key by hand. But `None` would then mean whatever the graph does without a value at `X`, typically "compute X", and nothing makes the author say otherwise.
- **`X | None` setting `pipeline[X] = None`.** It replaces a provider of `X` with `None`, so every provider that takes `X` must accept `None`.
- **A framework `NotSet` type as an alternative.** One type for many parameters would be one key for all of them, which `PipelineBinding` refuses; a generic `NotSet[Mask]` is an empty alternative such as `NoMask`, written by the author with no new type.
- **Keying by the tag.** A member `Referenced[T]` needs no tag.
- **Keying by `type(value)` itself.** A subclass instance passes the union unchanged, so its own type is no key of the union. The nearest member in its MRO is.
- **A union blank cut once per tag.** A stage would cut the pipeline at the key of each call's tag, the first time that tag comes, and hold one cut per tag; each cut computes its fixed part once. It needs one graph for all alternatives, and would also let a driver cut a table of unions. No story needs it yet; it is the extension to add when one does, such as a beam-centre scan in a stage.
- **No check of held values**, leaving defaults to the spec by convention and to tests. A test that runs each alternative but does not compare the results misses a held value.
- **A check of held values at every key a parameter sets.** A value at a plain parameter's key is harmless, and sciline cannot leave a key empty once a value is set, so the check would make an author insert a provider for every default of a workflow factory, for no gain.
