# essinstruments

Specs and bindings of the reductions of ESS instruments, one subpackage per instrument (ADR 0007): `ess.instruments.loki` holds LoKI's, `specs.py` with the specs and `bindings.py` with their bindings, which need the extra `[loki]`.

[docs/loki-reduction.ipynb](docs/loki-reduction.ipynb) runs LoKI's specs on `local()`, on the runs of the LoKI detector test.

## The LoKI application

`pip install 'essinstruments[loki-app]'` adds Panel, plopp, and esssans. [docs/loki-app.ipynb](docs/loki-app.ipynb) starts the application in a notebook, on `local()` over a folder of the esssans tutorial files and a table that gives each file's metadata.
To serve it in a browser, give Panel a function, so that each session builds its app and the app polls the client from a periodic callback of that session. An app built before `pn.serve` is never polled:

```python
import panel as pn

from ess.apps import FolderFiles, local
from ess.instruments.loki.app import LokiApp
from ess.instruments.loki.bindings import BINDINGS

pn.extension('tabulator')
files = FolderFiles('/data/loki-test', table)  # a row per file, as in the notebook
client = local(proposal='loki-test', files=files, bind=BINDINGS)
pn.serve(lambda: LokiApp(client).panel(), port=5006)
```

A sample run is a row of the batch. Its file's `sample` metadata names the sample and pairs the run with the transmission run of the same name.
