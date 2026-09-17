# essapps

Framework for ESS data-reduction applications: run records and references, sessions with warm workflows, a small scheduler, and a data store, as sketched in [docs/developer/architecture.md](../../docs/developer/architecture.md).

Local mode only: client, backend, launcher, session, and data store in one Python process, with a subprocess launcher as the throwaway execution shape.

## Try it

```python
from ess.apps.client import local
from ess.apps.examples import HISTOGRAM, LOAD, SUM, registry, write_run
from ess.apps.spec import Ref

client = local('/tmp/essapps', instrument='dream', proposal='p1', submitter='me', registry=registry())

# Files on this machine are records too; nothing is copied.
run = client.file(write_run('/tmp/run1.h5', [1.0, 5.0, 2.0, 6.0]))

# A run in the session: the output stays in memory, the record is complete.
loaded = client.run(LOAD, {'run': run, 'scale': 2.0})
data = loaded.ref('data')

# Interactive reruns of a sciline pipeline; 'bins' is declared cheap, so the
# second run reuses the warm workflow. Both carry the label 'hist', the slot the
# plot owns, so the second supersedes the first.
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
```

Every run through `throwaway=True` instead executes in a subprocess that writes its outputs and a completion marker to disk; the backend reconciles from the marker, so `client.wait([...])` is needed before reading outputs. The registry must then be importable by name, for example `registry='ess.apps.examples:registry'`.

Workflow authors bind a spec to a factory returning the callable; `ess.apps.warm.WarmPipeline` wraps a sciline pipeline and `ess.apps.testing.assert_warm_equals_cold` is the one check on its reuse rules. Templates, batch, and the trigger loop are in `ess.apps.templates`; publication with a provenance snapshot is `client.publish`.

## Tests

```
pip install -e "packages/essapps[test]"
pytest packages/essapps/tests
```
