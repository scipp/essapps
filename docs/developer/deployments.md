# Deployments: which process holds what

The terms *workspace*, *client*, *controller*, *binding*, and *history* are defined in [README.md](README.md#components).
An *application* is a program that holds a client: a controller, and a *screen* if a person uses it.
In each deployment of the first release, one Python process holds the application, its client, and the workspace:

```
 screen ──(a)──▶ application ──(b)──▶ workspace ──(c)──▶ binding
```

| Boundary | Interface | Between two processes? |
|---|---|---|
| (a) screen to application | the application's own: HTTP for a web page, the channel of the widget library for widgets | yes, when a browser draws the screen |
| (b) application to workspace | the client | no |
| (c) workspace to binding | `Binding`, `Driver` | no: 4 threads of the same process |

A screen never calls a workspace.
What a screen shows is computed in Python next to the data, and only the result is sent to the screen: a plot function returns a figure, and a form is built from the spec's params model.
So the client is an interface for Python code, and the HTTP interface of a page belongs to that page.

## Notebook

```
 VISA machine or laptop, one process
 ┌───────────────────────────────────────────────┐
 │ notebook cells                                │
 │ Client                                        │
 │ Workspace      local(...): history in memory  │
 │                open_folder(directory): folder │
 │   4 threads run the bindings                  │
 └───────────────────────────────────────────────┘
```

One process holds the cells, the client, and the workspace.
An application of widgets in a notebook is the same case: its controller runs in the notebook's process, and the browser only draws the widgets.
This deployment is implemented.

## Technique application with a batch page

```
 browser                     VISA machine, one process per folder
 ┌─────────────┐   HTTP      ┌───────────────────────────────────────────────┐
 │ page:       │ ──────────▶ │ web server     rows, edits, settings (memory) │
 │ selection,  │  the page's │ batch logic, Client                           │
 │ filter, tab │  own API    │ Workspace      open_folder(directory)         │
 └─────────────┘             │   4 threads run the bindings                  │
                             └───────────────────────────────────────────────┘
                               folder: files.csv  log.jsonl  store/<record>/<output>.h5
```

The page holds only what it shows, and every rule is in the Python process.
The command `essapps-batch` opens the same folder when the application is not running.
Two users of one proposal have two folders.
The batch logic (`ess.apps.batch`), the command, a view in Panel, and the batch of LoKI (`ess.instruments.loki.batch`) are implemented.
The web page (`ess.apps.batch.web`) is experimental and runs as a demo.

## Automatic reduction

```
 DMSC server, one process per instrument
 ┌──────────────────────────────────────────────────────────────────┐
 │ loop: for each new file, submit what the file needs              │
 │ page to configure the reduction                                  │
 │ configuration: per instrument, adjusted per proposal             │
 │ a Client and a Workspace per proposal: history and store on disk │
 │   threads run the bindings                                       │
 └──────────────────────────────────────────────────────────────────┘
      │ reads new raw files                │ writes result files, creates
      ▼ through a file source              ▼ derived datasets in SciCat
```

An instrument has one stream of new files, so it has one automatic reduction.
The process holds a workspace for each proposal that has new files.
It runs with no page open, and users find its results through SciCat, not by connecting to it.
A scientist who needs a variant of the automatic reduction runs a batch.
Of this deployment, only the loop exists (`TriggerLoop`, [automatic-reduction.md](automatic-reduction.md)).

## One proposal, several histories

A workspace holds one proposal.
A proposal has several workspaces, each with its own history: one in the automatic reduction, and one for each folder or notebook of a user.

```
 automatic reduction        user A's batch folder        user B's notebook
   history 1                  history 2                    history 3
       └───────────── a value passes only as a file ───────────┘
```

A reference names a record of the same history.
So a batch of user A cannot reference the beam centre that the automatic reduction computed, and two users of a proposal do not see each other's records.
A value reaches another history as a file:

```
 record's output ──▶ written ──▶ named ──▶ published
                                               │
 record in another history ◀── loaded ◀── listed
```

| Step | What happens |
|---|---|
| written | a writer turns an output of a type into a file of a format, such as NXcanSAS for a SANS curve |
| named | the file gets a folder and a name |
| published | a derived dataset is created in SciCat, with its input datasets and software |
| listed | a file source lists the file with a kind, such as `mask` |
| loaded | a spec reads the file and gives the value as an output |

The inputs that instrument scientists prepare for later proposals, such as masks and calibrations, take the same path.
Parts of the path exist: a writer for HDF5 (`ess.apps.batch.export`), a file source that lists such files (`ess.instruments.loki.files`), and a spec that loads mask files (`MASKS`).
No value takes the whole path yet, and the path has no design ([README, open question 11](README.md#open-questions)).
