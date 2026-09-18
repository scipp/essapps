# essapps

Framework for ESS data-reduction applications: run records and references, sessions that stage a workflow over the parameters a person moves, a small scheduler, and a data store, as sketched in [docs/developer/architecture.md](../../docs/developer/architecture.md).

Local mode only: client, backend, launcher, session, and data store in one Python process, with a subprocess launcher as the throwaway execution shape.

## Try it

```python
from pathlib import Path

from ess.apps.client import local
from ess.apps.examples import (
    HISTOGRAM, LOAD, NORMALIZE_COMBINE, NORMALIZE_CONTRIBUTE, SUM, registry, write_run
)
from ess.apps.sources import FolderSource
from ess.apps.spec import OutputRef, dataset_ref

Path('/tmp/runs').mkdir(exist_ok=True)
write_run('/tmp/runs/dream_1.h5', [1.0, 5.0, 2.0, 6.0])

client = local(
    '/tmp/essapps',
    instrument='dream',
    proposal='p1',
    submitter='me',
    registry=registry(),
    sources=[FolderSource('/tmp/runs', '*.h5')],
)

# Data the framework did not compute is named by its identity, here the run the
# file name carries; where the bytes are is asked of the folder at dispatch, and
# nothing is copied or stored. client.pick() is the query behind an input field:
# the datasets of every source and the outputs of completed records.
run = dataset_ref(instrument='dream', run=1)
assert run in [candidate.ref for candidate in client.pick()]

# A run in the session: the output stays in memory, the record is complete.
loaded = client.run(LOAD, {'run': run, 'scale': 2.0})
data = loaded.ref('data')

# Interactive reruns of a sciline pipeline: the session stages it over 'bins',
# so the second run comes out of the stage it is already holding.
# Both carry the label 'hist', the slot the plot owns, so the second supersedes
# the first.
first = client.run(HISTOGRAM, {'data': data, 'bins': 2}, label='hist')
second = client.run(HISTOGRAM, {'data': data, 'bins': 8}, label='hist')
assert second.reused and client.latest('hist').id == second.id
client.view(second.ref('histogram'))  # plain numpy arrays

# A group with pending outputs: map two loads, combine them, submitted together.
group = client.submit_group({
    'a': client.request(LOAD, {'run': run}),
    'b': client.request(LOAD, {'run': run, 'scale': 3.0}),
    'sum': client.request(SUM, {'runs': [OutputRef(record='@a', output='data'), OutputRef(record='@b', output='data')]}),
})
client.output(group['sum'], 'total')

# One pipeline cut into two specs: a member run produces a contribution, and a
# combine run sums contributions and normalises the sum. A series grows by
# passing the previous combine's contribution back in beside the new member.
member = client.run(NORMALIZE_CONTRIBUTE, {'run': run, 'floor': 1.5})
total = client.run(NORMALIZE_COMBINE, {
    'contributions': [member.ref('contribution')], 'scale': 2.0,
})
client.output(total, 'normalized')
```

Every run through `throwaway=True` instead executes in a subprocess that writes its outputs and a completion marker to disk; the backend reconciles from the marker, so `client.wait([...])` is needed before reading outputs. The registry must then be importable by name, for example `registry='ess.apps.examples:registry'`.

The checksum of every dataset file a run reads is on its record, so a recompute can tell whether it read the same bytes.

A spec is the signature of one callable and a record one call of it, so a sum over runs is **two specs**, not one with three entry points (D15): a contribute spec, whose one output holds the values at the workflow's accumulation keys, and a combine spec, which takes a collection of those contributions beside the parameters read after the sum and produces the combined contribution and the results. Both are ordinary specs: ordinary validation, forms, templates, records, and recompute, and the contributions are an ordinary collection parameter of data references, so the scheduler waits on them and nothing in the backend knows what a contribution is. The one thing a spec declares is `chain={'contributions': 'contribution'}`: that the output may come back as an element of the parameter and then stands for everything it was combined from. The spec checks that the two ends are a collection of data references and a data output of the same format; that the combination does not depend on grouping or order is checked by `ess.apps.testing.assert_combine_is_associative`.

Workflow authors bind a spec to a factory returning the callable; `ess.apps.adapter.PipelineAdapter` wraps a sciline pipeline as a stateless callable and as an offer of a `sciline.Stage` over any subset of its parameters, resolving each data reference as the path or object the binding asks for; which subset is the session's decision and lives in `ess.apps.stages.Stages`, and `ess.apps.testing.assert_stage_equals_workflow` is the one check that a stage returns what the workflow returns. `ess.apps.aggregation.Aggregation` cuts one sciline pipeline into the contribute and combine callables, and the pipeline's own single-run callable, reading the split back off the graph and refusing specs it disagrees with. It writes the contribute parameters that are not member parameters into the contribution, and the combine refuses contributions that disagree on them, which is the check no backend can make because only the binding knows which parameters may differ between members. Publication with a provenance snapshot is `client.publish`.

Templates, lookups, and rules are the stored data in `ess.apps.rules`, and the operations over them are in `ess.apps.batch` (D14). A **template** is an immutable, versioned partial request; a **lookup** is an ordered table beside it whose entries match dataset fields by a value within a tolerance, a glob pattern, or an open-ended run-number range, and supply fills; a **rule** adds a selector with a lower bound, a retry policy, exclusions, and optionally a series. `apply` is the one operation that makes requests from any of them: it fills each member through the ladder template, lookup entry, typed values, and returns a group to preview and submit whole, never submitting on its own. It accepts a `DataFrame` with the member key as index and the typed values as columns, and `batch_table` gives the batch back in the same shape; pandas stays at the client. `backlog`, `reprocess`, and `rerun` are the same operation over a query: the datasets before a new rule's bound, the members made by an older rule version, and the members with no completed record.

A **batch** is the records under one label, and nothing else is stored: `client.batch(label)` is the latest record per member key. `TriggerLoop` runs rules and keeps no memory. It fires on a dataset when the rule is active, the selector matches, the dataset lies after the rule's bound, it is not excluded, and no record exists under the rule's label with it as member key, or its failure is one the retry policy names and the records under that member key are fewer than the limit. Every clause is a query, so a restart fires on nothing twice, and `trigger_status` answers the same question for one dataset, with the reason. A rule with a series submits, per arrival, the member's run and a combine request over that series, both in one group. The combine references the previous combine and the members it does not cover when the combine spec declares a chain and every record that combine covers is still a current member; otherwise it references all current members, so a corrected member is not counted twice.

## A session on real data

`notebooks/loki-session.ipynb` tells one LoKI@Larmor session on the esssans tutorial files: pick a background run from a folder dataset source, compute the beam centre as its own record, feed it to the I(Q) reduction as a reference, move the Q binning on a slider under the label `iofq` so that the session stages the reduction over it and a rerun takes a quarter of a second rather than three, fork the plot into a second label, and read the provenance back to the dataset references. The specs are in `ess.apps.loki`, which needs the tutorial files and the `loki` extra (`pip install -e "packages/essapps[loki]"`), which brings in esssans.

## Tests

```
pip install -e "packages/essapps[test]"
pytest packages/essapps/tests
```
