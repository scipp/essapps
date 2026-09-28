# ESS data-reduction framework: the design in five minutes

The framework runs ESS data-reduction workflows in four ways: by hand, interactively, as a batch over many runs, and automatically on each new run.
It records every run so that any result can be traced to raw data, parameters, and software versions.
Workflows run in a notebook process or on a cluster without changes to workflow code.
[scoping.md](scoping.md) states the goals.

## What using it looks like

```python
client = local('/tmp/essapps', instrument='loki', proposal='p1', submitter='me',
               registry=registry(), sources=[FolderSource('/data/loki', '*.hdf', proposal='p1')])

run = dataset_ref(instrument='loki', run=60339)
centre = client.run(BEAM_CENTER, {'sample_run': run, ...})

tune = Template(spec=IOFQ, params={'sample_run': run, 'beam_center': centre.ref('center'), ...},
                blanks=('q',), outputs=('iofq',), name='iofq')       # the stage over 'q'

for num_bins in (50, 100, 200):                          # a slider, in effect
    iofq = client.run(tune, {'q': QEdges(start=0.01, stop=0.3, num_bins=num_bins)})
    plot(client.view(iofq.ref('iofq')))

client.provenance(iofq)                                  # back to the dataset references
```

## The ideas

1. **A run is a request, and the request is plain data.**
   It names a spec with its parameter values and the outputs to compute; the backend fills the spec's defaults, so the record holds every value the run used.
   The backend writes it down as a run record, together with what happened: status, outputs, package versions.
   A notebook, a UI, and an automatic trigger all submit the same kind of request.
2. **Data is named by reference.**
   A reference is either "output X of record Y" or a dataset identity such as a SciCat PID.
   Requests never contain file paths into our storage.
   A person may type a run number or a path, which the backend replaces by the dataset's identity at submission; a path names only a file in a folder a dataset source reads, so an ad-hoc local file must be put there first.
   Provenance is the graph of references between records, so no separate provenance model exists.
3. **Records are stateless, sessions are caches.**
   Interactive work runs in a session, a process that keeps outputs and intermediate results in memory.
   No record names a session, and everything a session holds can be recomputed from records.
   Batch and automatic reduction run each request in a throwaway process that writes its outputs to disk.
4. **A rerun recomputes only what the changed parameter affects.**
   The client names the stage as a template, whose blanks are the parameters that move, and the session holds a sciline `Stage` for it.
   Each rerun is still a complete record, with the `params` and outputs of a plain run with the same values: what varies is a hint given with the submission.
   A label on the request groups the reruns, so a person sees the latest result and not a hundred records.
5. **Chaining is a reference to an output that does not exist yet.**
   Requests submitted together may reference each other's outputs, and the backend holds each one until its inputs have completed.
   That is the only scheduling mechanism.
6. **A sum over runs is one run over a list of runs.**
   The spec's run parameter is a list, and the record names every run it sums.
   The binding drives the package's `sciline.Aggregation`: it reduces each run through the aggregation's contribute stage and accumulates, and the framework never adds arrays.
   A session holds the accumulation as a cache, so adding a run to a sum reduces only that run.
7. **Batch and automatic reduction are one mechanism.**
   A template is a partial request, a lookup fills parameters by matching dataset fields, and a rule adds a selector for datasets.
   The stage a slider fills is a template too.
   One operation, `apply`, makes requests from them.
   The trigger loop calls it for each new dataset and keeps no memory of its own.
8. **Only workflow code knows the graph.**
   The framework does not import sciline.
   An adapter in ess.reduce turns a pipeline into a workflow, and each run into a call of a `sciline.Stage`, and decides from the graph what a session can hold.
9. **The Python client interface is the API, and publication is explicit.**
   UIs use the client interface only, and HTTP is a later transport for it.
   Plots get small arrays through views, which are not recorded.
   Only results that someone chose to publish enter SciCat.

## Where to read next

| Time | Document | Content |
|---|---|---|
| 30 minutes | [architecture.md](architecture.md) | the whole design in teaching order, with code, a components table, and a table of decisions |
| as needed | [stages.md](stages.md) | interactive work: sessions, stages, slots, views |
| | [records.md](records.md) | run requests and run records, references, datasets, the data store, scheduling |
| | [workflow-contract.md](workflow-contract.md) | spec, the workflow protocol, inputs and outputs, validation, changes needed in scipp/ess#690 |
| | [aggregation.md](aggregation.md) | sums over runs as list parameters, series under a rule, sums spread over nodes |
| | [rules.md](rules.md) | templates, lookups, rules, `apply`, the trigger loop |
| | [operations.md](operations.md) | client interface, publication, deployment, failure handling |
| | [open-issues.md](open-issues.md) | open questions, findings from binding real workflows, deferred work |
| | [roadmap.md](roadmap.md) | the three delivery phases, what each needs, and what the skeleton lacks for the first |
| | [glossary.md](glossary.md) | terms, and where esslivedata uses a word differently |
| hands-on | [`packages/essapps`](../../packages/essapps/README.md) | the walking skeleton: a guided tour, tests, and a LoKI session notebook |

[user-stories.md](user-stories.md) holds the user stories that the design is checked against.
Unmaintained studies that read an earlier edition of the design against [Snakemake](prior-art/snakemake.md), [AiiDA](prior-art/aiida.md), [Mantid](prior-art/mantid.md), and [git](prior-art/git.md) are in `prior-art/`.
A later study reads the design against [ewoks](prior-art/ewoks.md), the ESRF workflow system, and asks whether to build on it.
