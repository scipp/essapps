# The contract between the framework and workflow code

This document defines how the framework calls scientific code.
It covers how a spec is bound to an implementation, how a workflow builds the stage a run request names, how input data reaches the code, how outputs come back, and the extensions the design needs on top of the workflow spec of scipp/ess#690.
[architecture.md](architecture.md) places this in the whole design.

## A workflow end to end

A spec and the plain function behind it, from `ess.apps.examples`:

```python
class LoadParams(BaseModel):
    run: OpaqueFile                                      # holds a reference
    scale: float = 1.0

class LoadOutputs(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))
    total: Quantity

LOAD = WorkflowSpec(name='load', version=1, params=LoadParams, outputs=LoadOutputs)

def load_workflow() -> Any:
    def run(params: LoadParams, inputs: Inputs) -> dict[str, Any]:
        data = sc.io.load_hdf5(inputs.path(params.run)) * params.scale
        return {'data': data,
                'total': Quantity(value=float(data.sum().value), unit=str(data.unit))}
    return run
```

`LOAD` declares the interface: a name, a version, a parameter model, an output model.
`load_workflow` is the **factory**, and what it returns is the **workflow**.
A plain function from the validated parameter model and the run's `Inputs` to the outputs by field name is a workflow; the framework wraps it in `FunctionWorkflow`.
The `run` field holds a reference in the request, in the record, and in the code alike, and the code asks `inputs` for the bytes in the form it wants.

## Spec and binding

**A spec is bound to an implementation through Python entry points.**
An installed package provides specs through the group `ess.apps.specs` and factories through `ess.apps.workflows` under the same entry-point name.
The backend loads every installed spec without importing workflow code, because it validates requests in a process that need not hold the scientific packages.
Only a runner asks the `Registry` for a `Binding`, which imports the factory.

**A notebook may also bind a spec in process**, through `Registry.bind`, unless an installed package already provides that name and version.
The record says which binding was used, so publication can tell a reproducible record from a development one.
See `binding.py`.

## The workflow protocol

**A spec is the signature of a pipeline, and a run record is one call of a stage cut from it.**
The workflow is the code behind the spec, and it builds the stage a run request names.
The protocol is `Workflow` in `binding.py`:

```python
class Workflow(Protocol):
    def stage(self, params: BaseModel, inputs: Collection[str], outputs: Collection[str],
              data: Inputs) -> StageCall: ...
    def accumulator(self, name: str) -> Accumulator: ...

class StageCall(Protocol):
    def __call__(self, params: BaseModel, intermediates: Mapping[str, Any],
                 inputs: Inputs) -> Mapping[str, Any]: ...
```

`stage` gets the request's parameters that are not in `vary`, with the spec's defaults filled at submit, and the stage's input and output names.
The input names are the parameters the request names in `vary` and the intermediates it supplies.
Each call of the returned `StageCall` gets the varied parameters as a model, and the supplied intermediates already as objects.
It returns the stage's outputs by field name.
A plain run is the stage with no inputs, whose outputs are the spec's results.

**What a stage holds is what its inputs cannot affect**, so holding it is a cache and dropping it is always safe.
The contract, for every stage and every call:

```text
wf.stage(p0, s, o, data)(p_s, i_s, data) == wf.stage(p0 | p_s, (), o, data)(∅, {}, data)
```

A stage over parameter inputs returns what a plain run with the same values returns.
A stage over an intermediate input returns what a plain run returns when that intermediate has the given value.

**`accumulator(name)` gives a fresh accumulator for an intermediate that an `Accumulate` may fill.**
An accumulator is sciline's: `push` a value, read `value`.
The runner pushes the outputs an `Accumulate` lists in the order listed, and asks nothing else of the accumulator ([aggregation.md](aggregation.md#a-growing-series)).

**A plain function is a workflow with parameter stages only.**
`FunctionWorkflow` wraps `(params, inputs) -> outputs`: its stages compute everything and hold nothing, it drops the outputs not asked for, and it refuses intermediate inputs and accumulators.
`as_workflow` wraps whatever a factory returns that has no `stage` method.

**The workflow is stateless.**
No call of `stage` affects a later one.
A throwaway runner makes the workflow from its factory, builds one stage, calls it once, and exits.
A session runner makes the workflow once per spec version and holds the stages it built ([stages.md](stages.md)).
A session holds the code it imported, so a change to workflow code takes effect in a new session, never in a running one.

**Whether a stage's inputs suffice for its outputs is known only here.**
A stage that leaves a needed parameter unset fails when `stage` builds it or when it is called, and the run fails with a structured reason.

**The framework never imports sciline.**
The adapter that turns a sciline pipeline into a workflow belongs with the workflow packages, in ess.reduce.

## Inputs: a path or an object

`Inputs` is how workflow code gets at the bytes a reference names:

```python
class Inputs(Protocol):
    def path(self, ref: Ref) -> Path: ...    # a local file holding the bytes
    def array(self, ref: Ref) -> Any: ...    # the scipp object a scipp reference names
```

**The workflow code chooses the form of each input, not the spec.**
A raw NeXus file is asked for as a path, because loading NeXus is workflow-specific: which detector banks, which monitors, and no single loaded object exists.
An opaque file, CIF or ORSO, is asked for as a path, because the framework cannot read it.
A processed array is asked for as an object, or as a path when the workflow has its own loader.

**Where the bytes come from is the runner's business.**
A session serves an object from its memory when it holds a copy and from scipp HDF5 otherwise, a throwaway runner from the work directory the backend filled at dispatch.
Neither the spec nor the workflow code can tell the two apart, which is what lets a chain of runs stay in memory in a session without any change to workflow code.
The spec says only what the bytes are, their **format**, so that the backend can check a reference against the field it fills and the picker can list candidates.

## Outputs

**A stage returns objects by field name and never writes files.**
An output field may be absent when the workflow's mode does not produce it.
The runner validates and stores what comes back: literal outputs through `literal_model(spec.outputs)`, kept inline in the record, and array outputs against the `ArraySpec` their field declares, then handed to the data store.
The data store serializes scipp objects to scipp HDF5.
Other types, such as CIF, ORSO, or NeXus products, must come with their own serializer, declared with the output, because the outputs that get published are often not scipp objects.

Chunk-wise processing of one large file, as the NMX workflow does, happens inside the workflow code and is invisible to the framework.
That the NMX product is then held in memory before it is written is a cost accepted here.

## The sciline adapter

**An adapter turns a sciline pipeline into a workflow, and each run request into a `sciline.Stage`.**
`PipelineAdapter` in `adapter.py` does it, and `examples.py` binds the `NORMALIZE` spec with it:

```python
ACCUMULATORS = {Numerator: sciline.Buffered(add), Denominator: sciline.Buffered(add)}

def normalize_aggregation(pipeline: sciline.Pipeline) -> sciline.Aggregation:
    return sciline.Aggregation(pipeline, members=[RunFile], accumulators=ACCUMULATORS,
                               outputs=[Normalized])

def normalize_workflow() -> PipelineAdapter:
    return PipelineAdapter(
        normalize_pipeline(),
        keys={'run': RunFile, 'floor': Floor, 'scale': Scale},
        resolve={'run': 'path'},
        targets={'normalized': Normalized, 'numerator': Numerator, 'denominator': Denominator},
        accumulators=ACCUMULATORS,
    )
```

`keys` maps each parameter field to the sciline key it sets, and `resolve` names the form in which each data reference is asked for.
`targets` maps each output field to the key to compute, intermediates included.
`accumulators` gives, by sciline key, a factory for the accumulator of each intermediate that may be accumulated.
It is the same dictionary a `sciline.Aggregation` takes, so a package defines it once for notebooks and for the binding.

For `stage` the adapter sets the parameters it is given on a copy of the pipeline and builds a `sciline.Stage` (scipp/sciline#245) from the keys of the stage inputs to the keys of the outputs.
An intermediate input cuts off its providers and everything upstream of them.
A `Stage` computes once everything the inputs cannot affect, holds it at its **frontier**, and on each call recomputes only what lies downstream of the inputs.
References in the parameters not varied are resolved once, when the stage is built; references in the stage inputs on every call.
Correctness follows from the graph for any choice of stage inputs, so the choice decides only where the frontier sits and what a rerun costs.
A parameter input that the outputs do not need, which `sciline.Stage` refuses, is held and ignored, because it cannot change the result and which parameters a caller varies must not decide whether a run succeeds.

This matches how notebooks already work: Q bins, d-spacing bins, cut axes and a beam centre enter after the expensive load and coordinate conversion.

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
| many-to-one | a collection-typed parameter of references, such as the per-angle curves a stitch takes |
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

**Exposed intermediates.**
A spec may name some of its outputs as `intermediates`: values inside the pipeline that a plain run does not compute, and that a request may supply in place of what computes them.

```python
class NormalizeOutputs(BaseModel):
    normalized: Array(ArraySpec(dims=('x',)))
    numerator: Array()
    denominator: Array()

NORMALIZE = WorkflowSpec(name='normalize', version=1, params=NormalizeParams,
                         outputs=NormalizeOutputs, intermediates=('numerator', 'denominator'))
```

- A plain run computes the spec's `results`, the outputs that are not intermediates; the serialized spec carries both.
- A request may name any output as an output and supply any intermediate.
- An intermediate is declared with a format like any output, because it may be stored, referenced, and viewed.

The exposed intermediates are the author's domain types that an app or an aggregation needs, such as a detector image, a beam centre, or a numerator and denominator.
They are listed once per spec, not once per stage.
The spec says nothing about the graph: which parameters an intermediate depends on is known only to the workflow code.
`WorkflowSpec` in `spec.py` checks that every intermediate is an output.
This is an extension of this design rather than a field of scipp/ess#690.

## Validation

**Shape**: JSON Schema, in the backend, always.

**Parameters**: the pydantic parameter model, which catches cross-field rules the schema cannot express.
A parameter the spec does not declare is refused, because a pydantic model ignores unknown fields unless its author forbids them, and a reduction parameter dropped in silence gives a wrong number without an error.
scipp/ess#690 forbids extra fields only on its empty model, so the backend checks the top-level fields itself.
Requiring a closed parameter model in the spec would be the better place.
The same check covers the request's names: a name in `vary` must be a parameter of the spec with a value in `params`, a name in `supplied` an exposed intermediate, and an output an output of the spec.
The check sees the request with the spec's defaults filled, as it will be recorded.
A request that supplies no intermediate is validated against the whole parameter model, so a missing required parameter is refused.
A request that supplies an intermediate is validated field by field, because whether the stage's inputs suffice for its outputs depends on the graph: such a request that leaves a needed parameter unset fails when it runs ([records.md](records.md#what-the-backend-checks)).
The backend runs this layer by importing the spec module alone, never a factory.
esslivedata keeps the spec separate from the workflow factory precisely so that specs can be validated without importing workflow code, and scipp/ess#690 must keep that separation.

**Runnability**: every reference resolves to a record the submitter may read and, for a collection element, to a key the producer declares.
An intermediate supplied from a run record of the same spec agrees with it on every parameter both requests set in `params`, apart from those either request varies.
Files exist where the launcher would look, and the launcher's environment has the spec.
Anything past that, such as a file that opens but lacks a monitor, is a run that fails fast, not a validation error.

The runner remains authoritative for parameters (ADR 0001 in scipp/ess#690), so a disagreement with the backend's check is a deployment bug.
The backend refuses a request whose spec module it cannot import, unless the request opts out of the backend's parameter check.

## Test helpers

`testing.py` holds the checks a workflow package runs.

`assert_stage_equals_workflow(workflow, spec, params, stage_inputs, inputs)` builds one stage over the parameters named in the first of `stage_inputs`, with `params` set, and calls it with each of `stage_inputs` in turn, as a session calls a stage it holds.
It compares each result with a plain run, the stage with no inputs, with every value set.
That is the check on the contract of `stage`, and every workflow bound through an adapter runs it.

A second check, recomputing a completed record and comparing the outputs, lets a workflow package keep records from production as regression tests.

## Alternatives considered

**The framework knows sciline.**
It would build the pipeline, compute the requested nodes, and cache intermediates itself.
That ties the framework to one engine, and caching every intermediate is not affordable with event data while the graph does not know compute cost.

**A file-based contract**, where the workflow reads input files and writes output files.
This is simple and remote-friendly, but it forces every chain through disk, which defeats interactive work in a session.

**Two protocols**, one for one-shot execution and one for incremental reruns.
That gives two execution paths to test and keep consistent.
With `stage` as the one method, a plain run, a rerun, and an aggregation's member and finalize are all stages, and the only difference between runners is whether the stage is held.

**Contribute and combine specs with `carry`.**
A pipeline that aggregates is published as two specs cut at the values that add: a contribute spec per run, and a combine spec over a list of references to contributions.
A `carry` declaration on the combine spec says that its combined output may be passed back into its list.
The adapter must re-derive the member parameters to check that members agree, and the associativity promise sits on the spec although it is a property of the code.
Exposed intermediates and `Accumulate` express the same with one spec and no declaration.

**A summary of the graph in the spec.**
The spec lists, per output and intermediate, the parameters it depends on, so the backend can decide which parameters a supplied intermediate makes irrelevant and whether a stage's inputs suffice.
That puts a derived property of the graph into a document meant to be written by hand and read without workflow code, and every change to the pipeline must regenerate it.

**A data field typed as a union of the reference and the materialized value**, a path or a scipp object, with the runner materializing it according to a kind declared on the spec.
That makes the parameter model wrong in both phases, before and after materialization, and needs a validator that accepts anything not plain data.
It also puts a materialization instruction into an interface meant to be pure, and the choice between a path and an object presumes an execution location.

**Stages declared by the workflow author.**
What a choice costs depends on which parameter a person is changing, which only the caller knows.
A `Stage` recomputes everything downstream of all its inputs, so a stage whose inputs are the run and the Q bins loads the run again when the Q bins change.

## Costs

- Two validation points exist, the backend's and the runner's, and the runner's is the authoritative one.
- The runner loads scipp arrays whole.
- Reuse through a stage is exact only if providers are pure.
  `assert_stage_equals_workflow` is the check, and the record's `reused` flag lets publication insist on a result computed without a held stage.
- Authors must expose the intermediates that apps and aggregations use, each with a format.
- A request that supplies an intermediate and leaves a needed parameter unset fails when it runs, not when it is submitted.
- DREAM and imaging masks are Python callables today.
  Each such workflow needs a range vocabulary and a conversion before its requests are plain data.
- The backend must walk the request's values to find references, and the spec's JSON Schema, including nested models, to check them.
- An array output is checked against its `ArraySpec` in the runner at completion, because pydantic cannot check a scipp object.
