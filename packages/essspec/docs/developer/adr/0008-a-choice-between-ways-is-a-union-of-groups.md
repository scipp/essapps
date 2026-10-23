# ADR 0008: A choice between ways of obtaining a value is a union of groups; `X | None` means without X

- Status: proposed
- Deciders: Simon
- Date: 2026-10-22

## Context

A workflow often takes a value that the user obtains in one of several ways.
A beam centre is typed in, or found from a run with a tolerance.
A transmission is the one in the sample run's file, typed in, or computed from a transmission run.
A workflow also often has a step that a request may leave out, such as a mask or a direct-beam correction.

The spec must state these choices so that:

- a generic UI shows them, from the params schema alone ([ADR 0001](0001-minimal-workflow-spec.md));
- a request holds only values that mean something for the choice it makes;
- a reference to an output fits the choice it stands for, such as a found centre for "the centre";
- a `PipelineBinding` sets sciline keys from which sciline picks the providers, without a provider that takes the choice apart.

Terms from [ADR 0001](0001-minimal-workflow-spec.md) and the developer README:

- A *group* is a pydantic model at a place of a request, written as an object of its fields.
- A *place* is the value of a parameter, an element of a list, or a field of a group, at any depth.
- A *data value* is a value of a type that is neither a file, a group, a union of groups, a list, nor a dict, and has no JSON form, such as an `sc.DataArray`. It is a field of a group, which a request gives by reference only, since no request can write a data value inline (ADR 0001).
- A *template* is a request with some parameters left open, its *blanks*; the values it gives are its *fixed values*. A *plain request* gives every value.
- A *stage* is a template that the backend keeps: it computes what does not depend on the blanks once, and the rest at each call.

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
A parameter may be a union whose alternatives are groups, each with a tag; a union is never a field of a group, so that a choice between ways is a parameter of its own (ADR 0001):

```python
class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0
    y: float = 0.0

class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
    file: Path
    tolerance: float = 0.1

class IofQParams(BaseModel):
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
    ...

# the params of two requests; `finder` is the record of a request whose output `centre` is a GivenCentre
{'beam_centre': {'kind': 'find', 'file': dataset(run=60339), 'tolerance': 0.05}}
{'beam_centre': finder.ref('centre')}
```

- The tag is a `Literal` of one string, its own, without an alias, and the union is discriminated by the tag's name, not by a callable. The framework walks a value without running code of the spec, so it reads the tag by name.
- A request writes the tag inline, even where the alternative has a default for it, and never by reference: the tag says which alternative's fields the object has, and so which of them are files.
- A union that holds a group holds groups only: a value of another type has no tag.
- An output fits the union when it fits one alternative.

**`X | None` means *without X*.**
`mask: PixelMask | None = None` applies no mask for `None`; `direct_beam: DirectBeam | None`, a group of the direct-beam run's `file: Path`, applies no direct-beam correction.
A value obtained in another way is not `None` but an alternative that says so:

```python
class FromFile(BaseModel):                # an alternative without fields
    kind: Literal['file'] = Field('file', title="From the sample's file")

class TransmissionFraction(BaseModel):
    kind: Literal['given'] = 'given'
    value: float = Field(gt=0, le=1)

transmission: Annotated[FromFile | TransmissionFraction, Field(discriminator='kind')] = FromFile()
# not: transmission: TransmissionFraction | None = None   # None: the one in the run's file
```

A UI shows a union as a choice between its alternatives, and a type `X | None` as a toggle:

```
Transmission: (•) From the sample's file  ( ) Given: [0.75]  [or from a record ▾]
[x] Direct beam  File: [60340]
[ ] Mask
```

The title of an alternative's tag labels it in the choice, and the tag's description explains it; the schema titles a tag without a title by its value, `'given'` as "Given".
A union without a default starts with no alternative chosen, as any field without a default starts empty.
A toggle that is off reads as "without X", so `None` must mean that.
A type `U | None`, for a union `U`, is a toggle with a choice inside.
This is a guideline, not a rule the framework checks.
It has no exception for data values.
A data value obtained in one of several ways is a field of an alternative.
The workflow that computes it outputs the alternative, so that a reference to the output fits the union without a level in between.
The alternative is given by reference only, so a UI offers a record for it, `( ) From a record ▾`, and no fields:

```python
class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):
    kind: Literal['computed'] = 'computed'
    transmission: sc.DataArray

transmission: Annotated[FromFile | ComputedTransmission, Field(discriminator='kind')] = FromFile()

{'transmission': t611.ref('transmission')}   # t611's output is a ComputedTransmission
```

**A default never leaves out a correction that a correct reduction needs.**
A request that forgets a parameter, or a field of a group, must not get a reduction with a step silently left out.
So a correction that a correct reduction needs, but that a request may still leave out, is typed `X | None` with no default, `direct_beam: DirectBeam | None`, and the request writes `None`.
The output is I(Q) either way; the params of its record say which corrections were applied.
A correction that a correct reduction does not need, such as a mask, may default to `None`.
A step whose omission changes what a reader of the output must do with it, such as a normalization without which I(Q) is in counts, makes another output type, and so another spec.

**`PipelineBinding` sets one key per alternative of a union parameter, chosen by the tag.**
A union parameter has a key for each alternative, the alternative's class: `binding.keys['beam_centre'] == (GivenCentre, FindCentre)`.
The binding sets the key of the alternative that the value's tag names, so sciline picks the providers:

```python
def find_centre(settings: FindCentre) -> GivenCentre: ...   # reads settings.file

PipelineBinding(IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq]))
# tag 'given' -> pipeline[GivenCentre] = value   (replaces find_centre)
# tag 'find'  -> pipeline[FindCentre] = value    (find_centre computes GivenCentre)
```

`keys=` may give the alternatives one key, `(K, K)`, which every value then sets.
A union in a list parameter sets no key of its own: the list's key holds it, and a provider reads the tag of each row.

**`PipelineBinding` sets no key for a parameter typed `X | None`; `make_pipeline` says what `None` means.**
Every value of a union sets the key of an alternative that the request chose; `None` names no key.
Setting `X` to `None` would replace a provider of `X`.
Setting nothing would leave the meaning of `None` to whatever the pipeline does without a value at `X`, which in a workflow factory is typically "compute X", not "without X".
So `keys=` gives such a parameter `()`, and the binding refuses it otherwise, with a message that says so:

```python
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sciline.Pipeline(providers)     # made for this call, never one it keeps
    if fixed['direct_beam'] is None:
        pipeline[sans.DirectBeam] = None       # esssans' key; its providers take None: no correction
    else:
        pipeline[sans.DirectBeamFilename] = fixed['direct_beam'].file
    return pipeline

PipelineBinding(IOFQ, make_pipeline, keys={'direct_beam': ()})
```

**Neither a union nor a parameter typed `X | None` is a blank of a `PipelineBinding` stage.**
A union's key may depend on each value's tag, and `make_pipeline`, which gets no blank, may branch on it; this holds for `(K, K)` too, so that the rule has no exception.
A parameter typed `X | None` has no key, since `make_pipeline` decides what `None` means.
A client that varies such a parameter submits plain requests, which are correct but compute everything each time.

**`PipelineBinding` refuses a value that `make_pipeline` holds at a key of a union.**
A value held at a parameter's own key is harmless: the binding sets the request's value over it.
A union whose alternatives have keys of their own is different: one alternative sets its key, and the others do not.
If `make_pipeline` held a default `GivenCentre`, as `LokiAtLarmorTutorialWorkflow()` holds a `BeamCenter`, the tag `'find'` would set `FindCentre`, `iofq` would read the held value, and `find_centre` would never run.
The binding refuses a value held at any key of such a union, for any tag, since it cannot tell which key leads to which: the held value has replaced the provider that would show it.
So a value held at `FindCentre`, which would be harmless, is refused too.
A test that runs each alternative but does not compare the results passes; the binding refuses the held value at the first request instead.
A value held at a key that a parameter's key leads to has the same effect, but the binding cannot see it, for the same reason: the spec's defaults belong in the spec.

## Consequences

- A UI generator needs two components beyond a plain group: a choice between the alternatives of a union, and a toggle for a type `X | None`. A toggle is the choice between two alternatives, one of them empty.
- An output whose type is an alternative carries the tag, such as a `ComputedTransmission` output, so that it fits the union.
- An author who wraps a workflow factory that holds a value at a key of a union replaces it with a provider, `pipeline.insert(adapter)`, such as an adapter from `GivenCentre` to esssans' `BeamCenter`. Mapping the alternatives to other keys with `keys=` does not help while the held value stays further down the graph.
- An author tests that each alternative has its effect, and that a request with `None` after one with a value gives the result without X. The second fails if `make_pipeline` sets the value in a pipeline it keeps.
- A spec that tunes a value in a stage, such as a scan of the beam centre, types that parameter as a plain group, or its client submits plain requests.
- A table whose single-run parameter is a union has a key per alternative, so the driver of the multi-run spec cannot cut the single-run graph at one key for its rows.
- A choice for one field of a row of a table is not a union, since a union is never a field of a group: a correction that some rows have is a group typed `X | None` in the row, and a choice of way for all rows is a parameter. A table whose rows are a union, `list[A | B]`, gives each row the choice of its alternative.

## Alternatives considered

- **A selector field next to the fields of every mode**, `enabled: bool` or `mode: Literal[...]`, as esslivedata's detector and monitor views do. A request can then hold values that mean nothing, `{'enabled': False, 'tolerance': 0.05}`, and a required field must be given even for a mode that does not use it: a group that holds a run cannot be disabled without naming a run. An output of one mode's type does not fit.
- **A union held by a wrapper group.** The binding sets the wrapper's key, so a provider takes the union apart instead of sciline choosing providers by key.
- **An exception for data values**, whose `None` could mean something else when the field's description says what. A UI shows the description, but the toggle still reads "without X" when off, and a guideline with an exception is one more thing to teach. The alternative that holds the data value costs no level in a request when the workflow that computes it outputs the alternative.
- **No type `X | None` in the spec**, with an empty list, an `enabled` flag, or an empty alternative such as `NoMask` in its place. Every place that may go without a value would be a union, and a request that goes without it would write a tagged object, also at a field of a group, such as a transmission computed for one run of a table and not another. It puts a concern of sciline bindings into the spec, which `function_binding` and `DriverBinding` do not share: they get `None` as a plain value.
- **`X | None` keyed like a union**, with `None` setting nothing. `make_pipeline` would not set the key by hand. But `None` would then mean whatever the graph does without a value at `X`, typically "compute X", and nothing makes the author say otherwise.
- **`X | None` setting `pipeline[X] = None`.** It replaces a provider of `X` with `None`, so every provider that takes `X` must accept `None`.
- **A framework `NotSet` type as an alternative.** One type for many parameters would be one key for all of them, which `PipelineBinding` refuses; a generic `NotSet[Mask]` is an empty alternative such as `NoMask`, written by the author with no new type.
- **Keying by `type(value)`.** A subclass instance passes the union unchanged, so its type names the wrong key.
- **A union blank cut once per tag.** A stage would cut the pipeline at the key of each call's tag, the first time that tag comes, and hold one cut per tag; each cut computes its fixed part once. It needs one graph for all alternatives, and would also let a driver cut a table of unions. No story needs it yet; it is the extension to add when one does, such as a beam-centre scan in a stage.
- **No check of held values**, leaving defaults to the spec by convention and to tests. A test that runs each alternative but does not compare the results misses a held value.
- **A check of held values at every key a parameter sets.** A value at a plain parameter's key is harmless, and sciline cannot leave a key empty once a value is set, so the check would make an author insert a provider for every default of a workflow factory, for no gain.
