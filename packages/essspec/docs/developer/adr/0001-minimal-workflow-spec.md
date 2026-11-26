# ADR 0001: Minimal implementation-independent workflow specifications

- Status: proposed
- Deciders: Simon
- Date: 2026-09-14

## Context

Three mechanisms describe workflow interfaces to users, with overlapping purpose and no shared shape:

- `ess.reduce.parameter` and `ess.reduce.workflow`: a `Parameter` dataclass per sciline key in a global registry, found by walking the pipeline graph from the selected outputs. They drive the ipywidgets GUI.
- `ess.livedata.config.workflow_spec.WorkflowSpec`: a pydantic params model and a pydantic outputs model per workflow. It drives the live-data dashboard.
- `ess.nmx.configurations`: standalone pydantic models for batch reduction.

[scipp/ess#653](https://github.com/scipp/ess/issues/653) and [scipp/esslivedata#889](https://github.com/scipp/esslivedata/issues/889) analyse how to consolidate them.
Two earlier attempts stalled on scope.
A universal `ess.schemas` catalog of all workflows for all instruments turned shared conventions into a problem of coordinating releases across teams.
A rewrite of the essreduce widget layer ([scipp/ess#689](https://github.com/scipp/ess/pull/689)) kept sciline keys, workflow factories and widget code inside the spec, so the spec could not exist apart from one implementation.

We want the smallest layer from which a generic user interface can be generated from a workflow description alone: ipywidgets, a web dashboard, a command-line tool, or a GUI application.
The same spec must hold whether the workflow runs as a local sciline pipeline, behind a web service, or as a cluster job.
So nothing bound to an implementation may appear in the spec.

## Decision

The package `essspec`, imported as `ess.spec`, holds the spec and the bindings.
A spec, `WorkflowSpec`, states what a user gives and what they get back.
A binding, made by the workflow author from a pipeline, a driver or a function, computes the requests of one spec.
[params.md](../../params.md) is the guide to writing a spec, and [bindings.md](../../bindings.md) the guide to writing a binding.

- **The spec is pure interface.** `WorkflowSpec` holds `name`, `version`, `title`, `description`, a params model, an outputs model, `tables`, and an optional `code_revision`. It holds no workflow factory and no sciline keys. A `PipelineBinding` keys a parameter by its type, or by `keys=`, so the spec imports nothing from the workflow. A client of a remote service needs no binding at all.
- **A module that defines specs imports no workflow code.** It may import scipp for the types of data in a group that a reference fills, but never sciline or a workflow package. A service then loads and validates every spec it knows without importing pipelines. esslivedata separates the two for the same reason.
- **essspec depends only on pydantic.** The submodules that need scipp or sciline, such as `conversions` and `pipeline`, are not imported by `ess.spec`. The shared models of `ess.spec.parameters`, such as `QEdges`, contain no scipp; `ess.spec.conversions` turns them into scipp objects. Their values have no defaults, since sensible values depend on the workflow and the instrument. The package lives in scipp/essapps while it changes with the framework ([ADR 0007](../../../../../docs/developer/adr/0007-packages-split-by-dependencies.md)).
- **One params model and one outputs model per workflow**, each a pydantic class with a JSON schema. sciline sets files and data on a pipeline as parameters too, so the params model holds settings, files and data alike. A workflow without parameters has `NoParams`, which rejects every field, so code never branches on a missing model.
- **A field's type states how a request fills it.** `Referenced[T]` marks a field that a reference to an output of an earlier record fills. `Annotated[Path, File(...)]` marks a field that a file fills. A request writes every other field inline. A value that one user types in and another computes is a union, `BeamCentre | Referenced[BeamCentre]`. `Referenced[T]` is `Annotated[T, REFERENCED]`, so pydantic and a binding see `T`.
- **The request type is derived from the params model** (`ess.spec.request_types`): each `Referenced[T]` becomes an `OutputRef`, each file field a `FileRef`. Its JSON schema is the params schema, so the workspace, the schema and a form agree by construction.
- **The key `@ref` is reserved at every depth.** An object with it is a reference to an output or a file. A remote client that holds only the serialized spec then finds the references and files of a request, and a value written inline never reads as a reference.
- **The same closed list of field types holds at every depth**, so a form has one widget per type. A value of several parts is a group: `range: WavelengthRange`, not `tuple[float, float]`.
- **A reference to an output fills a group, never a number or a bare array.** The group's class says what the value is. A picker of the outputs that fit then does not list every number that any record holds.
- **A file field holds a `Path`.** The workspace resolves a file when the request arrives, and checks access and metadata then. The workflow gets the local path and keeps its own keys, such as esssans' `Filename[SampleRun]`. No output holds a `Path`, so every path a workflow gets has passed the access check. The marker does not read the file: a check that read it would read it from a network mount before the request runs, and the content is the workflow's to check.
- **Each parameter is validated alone.** A template, a stage call or a push gives some values only, so a check across parameters would hold for some requests of a spec and not for others. The params model has no validators. A check across parameters belongs to the binding and fails when the request runs.
- **A parameter that holds a reference is validated whole when the request runs.** Other parameters are validated whole when the request arrives. The data of a reference may not exist before then.
- **A table is a list parameter that `tables` names.** A multi-run spec shares the fields of its single-run spec by inheritance. `ess.spec.testing.check_one_row` checks that the two bindings agree on requests with one row per table.
- **Each output has a type of its own**, such as `iofq: IofQ`, or is a list or dict of one. Output selection is not modelled: like parameter slicing, it is a notion of sciline only.
- **The serialized spec has no inverse.** `spec.serialize()` returns a `SerializedWorkflowSpec`, plain data with the JSON schemas of params and outputs, which a service announces. Validators do not survive a JSON schema, so a deserialized spec would lie about its validation. The process that owns the model classes validates; a remote UI validates against the schema and the service decides. This matches the announcement-as-contract design of [scipp/esslivedata#889](https://github.com/scipp/esslivedata/issues/889).
- **The identity of a spec is `name` and `version`.** It has no `instrument`: instrument is meaningless for a technique-level workflow, and only the context that enumerates specs can make them unique. esslivedata keys workflows by `(instrument, name, version)` and supplies the instrument itself. `code_revision` records provenance and is not part of the identity.
- **How specs are enumerated is left open.** Prescribing one mechanism would recreate the catalog problem of `ess.schemas`. Entry points, with specs and factories in separate groups under the same name, fit the rule that a spec module imports no workflow code.
- **esslivedata's `WorkflowSpec` is meant to inherit from this one**, adding its live-data fields. So the field names `name`, `version`, `title`, `description`, `params` and `outputs` are esslivedata's. Its specs are the case without `Referenced[...]` fields. A value that changes during a stream, such as a motor position, comes as a context key of `StreamProcessor` there; here it comes with the run.

## Consequences

- A generic UI, a command-line interface among them, can be generated from `WorkflowSpec` alone, and from `SerializedWorkflowSpec` across processes.
- Installing essspec pulls in no science stack. A spec module with scipp types imports scipp, which needs only numpy.
- `ess.reduce.parameter`, `ess.reduce.workflow` and their widgets are meant to be superseded. Removing them is essreduce's decision. Their graph-derived parameter discovery would be dropped, not ported.
- `ess.nmx.configurations` and esslivedata migrate one package at a time. A package that never migrates costs the others nothing.
- A validator of a group that holds a reference rejects a value only when the request runs. A failure then fails the record, or stops the accumulator for a push.

## Alternatives considered

- **A `Parameter` per sciline key in a registry, found from the selected outputs.** It treats output selection as slicing the workflow, which only sciline can express. Workflows with different parameters for different outputs are distinct specs.
- **Two models, params written inline and inputs given by reference.** A value used both ways, such as a beam centre, needs a spec per source, and moving a field between the models breaks every client.
- **Inputs that also accept a value inline.** Removes the reason for two models.
- **The sources of a field worked out from its type.** A group that gains a file field or an array changes how every field of its type is filled, and "typed in, or found" cannot be stated.
- **A constant spec per typed value, for a request to reference.** Needs a submission per typed value.
- **A field typed as the reference.** The binding gets references, not data.
- **A union of the reference and the data, `ComputedTransmission | OutputRef`.** Needs a validator that accepts anything not plain data, and hides that member from the JSON schema.
- **A field typed as the data, with validation switched by phase.** Every validator would run on references when the request arrives.
- **Every value tagged, `{"value": ...}` or `{"ref": ...}`.** Every value written inline in requests, the log and esslivedata's params gets a wrapper.
- **`$ref` as the key of a reference.** It is a JSON Schema keyword, so a reference in a default or an example of a schema would be misread.
- **`@ref` reserved only at fields typed `Referenced[...]`.** A misplaced reference would fail with pydantic's type error, not with the rule that says where a reference goes.
- **A reference at a field of any type, such as a number.** A picker would list every number that any record holds.
- **`NewType`s to say what a value is.** The fit check would have to look through them, and a field typed plain `sc.DataArray` still accepts every array.
- **Every parameter a group, as a rule.** The binding already asks for `keys=` at a parameter that is not a group, so a group per parameter is a convention.
- **Any type that pydantic makes a JSON schema of, such as `tuple[float, float]`.** A form has no widget for it, and the fields of a group serve.
- **References only at a parameter or a row, never inside a group.** The language would differ between a parameter and a field: a sample's computed transmission could not sit next to its run.
- **The rules stated twice, by a walk that validates a request and by a schema-only copy of the params.** The two drifted.
- **A file as a plain `Path`.** The file source checks nothing, and a picker cannot tell which kind of file a field accepts.
- **A pydantic class per kind of file.** The workflow gets the model, so a sciline workflow whose providers accept `Filename[SampleRun]` needs an adapter per file.
- **A reference to a whole list.** Table rules, such as distinct rows, could be checked only when the request runs.
- **Rows also compared by the data read.** The data are often arrays without a JSON form, so it would cover few tables.
- **Annotations for the format and array structure of data.** The field types tell the framework the same.
- **A `tabulate` that derives the multi-run model from the single-run spec.** The model has no static type, drops the model validators of the single-run model, and needs rules for where field metadata goes.
- **A table as several list parameters of equal length.** The values of one run are spread over several lists and paired by position, and a list left out of a push needs filling.
- **Tables named apart from their parameters.** A second namespace, which blanks, pushes and the driver would mix with the names of parameters.
