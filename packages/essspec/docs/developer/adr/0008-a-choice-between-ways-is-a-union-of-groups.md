# ADR 0008: A choice between ways of obtaining a value is a union of groups, written inline; `X | None` means without X

- Status: proposed
- Deciders: Simon
- Date: 2026-10-26

## Context

A workflow often takes a value that the user obtains in one of several ways.
A beam centre is typed in, or found from a run with a tolerance.
A transmission is the one in the sample run's file, typed in, or computed from a transmission run.
A workflow also often has a step that a request may leave out, such as a mask or a direct-beam correction.

The spec must state these choices so that:

- a generic UI shows them, from the params schema alone ([ADR 0001](0001-minimal-workflow-spec.md));
- a request holds only values that mean something for the choice it makes;
- a workflow outputs a value without knowing which specs take it, and the output fits each place typed by its class, such as a found centre at `beam_centre: BeamCentre`;
- a `PipelineBinding` sets sciline keys from which sciline picks the providers, without a provider that takes the choice apart.

Terms from [ADR 0001](0001-minimal-workflow-spec.md) and the developer README:

- A *group* is a pydantic model at a place of a request, written as an object of its fields.
- A *place* is the value of a parameter, an element of a list, or a field of a group, at any depth.
- A *reference* names data that exist elsewhere: an output of a record, `finder.ref('centre')`, or a dataset. It *fits* a place when the data it names can be the value there.
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

# the params of two requests
{'beam_centre': {'kind': 'given', 'x': 0.012, 'y': -0.003}}
{'beam_centre': {'kind': 'find', 'file': dataset(run=60339), 'tolerance': 0.05}}
```

`BeamCentre` is a group of the same fields as `GivenCentre`, without a tag:

```python
class BeamCentre(BaseModel):
    x: float = 0.0
    y: float = 0.0
```

- The tag is a `Literal` of one string, its own, without an alias, and the union is discriminated by the tag's name, not by a callable. The framework walks a value without running code of the spec, so it reads the tag by name.
- A request writes the tag inline, even where the alternative has a default for it: the tag says which alternative's fields the object has, and so which of them are files.
- A union that holds a group holds groups only: a value of another type has no tag.

**A union is written inline only: no reference fits it.**
If an output could fit a union, it would carry the tag of one alternative.
The workflow that computes it would then name an alternative of each spec that takes it, so a producer would know its consumers.
A centre finder that outputs `BeamCentre` fits `beam_centre: BeamCentre`, but not `GivenCentre | FindCentre`.
An alternative that only a reference can give, one that holds a data value, would carry a tag that no request writes.
So:

- `WorkflowSpec` refuses an alternative that a request cannot write inline: one with a field that is a data value.
- No reference stands for the value of a union: a request with a reference at a union is refused at submission.
- A field of an alternative is a place of its own, so `FindCentre.file` takes a dataset, and a field of an alternative that is a group takes an output.
- The params schema of a union has no reference form.

At each place, the spec author picks a group or a union:

```python
beam_centre: BeamCentre = BeamCentre()        # typed in, or from a record: finder.ref('centre')
beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
                                              # typed in, or found in this graph; no reference fits
```

A value obtained either as a record's output or in another way is a group, given by reference, and the other way is a workflow that outputs the group.
A published normalization, a file of the factor that a vanadium reduction computes, is given by submitting a workflow that reads the file and outputs the normalization, and by referencing that output.
In both cases the stories and this ADR name, the normalization and the transmission, the other way is a file of the data that a workflow computes: `Computed | File`.
A way to hide that difference from the user is left open.

A union names only ways that a request writes inline, such as a transmission taken from the sample's file, typed in, or computed in this graph from a transmission run.
A data value that another workflow computes is a field of a group given by reference, such as `ComputedTransmission`, the output of a transmission reduction:

```python
class FromFile(BaseModel):                # an alternative without fields
    kind: Literal['file'] = Field('file', title="From the sample's file")

class TransmissionFraction(BaseModel):
    kind: Literal['given'] = 'given'
    value: float = Field(gt=0, le=1)

class FromRun(BaseModel):
    kind: Literal['run'] = 'run'
    file: Path                            # the transmission run

transmission: Annotated[FromFile | TransmissionFraction | FromRun, Field(discriminator='kind')] = FromFile()
transmission: ComputedTransmission        # a group with a data value, by reference: t611.ref('transmission'),
                                          # or the output of a workflow that reads the sample's file
```

**`X | None` means *without X*.**
`mask: PixelMask | None = None` applies no mask for `None`; `direct_beam: DirectBeam | None`, a group of the direct-beam run's `file: Path`, applies no direct-beam correction.
A value obtained in one of several ways that a request writes inline is a union that names each way, not `None` for one of them:

```python
transmission: Annotated[FromFile | TransmissionFraction, Field(discriminator='kind')] = FromFile()
# not: transmission: TransmissionFraction | None = None   # None: the one in the run's file
```

A UI shows a group as its fields, with a record as the other option; a union as a choice between its alternatives; and a type `X | None` as a toggle, which adds the option `None` to either:

```
Beam centre: x [0.012] y [-0.003]  [or from a record ▾]
Transmission: (•) From the sample's file  ( ) Given: [0.75]
[x] Direct beam  File: [60340]
[ ] Mask
```

The title of an alternative's tag labels it in the choice, and the tag's description explains it; the schema titles a tag without a title by its value, `'given'` as "Given".
A union without a default starts with no alternative chosen, as any field without a default starts empty.
A type `U | None`, for a union `U`, is a toggle with a choice inside.
A toggle that is off reads as "without X", so `None` must mean that.
That `None` means *without X* is a guideline, not a rule the framework checks.
It has no exception for data values: `None` in `transmission: ComputedTransmission | None` does not mean "the one in the run's file".

**At a field of a group, `X` in `X | None` is a group.**
This holds at any depth, and at an element of a list there: `range: WavelengthRange | None`, not `start: float | None` and `stop: float | None`.
So each toggle is over the fields of a group, as in esslivedata's dashboard, which shows `range_filter: TOARange | None` as a card with a switch (scipp/esslivedata#1426).
A value that a request may leave out is a field of such a group: an optional file is a run, `transmission: TransmissionRun | None`, and an optional data value is a group of it, `mask: PixelMask | None`.
A value written whole that may be empty is `[]` or `()`, not `None`.
A parameter is a group or a union of groups already, so the rule changes nothing there.
A union stays a parameter only, while a group typed `X | None` may be a field of a group.
A union sets one sciline key per alternative; `None` sets no key, so a group typed `X | None` inside a group loses nothing.

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

- A UI generator needs two components beyond a plain group: a choice between the alternatives of a union, and a toggle for a type `X | None`, whose `X` is a group or a union of groups, never a single input. A toggle is the choice between two alternatives, one of them empty. A UI offers no reference at the place of a union, since no reference stands for its value, but it offers one at a field of an alternative that takes one, as at any place.
- An output carries no tag. A workflow's output fits each place typed by its class, in any spec, without a class per consumer.
- No reference stands for the value of a union, so the spec author picks a group, which takes a record's output, or a union, whose alternatives a request writes inline. "A record, or another way" is a group given by reference, plus a workflow that outputs the group from the other way.
- An author who wraps a workflow factory that holds a value at a key of a union replaces it with a provider, `pipeline.insert(adapter)`, such as an adapter from `GivenCentre` to esssans' `BeamCenter`. Mapping the alternatives to other keys with `keys=` does not help while the held value stays further down the graph.
- An author tests that each alternative has its effect, and that a request with `None` after one with a value gives the result without X. The second fails if `make_pipeline` sets the value in a pipeline it keeps.
- A spec that tunes a value in a stage, such as a scan of the beam centre, types that parameter as a plain group, or its client submits plain requests.
- A table whose single-run parameter is a union has a key per alternative, so the driver of the multi-run spec cannot cut the single-run graph at one key for its rows.
- A choice for one field of a row of a table is not a union, since a union is never a field of a group. A choice of way per row, such as a transmission from the sample run or from a separate transmission run, is a table whose rows are a union, `list[A | B]`, each row written inline; a correction that some rows have is a group typed `X | None` in the row; and a choice of way for all rows is a parameter.

## Alternatives considered

- **A selector field next to the fields of every mode**, `enabled: bool` or `mode: Literal[...]`, as esslivedata's detector and monitor views do. A request can then hold values that mean nothing, `{'enabled': False, 'tolerance': 0.05}`, and a required field must be given even for a mode that does not use it: a group that holds a run cannot be disabled without naming a run.
- **A union held by a wrapper group.** The binding sets the wrapper's key, so a provider takes the union apart instead of sciline choosing providers by key.
- **A reference that fits a union when the output fits an alternative.** One place then offers a value typed in, computed in this graph, or taken from a record. But an output fits only if its class is an alternative, with its tag, so the workflow that computes it knows the specs that take it. A centre finder outputs a tagged `GivenCentre`, a second class next to the `BeamCentre` that a place typed `BeamCentre` takes, and an alternative given only by reference, such as a computed transmission, carries a tag that no request writes.
- **A tag owned by the framework**, through a pydantic callable discriminator: `Annotated[BeamCentre | FindCentre, Choice()]`, where a request names the alternative by its class name under a reserved key, `{'@kind': 'FindCentre', 'file': ..., 'tolerance': 0.05}`, and no group has a tag field. An untagged `BeamCentre` output then fits the union, and plain pydantic validates and dumps it. But it adds an annotation that the framework owns, makes class names part of a spec's interface, and differs from the pydantic-native `kind` fields that esslivedata uses. With no reference at a union, a tag field that the author writes serves.
- **An exception for data values**, whose `None` could mean something else when the field's description says what, such as `transmission: ComputedTransmission | None` with `None` for the one in the run's file. A UI shows the description, but the toggle still reads "without X" when off, and a guideline with an exception is one more thing to teach.
- **No type `X | None` in the spec**, with an empty list, an `enabled` flag, or an empty alternative such as `NoMask` in its place. Every place that may go without a value would be a union, and a request that goes without it would write a tagged object, also at a field of a group, such as a mask for one run of a table and not another. It puts a concern of sciline bindings into the spec, which `function_binding` and `DriverBinding` do not share: they get `None` as a plain value.
- **`X | None` at any field**, such as `offset: float | None` or `run: Path | None`. A UI would show a second kind of toggle, next to a single input or a dataset picker. Each such case is written as well as a group, `range: WavelengthRange | None` or `transmission: TransmissionRun | None`, whose class also names the role. A list or a dict that may be `None` has two empty values, `None` and `[]`.
- **`X | None` keyed like a union**, with `None` setting nothing. `make_pipeline` would not set the key by hand. But `None` would then mean whatever the graph does without a value at `X`, typically "compute X", and nothing makes the author say otherwise.
- **`X | None` setting `pipeline[X] = None`.** It replaces a provider of `X` with `None`, so every provider that takes `X` must accept `None`.
- **A framework `NotSet` type as an alternative.** One type for many parameters would be one key for all of them, which `PipelineBinding` refuses; a generic `NotSet[Mask]` is an empty alternative such as `NoMask`, written by the author with no new type.
- **Keying by `type(value)`.** A subclass instance passes the union unchanged, so its type names the wrong key.
- **A union blank cut once per tag.** A stage would cut the pipeline at the key of each call's tag, the first time that tag comes, and hold one cut per tag; each cut computes its fixed part once. It needs one graph for all alternatives, and would also let a driver cut a table of unions. No story needs it yet; it is the extension to add when one does, such as a beam-centre scan in a stage.
- **No check of held values**, leaving defaults to the spec by convention and to tests. A test that runs each alternative but does not compare the results misses a held value.
- **A check of held values at every key a parameter sets.** A value at a plain parameter's key is harmless, and sciline cannot leave a key empty once a value is set, so the check would make an author insert a provider for every default of a workflow factory, for no gain.
