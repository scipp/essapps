# essapps

Framework for ESS data-reduction applications: run records and references, sessions with warm workflows, a small scheduler, and a data store, as sketched in [docs/developer/architecture.md](../../docs/developer/architecture.md).

Local mode only: client, backend, launcher, session, and data store in one Python process, with a subprocess launcher as the throwaway execution shape.

## Try it

```python
from pathlib import Path

from ess.apps.client import local
from ess.apps.examples import HISTOGRAM, LOAD, NORMALIZE, SUM, registry, write_run
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, Ref

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
run = DatasetRef(instrument='dream', run=1)
assert run in [candidate.ref for candidate in client.pick()]

# A run in the session: the output stays in memory, the record is complete.
loaded = client.run(LOAD, {'run': run, 'scale': 2.0})
data = loaded.ref('data')

# Interactive reruns of a sciline pipeline; 'bins' is declared cheap, so it is an
# input of the stage and the second run comes out of the state the stage holds.
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
    'sum': client.request(SUM, {'runs': [Ref(record='@a', output='data'), Ref(record='@b', output='data')]}),
})
client.output(group['sum'], 'total')

# A declared additive combine: a member run produces the contribution and none of
# the other outputs, a combine request adds contributions and normalises. A series
# grows by combining the previous combine with the new member.
member = client.run(NORMALIZE, {'run': run, 'floor': 1.5}, stage='contribute')
total = client.run(NORMALIZE, {'scale': 2.0}, stage='combine', contributions=[member])
client.output(total, 'normalized')
```

Every run through `throwaway=True` instead executes in a subprocess that writes its outputs and a completion marker to disk; the backend reconciles from the marker, so `client.wait([...])` is needed before reading outputs. The registry must then be importable by name, for example `registry='ess.apps.examples:registry'`.

The checksum of every dataset file a run reads is on its record, so a recompute can tell whether it read the same bytes.

A spec may mark one output as its **contribution**, the value at the workflow's accumulation keys, and declare which parameters its finalize stage reads (D15). Such a workflow has three entry points instead of one callable, contribute, combine, and finalize, of which the callable is the first and the last composed. A request says which of them it runs: `stage='contribute'` is a member run of a series, whose only output is the contribution, and `stage='combine'` carries the finalize parameters and references the contributions to combine, which for a chained series are the previous combine's and the new member's. Contributions are references like any other, so they are in the private cache of a session and on disk in the throwaway shape, and the scheduler waits on them. The backend refuses a combine request that carries a contribute parameter, references a contribution of another spec, or references members that disagree on the parameters contribute reads.

Workflow authors bind a spec to a factory returning the callable; `ess.apps.warm.WarmPipeline` wraps a sciline pipeline as a `sciline.Stage` whose inputs are the cheap parameters, and `ess.apps.testing.assert_warm_equals_cold` is the one check on its reuse rules. `ess.apps.aggregation.AggregatePipeline` wraps a sciline pipeline as the three entry points, building a `sciline.Aggregation` from the accumulation keys and an accumulator per key, and refusing at bind time a declaration the graph disagrees with; `ess.apps.testing.assert_combine_is_associative` is the one check on a declared combine. Publication with a provenance snapshot is `client.publish`.

Templates, lookups, and rules are the stored data in `ess.apps.rules`, and the operations over them are in `ess.apps.batch` (D14). A **template** is an immutable, versioned partial request; a **lookup** is an ordered table beside it whose entries match dataset fields by a value within a tolerance, a glob pattern, or an open-ended run-number range, and supply fills; a **rule** adds a selector with a lower bound, a retry policy, exclusions, and optionally a series. `apply` is the one operation that makes requests from any of them: it fills each member through the ladder template, lookup entry, typed values, and returns a group to preview and submit whole, never submitting on its own. It accepts a `DataFrame` with the member key as index and the typed values as columns, and `batch_table` gives the batch back in the same shape; pandas stays at the client. `backlog`, `reprocess`, and `rerun` are the same operation over a query: the datasets before a new rule's bound, the members made by an older rule version, and the members with no completed record.

A **batch** is the records under one label, and nothing else is stored: `client.batch(label)` is the latest record per member key. `TriggerLoop` runs rules and keeps no memory. It fires on a dataset when the rule is active, the selector matches, the dataset lies after the rule's bound, it is not excluded, and no record exists under the rule's label with it as member key, or its failure is one the retry policy names and the records under that member key are fewer than the limit. Every clause is a query, so a restart fires on nothing twice, and `trigger_status` answers the same question for one dataset, with the reason. A rule with a series submits, per arrival, the member's contribute run and a combine request chained to the previous combine of that series, both in one group.

## A session on real data

`notebooks/loki-session.ipynb` tells one LoKI@Larmor session on the esssans tutorial files: pick a background run from a folder dataset source, compute the beam centre as its own record, feed it to the I(Q) reduction as a reference, move the Q binning on a slider under the label `iofq` so that the warm stage reruns in a quarter of a second rather than three, fork the plot into a second label, and read the provenance back to the dataset references. The specs are in `ess.apps.loki`, which needs the tutorial files and the `loki` extra (`pip install -e "packages/essapps[loki]"`), which brings in esssans.

## Tests

```
pip install -e "packages/essapps[test]"
pytest packages/essapps/tests
```
