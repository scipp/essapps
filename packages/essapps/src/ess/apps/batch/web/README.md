# The batch page

`ess.apps.batch.web` shows each batch of a workspace (`ess.apps.batch.BatchManager`) as a web page: the rows with their status, the settings, the runs, and the results as a plot.
One Python process holds the workspace, the batches and the server; the browser holds only what it shows.
The page of a batch is at `/batches/<label>/`.

## Running it

```sh
sh packages/essapps/src/ess/apps/batch/web/static/vendor.sh   # once: the page's libraries into static/vendor/ (needs npm)

python -m ess.apps.batch.web.demo [FOLDER] --port 8000                  # fake runs, fake I(Q)
python -m ess.apps.batch.web.demo --arrive 10 [--count N]               # fake runs, a sample arriving every 10 s
python -m ess.instruments.loki.web FOLDER --port 8000                   # LoKI's I(Q) on a workspace folder
python -m ess.instruments.loki.arrivals [FOLDER] --arrive 15 [--count N] # LoKI's I(Q), runs arriving in a new folder
python -m ess.instruments.loki.arrivals --follow 2                      # the same, following the files, a step every 2 s
```

Open `http://localhost:8000/`: it redirects to the page of the batch that follows, else to that of the only batch; otherwise it shows the list of batches, to open one or make one (see [Making a batch](#making-a-batch)).
Each `FOLDER` is a workspace folder (`ess.apps.folder`); the demos make a temporary one if none is given, and say which.
At a start, the demo and the LoKI page make a batch of the rows their source gives if no batch is listed, so also at a start after a person removed the last one; the batches and the batch that follows are kept in the folder.
The demo needs the extra `essapps[web]`, the LoKI modules `essinstruments[loki-app]`.
The LoKI page reads the masks from a record, so reduce the detector and the masks on the folder first, with two `essapps-batch` commands that [the docstring of `ess.instruments.loki.web`](../../../../../../essinstruments/src/ess/instruments/loki/web.py) gives; the LoKI arrivals do this themselves.
The two commands make the batches `detector` and `masks`, so the LoKI page then makes no batch of I(Q): make it with **+ New batch**.
With `--arrive`, the runs of one sample (fake demo) or one run (LoKI) arrive every `SECONDS`, until `--count` have arrived. Turn on **Follow** to add the rows they fill and reduce them; **Rescan runs** lists them in the Runs view and in the choices of a cell, and changes no row.
Each page reads `files.csv` again when it is replaced (`ess.apps.folder.ArrivingFiles`), so a run registered by hand, with `ess.apps.folder.write_files_table`, shows too.
The demo and the LoKI page take `--interval SECONDS`, the time between two steps of Follow (5).
The LoKI arrivals take `--follow [SECONDS]`, which turns the switch of their one batch on from the start, with a step every `SECONDS` (5).

## The table

The rows of the page are the table of the batch (`Batch.frame()`): one row per key, a column per parameter that each row sets, and the other columns as notes, shown as text.
A row is shown by its note `name`, else by its key.
Each change of rows on the page, an edit of cells, **+ Add row**, **Remove selected** and **Load table**, makes the new table and puts it to the batch (`Batch.put`); what the batch refuses is said, and nothing changes.
A run chosen in a cell is kept as the text of its file (`table.cell_of`).
**Load table** replaces the whole table, its columns too; the page then shows the cells and the settings form of the columns loaded.
**Download as CSV** and **Download as XLSX** give the table with the results of its rows (`frames.with_results`).

## Settings and Follow

The settings form holds a change until **Apply** writes it to the batch; **Discard** shows the batch's settings again.
While a person has changed the form and not applied the change, the bar above it reads "Changed, not applied yet", and the rows still read as with the batch's settings.
Apply is refused while the form misses something, and the bar says what; it also names the settings kept that the form cannot show, which Apply replaces.
While the form holds a change, **Reduce**, the ways of its menu, **Reduce them again** and **Download settings** are refused, so that nothing is reduced or saved with settings other than those shown.
**Load settings** and "Put these settings in the form" in the row tab fill the form, after a confirmation if it holds a change; a file that cannot be read whole leaves the form as it was.
A change of the settings applies to the rows not reduced yet.
A row reduced before stays `done`, marked "earlier settings", and **Reduce** leaves it (`ess.apps.batch.left_to_reduce`).
**Reduce them again** in the Settings tab reduces those rows (`earlier_settings`), after a confirmation that names how many.
To try settings on one row, change them, **Apply**, select the row, and choose **Reduce selected** in the menu ▾ next to **Reduce**.
The row tab then shows what the row was reduced with.
To keep the settings, press **Reduce them again**; to drop them, change them back and **Apply**, and the row tried reads "earlier settings".
With Follow on, the settings applied for the trial are also those of the rows that Follow reduces meanwhile, such as the rows of runs that arrive.
The page sends its actions in the order a person did them, so a value typed and Apply clicked at once are applied with one click.
Each state replaces the form, but not while the focused input holds a value typed and not sent; Escape puts back the value sent last.

**Follow** is a switch next to **Rescan runs**.
Turning the switch on makes this batch the one that follows, and no other (`BatchManager.follow`); the page of another batch then says which batch follows.
The manager refuses a batch that was not made from a source, and the page says why.
While a batch follows, a timer in the server runs a step every `interval` seconds (5 by default; `Pages(manager, interval=...)`, or `--interval SECONDS`), under the lock of every request, with or without a page open.
A step is `BatchManager.step`:

```python
done = manager.step()  # a Step, None if no batch follows
done.refused           # why a row could not be submitted, by key
done.waiting           # the keys of the rows of the source not filled yet
```

It adds the rows of the source that the batch has not considered and that are filled, and reduces the rows that are ready and have no result (`ess.apps.batch.not_reduced`).
A row that waits for a run is added at the first step after the run arrives; the page of the batch that follows says how many rows wait.
A row that a person removed is not added again.
A step changes no row that the table has: a run that arrives later, or a correction in the Runs view, leaves the cells as they are.
Follow reduces with the batch's settings, whatever the form holds.
A row with a result waits for **Reduce**, also one that is outdated or failed.
A row that a person adds, or edits before it has a result, is reduced by the next step once it is complete.
A correction is an action of the workspace, made in the Runs view of any page, also of the page without a batch (`POST api/runs/cells`).
An action submits only what a person asks to reduce; the next step reduces what the rule gives after it. Apply submits nothing, and the next step reduces the rows without a result with the settings applied.
Apply forgets why Follow could not reduce a row, since the next step tries again.
The page of the batch that follows shows "Following, every N s", how many rows wait, why the last step failed if it did, and, as an `incomplete` row, why a row could not be submitted.
Whatever a step raises, from the files, the workflow or the workspace, is the step's failure, said on the page of the batch that follows; the next step tries again.

## The list of batches

The view **Batches**, next to the rows and **Runs**, lists every batch of the workspace: its title, label and description, and which batch follows; the batch of the page is marked.
Each title is a link to the page of its batch.
Opening the view, and **Count again**, counts the rows of each batch by status (`Pages.counts`).
The statuses are those of the chips of each batch's page, the rows that Follow could not submit `incomplete` too.
A count reads the state of every batch under the lock of all pages, so the pages and Follow wait until it ends.
A batch that cannot be counted, such as one whose table cannot be read, shows why in its row.
No poll counts, so the counts shown are those of the last count; a count that fails shows none.
The list and the Runs view are the same on every page.

## Making a batch

**+ New batch** in the Batches view opens a form above the list:

| Field | What it is |
|---|---|
| Title, Description | the header of the batch; the title is required |
| Source | a source that the application registers (`BatchManager(sources=...)`), or "none: no rows" |
| Workflow | a spec that the workspace binds and that has a parameter for each column that the source fills; any spec without a source |
| Columns | the columns that the source fills, fixed; the other parameters of the spec whose cell the page edits, to tick (a group of fields, such as a Q binning, stays a setting) |

**Make** sends the form to `POST api/batches`, and the page goes to the page of the new batch.
With a source, the batch holds every row that the source has filled now, with an empty column for each column ticked that the source does not fill; Follow adds the rows that wait and those that arrive later.
Without a source, the batch holds no rows and the columns ticked; rows are added on its page or by **Load table**.
In code:

```python
label = manager.create(spec, title=title, source=source,
                       choose=lambda given: given.assign(thickness=None))
```

The choices come from `GET api/sources`; what can be made is decided by `BatchManager.create`, and what it refuses, such as an empty title, shows at the form, with nothing made.
**Make** is disabled while it is sent, so a double click makes one batch.
The new batch starts with the settings that the application gives its spec (`BatchManager(settings=...)`).
A source that no spec fits is not offered.

The form is closed until **+ New batch** is pressed.
`/` serves the page without a batch if no batch follows and there are none or several: the Batches view, with the form open if there is no batch, and the Runs view.
It loads both once and polls nothing; making a batch goes to its page.
`/` answers with `Cache-Control: no-store`, so that Back and a `/` opened again ask the server, which redirects once one batch follows or there is one batch.
The page of a batch that cannot be shown, such as one whose table cannot be read, says why, and shows the Batches and Runs views; the form is open only if there is no batch.
So does an open page of a batch removed meanwhile, at its next poll, which the server answers 404 ("There is no batch X.").

## Changing and removing a batch

The tab **Batch**, next to **Result**, **Settings** and the row tab, shows the header of the batch of the page.
Its workflow, columns and label are shown as text.
Its title and description are a form, which **Save** sends to `POST api/batch` (`BatchManager.change`).
What the manager refuses, such as an empty title, shows at the form, and nothing changes.

**Remove this batch**, after a confirmation, removes the header, the settings and the table of the batch (`POST api/batch/remove`, `BatchManager.remove`); its results stay in the workspace.
If the batch follows, none follows from then on.
The page then goes to `/`, which redirects to the batch that follows or the only batch left, or shows the page without a batch.

## Layers

```
static/           the page: index.html, app.js, tables.js, form.js
                  holds what is shown: selection, filter, tab, sort, axes
   │   GET batches/<label>/api/state?after=<version>     POST .../api/cells, .../api/reduce, ...
   ▼
api.py            HTTP: create_app(pages); each action is a POST that
                  answers with the new State (models.py), 422 {message}, or
                  404 for a label that is no batch
   ▼
pages.py          Pages: what the pages share, without HTTP: the manager,
                  the step of Follow (tick), the corrections of runs, the list of
                  batches and its counts, the runs, making and removing a
                  batch, one lock around every request and step
page.py           Page: what the page of one batch does, without HTTP: the
                  rows (each change a frame put to the batch), the settings
                  form (a draft until Apply), the refusals, its header
   ▼
ess.apps.batch    the logic: BatchManager, step, Batch, table, states,
                  CorrectedFiles; shared with the command essapps-batch
```

`/docs` on a running server lists the API.

## How the page learns of changes

The page asks for `GET api/state?after=<version>&start=<start>`, relative to its own URL `batches/<label>/`, every second.
The server answers 204 if nothing changed since `version`, and else the whole `State`.
`version` counts each action, each step of Follow that changes what the page shows (the rows, the runs, the records, the refusals, or its failure), each change of the batch that follows, and each change of the workspace's records, such as a reduction that finished.
`start` is new at each start of the server; a page that sees another one loads again.
`State.runs` counts the changes of the runs, which all pages share; the Runs view asks for `api/runs` when it moves.
`State.batches` lists the batches, so the version moves when the headers of the batches or the batch that follows change, whoever changed them; only the page without a batch asks for `api/batches`.
`api/batches`, `api/batches/counts`, `api/sources`, `api/runs` and the corrections of runs (`api/runs/cells`, `api/runs/cells/revert`) belong to the workspace: they answer the same under any label, and at `/api/...`.

## How the page asks

One function of `app.js` makes every request of the page, other than the poll.
It answers the body, or shows the message of the failure (the server's `message` for a 422, "The server answered 500.", or that the server does not answer) and answers nothing.
It also drops an answer when a newer request of the same kind was sent (`/api/row`, `/api/runs`, `/api/plot`), or when what it was for changed, such as a row that is no longer the only one selected.
An older answer of the poll is dropped by its `version`.
Actions wait for each other in a queue, so that the server gets them in the order a person did them.
The keys of many rows (a plot, the results as a zip) go in the body of a POST, since a URL has a limit.
The page asks for `api/...`, relative to its own URL, and `/` redirects by a relative URL, so a parent app can mount it under a prefix, and the answers are gzipped for a client that accepts it.
Starlette does not run the lifespan of a mounted app, which owns the timer of Follow, so the parent runs it:

```python
batch = create_app(pages)
parent = FastAPI(lifespan=lambda _: batch.router.lifespan_context(batch))
parent.mount('/batch', batch)
```

## Where the definition is kept

The headers, the tables, the settings, the batch that follows, and the corrections of runs are the definitions of the workspace.
The demo and the LoKI page give the manager and the file source the folder's definitions, `FOLDER/definitions/` (`ess.apps.batch.FolderDefinitions`), so a restart shows the batches, their definitions and the batch that follows again.
The settings form is a draft of the batch's settings: **Apply** writes it to the batch, and so to the definitions. A draft not applied is lost when the server stops.
Results are records of the workspace, in `FOLDER/store/`.
See [Where the definitions are kept](../../../../../docs/batch.md#where-the-definitions-are-kept).

## Limits

- One process holds the folder. `essapps-batch` opens it only while the server is stopped; its `reduce` imports what it is given into the label's definitions, which the page shows at its next start.
- One `Batch` per label. Another one on the page's label writes its whole table at each change, over that of the page ([Where the definitions are kept](../../../../../docs/batch.md#where-the-definitions-are-kept)).
- One user. Two open pages of one batch show and edit the same state.
- One lock for all pages: a request waits while a step of Follow runs on another batch.
- A batch that cannot be shown, such as one whose table cannot be read, has no tab **Batch**, so it is removed only in code.
- The confirmations are the browser's dialogs (`confirm`).

## Browser tests

```sh
cd packages/essapps
python -m pytest tests/batch/web_browser_test.py   # in Chromium
python -m pytest tests/batch/web_test.py           # the API, without a browser
```

The browser tests need Playwright, a Chromium (`playwright install chromium`, or `chromium` on the `PATH`), and the libraries of `vendor.sh`.
Without one of the three they skip, and with the environment variable `CI` set they fail.
They do not cover loading an XLSX table, an edit of several rows in the Runs view, or the messages for a wrong settings file.
