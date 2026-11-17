# The batch page

`ess.apps.batch.web` shows a batch (`ess.apps.batch.Batch`) as a web page: the rows with their status, the settings, the runs, and the results as a plot.
One Python process holds the workspace, the batch and the server; the browser holds only what it shows.

## Running it

```sh
sh packages/essapps/src/ess/apps/batch/web/static/vendor.sh   # once: the page's libraries into static/vendor/ (needs npm)

python -m ess.apps.batch.web.demo [FOLDER] --port 8000         # fake runs, fake I(Q)
python -m ess.apps.batch.web.demo --arrive 10 [--count N]      # fake runs, a sample arriving every 10 s
python -m ess.instruments.loki.web FOLDER --port 8000          # LoKI's I(Q) on a workspace folder
python -m ess.instruments.loki.arrivals [FOLDER] --port 8000   # LoKI's I(Q), runs arriving in a new folder
```

Open `http://localhost:8000/`. Each `FOLDER` is a workspace folder (`ess.apps.folder`); the demo and the arrivals make a temporary one if none is given.
The demo needs the extra `essapps[web]`, the LoKI modules `essinstruments[loki-app]`.
The LoKI page reads the masks from a record, so reduce the detector and the masks on the folder first, with `essapps-batch` (see the docstring of `ess.instruments.loki.web`).
In the LoKI arrivals demo, a run arrives every `--interval` seconds (15); in the fake demo with `--arrive`, the runs of one sample arrive every `SECONDS`, until `--count` samples have arrived. Press **Rescan runs** to see them, and **Reduce** to reduce the rows they complete.
Both read `files.csv` again when it is replaced (`ess.apps.folder.ArrivingFiles`).

## Layers

```
static/           the page: index.html, app.js, tables.js, form.js
                  holds what is shown: selection, filter, tab, sort, axes
   │   GET /api/state?after=<version>     POST /api/scan, /api/cells, /api/reduce, ...
   ▼
api.py            HTTP: create_app(batch, ...); each action is a POST that
                  answers with the new State (models.py), or 422 {message}
   ▼
server.py         Server: what the page does, without HTTP; holds the Batch,
                  the settings form, the corrections of runs, the refusals;
                  one lock around every request
   ▼
ess.apps.batch    the logic: Batch, RowTable, states, CorrectedFiles;
                  shared with the Panel view and the command essapps-batch
```

`/docs` on a running server lists the API.

## How the page learns of changes

The page asks for `GET /api/state?after=<version>&start=<start>` every second.
The server answers 204 if nothing changed since `version`, and else the whole `State`.
`version` counts each action and each change of the workspace's records, such as a reduction that finished.
`start` is new at each start of the server; a page that sees another one loads again.
`State.runs` counts the changes of the runs; the Runs view asks for `/api/runs` when it moves.

## Where the definition is kept

The edits of rows, the settings, and the corrections of runs are the definition of the batch.
The demo and the LoKI page give the batch and the file source the folder's definitions, `FOLDER/definitions/` (`ess.apps.batch.FolderDefinitions`), so a restart shows the definition again; without definitions it is lost when the server stops.
The settings form is a draft of the batch's settings: a change that leaves nothing missing is written to the batch, and so to the definitions.
Results are records of the workspace, in `FOLDER/store/`.
See [Where the definition is kept](../../../../../docs/batch.md#where-the-definition-is-kept).

## Limits

- One process holds the folder. `essapps-batch` opens it only while the server is stopped; its `reduce` imports what it is given into the label's definitions, which the page shows at its next start.
- One `Batch` per label. Another one on the page's label writes its whole table of edits at each change, over those of the page ([Where the definition is kept](../../../../../docs/batch.md#where-the-definition-is-kept)).
- One user. Two open pages show and edit the same state.

## Browser tests

```sh
cd packages/essapps
python -m pytest tests/batch/web_browser_test.py   # in Chromium
python -m pytest tests/batch/web_test.py           # the API, without a browser
```

The browser tests need Playwright, a Chromium (`playwright install chromium`, or `chromium` on the `PATH`), and the libraries of `vendor.sh`.
Without one of the three they skip, and with the environment variable `CI` set they fail.
They do not cover loading an XLSX table, an edit of several rows in the Runs view, or the messages for a wrong settings file.
