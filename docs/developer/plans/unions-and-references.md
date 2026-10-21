# Unions and references at any place

A proposal that grew out of PR #73, to review before the work starts. Three reviewers read an earlier draft, one each for the model, for requests and the file access check, and for bindings and the commit stack. Their reports and probes are local, in `.scratch/unions-and-references/`. The open points are under "Questions".

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

In the call phase, pydantic validates each parameter's value whole (see "Validation"), so the model validators of its groups run without #77's `check_model`. #77 then rebases to cover the params model, stages and accumulators. #74's file detection by JSON schema replaces `is_file` and `holds_file` later, and this PR only calls them.

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

In the second way, a workflow without a provider for the mask from `NoMask` fails with `UnsatisfiedRequirement`, since the key of the other alternative is not set either. It is not silently given a default, as long as the pipeline holds none (see "Defaults belong to the spec" below). A field without a key cannot be a blank, as on `main`.

### Validation

Each parameter's value is validated once per phase, by pydantic:

| Phase | What is validated |
|---|---|
| request phase | each inline place alone, from its JSON form; a group with no reference inside, whole, from its JSON form |
| call phase, once the references are read | the parameter's value whole: the JSON form the record holds, with each reference replaced by the data read, by `model_validate(..., strict=False)` |

In the call phase, every validator of every group runs once, with all fields at hand: field validators of any mode, with `info.data`, and model validators. So `RangeModel`'s and `EdgesModel`'s `@field_validator('stop')`, which reads `info.data.get('start')`, needs no change, in `ess.spec` or in esslivedata. Its error names the field (probe):

```python
inline = from_json(to_json({'start': 10.0, 'unit': 'mm'}))   # as the record holds them
Range.model_validate({**inline, 'stop': 5.0}, strict=False)  # stop read from another record
# ValidationError at ('stop',): stop must be greater than start
```

A group without a reference inside is validated whole already in the request phase, so a form shows such an error at its field before anything runs. That covers the ranges and edges of the examples, which are only numbers.

Two points make validating once from the stored JSON form sound:

- **No validator runs twice on one value.** The call phase starts again from the JSON form, not from the typed values of the request phase. A validator that changes a value it has validated already, such as `AfterValidator(lambda v: v * 2)`, is refused in the request phase on `main` already: its JSON form does not read back as the value (`inline`).
- **`strict=False` accepts no less than the request phase did.** The request phase validates each inline place from JSON, where pydantic's strict mode takes an enum from its string. The call phase validates JSON-decoded values in Python mode, where a strict model would refuse that string (`Input should be an instance of Unit`, probe). The request phase has already applied the strict rules to every inline place, and a reference's data fit the place by type.

The rules on every group in the params:

- refused: serializers, computed fields, and aliases. The record holds a group as the object of its places, each in the JSON form of its own type, and these would make the group's own JSON form differ from that object.
- allowed: field validators and model validators of any mode. #77's refusal of `before` and `wrap` model validators gave as its reason that their input differs between the phases. Here the call phase's input is the request's JSON form, with data only where the request gave a reference, so #77 can reconsider that refusal when it rebases.

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

**Defaults belong to the spec.** `make_pipeline` holds no value at a key that a parameter sets, or at a key that a parameter's key leads to. A held default can make a request's value have no effect, and nothing says so:

```python
base = sciline.Pipeline([find_centre, iofq], params={BeamCentre: BeamCentre()})  # a default centre
binding = PipelineBinding(IOFQ, lambda fixed: base)

binding.compute(IofQParams(..., beam_centre=FindCentre(run=..., tolerance=0.05)))
# the binding sets pipeline[FindCentre]
# iofq reads BeamCentre, which holds the default; find_centre never runs
# -> I(Q) with the default centre, though the request asked to find it
```

The bindings reviewer ran this (`probes-bindings/p1`). The default belongs in the spec instead, `beam_centre: ... = BeamCentre()`, where a user sees it. The `PipelineBinding` docstring and the workflow-authors notebook state this convention. The binding does not check it.

### Outputs

No change. `X | None` on an output means that the graph may lack it. A union output stays refused.

### Errors at a place

pydantic puts the tag into the location of an error inside a union, `('beam_centre', 'find', 'tolerance')`. The framework drops the element after a union place, so that the error names `beam_centre.tolerance` whether pydantic or the framework picked the alternative.

## Commits

0. **`PipelineBinding` keys** (rewrites `dd2d382` and `a580d4b` in place):
   - `keys` as tuples;
   - `X | None` with a key refused;
   - the docstring states that defaults belong to the spec.

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
   - the call phase validates each parameter's value once, from its JSON form with the data of its references;
   - the group rules apply to every group, and field validators are no longer refused on groups that hold a file.

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
  - `keys=` takes tuples;
  - a driver built on `single.pipeline`.
- **Commit 1:** a dataset for a whole group is refused; `apply(at=('sample', 'run'))` fills `sample.run`; a rule counts only the dataset at that place.
- **Commit 2:**
  - references at `reduction.mask`, at `runs[2].transmission`, and at `Centre.by.run` through a wrapper group;
  - refusals: `@ref` in an inline value at any depth, a file in a tuple, a reference to an output that holds a file, a reference at a tag;
  - a range whose `stop` is given by a reference fails in the call phase, at `stop`, when `stop <= start`;
  - a range given inline fails in the request phase, at `stop`;
  - a strict model with an enum field passes the call phase.
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
| `model_construct` of a group from its fields, and the refusal of field validators on such a group | `field_types.validated`, `_group_problems` |

A log that the current code wrote may hold `@ref` deeper than today's places. The new code does not read it back, and the PR says so.

## Alternatives considered

- **A union held by a group** (#72). It works today, and with references at any place an output could feed its field. But the group is a wrapper with no meaning of its own. The binding sets the wrapper's key, so the workflow takes the union apart in a provider, instead of sciline choosing by key.
- **A selector field in the group**, `enabled: bool` or `mode: Literal[...]` next to all fields of all modes, as esslivedata's detector and monitor views do. A request can hold values that mean nothing (`enabled=False, start=3.0`), and an output of one mode's type does not fit.
- **`X | None` sets `pipeline[X] = None`.** This replaces a provider that `make_pipeline` added for the case of `None`.
- **`X | None` sets no key when `None`.** This leaves whatever `make_pipeline` holds at the key, such as a default value, which the request did not choose.
- **A `NotSet` type from the framework**, as an alternative in a union. One type for many parameters would be one sciline key for all of them, so it would have to be generic (`NotSet[Mask]`). An empty alternative that the author writes, such as `NoMask`, does the same with no new type, and its tag can say what "not set" means for this parameter.
- **Keying by `type(value)`.** Wrong for a subclass instance and a `NewType` alternative.
- **Making a group from its fields, `model_construct` and then its model validators.** A field validated alone runs without its group's field validators, so a check such as `stop > start` would be skipped once a request gives one field by reference. Refusing field validators on every group would avoid that, at the cost of moving such checks into model validators, whose errors name the group instead of the field.
- **Validating a group again from its typed values.** A `BeforeValidator` that splits a string fails on the tuple it made (probe), and a validator would see its own output.
- **A framework check that every key the binding sets is read by the graph.** It needs the graph that the cut computes for each request. It would also refuse a parameter that only an optional output reads, when the graph lacks that output. The convention that defaults belong to the spec removes the cause instead.
- **`apply(value=lambda ref: {'run': ref})`.** It says how the blank is filled, but not where the dataset is. Automatic-reduction rules need the place to count a run as done, so `at=` serves both.
- **A reference to a whole list.** The model has no rule against it, but this PR refuses it with "not supported yet", for three reasons:
  - `fits(list[IofQ], list[sc.DataArray])` is `False`, while row by row `IofQ` fits `sc.DataArray`;
  - the length constraints and a table's distinct rows are checked in the request phase, where a reference has no rows;
  - a `list[Path]` output must not bring files past the access check.
- **Keeping the depth limit.** Its stated reason is that the framework finds references without the spec. A walk of every depth does that too, with `@ref` reserved everywhere.

## Decided

All questions are settled. `X | None` with a key is refused in `PipelineBinding` only. Field validators stay allowed, by validating each value once in the call phase. Defaults belong to the spec, by convention.

## Implementation notes

For an implementer starting from this file alone.

**Setup.**
- This plan lives on the branch `pipeline-copy-and-keys` (PR #73, closed), which is its backup.
- Implement on a new branch `unions-and-references`, from `origin/main` once #78 is merged. `origin/main` already has #72 and #75. The local `main` may be stale, so `git fetch` first. Open a new PR from it, and point to it from #73.
- `dd2d382` and `a580d4b` on `pipeline-copy-and-keys` are superseded by commit 0. Keep only their `.copy()` change and the test `test_make_pipeline_may_return_a_pipeline_it_holds`.
- Create a `.venv` with `--system-site-packages`, holding editable sciline, essspec and essdispatch. Use that python; the conda base may shadow sciline.
- Run `pytest -n auto` in `packages/essspec` and `packages/essdispatch` after each commit.
- Edit the notebooks under `docs/developer/roles/` as `.ipynb` directly. They import `toy_sans.py` next to them. `nbconvert` stalls on notebooks with Output widgets, so run those cells by hand.

**#78 first.** A client turns a model into a dict with `model_dump(exclude_unset=True)` (`as_values` in `records.py`). That drops a union alternative's tag, which is a default, so the backend cannot pick the alternative. #78 fixes this. It is small and merges cleanly into `origin/main`, so merge it before commit 3, or take its commit into the branch.

**Where each commit starts.**

| Commit | Start at |
|---|---|
| 0 | `PipelineBinding.__init__` (key table), `keys`, `_by_key`, `make_stage` (blanks) in `essspec/pipeline.py`; `toy_sans.py:313,327` and `pipeline_test.py` read `keys[...]` |
| 1 | `checks.py` (the `DatasetRef` branch under `by_field`, about line 234), `batch.py` (`apply`, `try_apply`, `dataset_blank`), `rules.py` (`Rules._requests`, `record.request.datasets()`) |
| 2 | `refs.py` (`places`, `iter_refs`, `map_refs`), `field_types.py` (`validated`, `by_field`, `inline`, `model_of`), `checks.py` (`_one`, `_group`, `_ref`, `_inline`), `_workflow_spec.py` (`_group_problems`, `_forms`, `_group_forms`, `_params_schema`, `takes_inline`) |
| 3 | `_workflow_spec.py` (`_params_problems`, `_one_or_list`, `is_single` in `field_types.py`, `_forms` for `Annotated` unions), `field_types.fits`, `pipeline.py` (union keys by tag), #75's error locations in `refs.py` (`Loc`) |

**Where the backend holds a request.** The record holds each value in its JSON form, with references as `@ref` objects. The backend reaches references only through `iter_refs` and `map_refs` (`backend.py:305, 820, 1284`), and `records.py` does the same (`Request.datasets()` and its siblings), so a walk of every depth in `refs.py` reaches them unchanged. `places` is called directly only in `checks.py:198,388` and `field_types.inline`. Requests are compared by these JSON forms, so a reference at depth compares as it does at the top.

**Probes to start tests from.**
- `probes-bindings/p1`: the beam centre with a held default. The resulting test now pins the docstring example, not a refusal.
- `probes-requests/p1` and `p2`: unions, tags, error locations.
- `probes-model/p1`: subclass and `NewType` alternatives.
- The call-phase validation probe is in "Validation" above.

All of these are in `.scratch/unions-and-references/`.

**Docs.**
- The README sections on requests, references, specs and bindings, and on `PipelineBinding`.
- `automatic-reduction.md` (the blank a dataset fills).
- `workflow-authors.ipynb`: section 1, the rules of the spec, including #72's union bullet; section 2, the routes to keys and the `| None` guidance.

Describe the design as it is, with no history. This file stays out of the PR, since the docstrings and the README then hold the design.
