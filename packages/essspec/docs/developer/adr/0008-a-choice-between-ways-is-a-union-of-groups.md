# ADR 0008: A choice between ways of obtaining a value is a union of groups; `X | None` means without X

- Status: proposed
- Deciders: Simon
- Date: 2026-10-22

## Context

A workflow often takes a value that the user obtains in one of several ways.
A beam centre is typed in, or found from a run with a tolerance.
A transmission is the one in the sample run's file, typed in, or computed from a transmission run.
A workflow also often has a step that a request may leave out, such as a mask or a background subtraction.

The spec must state these choices so that:

- a generic UI shows them, from the params schema alone ([ADR 0001](0001-minimal-workflow-spec.md));
- a request holds only values that mean something for the choice it makes;
- a reference to an output fits the choice it stands for, such as a found centre for "the centre";
- a `PipelineBinding` sets sciline keys from which sciline picks the providers, without a provider that takes the choice apart.

Terms used below:

- A *group* is a pydantic model at a place of a request, written as an object of its fields ([ADR 0001](0001-minimal-workflow-spec.md)).
- A *place* is the value of a parameter, an element of a list, or a field of a group, at any depth.
- A *tag* is the field that names an alternative of a union, such as `kind: Literal['find']`.
- A *key* is the sciline key that `PipelineBinding` sets for a parameter.
- A *blank* is a parameter that a template leaves open; a stage of a `PipelineBinding` cuts the pipeline at the keys of its blanks and computes the rest once.

## Decision

**A choice between ways is a discriminated union of groups.**
A parameter, or a field of a group, may be a union whose alternatives are groups, each with a tag:

```python
class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0
    y: float = 0.0

class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
    run: SampleFile
    tolerance: float = 0.1

beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()

{'beam_centre': {'kind': 'find', 'run': dataset(run=60339), 'tolerance': 0.05}}
{'beam_centre': finder.ref('centre')}    # an output of type GivenCentre
```

- The tag is a `Literal` of one string, its own, without an alias, and the union is discriminated by the tag's name, not by a callable. The framework walks a value without running code of the spec, so it reads the tag by name.
- A request writes the tag inline, even where the alternative has a default for it, and never by reference: the tag says which alternative's fields the object has, and so which of them are files.
- A union that holds a group holds groups only: a value of another type has no tag.
- An output fits the union when it fits one alternative.

**`X | None` means *without X*.**
`mask: PixelMask | None = None` applies no mask for `None`; `background: BackgroundRun | None` subtracts no background.
A value obtained in another way is not `None` but an alternative that says so:

```python
class FromFile(BaseModel):                # an alternative without fields
    kind: Literal['file'] = 'file'

class TransmissionFraction(BaseModel):
    kind: Literal['given'] = 'given'
    value: float = Field(gt=0, le=1)

transmission: Annotated[FromFile | TransmissionFraction, Field(discriminator='kind')] = FromFile()
# not: transmission: TransmissionFraction | None = None   # None: the one in the run's file
```

A UI shows a union as a choice between its alternatives, and `X | None` as a toggle:

```
Transmission: (•) from the sample's file  ( ) given: [0.75]  [or from a record ▾]
[x] Background  Run: [60338]
[ ] Mask
```

A toggle that is off reads as "without X", so `None` must mean that.
This is a guideline, not a rule the framework checks: a data value, such as an `sc.DataArray`, cannot be an alternative, so where `None` means something else for it, the field's description says what.

**A default never leaves out a correction that a correct reduction needs.**
A request that forgets a field must not get a reduction with a step silently left out.
So a needed correction that a request may leave out is optional without a default, `background: BackgroundRun | None`, and the request writes `None`.
The output is I(Q) either way; the params of its record say which corrections were applied.
A correction that a correct reduction does not need, such as a mask, may default to `None`.
A step whose omission changes what a reader of the output must do with it, such as its unit, makes another output type, and so another spec.

**`PipelineBinding` sets one key per alternative, chosen by the tag.**
A union has a key for each alternative, the alternative as declared, a `NewType` included: `binding.keys['beam_centre'] == (GivenCentre, FindCentre)`.
The binding sets the key of the alternative that the value's tag names, so sciline picks the providers:

```python
def find_centre(settings: FindCentre, data: SampleData) -> GivenCentre: ...

PipelineBinding(IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq]))
# tag 'given' -> pipeline[GivenCentre] = value   (replaces find_centre)
# tag 'find'  -> pipeline[FindCentre] = value    (find_centre computes GivenCentre)
```

**`PipelineBinding` sets no key for `X | None`; `make_pipeline` says what `None` means.**
Each value of a union sets one key, but `None` has no key to set.
Setting `X` to `None` would replace a provider of `X`, and setting nothing would leave the meaning of `None` to whatever the pipeline holds or computes at `X`.
So `keys=` gives such a parameter `()`, and the binding refuses it otherwise, with a message that says so:

```python
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sciline.Pipeline(providers)     # made for this call, never one it keeps
    if fixed['background'] is None:
        pipeline.insert(no_background)
    else:
        pipeline[BackgroundRun] = fixed['background']
    return pipeline

PipelineBinding(IOFQ, make_pipeline, keys={'background': ()})
```

**Neither a union nor `X | None` is a blank of a `PipelineBinding` stage.**
A union's key depends on each value's tag, and `X | None` has no key, since `make_pipeline`, which gets no blank, decides what `None` means.
A client that varies such a parameter submits plain requests, which are correct but compute the fixed part each time.

**`PipelineBinding` refuses a value that `make_pipeline` holds at a key of a union.**
A value held at a parameter's own key is harmless: the binding sets the request's value over it.
A key of a union is set by one alternative only.
If `make_pipeline` held a default `GivenCentre`, as a workflow factory that sets a default beam centre does, the tag `'find'` would set `FindCentre`, `iofq` would read the held value, and `find_centre` would never run.
A test that only runs each alternative passes; the binding refuses the held value at the first request instead.
A value held at a key that a parameter's key leads to has the same effect, but the binding cannot see it, since the held value has replaced the provider in the graph: the spec's defaults belong in the spec.

## Consequences

- A UI generator needs two components beyond a plain group: a choice between the alternatives of a union, and a toggle for `X | None`. A toggle is the choice between two alternatives, one of them empty.
- An output whose type is an alternative carries the tag, such as `TRANSMISSION`'s `TransmissionFraction`, so that it fits the union.
- An author who wraps an existing workflow whose factory holds defaults, such as esssans' beam centre, removes the default at a key of a union, or maps the alternatives to other keys with `keys=`.
- An author tests that each alternative has its effect, and that a request with `None` after one with a value gives the result without X. The second fails if `make_pipeline` sets the value in a pipeline it keeps.
- A spec that tunes a value in a stage, such as a scan of the beam centre, types that parameter as a plain group, or its client submits plain requests.

## Alternatives considered

- **A selector field next to the fields of every mode**, `enabled: bool` or `mode: Literal[...]`, as esslivedata's detector and monitor views do. A request can then hold values that mean nothing, `{'enabled': False, 'tolerance': 0.05}`, and a required field must be given even for a mode that does not use it: a group that holds a run cannot be disabled without naming a run. An output of one mode's type does not fit.
- **A union held by a wrapper group.** The binding sets the wrapper's key, so a provider takes the union apart instead of sciline choosing providers by key.
- **No `X | None` in the spec**, with an empty list, an `enabled` flag, or an empty alternative such as `NoMask` in its place. A data value cannot be an alternative, so an optional data value would need a wrapper group. Optional fields of a group, such as a transmission computed for one run of a table and not another, would need one in every row. It puts a concern of sciline bindings into the spec, which `function_binding` and `DriverBinding` do not share: they get `None` as a plain value.
- **`X | None` keyed like a union**, with `None` setting nothing. `make_pipeline` would not set the key by hand. But an author who forgets the `None` case gets whatever provider of `X` the pipeline has, silently, and no error says what `None` must mean.
- **`X | None` setting `pipeline[X] = None`.** It replaces a provider of `X` with `None`, so every provider that takes `X` must accept `None`.
- **A framework `NotSet` type as an alternative.** One type for many parameters would be one key for all of them, which `PipelineBinding` refuses; a generic `NotSet[Mask]` is an empty alternative such as `NoMask`, written by the author with no new type.
- **Keying by `type(value)`.** A subclass instance passes the union unchanged, and a `NewType` alternative's values are instances of its group, so the type names the wrong key.
- **A union blank cut once per tag.** A stage would cut the pipeline at the key of each call's tag, the first time that tag comes, and hold one cut per tag; each cut computes its fixed part once. It needs one graph for all alternatives. No story needs it yet; it is the extension to add when one does, such as a beam-centre scan in a stage.
- **No check of held values**, leaving defaults to the spec by convention and to tests. A test that only runs each alternative misses a held value.
- **A check of held values at every key a parameter sets.** A value at a plain parameter's key is harmless, and sciline cannot remove a held value, so the check would make an author rebuild a workflow factory to drop defaults that do no harm.
