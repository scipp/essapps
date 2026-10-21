# Unions and references at any place

A proposal for PR #73, to review before the work starts. Three reviewers read an earlier draft, one each for the model, for requests and the file access check, and for bindings and the commit stack. Their reports and probes are local, in `.scratch/unions-and-references/`. The open points are under "Questions".

## Summary

A request may give every *place* of a value either inline or by reference, at any depth. A parameter may be a discriminated union of groups. `PipelineBinding` sets the sciline key of the alternative that the value's tag names. `None` is a value like any other. `PipelineBinding` sets no key to `None` and never leaves a key out because a value is `None`: a parameter typed `X | None` needs explicit handling by the workflow.

```python
class BeamCentre(BaseModel):                 # an alternative: the centre, given
    kind: Literal['given'] = 'given'
    x: float = 0.0
    y: float = 0.0

class FindCentre(BaseModel):                 # an alternative: settings to find it
    kind: Literal['find'] = 'find'
    run: SampleFile                          # a file, two levels down
    tolerance: float = 0.1

class Sample(BaseModel):                     # a run, and what is given next to it
    run: SampleFile
    transmission: TransmissionFraction | None = None   # None: the one in the run's file

class Reduction(BaseModel, arbitrary_types_allowed=True):
    bins: int = 100
    mask: Mask | None = None                 # Mask is a sc.DataArray: no JSON form

class IofQParams(BaseModel):
    sample: Sample
    can: CanFile
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: Annotated[BeamCentre | FindCentre, Field(discriminator='kind')] = BeamCentre()
```

Requests that the spec on `main` cannot express:

```python
{'beam_centre': {'kind': 'find', 'run': dataset(run=60339), 'tolerance': 0.05}}
{'beam_centre': centre_record.ref('centre')}           # FIND_CENTRE outputs a BeamCentre
{'reduction': {'bins': 50, 'mask': mask_record.ref('mask')}}
```

The binding keys each alternative:

```python
def find_centre(settings: FindCentre, ...) -> BeamCentre: ...

binding = PipelineBinding(IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq]))
binding.keys['beam_centre']   # (BeamCentre, FindCentre)
# tag 'given' -> pipeline[BeamCentre] = value   (replaces find_centre)
# tag 'find'  -> pipeline[FindCentre] = value   (find_centre computes BeamCentre)
```

Here one graph serves both alternatives, because `find_centre` leads from one alternative's key to the other's. Where the alternatives need different providers for one key, as esslivedata's TOA and wavelength modes do (`histogram_raw_monitor` and `histogram_wavelength_monitor` both return `MonitorHistogram`), `make_pipeline` branches on the fixed value. That always works, since a union is never a blank.

## Base

This work goes first: #74, #77 and #78 are updated after it. The branch is rebased onto `origin/main`, which has #72 and #75.

From #77 this PR takes the group part, `check_model` run on a group once it is made from its fields (see "Validation"). Every group may now be made from its fields, and without it a model validator of a group would stop running. #77 then rebases to cover the params model, stages and accumulators. #74's file detection by JSON schema replaces `is_file` and `holds_file` later, and this PR only calls them.

## The model

### Places

A request is a JSON object of parameter values. A *place* is a position in it where the request may write either a value or a reference to data that exist elsewhere. The places are:

- the value of each parameter, such as `reduction`;
- each element of a list, such as `sample[2]`;
- each field of a group (a pydantic model), at any depth, such as `reduction.mask` or `beam_centre.run`.

A request of the spec in the summary, with its places marked:

```python
{
    'sample': {                                    # sample
        'run': dataset(run=101),                   # sample.run
        'transmission': t_record.ref('fraction'),  # sample.transmission
    },
    'can': dataset(run=110),                       # can
    'reduction': {                                 # reduction
        'bins': 50,                                # reduction.bins
        'mask': mask_record.ref('mask'),           # reduction.mask
    },
    'beam_centre': {                               # beam_centre
        'kind': 'find',                            # (the tag: not a place)
        'run': dataset(run=103),                   # beam_centre.run
        'tolerance': 0.05,                         # beam_centre.tolerance
    },
}
```

On `main`, a request may hold a reference only at a parameter, at an element of a list parameter, and at a field of a group that holds a file. So `reduction.mask` and `beam_centre.run` above are refused there.

The contents of a tuple, a set, a dict, a `TypedDict` or a dataclass are not places. Such a value is written whole, as JSON, such as `'limits': [0.1, 0.5]` for `tuple[float, float]`.

### What a request writes at a place

The type at the place decides:

| Type at the place | The request writes | Example |
|---|---|---|
| a file: `Path`, or a `NewType` of it | a reference to a dataset | `'can': dataset(run=110)` |
| a group | an object of its fields, each field a place; or a reference to an output | `'reduction': {'bins': 50, 'mask': ...}`, `'beam_centre': c_record.ref('centre')` |
| a union of groups | an object of the chosen alternative's fields, with its tag; or a reference to an output | `'beam_centre': {'kind': 'find', ...}` |
| any other type with a JSON form: a number, a string, an enum, a tuple | the value as JSON (*inline*); or a reference to an output | `'bins': 50` |
| any other type without a JSON form, such as `sc.DataArray` | a reference to an output | `'mask': mask_record.ref('mask')` |
| `X \| None` | `None`, or what `X` takes | `'transmission': None` |

A group whose fields are all written inline is just its JSON form, as on `main`: `{'bins': 50}` for `reduction` reads the same either way.

Four rules go with the table:

- **A reference to an output** is accepted where the output's type fits the place's type (README, "fits"), and the output's type holds no file. Without the second condition, a `FindCentre` output, which holds a path, could bring a file in that no dataset check has passed.
- **A file only by a dataset reference**, at any place. A file inside a tuple, a dict or a dataclass, which is not a place, is refused when the spec is made.
- **The tag of a union is written inline, never as a reference.** The request phase needs the tag to know which alternative's fields the object has, and so which of them are files. The schema offers no reference at the tag. A callable `Discriminator` and an alias on the tag are refused. The request writes the tag even though the alternative has a default for it, since pydantic needs it.
- **`@ref` is a reserved key.** An object with the key `@ref` is a reference, at any depth. So no value written inline, a default included, may hold a dict with that key.

The serialized schema says what a request may hold: each place is `anyOf [inline, reference]` (or only the reference, for a type without a JSON form), and each inline object keeps `not: {required: ['@ref']}`. A widget may hide the reference where it has no use.

### Types of a place

| Type | Example |
|---|---|
| `X` | `reduction: Reduction` |
| `X \| None` | `transmission: TransmissionFraction \| None = None` |
| `list[X]`, a table if `tables` names it | `sample: list[Sample]` |
| a union `U`, or `U \| None` | `beam_centre: BeamCentre \| FindCentre` |

A *union* is a pydantic discriminated union of groups, `Annotated[A | B, Field(discriminator='kind')]`. Each alternative has a tag field typed as a one-value `Literal`, such as `kind: Literal['find']`. `list[U]` is a list of places, each a union.

Refused, as on `main`: `dict[str, X]` and `list[X] | None`. New refusals: a union without a discriminator, and a union of a group and a type that is not a group. A measured transmission as one alternative is written as a group that holds the data, `{'kind': 'measured', 'data': t.ref('transmission')}`, with `data` given by reference.

### `None` is a value

At every place, `None` is a value like any other, and it reaches the workflow's code:

- a parameter of a `DriverBinding` or a `function_binding` arrives as `None`;
- a field of a group reaches its provider inside the group: `sample.transmission is None`;

and the code branches on it.

`PipelineBinding` would have to turn a `None` parameter into a key, and both ways of doing that hide behaviour:

```python
pipeline[X] = None     # replaces a provider that make_pipeline added for this case
# or no key at all     # leaves a default that make_pipeline holds, which the request did not choose
```

So `PipelineBinding` refuses a parameter typed `X | None` (or `U | None`) that has a key. The workflow handles `None` in one of two explicit ways:

```python
# 1. make_pipeline takes the field itself
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sciline.Pipeline(providers)
    if fixed['direct_beam'] is None:
        pipeline[DirectBeam] = None
    else:
        pipeline[DirectBeamFilename] = fixed['direct_beam']
    return pipeline

PipelineBinding(SPEC, make_pipeline, keys={'direct_beam': ()})

# 2. the spec names the case as an alternative, with a key of its own
class NoMask(BaseModel):
    kind: Literal['none'] = 'none'

mask: Annotated[GivenMask | NoMask, Field(discriminator='kind')] = NoMask()
# keys: (GivenMask, NoMask); a provider of the workflow takes NoMask
```

In the second way, a workflow that forgets to read `NoMask` is refused, not silently given a default (see "Unread keys" below). A field without a key cannot be a blank, as on `main`.

### Validation

| Phase | What is validated |
|---|---|
| request phase | each inline place alone, from its JSON form |
| call phase, once the references are read | each group, innermost first: `model_construct` from its fields, then its model validators (`check_model`, from #77) |

A group is not validated again from its typed fields with `model_validate`. That would run a field's validators twice: `Annotated[float, AfterValidator(lambda v: v * 2)]` turns 3.0 into 6.0 alone and 12.0 whole (probe). A check of one value belongs to the value's type, such as `Annotated[float, AfterValidator(...)]`, which runs when the place is validated alone. A check across fields is a model validator, `mode='after'`.

The rules on every group in the params, which `main` states for groups that hold a file:

- allowed: `mode='after'` model validators that take only `self`, and validators in the `Annotated` metadata of field types;
- refused: field validators of any mode, `before` and `wrap` model validators, serializers, computed fields, and aliases.

The reason for refusing field validators is that a field validated alone, by `model_of`, runs without its group's field validators. `RangeModel` and `EdgesModel` check `stop > start` in a field validator, `@field_validator('stop')` with `info.data.get('start')`, in `ess.spec` and in esslivedata alike. On `main` and in esslivedata that check runs, because these groups are always validated whole, from JSON. Once a field of such a group may be given by reference, the group is made from its fields, and the check would be skipped silently. So the check moves to a model validator, as `EdgesModel` already does for its log check:

```python
@model_validator(mode='after')
def _stop_after_start(self) -> Self:
    if self.stop <= self.start:
        raise ValueError('stop must be greater than start')
    return self
```

### `PipelineBinding`

```
X              -> set key X
U              -> set the key of the alternative whose tag the value has
list[X]        -> set key list[X] to the list, as now; list[U] likewise
X | None       -> refused, unless keys= gives no key: ()
```

- The key of an alternative is the alternative as declared, a `NewType` included. `type(value)` would be wrong: a subclass instance passes a union unchanged (`revalidate_instances='never'`), and a `NewType` alternative's instance has the base type. Two parameters of one union type are told apart by `NewType` alternatives, as two parameters of one plain type are today.
- `keys[name]` is a tuple for every field: `(X,)`, `(A, B)` in declaration order, or `()` for a field that `make_pipeline` uses itself. `keys=` takes the same tuples. Outputs are `(K,)`. A driver writes `(key,) = single.keys['sample']`.
- No two *parameters* share a key, over all alternatives. An output may share a parameter's key: `centre: BeamCentre` as an output reports the centre given or found.
- A union cannot be a blank, since its key depends on its value. `make_stage` refuses it.
- `DriverBinding`, `function_binding`, `fixed_values` and `ess.spec.testing` need no change.

**Unread keys.** The binding sets a key for each parameter. If no provider on the way to the outputs reads that key, the request's value has no effect, and nothing says so:

```python
base = sciline.Pipeline([find_centre, iofq], params={BeamCentre: BeamCentre()})  # a default centre
binding = PipelineBinding(IOFQ, lambda fixed: base)

binding.compute(IofQParams(..., beam_centre=FindCentre(run=..., tolerance=0.05)))
# the binding sets pipeline[FindCentre]
# iofq reads BeamCentre, which holds the default; find_centre never runs
# -> I(Q) with the default centre, though the request asked to find it
```

The bindings reviewer ran this (`probes-bindings/p1`). The binding refuses such a request: after setting its keys, it checks that each one is read by the graph that computes the outputs, which `_Cut` already knows (`sciline.Stage(...).keys`). The error names the parameter and its key: `beam_centre: FindCentre is set, and no provider reads it`. The check covers every key, not only a union's: a plain parameter that the graph never reads is the same mistake.

### Outputs

No change. `X | None` on an output means that the graph may lack it. A union output stays refused.

### Errors at a place

pydantic puts the tag into the location of an error inside a union, `('beam_centre', 'find', 'tolerance')`. The framework drops the element after a union place, so that the error names `beam_centre.tolerance` whether pydantic or the framework picked the alternative.

## Commits

0. **`PipelineBinding` keys** (rewrites `dd2d382` and `a580d4b` in place):
   - `keys` as tuples;
   - `X | None` with a key refused;
   - unread keys refused.

   `.copy()` of the pipeline stays.
1. **No dataset for a whole group.** The request form `{'sample': dataset(run=101)}` goes.
   - `apply` and `try_apply` take the place a dataset fills, `at=('sample', 'run')`, which defaults to `(blank,)` for a file blank.
   - Automatic-reduction rules count a run as done only when it is the dataset at that place. With references at any depth, `beam_centre.run` would otherwise mark run 60339 as reduced.
   - `file_field` goes.
2. **References at any place.** `places`, `iter_refs` and `map_refs` walk every depth, and into a union by its tag. `checks.py` checks each place by its type. Refused:
   - a reference to an output that holds a file;
   - `@ref` at any depth of an inline value or a default;
   - a reference at a tag.

   Validation follows the table above:
   - `by_field` goes;
   - `check_model` comes from #77;
   - the group rules apply to every group;
   - the `RangeModel` and `EdgesModel` checks move.

   `_forms` and `_group_forms` give the forms at every place. A union inside a group exists since #72, so the walk by tag is tested through one (`Centre.by.run`).
3. **Unions as parameters.**
   - The spec rules take a union and a union option.
   - The discriminator stays on the union, where `_forms` today passes field metadata down to each alternative.
   - `fits(data, union)` holds when the data fit one alternative.
   - The key rule for unions, and the union blank refused.
   - #72's "a parameter is not a union" bullet and its error hint go, and the notebook's `ByMonitor | ByTime` example moves to a parameter.

Each commit updates the README and the workflow-authors notebook where they describe what it changes.

## Tests

- **Commit 0:**
  - an `X | None` parameter with a key is refused, and one with `keys={...: ()}` reaches `make_pipeline` as `None`;
  - an unread key is refused;
  - `keys=` takes tuples;
  - a driver built on `single.pipeline`.
- **Commit 1:** a dataset for a whole group is refused; `apply(at=('sample', 'run'))` fills `sample.run`; a rule counts only the dataset at that place.
- **Commit 2:**
  - references at `reduction.mask`, at `runs[2].transmission`, and at `Centre.by.run` through a wrapper group;
  - refusals: `@ref` in an inline value at any depth, a file in a tuple, a reference to an output that holds a file, a reference at a tag;
  - a model validator of a group runs in the call phase when a reference gives one of its fields, and refuses `stop <= start`;
  - a field validator on a group is refused.
- **Commit 3:**
  - the spec: a union parameter is accepted with a discriminator and refused without one;
  - fits and files: an output of type `BeamCentre` fits `BeamCentre | FindCentre`, and `beam_centre.run` takes a dataset reference;
  - keys:
    - one graph serves both alternatives;
    - the key is chosen by tag, for `NewType` alternatives and for a subclass instance;
    - two union parameters with `NewType` alternatives get separate keys;
    - a union left out takes its default alternative's key;
    - `list[U]` is keyed `list[U]`;
    - a union blank is refused;
  - stages: `check_caching` with both alternatives and the blank `sample` upstream of `find_centre`;
  - errors: an error inside a union names `beam_centre.tolerance`.

## Removed, and who reads it today

| Removed | Readers on `origin/main` |
|---|---|
| `by_field`, the "holds a file" trigger | `checks.py`, `field_types.validated`, `_workflow_spec` (`takes_inline`, `_params_schema`, `_forms`) |
| the depth limit of `places` | `refs.py`, `checks.py`, `Rules._requests` (via `datasets()`); README "three places only" |
| a dataset for a whole group, `file_field` | `checks.py`, `client.py` docstring, `batch.py`, README, `automatic-reduction.md`, workflow-authors notebook |
| "a parameter is not a union" (#72) | `_params_problems` error hint, notebook, README |
| `keys[name]` as one key | `toy_sans.py:313,327` (three notebooks), `pipeline_test.IofQDriver`, `test_keys_give_the_key_of_each_field` |
| field validators on groups | `RangeModel`, `EdgesModel` in `essspec/parameters.py` |

A log that the current code wrote may hold `@ref` deeper than today's places. The new code does not read it back, and the PR says so.

## Alternatives considered

- **A union held by a group** (#72). It works today, and with references at any place an output could feed its field. But the group is a wrapper with no meaning of its own. The binding sets the wrapper's key, so the workflow takes the union apart in a provider, instead of sciline choosing by key.
- **A selector field in the group**, `enabled: bool` or `mode: Literal[...]` next to all fields of all modes, as esslivedata's detector and monitor views do. A request can hold values that mean nothing (`enabled=False, start=3.0`), and an output of one mode's type does not fit.
- **`X | None` sets `pipeline[X] = None`.** This replaces a provider that `make_pipeline` added for the case of `None`.
- **`X | None` sets no key when `None`.** This leaves whatever `make_pipeline` holds at the key, such as a default value, which the request did not choose.
- **A `NotSet` type from the framework**, as an alternative in a union. One type for many parameters would be one sciline key for all of them, so it would have to be generic (`NotSet[Mask]`). An empty alternative that the author writes, such as `NoMask`, does the same with no new type, and its tag can say what "not set" means for this parameter.
- **Keying by `type(value)`.** Wrong for a subclass instance and a `NewType` alternative.
- **Validating a group whole with `model_validate` in the call phase.** It runs the field validators twice and can change a value that the request phase accepted.
- **Allowing `after` field validators on groups.** They run when the group is validated from JSON, and are skipped when it is made from its fields. So whether a check runs would depend on whether a request gives one field by reference.
- **`apply(value=lambda ref: {'run': ref})`.** It says how the blank is filled, but not where the dataset is. Automatic-reduction rules need the place to count a run as done, so `at=` serves both.
- **A reference to a whole list.** The model has no rule against it, but this PR refuses it with "not supported yet", for three reasons:
  - `fits(list[IofQ], list[sc.DataArray])` is `False`, while row by row `IofQ` fits `sc.DataArray`;
  - the length constraints and a table's distinct rows are checked in the request phase, where a reference has no rows;
  - a `list[Path]` output must not bring files past the access check.
- **Keeping the depth limit.** Its stated reason is that the framework finds references without the spec. A walk of every depth does that too, with `@ref` reserved everywhere.

## Questions

1. **`X | None` in `PipelineBinding`.** The proposal above refuses it with a key, and keeps `X | None` everywhere else as a plain value. That is a smaller step than refusing `| None` in the spec altogether: in a group field, and for the other two bindings, `None` reaches code that must branch on it, and nothing is hidden. `Sample.transmission: TransmissionFraction | None` stays as it is. The refusal can be lifted later, if a story needs a key rule for it. Do you agree, or do you want `| None` refused in the spec as well?
> Simon: ok

2. **Field validators in esslivedata.** esslivedata's `RangeModel` and `EdgesModel` have the same `@field_validator('stop')` as `ess.spec`'s, which they were copied from. To use them in a spec, esslivedata moves the check into a model validator, as above. That is a separate esslivedata PR, with no change in behaviour there, since esslivedata validates these models whole. Agreed?
> Simon: I still feel this is odd -- forbidding field-validators entirely, for everything, just because some groups may contain references. The examples we see do not, since it is just numbers (range limites, edge params). But maybe there is little harm (except making it more tricky to highlight offending fields in the generated widgets in the UI?).

3. **Unread keys** (see "`PipelineBinding`"). The check also refuses a parameter that only an optional output reads, when the graph has no provider for that output. The value would have no effect then, either. Agreed that this is refused too?
> Simon: Do I understand correctly that this check required inspecting the sciline graph? The "default on the graph/binding" problem is real, and imho we should just avoid doing that (and make it the convention when authoring workflows), is checking this in the framework the right approach?
