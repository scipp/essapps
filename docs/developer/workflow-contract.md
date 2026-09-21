# The contract between the framework and workflow code

This document defines how the framework calls scientific code.
It covers how a spec is bound to an implementation, how input data reaches the code, how outputs come back, what a workflow may offer to make reruns cheap, and the extensions the design needs on top of the workflow spec of scipp/ess#690.
[architecture.md](architecture.md) places this in the whole design.

## A workflow end to end

A spec and the callable behind it, from `ess.apps.examples`:

```python
class LoadParams(BaseModel):
    run: OpaqueFile                                      # holds a reference
    scale: float = 1.0

class LoadOutputs(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))
    total: Quantity

LOAD = WorkflowSpec(name='load', version=1, params=LoadParams, outputs=LoadOutputs)

def load_workflow() -> Workflow:
    def run(params: LoadParams, inputs: Inputs) -> dict[str, Any]:
        data = sc.io.load_hdf5(inputs.path(params.run)) * params.scale
        return {'data': data,
                'total': Quantity(value=float(data.sum().value), unit=str(data.unit))}
    return run
```

`LOAD` declares the interface: a name, a version, a parameter model, an output model.
`load_workflow` is the **factory**, and what it returns is the **workflow**, a callable from the validated parameter model and the run's `Inputs` to the outputs by field name.
The `run` field holds a reference in the request, in the record, and in the callable alike, and the callable asks `inputs` for the bytes in the form it wants.

## Spec and binding

**A spec is bound to an implementation through Python entry points.**
An installed package provides specs through the group `ess.apps.specs` and factories through `ess.apps.workflows` under the same entry-point name.
The backend loads every installed spec without importing workflow code, because it validates requests in a process that need not hold the scientific packages.
Only a runner asks the `Registry` for a `Binding`, which imports the factory.

**A notebook may also bind a spec in process**, through `Registry.bind`, unless an installed package already provides that name and version.
The record says which binding was used, so publication can tell a reproducible record from a development one.
See `binding.py`.

## The callable

**A spec is the signature of one callable, and a record is one call of it.**
No request runs part of a spec, and no spec stands for several callables.
A workflow that is run in parts is published as one spec per part, which is why an aggregation over runs is two specs ([aggregation.md](aggregation.md)).

**The callable is stateless.**
No call affects a later one.
A throwaway runner makes it from its factory, calls it once, and exits.
A session runner makes it once per spec version.
What a session keeps between runs it keeps itself, and the workflow does not.
A session holds the code it imported, so a change to workflow code takes effect in a new session, never in a running one.

**The framework never imports sciline.**
The adapter that turns a sciline pipeline into a callable belongs with the workflow packages, in ess.reduce.

## Inputs: a path or an object

`Inputs` is how the callable gets at the bytes a reference names:

```python
class Inputs(Protocol):
    def path(self, ref: Ref) -> Path: ...    # a local file holding the bytes
    def array(self, ref: Ref) -> Any: ...    # the scipp object a scipp reference names
```

**The callable chooses the form of each input, not the spec.**
A raw NeXus file is asked for as a path, because loading NeXus is workflow-specific: which detector banks, which monitors, and no single loaded object exists.
An opaque file, CIF or ORSO, is asked for as a path, because the framework cannot read it.
A processed array is asked for as an object, or as a path when the workflow has its own loader.

**Where the bytes come from is the runner's business.**
A session serves an object from its memory when it holds a copy and from scipp HDF5 otherwise, a throwaway runner from the work directory the backend filled at dispatch.
Neither the spec nor the callable can tell the two apart, which is what lets a chain of runs stay in memory in a session without any change to workflow code.
The spec says only what the bytes are, their **format**, so that the backend can check a reference against the field it fills and the picker can list candidates.

## Outputs

**The callable returns objects by field name and never writes files.**
An output field may be absent when the workflow's mode does not produce it.
The runner validates and stores what comes back: literal outputs through `literal_model(spec.outputs)`, kept inline in the record, and array outputs against the `ArraySpec` their field declares, then handed to the data store.
The data store serializes scipp objects to scipp HDF5.
Other types, such as CIF, ORSO, or NeXus products, must come with their own serializer, declared with the output, because the outputs that get published are often not scipp objects.

Chunk-wise processing of one large file, as the NMX workflow does, happens inside the callable and is invisible to the framework.
That the NMX product is then held in memory before it is written is a cost accepted here.

## Offering a stage

**A workflow may offer a stage, a callable over a subset of its parameters that holds what those parameters cannot affect.**
That is what makes an interactive rerun cheap when one parameter moves.
The offer is the `StagedWorkflow` protocol in `binding.py`, abridged:

```python
class StagedWorkflow(Protocol):
    default_stage_inputs: Collection[str]

    def __call__(self, params: BaseModel, inputs: Inputs) -> Mapping[str, Any]: ...

    def stage(self, params: BaseModel, stage_inputs: AbstractSet[str],
              inputs: Inputs) -> Workflow: ...
```

The **stage inputs** are a set of parameter field names.
`stage` returns a callable with the workflow's own signature, valid for every request that equals `params` in all fields outside the stage inputs:

```text
wf.stage(p0, s, inputs)(p, inputs) == wf(p, inputs)    for every p equal to p0 outside s
```

What a stage holds is therefore a cache, and dropping one is always safe.
`default_stage_inputs` names the fields to feed when nothing better is known, and a plain function offers no stage.

The workflow stays stateless.
Only a session decides whether to ask for a stage, and it holds the ones it asked for.
Which parameters are the stage inputs is therefore the session's choice, not part of the spec and not the author's.
[stages.md](stages.md) describes how a session uses the offer.

## The sciline adapter

**An adapter turns a sciline pipeline into a workflow.**
`PipelineAdapter` in `adapter.py` does it, and `examples.py` binds the `HISTOGRAM` spec with it:

```python
def histogram_workflow() -> PipelineAdapter:
    pipeline = sciline.Pipeline([filter_data, histogram])
    return PipelineAdapter(
        pipeline,
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        resolve={'data': 'array'},
        targets={'histogram': Histogram},
        default_stage_inputs=['bins'],
    )
```

`keys` maps each parameter field to the sciline key it sets, `resolve` names the form in which each data reference is asked for, and `targets` maps each output field to the key to compute.
On a call it copies the pipeline, sets the parameters, and computes the targets.

For `stage` it sets the fields outside the stage inputs on a copy of the pipeline and builds a `sciline.Stage` (scipp/sciline#245) from the keys of the stage inputs to the targets.
A `Stage` computes once everything the inputs cannot affect, holds it at its **frontier**, and on each call recomputes only what lies downstream of the inputs.
Correctness follows from the graph for any choice of stage inputs, so the choice decides only where the frontier sits and what a rerun costs.
A stage input that the targets do not need, which `sciline.Stage` refuses, is held and ignored, because it cannot change the result and a caching choice must not decide whether a run succeeds.

This matches how notebooks already work: Q bins, d-spacing bins, cut axes and a beam centre enter after the expensive load and coordinate conversion.
A `Stage` is one implementation of a spec's callable, and a sciline `Aggregation` is a composition of two such callables, which is why an aggregation over runs is two specs.

## Changes to the spec of scipp/ess#690

The spec of scipp/ess#690 is assumed merged as is, with the extensions below.
They are small additions to the vocabulary, apart from the output side, whose shape they change.

**Inputs are parameters of data-reference type.**
The spec has one parameter model and no separate input section.
The parameter vocabulary gains a **data reference** type: a field whose value is a reference, annotated with the **format** of the bytes it names (`NexusFile`, `OpaqueFile`, or `Array` for scipp data) and, for scipp data, with the same `ArraySpec` that outputs declare.
A parameter of this type is what this design calls an **input**.
A field may be a union of a literal and a reference, for a value such as a beam centre that a user may type in or take from a previous run.
The format of a dataset reference is not checked at submission, so a dataset that is not what the field declares fails when the workflow reads it.
Every difference between an input and a parameter is behaviour selected by the field's type: resolution of run numbers and PIDs, provenance edges, validation timing, and which widget a UI shows.
esslivedata separates the two because its inputs are streams routed at runtime, whereas here every input is known at submission.

A UI choosing a value for such a field asks the **picker**, `Client.pick` in `client.py`, which returns candidates of matching format as rows of one shape: a reference, its format, and display fields.
They come from the record store and from every dataset source the client has.
Nothing is stored to make that list, and a further place to pick from is another dataset source, not a change to the picker.

**Collections on both sides.**
The vocabulary gains a **collection**: a list or a dict of values of one declared type, usable as a parameter and as an output.
A reference may name a whole output or one element of it by key (`OutputRef.key`).
This gives every mapping between outputs and inputs with one mechanism:

| Mapping | Form |
|---|---|
| many-to-one | a collection-typed parameter of references, such as the contributions a combine spec takes |
| one-to-many | a collection output, per detector bank or per angle, consumed whole or element by element |
| many-to-many | a group whose members each reference one element of a pending output by key |

Keys are declared on the spec where the author can name them, such as bank names, and free otherwise.
Elements of a collection output are stored and served individually, so reading one bank does not load the rest.
No current workflow needs fan-out whose keys are known only after reading the data: Bifrost groups by rotation inside its pipeline, and imaging has no tomography grouping.
If such a case arises it is a rule on the completed producer, one template instantiation per key, and not a scheduler feature.
Snakemake put fan-out in the scheduler, as checkpoints, and it became the most confusing part of the tool.

**Outputs are a typed model in the same vocabulary.**
A spec declares its outputs as a model class mirroring the parameters, with a JSON Schema in the serialized form.
An array output is a data-reference field constrained by `ArraySpec`, which gains a `binned` flag.
A beam centre is a vector with unit, a fit result a float with unit, a CIF file a reference of opaque format.
Output fields may be optional, and title and description are field metadata.
A downstream parameter may take a reference to any output field whose type matches, so chaining is a type check between two fields of one vocabulary.
The spec as proposed gives non-array outputs no type, which breaks "outputs can be inputs" for the values, such as beam centres and direct beams, that most often feed the next workflow.
Storage placement, inline or in the data store, stops being a spec concept.

**A code revision and declared failure reasons.**
A spec may carry a **code revision**, a git commit or a package version, so that a record made from a development branch is honest about what ran.
It is read by the framework and by UIs and means nothing to a throwaway run.
A spec may also declare named failure reasons, each with a message.
A workflow that fails for a declared reason returns it, the record carries its name, a UI can explain it, and a rule's retry policy can match it.

**A chain declaration.**
A spec may declare `chain`, a mapping from a collection parameter of data references to one of the spec's own outputs:

```python
COMBINE = WorkflowSpec(name='normalize-combine', version=1,
                       params=CombineParams, outputs=CombineOutputs,
                       chain={'contributions': 'contribution'})
```

That output may then be passed as an element of the parameter, where it stands for everything it was combined from, which is what lets a series be chained.
`WorkflowSpec` in `spec.py` validates the shapes at the two ends: the parameter is a collection of data references, the output exists, and their formats match.
That the combination does not depend on grouping and order is the author's promise, which no spec can check and a test helper does.
This is the only declaration an aggregation needs, and it is an extension of this design rather than a field of scipp/ess#690.
A declaration belongs on a spec only if it has a reader that cannot import workflow code, and `chain` has two, `apply` and the trigger loop.
[aggregation.md](aggregation.md) covers the rest.

## Validation

**Shape**: JSON Schema, in the backend, always.

**Parameters**: the pydantic parameter model, which catches cross-field rules the schema cannot express.
A parameter the spec does not declare is refused, because a pydantic model ignores unknown fields unless its author forbids them, and a reduction parameter dropped in silence gives a wrong number without an error.
scipp/ess#690 forbids extra fields only on its empty model, so the backend checks the top-level fields itself.
Requiring a closed parameter model in the spec would be the better place.
The backend runs this layer by importing the spec module alone, never a factory.
esslivedata keeps the spec separate from the workflow factory precisely so that specs can be validated without importing workflow code, and scipp/ess#690 must keep that separation.

**Runnability**: every reference resolves to a record the submitter may read and, for a collection element, to a key the producer declares.
Files exist where the launcher would look, and the launcher's environment has the spec.
Anything past that, such as a file that opens but lacks a monitor, is a run that fails fast, not a validation error.

The runner remains authoritative for parameters (ADR 0001 in scipp/ess#690), so a disagreement with the backend's check is a deployment bug.
The backend refuses a request whose spec module it cannot import, unless the request opts out of the backend's parameter check.

## Test helpers

`testing.py` holds the checks a workflow package runs.

`assert_stage_equals_workflow` drives a workflow through a sequence of parameter sets, once through the session's own stage store and once through fresh calls of the stateless callable, and asserts equal outputs.
That is the check on the contract of `stage`, and every workflow that offers one runs it.

`assert_combine_is_associative` checks the promise behind `chain`.
It combines the contributions of several members in one group, in two groups, one at a time, and in reverse order, and compares each result with the first.
Every spec that declares a chain runs it.

A third check, recomputing a completed record and comparing the outputs, lets a workflow package keep records from production as regression tests.

## Alternatives considered

**The framework knows sciline.**
It would build the pipeline, compute the requested nodes, and cache intermediates itself.
That ties the framework to one engine, and caching every intermediate is not affordable with event data while the graph does not know compute cost.

**A file-based contract**, where the workflow reads input files and writes output files.
This is simple and remote-friendly, but it forces every chain through disk, which defeats interactive work in a session.

**Two protocols**, one for one-shot execution and one for incremental reruns.
That gives two execution paths to test and keep consistent.
With one callable the only difference between runners is whether the callable is kept, and a combine request is a call of its own spec's callable like any other.

**A data field typed as a union of the reference and the materialized value**, a path or a scipp object, with the runner materializing it according to a kind declared on the spec.
That makes the parameter model wrong in both phases, before and after materialization, and needs a validator that accepts anything not plain data.
It also puts a materialization instruction into an interface meant to be pure, and the choice between a path and an object presumes an execution location.

**Stage inputs declared by the workflow author.**
What a choice costs depends on which parameter a person is changing, which only the session sees.
A `Stage` recomputes everything downstream of all its inputs, so a stage whose inputs are the run and the Q bins loads the run again when the Q bins change.

## Costs

- Two validation points exist, the backend's and the runner's, and the runner's is the authoritative one.
- The runner loads scipp arrays whole.
- Reuse through a stage is exact only if providers are pure.
  `assert_stage_equals_workflow` is the check, and the record's `reused` flag lets publication insist on a result computed without a held stage.
- A sum over a growing list of runs is not a case for a stage but an aggregation.
- DREAM and imaging masks are Python callables today.
  Each such workflow needs a range vocabulary and a conversion before its requests are plain data.
- The backend must walk the request's values to find references, and the spec's JSON Schema, including nested models, to check them.
- An array output is checked against its `ArraySpec` in the runner at completion, because pydantic cannot check a scipp object.
