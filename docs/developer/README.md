# ESS data-reduction framework: the design in five minutes

The framework runs ESS data-reduction workflows in four ways: by hand, interactively, as a batch over many runs, and automatically on each new run.
It records every run so that any result can be traced to raw data, parameters, and software versions.
Workflows run in a notebook process or on a cluster without changes to workflow code.
[scoping.md](scoping.md) states the goals.

## What using it looks like

```python
client = local('/tmp/essapps', instrument='loki', proposal='p1', submitter='me',
               registry=registry(), sources=[FolderSource('/data/loki', '*.hdf')])

run = dataset_ref(instrument='loki', run=60339)
centre = client.run(BEAM_CENTER, {'sample_run': run, ...})

for num_bins in (50, 100, 200):                          # a slider, in effect
    iofq = client.run(IOFQ, {'sample_run': run, 'beam_center': centre.ref('center'),
                             'q': QEdges(start=0.01, stop=0.3, num_bins=num_bins), ...},
                      label='iofq')
    plot(client.view(iofq.ref('iofq')))

client.provenance(iofq)                                  # back to the dataset references
```

## The ideas

1. **A run is a request, and the request is plain data.**
   It names a workflow spec, parameter values, and input data.
   The backend writes it down as a record, together with what happened: status, outputs, resolved parameters, package versions.
   A notebook, a UI, and an automatic trigger all submit the same kind of request.
2. **Data is named by reference.**
   A reference is either "output X of record Y" or a dataset identity such as a SciCat PID.
   Requests never contain file paths into our storage.
   Provenance is the graph of references between records, so no separate provenance model exists.
3. **Records are stateless, sessions are caches.**
   Interactive work runs in a session, a process that keeps outputs and intermediate results in memory.
   No record names a session, and everything a session holds can be recomputed from records.
   Batch and automatic reduction run each request in a throwaway process that writes its outputs to disk.
4. **A rerun recomputes only what the changed parameter affects.**
   The session holds a sciline `Stage` for the workflow and chooses its inputs from what the person changes.
   Each rerun is still a complete record.
   A label on the request groups the reruns, so a person sees the latest result and not a hundred records.
5. **Chaining is a reference to an output that does not exist yet.**
   Requests submitted together may reference each other's outputs, and the backend holds each one until its inputs have completed.
   That is the only scheduling mechanism.
6. **A sum over runs is two plain specs.**
   A contribute spec reduces one run to its contribution, and a combine spec adds contributions and normalises.
   One declaration, `chain`, lets a growing series reuse the previous sum.
   The framework never adds arrays.
7. **Batch and automatic reduction are one mechanism.**
   A template is a partial request, a lookup fills fields from dataset metadata, and a rule adds a selector for datasets.
   One operation, `apply`, makes requests from them.
   The trigger loop calls it for each new dataset and keeps no memory of its own.
8. **Workflow code is a stateless callable behind a spec.**
   The framework does not import sciline.
   An adapter in ess.reduce turns a pipeline into the callable.
9. **The Python client interface is the API, and publication is explicit.**
   UIs use the client interface only, and HTTP is a later transport for it.
   Plots get small arrays through views, which are not recorded.
   Only results that someone chose to publish enter SciCat.

## Where to read next

| Time | Document | Content |
|---|---|---|
| 30 minutes | [architecture.md](architecture.md) | the whole design in teaching order, with code, a components table, and a table of decisions |
| as needed | [stages.md](stages.md) | interactive work: sessions, stages, slots, views |
| | [records.md](records.md) | requests, records, references, datasets, the data store, scheduling |
| | [workflow-contract.md](workflow-contract.md) | spec, callable, inputs and outputs, validation, changes needed in scipp/ess#690 |
| | [aggregation.md](aggregation.md) | contribute and combine specs, `chain` |
| | [rules.md](rules.md) | templates, lookups, rules, `apply`, the trigger loop |
| | [operations.md](operations.md) | client interface, publication, deployment, failure handling |
| | [open-issues.md](open-issues.md) | open questions, findings from binding real workflows, deferred work |
| | [glossary.md](glossary.md) | terms, and where esslivedata uses a word differently |
| hands-on | [`packages/essapps`](../../packages/essapps/README.md) | the walking skeleton: a guided tour, tests, and a LoKI session notebook |

Background studies: [user-stories.md](user-stories.md), [staging.md](staging.md) on delivery phases, [stateless.md](stateless.md) on designs without sessions, and the design read against [Snakemake](snakemake.md), [AiiDA](aiida.md), [Mantid](mantid.md), and [git](git.md).
