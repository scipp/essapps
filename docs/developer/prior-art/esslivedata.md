# esslivedata as prior art for bindings

esslivedata runs ESS workflows on live data, with params models that a dashboard shows and the backend validates.
It solves the problem that a binding solves here: calling a sciline workflow with the values of a spec ([What a binding provides](../README.md#what-a-binding-provides)).
`ess.spec` is meant to hold its params models too ([ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md#convergence-with-esslivedata)).

Source, read on 2026-10-23: `scipp/esslivedata` [a5b1828][src], under `src/ess/livedata/`.

## What it is

Each instrument has a `specs.py`, with the params and outputs models, and a `factories.py`, the sciline glue, under `config/instruments/<instrument>/`.
Only the backend services load the factories (`instrument.load_factories()` in `services/*.py`), so the dashboard imports the specs without the workflow packages.
A *factory* is attached to a spec when it is registered, and makes a *workflow* for each job.
A workflow takes chunks of the streams and gives the outputs ([Workflow L22-L35][protocol]):

```python
class Workflow(Protocol):
    def accumulate(self, data: dict[str, Any], *, start_time: Timestamp, end_time: Timestamp) -> None: ...
    def finalize(self) -> dict[str, Any]: ...
    def clear(self) -> None: ...
```

Most sciline factories return a `StreamProcessorWorkflow` ([L26][spw]), which wraps essreduce's `StreamProcessor` with dynamic keys, context keys, target keys, and accumulators.
So a factory corresponds to a binding, and the workflow it returns to a driver.

## Findings

**The factory gets the params model instance.**
The job manager validates the params of a job's config into the spec's params model ([job_manager.py L175][jm], [validate_params L354-L369][validate]), and passes the instance to the factory ([L449-L459][call]).
`attach_factory` checks, when a factory is registered, that the spec's params model is the factory's `params` annotation or a subclass of it ([L258-L305][attach]).
Here `compute(params)` gets an instance too, and a binding takes its spec, so the pairing is made once, when the binding is made.

**Conversions and values that users never see are set in the factory.**
LoKI's factory sets the keys from the params by hand, `wf[QBins] = params.q_edges.get_edges()` and `wf[BeamCenter] = params.beam_center.get_vector()` ([L197-L199][loki-keys]), and sets values that no user sees, such as `CorrectForGravity(False)` and `DirectBeam = None` ([L92-L99][loki-base]).
BIFROST's custom Q map combines the fields of `axis1` and `axis1_edges` into one key, `CutAxis1` ([L197-L216][bifrost-cut]).
Here values that users never see are set in `make_pipeline` too, but a conversion is a provider that takes the spec's model, such as `ess.spec.conversions.edges_to_variable` called by one that makes `QBins` from `QEdges`, so that a user of the workflow without the framework can set either key.

**A model is already a sciline key.**
BIFROST's detector ratemeter sets `wf[DetectorRatemeterRegionParams] = params.region` ([L101][bifrost-region]), and a provider takes the model ([L340-L342][bifrost-provider]).
Here this is the default route: each parameter sets the key of its class.

**The graph and the outputs depend on a field of a group.**
LoKI's factory reads `params.transmission.mode` and, for one mode, inserts two providers and adds the output `transmission_fraction` to its target keys ([L205-L222][loki-mode]).
The outputs model declares `transmission_fraction` for every mode, with a template as its default ([specs.py L122-L134][loki-outputs]).
Here `make_pipeline` gets the fixed values, so the graph may depend on them, and an output that exists for some values only is declared `| None`.
Factories also read fields of groups elsewhere, such as BIFROST's `params.axis1.axis` ([L184-L194][bifrost-axes]).
Here a group sets one key, and a provider or `make_pipeline` reads its fields.

**The choice of source is passed beside the params.**
The user's choice of detector and monitors arrives as `source_name` and `aux_source_names`, passed only to a factory that declares them ([L449-L459][call]).
The factory maps each stream name to a key ([L154-L163][loki-streams]) and sets `NeXusDetectorName` from it ([L189][loki-source]).
Here a choice of source is a parameter like any other; streams are not modelled yet.

**Outputs are matched by name, and nothing checks the names.**
Target keys map hand-written output names to keys ([L201-L203][loki-targets]), and `finalize` returns a dict.
No instance of the outputs model is made; the job manager uses it to title each output, and skips a name the model lacks ([job_manager.py L132-L140][titles]).
Here a binding returns an instance of the outputs model, so pydantic checks the names, and a type checker checks a function binding.

**Some factory code works around the base workflows.**
LoKI's factory drops position coordinates from monitors that `monitor_to_wavelength` does not consume ([L131-L152][loki-workaround], esssans#244), and sets the empty-beam monitors to one count, so that the standard transmission provider gives the ratio of the sample's monitors ([L211-L219][loki-mode]).
Such code shows what the base workflows lack, not what a binding must allow.

## Borrow

- The params model instance as the factory's argument, checked against the spec once.
- A model as a sciline key, read by a provider.

## Do not borrow

- Output names unchecked against the outputs model.
- Conversions written in the glue, where a provider in the workflow package serves users without the framework too.
- An output that exists for some values only, declared as always there.
- `clear()` and windowed outputs on the driver: each breaks the arrival symmetry ([ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md)).

[src]: https://github.com/scipp/esslivedata/tree/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata
[protocol]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/workflows/workflow_factory.py#L22-L35
[spw]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/workflows/stream_processor_workflow.py#L26
[jm]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/core/job_manager.py#L175
[validate]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/workflows/workflow_factory.py#L354-L369
[call]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/workflows/workflow_factory.py#L449-L459
[attach]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/workflows/workflow_factory.py#L258-L305
[titles]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/core/job_manager.py#L132-L140
[loki-base]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L92-L99
[loki-workaround]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L131-L152
[loki-streams]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L154-L163
[loki-source]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L189
[loki-keys]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L197-L199
[loki-targets]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L201-L203
[loki-mode]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/factories.py#L205-L222
[loki-outputs]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/loki/specs.py#L122-L134
[bifrost-region]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/bifrost/factories.py#L101
[bifrost-axes]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/bifrost/factories.py#L184-L194
[bifrost-cut]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/bifrost/factories.py#L197-L216
[bifrost-provider]: https://github.com/scipp/esslivedata/blob/a5b1828ada8c3d95f267a34ab8b9c10a99f9448f/src/ess/livedata/config/instruments/bifrost/factories.py#L340-L342
