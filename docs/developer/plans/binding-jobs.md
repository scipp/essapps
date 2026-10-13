# What a binding does: mapping, strategy, protocol

**Status: design note, open. Nothing here is decided.**
It amends the binding part of the design: [README, What a binding provides](../README.md#what-a-binding-provides), [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec), and [ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md).
Questions for Simon are at the end, each with a `> Simon:` line.

## In short

A *binding* is the code behind a spec. Today one object does three jobs:

1. **Mapping**: which workflow input each field of the params model sets, which workflow result each output is, and which results add up over the rows of a table.
2. **Strategy**: how a plain request, a stage, and a held state are computed: by calling a function, through `sciline.Stage`, by summing rows.
3. **Protocol**: what the backend passes to the binding and gets back. Today that is dicts by name, with data in place of references.

Only the mapping is specific to a workflow.
The strategy is generic per kind of workflow (a function, a sciline pipeline), and the protocol is the backend's interface.
This note separates the three, lists what the mix costs today, and proposes amendments.
Some settle a cost outright.
Three questions stay open: whether the protocol passes model instances, how data fields reach the workflow, and whether a mapping is a table or code.

## The example

The toy SANS reduction of [roles/toy_sans.py](../roles/toy_sans.py): a sciline pipeline of one sample run and one can run, a single-run spec, and a multi-run spec.

```python
pipeline = sciline.Pipeline([load, numerator, denominator, iofq])

class Run(BaseModel):          # a group of IOFQ, and a row of IOFQ_MULTI
    file: NexusFile            # a data field

class Reduction(BaseModel):
    bins: int = 2

class IofQParams(BaseModel):   # IOFQ
    sample: Run
    can: Run
    reduction: Reduction = Field(default_factory=Reduction)

class MultiIofQParams(BaseModel):   # IOFQ_MULTI
    sample_runs: list[Run] = Field(min_length=1)
    can_runs: list[Run] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)

class IofQOutputs(BaseModel):  # both specs
    iofq: Array(ArraySpec(dims=('Q',), unit='dimensionless'))
```

## Today: one object, three jobs

`PipelineBinding.stage` (`ess/spec/pipeline.py`), with the job each line does:

```python
def stage(self, fixed, blanks):
    blank_keys = self._keys.of(blanks)                 # 1: the keys that the blanks set
    pipeline = self._keys.set(self._pipeline, fixed)   # 1: the fixed values onto their keys
    cut = _Cut(pipeline, outputs, blank_keys)          # 2: sciline.Stage, cut at the blanks
    def call(**values):                                # 3: the blanks by name, as dicts
        results = cut(self._keys.by_key(values))       # 1: the blank values onto their keys
        return {name: results[key] for name, key in self._outputs.items()}  # 1 and 3: a dict
    return call
```

The backend's side of the protocol (`ess/dispatch/backend.py`, `_call`, `_typed`, `_read`, `_compute`, `_unfit`):

```python
values = _values(IofQParams.model_validate(params))   # validated, then dumped to dicts
call = binding.stage(read(fixed), blanks)             # once per stage, at its first request;
                                                      # read: data in place of references
outputs = dict(call(**read(blank_values)))            # once per request
# the names of `outputs` are checked against IofQOutputs; no IofQOutputs is ever made
```

Every kind of binding mixes the jobs differently:

| Binding | 1 Mapping | 2 Strategy | 3 Protocol |
|---|---|---|---|
| `PipelineBinding` (`ess.spec.pipeline`) | `params`: paths to keys; `outputs`: names to keys | plain request: one `sciline.Stage`; stage: cut at the keys of the blanks; held state: none, so the backend keeps the rows | dicts by name |
| `AccumulatingPipelineBinding` (`ess.spec.pipeline`) | the same, with `'sample_runs.file'` for each row's field, plus `accumulate`: per table, the keys summed over its rows | one cut per table up to the accumulated keys, sums, one cut from the sums to the outputs; the same for plain requests, stages, and held states | dicts by name |
| a function, wrapped by `FunctionBinding` (`ess.dispatch.bindings`) | the function's signature | each call computes everything; held state: the backend keeps the rows (`open_held_state`, `ess.dispatch.bindings`) | dicts by name |
| `combine(op)` (`ess.spec.combine`) | the fields of a row are the outputs | fold the rows with `op` | dicts by name |
| a held state written by hand | inside the author's code | written by the author | dicts by name |

## What the mix costs

1. **A function's author writes against the protocol, not against the models.**
   The function takes groups as dicts and returns a dict: `def iofq(sample: dict, can: dict, reduction: dict) -> dict`.
   The backend held a validated `IofQParams` and dumped it (`backend.py`, `_values` and `_typed`).
   The models the author wrote appear nowhere in the function.
2. **Nothing pairs a binding with its spec until a request runs.**
   `bind={IOFQ: binding}` is the only link, and `PipelineBinding` never sees the spec.
   A path that names no field fails at the first request (`no sciline key for the parameters`).
   An output the binding does not return fails when the request completes (`_unfit`).
3. **Two sciline classes for one strategy.**
   Their constructors differ in the mapping only: `'sample.file'` against `'sample_runs.file'`, and `accumulate`.
   Which strategy applies follows from whether `accumulate` is given.
4. **Strategies live in two packages.**
   The sciline strategies are in `ess.spec.pipeline`; the function and keep-the-rows strategies are in `ess.dispatch.bindings`.
   `ess.spec.testing.check_arrival_and_order` takes only a binding with `held_state`, so the keep-the-rows strategy is tested only in essdispatch.
5. **"Kept by the binding's author" covers framework code.**
   The README's Symmetries table says the binding's author keeps caching, arrival, and order.
   For a sciline package, the strategy keeps them (`sciline.Stage`, the sums).
   The author promises less: the workflow is pure, the accumulated results add up over runs, and the mappings of the single-run and multi-run spec describe the same workflow.
   So a package's `check_caching` mostly tests `ess.spec.pipeline`.
6. **The protocol fixes the form of data.**
   `DatasetSource.read` returns "the dataset's content, in the form workflows take": one form per dataset source.
   ADR 0001 says the workflow asks for the form it wants, but no part of a binding can say which.
   A sciline loader wants a path (`Filename[SampleRun]`); a workflow that reads another record's output wants the array.
7. **There is no place for conversions.**
   A group such as `QEdges` (start, stop, number of bins) must become a bin-edge array for sciline.
   ADR 0001 has the binding validate the dict again, `QEdges.model_validate(values['q_edges'])`.
   With `PipelineBinding`, that is a provider in the workflow package that takes a dict, so the workflow package knows the protocol's form.
8. **A table of paths to keys cannot say everything a mapping must say.**
   esslivedata's factories (see Prior art) also set values that users never see, such as `CorrectForGravity(False)`, and change the pipeline and its outputs depending on an enum field: one transmission mode inserts a provider and adds the output `transmission_fraction`.
   `PipelineBinding` has no place for either; such values go into the pipeline the workflow package hands over.

## The three jobs

### 1. Mapping

The mapping says, for one spec and one workflow, which input each field sets, which result each output is, and which results add up over the rows of each table.
It is specific to the workflow.
The workflow author writes it, or the instrument's glue does ([ADR 0007](../adr/0007-packages-split-by-dependencies.md) places facility defaults and the choice of settings outside the workflow package).
For a sciline pipeline, with amendment B below:

```python
single = PipelineBinding(IOFQ, pipeline,
                         params={'sample.file': RunData[SampleRun], 'can.file': RunData[CanRun],
                                 'reduction.bins': Bins},
                         outputs={'iofq': IofQ})
multi = PipelineBinding(IOFQ_MULTI, pipeline,
                        params={'sample_runs.file': RunData[SampleRun], 'can_runs.file': RunData[CanRun],
                                'reduction.bins': Bins},
                        outputs={'iofq': IofQ},
                        accumulate={'sample_runs': (Numerator[SampleRun], Denominator[SampleRun]),
                                    'can_runs': (Numerator[CanRun], Denominator[CanRun])})
```

For a function, the mapping is its signature, whose form depends on the protocol (amendment D).

The author of a mapping promises:

- the workflow is pure: it modifies no input, and returns the same results for the same inputs;
- the accumulated results add up over runs, such as a numerator and a denominator, not a normalized curve;
- the mappings of the single-run and multi-run spec describe the same workflow (the one-row symmetry).

### 2. Strategy

A strategy computes a plain request, a stage, and a held state for one kind of workflow, given its mapping.
It is generic, so it is written and tested once.
The kinds today:

| Kind | Plain request | Stage | Held state |
|---|---|---|---|
| function | call it | call it, with every value, at each call | keep the rows; compute the plain request for each state read |
| sciline pipeline | one `sciline.Stage` | cut at the keys of the blanks | cut at the accumulated keys, sum, compute the outputs from the sums |
| fold (`combine`) | fold the rows | the same | fold each push |

A package may write its own strategy, as a held state written by hand.
Spectroscopy needs one that bins each run's events into the held grid, instead of adding a histogram per run.

A strategy keeps caching, arrival, and order, given the promises of its mapping:

| Symmetry | Kept by the strategy | Given the mapping's promise |
|---|---|---|
| caching | the cut at the blanks | the workflow is pure |
| arrival, order | the sums, or keeping the rows | the accumulated results add up over runs and depend on one table only (the sciline strategy checks the second when it is made) |
| one-row | nothing | the single-run and multi-run mappings describe the same workflow |

### 3. Protocol

The protocol is what the backend calls ([ess.spec.binding](../../../packages/essspec/src/ess/spec/binding.py)):

```python
call = binding.stage(fixed, blanks)   # once per plain request, or per stage at its first request
call(**blank_values)                  # once per request
held = binding.held_state(fixed)      # once per accumulator
held.push(rows)                       # once per push, in order, one at a time
held.outputs()                        # once per state that is read
```

It carries the backend's promises to the binding: every value with defaults filled in, data read, a push never while the outputs of the state before it are in use.
Today it passes groups and rows as dicts, with data in place of each reference, and takes the outputs as a dict.
The client never sees it, and neither does the author of a sciline workflow.
The author of a function sees it, through the function's signature.

## Amendments

They are independent of each other.
A to C settle costs now; D to G are open.

### A. Name the jobs in the docs

The README's "What a binding provides", ADR 0006, and [roles/workflow-authors.ipynb](../roles/workflow-authors.ipynb) explain a binding as a mapping and a strategy, called through the protocol.
The Symmetries table splits "Kept by" into the strategy and the mapping's promise, as in the table above.
Settles cost 5.

### B. One sciline class, which takes its spec

`PipelineBinding(spec, pipeline, params=..., outputs=..., accumulate=None)` replaces both sciline classes, as in the mapping example above.
When it is made, it checks that each path names a parameter or a field of a group or row, that each output name is an output of the spec, and that `accumulate` names tables only.
With `accumulate`, the sciline strategy sums rows; without it, a table is set on its key as a whole, a list of rows, and an accumulator keeps the rows.

A binding that knows its spec makes the `bind` mapping redundant:

```python
local(proposal='p1', datasets=datasets, bind=[single, multi, function_binding(FORWARD, forward)])
```

and the entry point of [ADR 0007](../adr/0007-packages-split-by-dependencies.md) returns a list of bindings.
Settles costs 2 and 3.

### C. All strategies in `ess.spec`

`FunctionBinding` and the keep-the-rows held state move from `ess.dispatch.bindings` to `ess.spec`.
`ess.spec.testing` then checks every strategy, and a package tests any binding without `ess.dispatch`.
Settles cost 4.

### D. The protocol passes model instances (open, needs E)

```python
def iofq(params: IofQParams) -> IofQOutputs: ...      # a function binding

call = binding.stage({'sample': Run(...), 'can': Run(...)}, blanks=('reduction',))
call(IofQParams(sample=..., can=..., reduction=Reduction(bins=4)))   # -> IofQOutputs

held = binding.held_state({'reduction': Reduction(bins=2)})
held.push({'sample_runs': Run(...)})                  # the Run the client pushed
held.outputs()                                        # -> IofQOutputs
```

`stage` gets the fixed groups by name, and a blank is absent, so a strategy that reads a blank in the fixed part fails at once.
The callable gets the full plain request, which the backend has already validated.
So `stage` returns what a function binding is, `Callable[[IofQParams], IofQOutputs]`, and a function binding is a binding staged with nothing fixed.
`held_state` gets the fixed groups by name too: a state may be an invalid request until it is read, such as one with sample rows and no can row yet, so there is no valid `MultiIofQParams` to pass.

With `WorkflowSpec` generic in its outputs model as well as its params model, a type checker checks `function_binding(IOFQ, iofq)`.
The output names are checked where the author writes them, `IofQOutputs(iofq=...)`, not in the backend.
Settles cost 1, for function bindings and held states written by hand.

Alternatives considered:

- **Defaults for every parameter that can be a blank**, so that `stage` gets `IofQParams(**fixed)`.
  Required parameters, such as `sample`, become optional: a request without them is no longer refused, and a form no longer marks them required.
  A blank holds a value that is never used, so a strategy that reads it computes a wrong result without an error.
  The params model's own validators run on these values.
- **Staging from the full instance of the first call.**
  `stage` gets a valid instance, but its blank fields hold the first call's values, which the fixed part must not use; `check_caching` catches a strategy that does.
  It does not extend to held states, for the reason above.
- **Dicts, as today.**

### E. How data fields reach the workflow (open)

A data field is declared as a reference (`NexusFile = Annotated[Ref, DataField(...)]`), and the workflow needs the data.
Today the backend replaces each reference by the data in the dicts it passes, which only works because dicts carry no types.
Outputs are the harder case: nearly every output is a data field.

Options:

- **E1. Dicts, data in place of references.** Today. Rules out D.
- **E2. A handle type.** `NexusFile` and `Array` are classes that hold a reference, and inside the backend also a way to the data: a path, or the value. A request and a call use the same model class. The mapping says which form the workflow wants (cost 6).
- **E3. Two forms of each model**, generic over the type of its data fields: `Run[Ref]` in requests, `Run[Path]` in calls. Every model with a data field gets a type parameter.
- **E4. `model_construct` with the data in fields typed as references.** No copy, but the static types are wrong, and dumping such an instance fails.
- **E5. Data apart from the params instance**, as esslivedata passes streams apart from its params. The workflow gets `IofQParams` without its data fields, and the data by field path. A group such as `Run` is then split in two for the workflow, and the mapping joins the halves.

What a solution must give:

- one model class per group, for requests and for calls;
- static types that hold where the author reads them;
- the form the workflow takes chosen by the mapping, not by the dataset source;
- no copy of a large array, in inputs or in outputs;
- references in records, so that history and provenance stay small.

### F. Conversions in the mapping (open)

A conversion turns a field's value into what the workflow takes, such as a `QEdges` group into a bin-edge array.
It could be part of the mapping:

```python
params={'reduction.q_edges': (QBins, edges_to_variable), ...}
```

or stay in the workflow, as a provider that takes the group.
In the mapping, the workflow package needs no provider that knows the protocol's form (cost 7); in the workflow, the mapping stays a table of names.
esslivedata puts the conversion on the group's model, `QEdges.get_edges()`, and its factory calls it; `ess.spec.conversions.edges_to_variable` does the same for `EdgesModel`.
With model instances in the protocol (D), a mapping could name such a method: `'reduction.q_edges': (QBins, QEdges.get_edges)`.

### G. A table or code (open)

A mapping can be a table, as in B, or code that configures the pipeline from the params, as esslivedata's factories are:

```python
def configure(pipeline: sciline.Pipeline, params: SansWorkflowParams) -> sciline.Pipeline:
    pipeline = pipeline.copy()
    pipeline[Bins] = params.reduction.bins
    pipeline[CorrectForGravity] = CorrectForGravity(False)   # a value users never see
    if params.transmission.mode is TransmissionMode.constant:
        pipeline[TransmissionFraction[SampleRun]] = sc.scalar(1.0)
    else:
        pipeline.insert(transmission_from_monitor)          # the graph depends on a field
    return pipeline
```

Code can say everything (cost 8).
A table tells the strategy which keys each parameter sets, and the sciline strategy needs exactly that: a stage cuts at the keys of the blanks, and an accumulator checks that each sum depends on the rows of one table.
From code alone, the strategy cannot know which keys a blank touches, so it could not cut a stage, or check that an accumulator's sums each depend on one table.

A middle way gives each parameter a function and the keys it may set or insert:

```python
params={'reduction.bins': Bins,                                          # a key, as today
        'transmission': (configure_transmission, {TransmissionFraction[SampleRun]})}
```

The strategy then cuts at the declared keys.
Values that users never see belong in the pipeline that the workflow package hands over, as today.
An output that exists only for some values of a field, such as `transmission_fraction`, is an optional output of the spec.

## Prior art: esslivedata

esslivedata solves the same problem for live data.
Each instrument is a package with `specs.py`, the params and outputs models, which the dashboard imports, and `factories.py`, the sciline glue, which only the backend loads.
In terms of the three jobs:

| Job | esslivedata |
|---|---|
| 1 mapping | code: a factory per spec sets each sciline key from the params by hand (`wf[QBins] = params.q_edges.get_edges()`), sets values users never see (`wf[CorrectForGravity] = CorrectForGravity(False)`), and changes the graph and the outputs by an enum field (`config/instruments/loki/factories.py`) |
| 2 strategy | chosen by the same factory: it returns a `StreamProcessorWorkflow`, given dynamic keys, accumulators, and target keys; other strategies are other implementations of its `Workflow` protocol |
| 3 protocol | `factory(source_name=..., params=SansWorkflowParams(...), aux_source_names=...)`, then `accumulate(data)` and `finalize() -> dict` on the workflow it returns |

What carries over:

- **The factory gets the params model instance.** The backend validates the wire dict into the model and passes it; `attach_factory` checks the factory's `params` type hint against the spec when it is registered. This is amendment D, in use.
- **It works there because data are not params.** A workflow's data arrive as streams, named by source; no field of the params model holds a reference. In essapps, runs are fields of the params model, which is why E is open.
- **The mapping is code** (cost 8, amendment G), and the factory mixes it with the choice of strategy.
- **Outputs are matched by name, unchecked.** The factory's target keys map output names to keys; nothing checks the names against the outputs model, and no instance of the outputs model is made. The same holds in essapps today (cost 2).

## Where the jobs live

| Job | Written by | Lives in | Tested by |
|---|---|---|---|
| 1 mapping | the workflow author, or the instrument's glue | the workflow package, or `essdispatchinstruments` (ADR 0007) | `check_one_row`, the checks of the strategy when the binding is made |
| 2 strategy | the framework; a package for a held state written by hand | `ess.spec` | `ess.spec`'s tests, once |
| 3 protocol | the framework | declared in `ess.spec.binding`, called by `ess.dispatch.backend` | essdispatch's tests |

A mapping as a table is not tied to this framework's strategies.
`StreamProcessor`, through which esslivedata accumulates, takes the same description: dynamic keys (the fields of a row), accumulated keys, and target keys (the outputs).
One table could serve both frameworks; a mapping written as code, as esslivedata's are, serves only the strategy it builds.

## What does not change

The client API, records, the symmetries, and the backend's promises to a binding: when it calls which method, from how many threads, and that a push waits for the readers of the state before it.

## Questions

1. Are mapping, strategy, and protocol the right names? "Binding" would stay the name of the whole: a mapping and a strategy, called through the protocol.

> Simon:

2. B: should a binding take its spec, so that `bind` becomes a list?

> Simon:

3. B: one sciline class with an optional `accumulate`, or keep two classes?

> Simon:

4. C: move the function and keep-the-rows strategies to `ess.spec`?

> Simon:

5. D: given a solution to E, should the protocol pass model instances?

> Simon:

6. E: which option to try first? I would try E2, the handle, on the toy SANS example: a `NexusFile` that gives a path, and an `Array` that gives the value.

> Simon:

7. F: conversions in the mapping, or in the workflow?

> Simon:

8. Who writes the mapping for an instrument: the workflow package, or the instrument's glue in `essdispatchinstruments`?

> Simon:

9. G: a table, code, or a function per parameter with the keys it declares? Do you know of a real reduction, other than esslivedata's transmission mode, whose graph depends on a field?

> Simon:
