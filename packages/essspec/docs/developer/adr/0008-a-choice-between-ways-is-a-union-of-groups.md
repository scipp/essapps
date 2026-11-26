# ADR 0008: A choice between ways of obtaining a value is a union of groups; `X | None` means without X

- Status: proposed
- Deciders: Simon
- Date: 2026-10-26

## Context

A workflow often needs a value that the user obtains in one of several ways.
A beam centre is typed in, or found from a run with a tolerance.
A transmission is the one in the sample run's file, typed in, or computed from a transmission run.
A workflow also often has a step that a request may go without, such as a mask or a direct-beam correction.

The spec must state these choices so that:

- a generic UI shows them from the params schema alone ([ADR 0001](0001-minimal-workflow-spec.md));
- a request holds only values that mean something for the choice it makes;
- a workflow outputs a value without knowing which specs use it, such as a found centre for `beam_centre: BeamCentre | Referenced[BeamCentre]`;
- a `PipelineBinding` sets sciline keys from which sciline picks the providers, without a provider that takes the choice apart.

## Decision

[params.md](../../params.md) is the guide to unions and `None` in a spec, and [bindings.md](../../bindings.md) to how `PipelineBinding` keys them.

- **A choice between alternatives is a discriminated union of groups.** Each alternative has a tag field, a `Literal` of one string, and the union is discriminated by the tag's name. The params schema, a form and an error message then read the tag by its name, without running code of the spec. A request writes the tag even where it has a default, since the tag says which alternative's fields the object has.

  ```python
  beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()
  {'beam_centre': {'kind': 'find', 'tolerance': 0.05}}
  ```

- **A reference fits a union through an untagged member `Referenced[T]`.** A centre finder outputs a plain `BeamCentre`, and knows nothing of the specs that use it. The key `@ref` tells a reference from a value written inline, so the discriminator sits on the union of the members written inline.
- **`X | None` means *without X*, at any depth.** A form shows `X | None` as a choice that adds "None" to the alternatives of `X`, and "None" reads as "without X". So a value obtained in one of several ways is a union that names each, never `None` for one of them. The framework does not check this guideline, and it has no exception.
- **A default never skips a correction that a correct reduction needs.** A correction that a request may still go without is typed `X | None` with no default, so the request writes `None` and its record shows it.
- **`PipelineBinding` sets the key of the member that a value chose, and no key for `None`.** Setting `pipeline[X] = None` would replace a provider of `X`. Setting nothing would let `None` mean whatever the graph does without `X`, in a workflow factory often "compute X". So `make_pipeline` states what `None` means.
- **`PipelineBinding` rejects a value that `make_pipeline` holds at a key of a union whose members have keys of their own.** With a held `GivenCentre`, a request for `'find'` sets `FindCentre`, and `find_centre` never runs. The binding cannot tell which key leads to which, so it also rejects a held value that would be harmless.

## Consequences

- A UI generator needs one component beyond a group and a picker of records: a choice between alternatives, for a union and for `X | None`, at any depth.
- An output carries no tag, and fits each `Referenced[T]` of its class in any spec.
- One parameter states every way to obtain its value, so one spec serves a user who types it in, one who computes it in this graph, and one who takes it from a record.
- An author who wraps a workflow factory that holds a value at a key of a union replaces that value with a provider, such as an adapter from `GivenCentre` to esssans' `BeamCenter`.
- Neither a union nor `X | None` can be a blank of a stage: the key of a union may depend on the value, and `X | None` has no key. A client that varies one submits plain requests, which compute everything each time.
- An author tests that each alternative has its effect, and that `None` after a value returns the result without `X`.

## Alternatives considered

- **A selector field next to the fields of every mode**, `enabled: bool` or `mode: Literal[...]`, as in esslivedata. A request can hold values that mean nothing, and a required field must be given for a mode that does not use it.
- **A union held by a wrapper group.** The binding sets the wrapper's key, and a provider takes the union apart.
- **A reference that fits a tagged alternative.** The workflow that computes the output would carry the tag of each spec that uses it.
- **A tag owned by the framework**, a class name under `@kind`. It adds an annotation the framework owns, makes class names part of the interface, and differs from esslivedata's `kind` fields.
- **The discriminator on the whole union.** pydantic would need a tag on the `Referenced[...]` members.
- **An exception for groups that only a reference fills**, where `None` means something else. "None" still reads "without X", and an exception is one more thing to teach.
- **No `X | None` in the spec**, with an empty alternative such as `NoMask`. It puts a concern of sciline bindings into the spec, which `function_binding` and `DriverBinding` do not share.
- **`X | None` only at a parameter.** A form that shows every `X | None` as a choice removes the reason.
- **`X | None` keyed like a union, `None` setting nothing.** `None` would mean whatever the graph does without `X`.
- **`X | None` setting `pipeline[X] = None`.** Every provider that uses `X` must then accept `None`.
- **A `NotSet` type of the framework.** One type for many parameters is one key for all of them.
- **Keying by the tag.** A member `Referenced[T]` has no tag.
- **Keying by `type(value)`.** A subclass is not a member of the union; the nearest member in its MRO is.
- **A union blank cut once per tag.** No user story needs it yet. It is the extension to add for a beam-centre scan in a stage.
- **No check of held values.** A test that runs each alternative without comparing the results misses a held value.
- **A check of held values at every key that a parameter sets.** A value at a plain parameter's key is harmless, and the check would force a provider for every default of a workflow factory.
