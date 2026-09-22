# essapps

Framework for ESS data-reduction applications: run records and references, sessions that stage a workflow over the parameters a person moves, a small scheduler, and a data store, as described in [docs/developer/architecture.md](../../docs/developer/architecture.md).

Local mode: client, backend, launcher, session, and data store in one Python process, with a subprocess launcher as the throwaway execution shape.
Over HTTP: the same client against a backend served from another process.

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

### Over HTTP

The `service` extra brings in FastAPI, httpx, and uvicorn. Serve a backend from one shell:

```sh
essapps serve --root /tmp/essapps-served --registry ess.apps.examples:registry --datasets /tmp/runs
```

Every run it takes executes in a throwaway process, so the registry is named, not passed. From another process, `remote` replaces `local` and returns a `Client` whose backend forwards each call:

```python
from ess.apps.remote import remote

client = remote('http://127.0.0.1:8000', instrument='dream', proposal='p1', submitter='me')
```

The `essapps` command does the same from a shell, with the flags of `submit` generated from the spec's parameter schema (`essapps submit load/v1 --help` lists them):

```sh
export ESSAPPS_INSTRUMENT=dream ESSAPPS_PROPOSAL=p1
essapps submit load/v1 --run run:dream/1 --scale 2.0   # prints the record id
essapps wait <record>
essapps output <record> total
```

## Where things are

| To see | Read | Design document |
|---|---|---|
| requests, records, references | `records.py`, `spec.py`, `backend.py` | [records.md](../../docs/developer/records.md) |
| the transport boundary, the server, the CLI | `backend.py`, `server.py`, `remote.py`, `cli.py` | [operations.md](../../docs/developer/operations.md#the-client-interface) |
| the callable, `Inputs`, the stage offer, entry points | `binding.py` | [workflow-contract.md](../../docs/developer/workflow-contract.md) |
| a sciline pipeline as a callable and as a stage | `adapter.py` | [workflow-contract.md](../../docs/developer/workflow-contract.md#the-sciline-adapter) |
| how a session chooses and holds stages | `stages.py`, `tests/stages_test.py` | [stages.md](../../docs/developer/stages.md) |
| contribute and combine specs, `chain` | `aggregation.py`, `examples.py` | [aggregation.md](../../docs/developer/aggregation.md) |
| templates, lookups, rules | `rules.py` | [rules.md](../../docs/developer/rules.md) |
| `apply`, backlog, reprocess, retry, the trigger loop, the batch table | `batch.py`, `tests/batch_test.py` | [rules.md](../../docs/developer/rules.md) |
| both execution shapes, the data store | `launcher.py`, `runner.py`, `datastore.py` | [records.md](../../docs/developer/records.md#where-runs-execute-and-where-data-lives) |
| test helpers for workflow packages | `testing.py` | [workflow-contract.md](../../docs/developer/workflow-contract.md#test-helpers) |
| real workflows bound to the framework | `loki.py`, `amor.py` | [open-issues.md](../../docs/developer/open-issues.md#what-binding-real-workflows-found) |

## A session on real data

`notebooks/loki-session.ipynb` tells one LoKI@Larmor session on the esssans tutorial files: pick a background run from a folder dataset source, compute the beam centre as its own record, feed it to the I(Q) reduction as a reference, move the Q binning on a slider under the label `iofq` so that the session stages the reduction over it and a rerun takes a quarter of a second rather than three, fork the plot into a second label, and read the provenance back to the dataset references. `notebooks/loki-batch.ipynb` reduces four samples from the same files as a batch, and each half starts with a `for` loop over `client.run` and breaks it. By hand: the loop cannot say which values a person chose once the default changes, so the batch becomes a template and a pandas frame of pinned values, as an ISIS batch file would. Automatically: the loop's memory of what it fired on is lost on a restart, so the loop becomes a query over the records, and what is left of it becomes a rule that selects sample runs by a journal's run role, fills each one's transmission run as the nearest before it, and fires as runs arrive. It shows the backlog, a correction of one member, and a reprocess under a new template version that keeps the pinned value.

The specs are in `ess.apps.loki`, which needs the tutorial files and the `loki` extra (`pip install -e "packages/essapps[loki]"`), which brings in esssans.

## Tests

```
pip install -e "packages/essapps[test]"
pytest packages/essapps/tests
```
