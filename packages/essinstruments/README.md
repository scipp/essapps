# essinstruments

Specs and bindings of the reductions of ESS instruments, one subpackage per instrument (ADR 0007).
`ess.instruments.loki` holds LoKI's specs in `specs.py` and their bindings in `bindings.py`. The bindings need the extra `[loki]`.

[docs/loki-reduction.ipynb](docs/loki-reduction.ipynb) runs LoKI's specs on `local()`, on the runs of the LoKI detector test.
[docs/loki-batch.ipynb](docs/loki-batch.ipynb) reduces several samples from a table as a batch, in a notebook and from the command line.

## The LoKI application

`pip install 'essinstruments[loki-app]'` adds Panel, plopp, and esssans. [docs/loki-app.ipynb](docs/loki-app.ipynb) starts the application in a notebook, on `local()` over a folder of the esssans tutorial files.
To serve it in a browser, pass Panel a function that builds the app. Each session then builds its own app, which polls the client from a periodic callback of that session. An app built before `pn.serve` is never polled:

```python
import panel as pn

from ess.apps import FolderFiles, local
from ess.apps.panel import App
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.plots import PLOTS

pn.extension('tabulator')
files = FolderFiles('/data/loki-test', table)  # a row per file, as in the notebook
client = local(proposal='loki-test', files=files, bind=BINDINGS)
pn.serve(lambda: App(client, plots=PLOTS).panel(), port=5006)
```

## The LoKI batch page

`python -m ess.instruments.loki.web FOLDER` serves the batch page of I(Q) (`ess.batch.web`) on a workspace folder.
`python -m ess.instruments.loki.arrivals` serves it on a new folder into which the tutorial runs arrive over time.
The [README of the page](../essbatch/src/ess/batch/web/README.md) explains how to use both.
