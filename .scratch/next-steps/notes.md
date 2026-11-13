# What to do next: living notes

Started 2026-11-12, on `main` at 5d59a28 (after PRs #180 and #182).
Comment with `> Simon:` lines. Each worry ends with one.

## The question (Simon, 2026-11-12)

> I'd like your opinion on what to do next here. We have a bunch of open issues, but I doubt that many of them are a priority now -- I am more worried about fully settling the high-level design and interfaces, ensuring we have an idea about all the components we need, and how they should assemble. I am sure there are still gaps and unknowns, as well as components/interfaces that are too complicated or not well-designed yet. Not looking for a quick answer. Find things to worry about, ranging from having a system developers can understand to performance-engineering worries, and more. What are things we should *not* worry about now (either because there are well isolated and can be decided later, or because we settled them with a good solution). Take living notes, starting with this question.

## Method

Five independent reads by fresh Opus agents, one angle each. I read the docs, the requirements, the issues, `protocol.py` and parts of `workspace.py` myself, wrote my hypotheses down before any report arrived, and checked the claims the lists below rest on.

| Angle | Report (in this folder) |
|---|---|
| Can a new developer understand the core? | `core-understandability.md` |
| Performance and concurrency | `performance.md` (scripts `perf_*.py`) |
| The spec and binding interface, judged from LoKI | `spec-interface.md` |
| Applications: Panel app, batch toolbox, web page | `applications.md` |
| Requirements and deployments against #155 | `coverage.md` |

Marks used below: **[checked]** I read the code or ran the script. **[agent]** one agent reports it, with file and line, and I did not repeat it. **[2 agents]** two agents found it independently.

## Short answer

The core mechanism is in better shape than its surroundings. Records, the history log, the two-phase check, the change feed, the spec as two pydantic models, and the small binding protocols were each examined by at least two agents and found sound.

The worries are at the three places where the system meets something outside one Python process:

1. **The disk.** `open_folder` (PR #174) made a log and a store durable. That fixed on disk several things the design still calls open or tentative: spec identity, file identity, labels and members, and the form in which a request is stored.
2. **Other processes.** Each decided deployment is one process that holds its own workspace. A proposal therefore has several workspaces with separate histories, and the README describes one shared workspace that no decided deployment has. The reduction runs on threads inside the application's process.
3. **Other people.** One technique is bound, by one author. No real accumulator driver exists. Five questions to people outside the team, which the first release depends on, were never asked.

A fourth group is inside the application layer, which grew fast in the last week: two models of "reduce what is not reduced yet" (batch and rules), three plot contracts, two batch presenters, and application state that lives only in memory.

My recommendation is to stop adding features for a short while and settle, in this order: (0) cheap stopgaps and the outside questions, (1) a drawing of each deployment, (2) a measured spike on where the reduction runs, (3) what is durable, (4) one model for batch and automatic reduction, (5) a second technique by a second author. Details in "Proposed order".

## Rulings and state of the steps

| Date | Ruling (Simon) | Consequence |
|---|---|---|
| 2026-11-12 | Agrees with the assessment in general. Proceed with step 0 and the deployment drawing. | Branch `log-format-version-and-small-fixes`; draft `deployments.md` in this folder. |
| 2026-11-12 | Worry 6: reflectometry is a decent second technique but similar to SANS. BIFROST may be more interesting, since it exercises the large volume and the accumulator. | The "one real driver" and the "second technique" of worry 6 can be one piece of work: a BIFROST binding with a driver. It also tests `Store.read` with a selection (worry 8) and the plot contract with cuts (worry 5). |
| 2026-11-12 | Worry 7, Q1 (role of a run): unclear and being worked on; assume something, and let the user override it, as the SANS batch app does with an input table. | Run corrections and the run table are a required component, not a workaround. This raises the weight of "corrections assume the app owns the file source" (worry 4). |
| 2026-11-12 | Q2 (service creates derived datasets): assume yes. | "One process, no `connect()`" stands for automatic reduction. |
| 2026-11-12 | Q3 (automatic results to SciCat without a person): may not matter; the owner may be copied from the raw dataset, or be a special automatic-reduction account. | Publication without a person is assumed; who owns the dataset is a detail of the publisher. |
| 2026-11-12 | Q4: automatic reduction has a base configuration per instrument, with settings adjusted or workflows selected per proposal, or within one proposal. | Configuration is layered: instrument, proposal, part of a proposal. A setting can change while the service runs. This is the settings template of a batch with overrides, which fits worry 4. |
| 2026-11-12 | Q5 (deployment): not settled; Ansible, VMs, Docker exist. | No constraint yet. |
| 2026-11-12 | `deployments.md` Q1: agrees with A. An application's logic is Python that holds a `Client`; the UI never talks to the workspace directly. Simon had always assumed this. | The plot contract (step 4) is a Python function beside the data that returns a figure. Forms are built from spec classes. #88, #118, #95 serve programs, not screens. Whether the client comes from `open_folder` or a later `connect()` stays open and changes no application. |
| 2026-11-12 | `deployments.md` Q2: agrees. The file path between histories (writer per output type, layout, publish, listing, load spec) is one framework design. | One ADR; belongs with step 3 or directly after it. |
| 2026-11-12 | `deployments.md` Q3: one running automatic reduction per instrument, since there is one incoming stream of files. A scientist who needs a variant uses batch reduction. | Still to design inside it: a workspace holds one proposal, so the process routes each file to its proposal's workspace; where the layered configuration is stored. |
| 2026-11-12 | `deployments.md` Q4: (c) first, meaning the process split goes between workspace and workers. Simon's reason for keeping application and workspace together: a result moves from worker to workspace once, but may be read by the application many times. | Step 2 as planned. To check there: a stage or a driver that lives in a worker process makes each cut of a live volume cross the split, so large-volume drivers may have to stay in the workspace's process. |
| 2026-11-13 | Step 2: one workspace thread is the default for now; the optimum depends on the instrument (file sizes, file counts, processing patterns). | `workers=1` default, `workers` keyword on `local()` and `open_folder()`. |
| 2026-11-13 | Step 2: worker processes are deferred. Simon does not know whether automatic reduction will be when they are first added; "time will tell". | No worker design now. README open question 10 stays open. The `started` event (`durable.md` item 5) stands on its own and does not wait for workers. |

Tracking issue: #184 (steps as checkboxes; tick a step and link its PR there).

| Step | State |
|---|---|
| 0. Stopgaps and small fixes | PR #185 merged 2026-11-12, after a fresh review whose three findings were fixed (a refused log file is left unchanged; a stored value that does not fit reads as `NotReadable`; a field typed `Any` holding `None` reads back). Content: (log format number, stored group lacking a required field refused, `Client.wait` linear; 1666 tests pass). Fix 4 (refuse an unused pipeline parameter) not done: `PipelineBinding` accepts an unused blank on purpose (`tests/pipeline_test.py::test_a_blank_no_output_depends_on_is_accepted`, module docstring of `pipeline.py`). Patch kept in `fix4_unused_param.patch`; awaits Simon. |
| 0. #155 update | done 2026-11-12: 20 rows corrected, 8 added (batch toolbox, command, Panel view, web page, corrections, `WorkspaceProtocol`, folder workspace, settings and rows as files); † removed, ‡ kept for two rows still only on `24-sans-batch` |
| 1. Deployment drawing | done: PR #186 merged 2026-11-13 (`docs/developer/deployments.md`, short section in the README, glossary and `users.md` corrected). Step 1 ticked in #184. |
| 2. Spike: threads against processes | done 2026-11-13 (`spike-threads-processes.md`); ticked in #184. Rulings: default of 1 workspace thread, settable per application; worker processes deferred. Merged from it: #187 (LoKI workflow built once), #188 (default and `workers` keyword). PR #190 open: README open question 10 states the result. |
| 3. ADR: what is durable | Done, merged by Simon: #192 (ADRs 0009, 0010; edits to 0004, 0002) <- #193 (spec digest, author's check) <- #194 (digest in the log, record of another spec readable, `Client.bound`) <- #195 (file identity). Item 5 merged (#189). Item 4 is issue #191. |
| 4. Batch and automatic reduction converged; plot contract | input document `batch-vs-rules.md` (2026-11-13): the automatic-reduction stories written with the batch model as scripts that run (`rules_*.py`), four gaps, three options, six questions with `> Simon:` slots. Recommendation: one model, rules removed. Awaits Simon. Plot contract and "one batch presenter" not started. |
| 5. Second technique (BIFROST), real driver | not started |

## Worries

Ranked by what waiting costs. "Interface" means code or files outside one module come to depend on it.

### 1. Durable formats exist before their content is settled (interface, grows daily)

`open_folder` writes `log.jsonl` and `store/` in a user's folder. Four things in them are undecided:

| In the log or store | State of the decision | Evidence |
|---|---|---|
| spec identity: `SpecId(name, version)`, version bumped by hand | #80 proposes a digest, not implemented. LoKI changed its interface in 3 commits over 5 days, every spec is still `version=1` | [agent] `spec-interface.md` §5 |
| file identity | `FolderFiles` uses `path:/abs/...`, `FakeFiles` uses `run:` or `pid:`. The trigger loop's "not reduced twice" and SciCat input datasets depend on it | [checked] `files.py:126`; [agent] `coverage.md` W3 |
| `label`, `member` | README open question 3 calls them tentative. They are the key of a batch row, of a rule's "handled" check, and of `@latest` | [2 agents]; [checked] `batch/state.py:91-103` |
| the request, stored resolved (file names resolved, defaults filled) | the batch compares requests as given, so it keeps its own copy in memory (#179 point 7) | [checked] `records.py:264`, `batch/reduction.py:122` |

The log lines carry no format version **[checked]**, while ADR 0004 says old formats must stay readable.

One consequence is a silent wrong value today **[checked, reproduced]**:

```python
# an output persisted when BeamCentre had fields x, y; then z: float = Field(gt=0) is added, version unchanged
hdf5.read(BeamCentre, path)   # -> BeamCentre(x=0.01, y=-0.02, z=None)   no error
# reason: hdf5._typed uses model_construct (hdf5.py:81), and the call phase passes the instance on
```

#80, #175 and #177 are one problem: a record's spec is known only through a binding that is loaded now.

Cost of waiting: every folder written adds data that a later decision must read or migrate.

What settles it: one ADR "what is durable", covering the four rows and a version field. Until then a stopgap takes an hour: write a format version as the first log line, refuse a folder of another version with a message, and state in `batch.md` that a folder does not outlive an upgrade. That makes the four decisions cheap again, and they can be taken without hurry.

> Simon:

### 2. No drawing says which process holds what (design gap)

The decided deployments (2026-11-05), for one proposal:

```
 automatic reduction (DMSC host)     batch app (VISA, user A)            notebook (VISA, user B)
 ┌─────────────────────────────┐    ┌──────────────────────────────┐    ┌──────────────────────┐
 │ trigger loop, frontend       │    │ browser ──HTTP── web Server   │    │ notebook code        │
 │ Client                       │    │ Batch, Client                 │    │ Client               │
 │ Workspace: log + store on    │    │ Workspace: open_folder,       │    │ Workspace: local(),  │
 │   disk, 4 worker threads     │    │   log + store, 4 threads      │    │   memory only        │
 └─────────────────────────────┘    └──────────────────────────────┘    └──────────────────────┘
        history 1                           history 2                          history 3
          └──────────── files and SciCat: the only path between them, not implemented ───────────┘
```

Three consequences, none written down:

- **The README's model is not the deployed one.** It says "every client of the proposal's workspace reads them" and "on the service, other notebooks and programs too". With three histories, a reference from one workspace to a record of another is refused (or raises a bare `KeyError`, #175). The beam centre that automatic reduction made cannot feed user A's batch. **[agent]** `coverage.md` W4
- **The way out and back in is a core path with no design.** A value passes between workspaces only as a file that a file source lists (story C2, xfail). That path is: writer per output type (#176) → layout on disk → publish (#152) → the file appears in a file source → a load spec reads it. Publication is on the critical path of the first release twice: the app "uploads to SciCat", and users find automatic results only through SciCat (#151). It also needs a record's file identities to become SciCat input dataset identifiers, which no document mentions. A workflow whose product is a file (NXcanSAS, ORSO, SQW) has no place in an outputs model either. **[agent]** `spec-interface.md` worry 7. I think this is the largest component group that nobody has designed.
- **Three process boundaries exist, and the design effort went to the one no deployment uses.**

  ```
  browser ──(a)──▶ application ──(b) WorkspaceProtocol──▶ workspace ──(c) Binding──▶ worker
  ```

  (a) exists since #180 and is specific to one page, which is fine **[agent]**. (b) is designed to be remote (`connect()`), and every decided deployment has it in one process. (c) is a thread pool. I expect (c) to be the first boundary that reality forces (worry 3), and it has the least design.

A question follows that I think should be decided explicitly: **does an application always run next to its workspace?** A Panel or ipywidgets app runs in a kernel, the web page has a Python server, the trigger loop is a process. If yes, then a plot function runs next to the data and only a figure reaches the screen, forms are built in Python from the spec classes, and several "what a remote UI needs" worries of the performance report (specs as data, coordinate values in `describe`, plots over the wire) concern only notebooks and agents that call a service, which can wait with `connect()`.

What settles it: a page in the developer README with one drawing per deployment, replacing the single components drawing, and a decision on the question above.

> Simon:

### 3. Reductions run on threads inside the application's process (needs a measurement)

`Workspace` runs bindings on `ThreadPoolExecutor(4)` in the process that also runs the notebook, the Panel app, the web server, or the trigger loop **[checked]** `workspace.py:560`.

Measured by the performance agent with toy reductions (`perf_convoy.py`, `perf_gil.py`) **[agent]**:

| 100 toy reductions | alone | with one busy Python thread in the process |
|---|---|---|
| kept in memory | 0.25 s | 3.99 s |
| persisted (one HDF5 write each) | 1.45 s | 58.3 s |

The busy thread is synthetic. The mechanism is real: a worker that releases the GIL for I/O waits up to 5 ms to get it back while Python code runs elsewhere. Also measured: h5py reads of compressed data gain nothing from 4 threads (1.02×), since h5py has one lock per process.

Not measured by anyone: a real LoKI reduction on threads against processes. #151 lists "does one process keep up?" as open. Besides speed, a 24/7 service that runs reductions in its own process shares their memory peaks and their crashes.

Good news **[agent, 2 reports]**: `Binding.compute(params) -> outputs` takes and returns values, so moving work to another process changes no interface of a workflow package. Bad news: every path of `workspace.py` assumes one dict of values in its own memory, and scipp values cannot be pickled, so values cross a process boundary as files.

A mode that looks cheap to me, to be tested: batch and automatic reduction persist everything they submit. For such requests a worker process can read what the request references from the store and write its outputs to the store, and the workspace process holds no value at all. Values kept in memory, stages and accumulators stay on threads, for interactive work. The batch's `staged=True` would then need a stage per worker, which #27 calls warm workers.

What settles it: a spike that measures one real LoKI reduction with 1 thread, 4 threads, 4 processes, and 4 threads next to a polling web page. It should come before more scheduling code is written, because its result says whether the value-holding part of `workspace.py` survives.

> Simon:

### 4. Batch and automatic reduction are two models of one task (convergence)

Both answer "which runs still need a reduction, paired with which other runs".

| | batch (`ess.apps.batch`) | rules (`ess.apps.rules`) |
|---|---|---|
| pairing | `RowSource`: a pure function from file metadata to rows; LoKI's is 25 lines | inside `submit(client, file)`, with lookups through the client |
| done already? | newest record by label, member, spec, submitter | a record under the label that holds the file at parameter `at` |
| not ready (no transmission yet) | the row has an empty cell | the function raises `LookupError` |
| after a failure | the row is "remaining" | the file counts as handled (#68) |
| a chain per run (centre, then I(Q)) | not possible: one spec per batch | the function submits both, and must reuse records of a call that stopped half-way |
| used by | CLI, Panel view, web page, LoKI | tests only; no instrument has a rule **[checked]** |

Simon's own description of the web page was "main candidate: auto-reduction, close to batch reduction", and ess-sans-widgets has Manual, Smart and Auto as three sources of rows for one table (#162).

My view: automatic reduction is a batch whose scan repeats on a timer and which reduces the rows that are ready and remaining. If that holds, `Rule` and `TriggerLoop` go, the automatic-reduction frontend is the batch page, and #68 and "rule status readable from another process" become properties of rows. What the batch lacks for this is a chain per row, which #162 needs anyway for a centre per sample. The coverage agent judged the trigger loop sound because it is tested; I disagree on the grounds above.

In the same group, all inside the application layer:

- **A batch has no identity of its own** (the label convention above), and **its definition lives in memory**: rows, edits, settings, corrections, what each row was submitted with. After a restart every row reads as not stale. **[2 agents]** Question from the application report, which I pass on: is a batch something the workspace knows (an ID, its template, its rows), or a convention over labels?
- **Two batch presenters** that disagree on five behaviours (`applications.md` W1, table). `batch/web/server.py` needs no widget library and no HTTP, so it already is the shared UI logic that #150 asks for. `batch/panel/view.py` is fused with Panel.
- **Run corrections** wrap the file source that the application gave to its own workspace (`CorrectedFiles`). That works only while the application builds the workspace.

What settles it: write the example of `automatic-reduction.md` (`reduce_sample`: centre, then I(Q), transmission may be missing) as batch rows and list what is missing. Decide the identity question together with worry 1. Delete `batch.panel`, or reduce it to drawing the `State` of the one presenter.

> Simon:

### 5. Three plot contracts, written against by technique packages (interface)

| Where | A plot is | Written by |
|---|---|---|
| `App(plots=)` | function from the outputs model to a plopp figure | `loki/plots.py`, 94 lines |
| `BatchView(draw=)` | function from records to a Panel object | the demo |
| web `create_app(draw=)` | function to a Plotly dict; reads outputs whole | the demo |

**[agent]** `applications.md` W5; **[checked]** `loki/plots.py`, `batch/web/plots.py`.

Simon flagged plots and slicing as "very important" in `architecture-boxes.md`, and #149 asks what a plot function returns. The answer depends on worry 2: if the application runs next to the workspace, a plot function runs next to the data and may return any figure object that the frontend can show (a plopp figure rendered to an image or to JSON for the browser), and it should take cuts, not whole outputs.

Cost of waiting: each instrument package writes its plots once per frontend.

> Simon:

### 6. The evidence is one technique, one author, and toy drivers (validation)

- Nobody but Simon and Claude has written a spec, a binding, or an application. The stated goal is a design the team accepts.
- What an author must hold, counted by the agents: about 55 rules for a spec and binding, 25 of them checked by nothing; about 15 concepts for an application; about 30 concepts to change `workspace.py`.
- LoKI needs about 6 lines of glue per parameter of I(Q), and a parameter that no provider reads is accepted with no effect **[agent]** (`binding_probe.py`). The gravity default is `False` in esssans and `True` in the LoKI spec.
- **How a stage cuts the sciline graph decides how parameters split and which specs exist.** Masks became their own spec so that the can is reduced once. So a change of caching can change a spec, which then meets worry 1. Authors should know this before they write specs. **[agent]** `spec-interface.md` worry 4
- Accumulators and freeze are about 610 of the 1980 lines of `workspace.py` (31%), and `accumulator`, `freeze`, `push` have no caller outside tests. The only drivers are toys. A real one needs esssans internals and the changes #40 lists.

The claim "the architecture is known to fit accumulators" rests on toy drivers. I do not propose to remove them. I propose to get the missing evidence: one real driver (summing the runs of one LoKI sample, the first "Combining" item of `sans.md`), and until then no polishing of the accumulator interface. The core agent recommends closing the accumulator's interface gaps now (no status, not in the change feed, `provenance` fails once stopped); I would wait for the real driver, since gaps closed against toys are guesses.

A second technique by a second author tests six findings at once: glue per field, stages shaping specs, a product that is a file (ORSO), summing runs, the plot contract, and whether the docs are enough. Reflectometry has real data (Amor) and an existing batch GUI to compare with.

> Simon: I think reflectometry is a decent candidate, but somewhat similar to SANS. Bifrost might be more interesting as it may exercise the "large volume" and "accumulator" aspects.

### 7. Five outside questions were never asked (free to ask)

| Question | Decides | Ask |
|---|---|---|
| How will files or SciCat say what role a run plays? | pairing in automatic reduction; the one "question that blocks the design" in the requirements | NICOS team, SciCat team, instrument scientists |
| May a service create derived datasets for a proposal, and as whom? | whether automatic results can reach users without `connect()` | DMSC SciCat team |
| Do automatic results go to SciCat without a person? | still Open in `tensions.md`, assumed in #151 | Simon, SciCat team, instrument scientists |
| Is automatic reduction set up per instrument or per proposal? | how logs, idle time and retention are laid out | instrument scientists |
| How are such services deployed and watched at DMSC (host, restart, logs)? | whether one process with `local()` is acceptable | whoever deploys esslivedata |

If the second answer is no, the plan "one process, no `connect()`" has no way to hand automatic results to users. **[agent]** `coverage.md` W1

> Simon: Q1: unclear, being worked on, we need to make some assumption and support overriding by the user (as in the SANS batch app using an input table); Q2: I think we should assume "yes"; Q3: not sure this matters, maybe it copies the user from the raw dataset? Or gets a special auto-reduction person?; Q4: Auto-reduction will have a base config per-instrument, with settings adjusted (or workflows selected) per proposal, or even within one proposal; Q5: not settled, there is Ansible, VMs, Docker, ...

### 8. Smaller interface points that are cheap now

- **`Store.read` reads a value whole** **[checked]** (`protocol.py:480`). `describe`, `select=` and each referencing request of a persisted output load the full file: 0.14 s per `describe` and 0.15 s per slider move on 800 MB from the page cache, against 0.00 s in memory **[agent]**. Batch and automatic reduction persist everything, so all their results are read this way. Only `FolderStore` and `FakeStore` implement the protocol today. Add a selection and a describe-without-data to the protocol before a third store exists.
- **No file source sees a new file.** `FolderFiles` reads its table once **[checked]**, `watch` polls `list` every second, and the service cannot reach Kafka. A spike of a read-only source over SciCat and `/ess` on CODA answers this, #57, and half of the file-identity question.
- **A failed record cannot say what kind of failure it was** (#68). This is a field of the finish event, so it belongs to worry 1.
- **`client.output` returns the workspace's own object** (README open question 7). `da *= 2` in a notebook changes what later requests read. A decision for `local()` only.

> Simon:

### 9. Understanding the core (no interface, can wait, but cheap parts exist)

My judgement after reading the reports and the code: a developer can understand what the system does from the README and `protocol.py`. Changing `workspace.py` safely needs about 30 concepts, and the cost concentrates in two places **[agent]** `core-understandability.md` W3 to W5:

- the accumulator lifecycle: six lifecycle flags, and `stopped` is written at two places with two meanings (`workspace.py:1027`, `1630`);
- reader counting: about ten places take a count and about ten give one back, at different times. A leak fails no test.

A split into classes would not help: the groups of state share one lock and call back into each other, which agrees with the earlier measurement that three simplifications removed 11%.

What I would do:

- Treat `workspace.py` as a kernel that changes rarely, and put new needs above it. The batch model shows this works: it uses only public client calls, and a test enforces the import direction.
- A vocabulary pass. `kept` has three meanings in the file, `stopped` three, `_unread` and `_unread_values` mean opposite things, the docs say "keeper" where the code says "reader".
- A glossary that maps each coined term to the standard one, for the team:

  | Term here | Standard name |
  |---|---|
  | log and views | event sourcing: append-only events, state rebuilt by replay |
  | record | immutable job record; with its status, a future |
  | keeper, reader | owner, and reference count held by pending consumers |
  | stage | cached partial evaluation of a pipeline |
  | accumulator, push, state n | streaming fold over rows; state n = fold of the first n |
  | freeze | the fold's result registered as a record, without a copy |
  | plain request | the equivalent request without stage or accumulator |

- One test fixture that closes the workspace after each client test and checks through weak references that no value is still held.

The risk to "a kernel that changes rarely" is worry 3, which is why the spike comes early.

> Simon:

## Not to worry about now

Settled with a good solution (examined by the agents named, with the reason):

| What | Why it holds |
|---|---|
| History as an append-only log with views | An event is applied as read back from its JSON, so a live and a replayed workspace agree; restart cost is linear; a half-written last line is dropped. (core, performance) |
| Records never change; status held apart | A freeze's record is an accumulator and a push count, constant size. (core, performance) |
| Request phase and call phase; errors as plain data with a position | Refusals land on the right form field and table cell in both UIs. (core, applications) |
| The change feed: one integer cursor, IDs only | Drives the Panel app, the listing, the trigger loop, and the web page. A poll with no change costs under 0.3 ms at 1000 rows. It becomes a long poll over HTTP unchanged. (three reports) |
| Calls return once logged and take lists | A batch is one call; 120 to 670 µs per request; 5 kB per pending request; the framework's ceiling is about 2500 requests per second. (performance) |
| The client holds no rules | Every method checked. `WorkspaceProtocol` is the one seam. (core) |
| A spec is two pydantic models; `Referenced[T]`; file fields | Importable without the workflow package; tests and bindings use plain data. The language rarely blocks a SANS feature. (spec) |
| The three binding protocols | 84 lines; location-independent. (spec, performance) |
| Accumulators add in place; a read blocks the next push | A live volume costs one volume plus the cut, as ADR 0003 claims. (performance) |
| `Fields`: a form model without widgets | Two renderers use it unchanged. (applications) |
| The batch model as a layer on the client; pairing as a plain function in the technique package | No private access anywhere. (applications) |
| `describe` and `select=` for values in memory | 0.00 s on 800 MB; only the cut is copied. (performance) |

Isolated, decide later without changing an interface:

| What | Why it can wait |
|---|---|
| `connect()`: transport, lease, memory cap, auth | Deferred on 2026-11-05. No call needs a callback in the client, and the batched calls suit HTTP. Condition: the SciCat answers of worry 7 do not force a hosted path. |
| Cluster executor, Slurm, dask | After the spike of worry 3. Bindings do not change. |
| The widget library, and the JavaScript | All rules sit in Python; 1250 lines of JavaScript only draw. |
| Detector banks (#166) | Fit as structure inside a value; blocked on esssans. |
| Serialized spec schema (#88, #118) | No reader in production code. Exception: if #80 computes spec identity from it. |
| Export layout (#176) | Stays in `write_outputs`. The writer per output type is part of worry 2. |
| Computed looks, general plots, mask drawing | Features on top of `describe`/`select=`. |
| Performance fixes inside one module | `Client.wait` is quadratic (64 s for 8000 records, fix in `client.py`); the web state costs rows × files (1.75 s at 1000 rows, `web/forms.py:249`); a stage's first call holds the other workers; history held twice in memory. |
| Other techniques' sizes (spectroscopy volumes, imaging stacks) | The mechanisms were designed for them; the numbers decide the executor, later. |
| Windows, PyQt, removal of a file (A4), rerun in the original environment (F2) | No interface cost now. F2 needs software versions in records, an added field once worry 1 is settled. |
| Demos (#183), status wording, polling intervals | Cosmetic. |

## Where I differ from the agent reports

- Trigger loop: the coverage report calls it sound. I think it duplicates the batch model (worry 4).
- Accumulator gaps: the core report says close them now, while nothing depends on them. I say wait for a real driver (worry 6).
- "What a remote UI needs" (performance report, worry 2): it assumes a UI on the far side of `connect()`. Whether such a UI will exist is the question of worry 2; only the plot contract is urgent.
- The batch toolbox: the coverage report calls it self-contained. The application and core reports show it fixes label semantics and wants a change to what a record stores, so it is not.

## Proposed order

0. **Hours.** Format version in the log and a refusal message; validate a group read back from the store; fix `Client.wait`; refuse a `PipelineBinding` parameter that no output depends on; update #155 (15 rows are stale) and close the stale issues. Send the five questions of worry 7.
1. **One page.** The drawing per deployment, and the decision whether an application runs next to its workspace (worry 2). List the steps of "out as a file, back in through a file source".
2. **One spike, measured.** Real LoKI reduction on threads against processes; worker processes for persisted requests (worry 3).
3. **One ADR.** What is durable: spec identity with the stored schema (#80), file identity, batch identity and labels, the request as given, failure kind (worries 1 and 8).
4. **Convergence.** Automatic reduction written as a batch; decide on `rules.py`; one presenter; the plot contract (worries 4 and 5).
5. **Evidence.** A second technique by a second author, and one real driver (worry 6).

Steps 1 and 2 come before 3 because they decide what must be durable and where values live. Step 5 could start earlier in parallel if someone on the team has time, since it does not depend on the others and its findings feed 3 and 4.

> Simon:

## The open issues, sorted

From `coverage.md` Question 5, which I agree with except where noted.

| Group | Issues |
|---|---|
| Carry a design decision of the worries above | #80, #175, #177 (one root, worry 1); #152, #176 (way out, worry 2); #151, #68 (automatic reduction, worries 4 and 8); #57, #131 (file source); #179 point 7; #162; #149 (plot contract) |
| Features that can wait | #24, #27, #40 (but see worry 6), #88, #95, #166, #183 |
| Small fixes | #118, #140 (still reproduces), #123 (one of three parts left) |
| Stale, close after a check | #148 (merged), #61 (pending references are relied on now), #124, most of #150 |
| Split or close | #81, a mixed list |
| Out of date | #155: 15 rows, and no row for the batch toolbox, the web page, `WorkspaceProtocol`, the folder workspace |

I have not edited any issue.

## My hypotheses before the reports, and what became of them

| | Hypothesis | Outcome |
|---|---|---|
| H1 | Three process boundaries; effort went to the unused one | Confirmed, and sharpened by "several workspaces per proposal" (worry 2) |
| H2 | Batch and rules are two models of one task | Not examined by an agent as such; my own comparison (worry 4) |
| H3 | Application state has no home | Confirmed by two reports |
| H4 | plopp plot contract does not reach the web page | Confirmed; three contracts, not two |
| H5 | One author, one technique | Confirmed with counts |
| H6 | Spec versions and persisted data | Confirmed, with a reproduced defect |
| H7 | File source blocked outside | Confirmed; the questions were never asked |
| H8 | `workspace.py` hard to hold | Refined: accumulator lifecycle and reader counting, the rest is sound |

Not foreseen by me: labels as the key of everything while called tentative; file identity differing per source; `Store.read` reading whole; the stage cut shaping specs; the GIL measurement.

## What I did not verify

- The 31% figure for accumulators, the five behaviours on which the two presenters differ, and all timings are the agents' measurements. I ran only `version_probe.py`.
- No measurement used real NeXus files or a network file system.
- The generality table for other techniques (`spec-interface.md` §3) is read from the requirements pages, not tried in code.

## Log

- 2026-11-12: file created, agents started.
- 2026-11-12: `applications.md` arrived. Checked: `batch/state.py:91-103` (submitter filter), `records.py:264` (resolved request), `batch/reduction.py:122-123` (state in memory), `batch/web/forms.py:249` (linear search).
- 2026-11-12: `coverage.md` arrived. Checked: `FolderFiles` reads its table once (`files.py:117-136`), `local()` takes no log (`workspace.py:1956`), `open_folder` writes `log.jsonl` and `store/` (`folder.py:145-150`), no version field in `log.py`.
- 2026-11-12: `spec-interface.md` arrived. Ran `version_probe.py`: `BeamCentre(x=0.01, y=-0.02, z=None)`.
- 2026-11-12: `core-understandability.md` arrived. Not re-measured.
- 2026-11-12: `performance.md` arrived. Checked by reading: `Client.wait` asks the status of every record after every change (`client.py:517-524`); `Store.read` has no selection (`protocol.py:480`).
- 2026-11-12: synthesis written (this version).
- 2026-11-12: Simon's notes on worries 6 and 7 recorded as rulings. #155 updated. Tracking issue #184 opened. `deployments.md` drafted. PR #185 opened for step 0.
- 2026-11-12: correction to my step 0. I listed "refuse a `PipelineBinding` parameter that no output depends on" as needing no input. It contradicts existing, tested behaviour, so it is a decision. The stored-group fix is also weaker than "validate on read": an existing test states that a group is not validated again when read, so only a missing required field is refused.
- 2026-11-12: PR #185 reviewed by a fresh agent, fixed, CI green, merged. `deployments.md` Q1 reworded after Simon asked what it means: application logic in a Python process that holds a `Client` (A), against a screen written to the workspace's web API (B). A leaves open whether the client comes from `open_folder` or a later `connect()`. Asked Simon whether an early application must work from a laptop outside ESS or continue with the app closed; either would move the hosted workspace earlier.
- 2026-11-12: Simon answered the four questions of `deployments.md`; recorded as rulings. Still open with Simon: (1) fix 4, my proposal is an author-side check in `ess.spec.testing`; (2) whether an early application must work from a laptop outside ESS or go on with the app closed.
- 2026-11-13: step 1 written on branch `184-deployments-in-readme`: section "Deployments" in `docs/developer/README.md` (three boundaries, one drawing per deployment, "one proposal, several histories" with the file path), `connect()` described as a hosted workspace that no decided deployment needs, "the proposal's history" renamed to "the workspace's history", open questions 10 to 12 added (where bindings run, the path between histories, the automatic reduction as a process). Found while writing: user stories D and E are written against a shared service (`connect(url, proposal=...)`; in E1 and E4 a user reads automatic results with `user = connect()`). Added a note in the stories' conventions; rewriting those stories belongs to step 4. Fresh review requested. Step 2 measurement started in a background agent (report: `spike-threads-processes.md`).
- 2026-11-13: PR #186 opened for step 1. Review findings fixed before opening: the batch web page is a demo, not an implemented deployment; "a reloaded web page" is no second client; drawings aligned; repetition cut. Left for later steps and named in the PR: stories D and E against a shared service (step 4); the rule "history is dropped once the proposal is idle" in `protocol.py` and `automatic-reduction.md` (step 3); the requirements glossary defines batch reduction as a service for many users, while the first release runs a batch in the application's process (for Simon: is that a first-release limit or a changed requirement?).
- 2026-11-13: Simon on #186: "looks good", asked whether the long section belongs in the main README. Moved it to `docs/developer/deployments.md`; the README keeps a short section (rule, boundaries, table of the three deployments). Simon on the glossary: a batch-reduction service need not be a program on DMSC servers; an application the user runs on VISA works as well. Glossary entry corrected in the same PR (commits 3e921d3, 105cc15). `users.md` "run as services" (Simon, 2026-10-02) left as it is; asked whether to reword.
- 2026-11-13: Simon: "as services" refers to automatic reduction, not batch reduction; for a first release, automatic reduction as an application on VISA is also acceptable. `users.md` reworded, noted in `deployments.md` and the README table. PR #186 merged, step 1 ticked in #184.
- 2026-11-13: step 3 groundwork: `durable.md` (agent, probes `durable_*.py`): seven items with what is on disk, options, a recommendation, and `> Simon:` slots. I added `> Claude:` lines on items 1 (treat a digest mismatch as another spec; author-side digest check in `ess.spec.testing`), 3 (do not close open question 3 before step 4), 4, 5 (do the `started` event first). New findings reproduced there: a renamed output field with a default reads back as the default without a message; a moved folder makes a trigger loop reduce its runs twice; after a restart a batch shows rows as completed that ran with other settings; a reduction that kills the process runs again at every open.
- 2026-11-13: step 2 measured (agent, 55 min; report `spike-threads-processes.md`). 4 I(Q) reductions of the LoKI tutorial files, 24 cores, wall time and peak memory:

  | Configuration | Wall s | Peak GB | Source |
  |---|---|---|---|
  | plain requests, 1 workspace thread | 14.8 | 8.2 | agent, 3 runs; my rerun 14.6 |
  | plain requests, 2 workspace threads | 16.1 | 14.8 | mine, 1 run |
  | plain requests, 4 workspace threads (default) | 20.1 | 27.0 | agent, 3 runs; my rerun 19.8 |
  | one stage, 1 workspace thread | 7.9 | 4.7 | mine, 1 run |
  | one stage, 2 workspace threads | 7.3 | 8.4 | mine, 1 run |
  | one stage, 4 workspace threads (as `iofq_batch`) | 7.3 | 14.9 | agent, 3 runs; my rerun 7.4 |
  | pool of 4 worker processes, outputs as store files | 8.5 | 28.5 | agent, 3 runs |

  With 8 rows: stage on 1 thread 12.4 s at 5.0 GB, on 2 threads 10.7 s at 8.3 GB, on 4 threads 11.1 s at 15 GB; pool 16.6 s at 30 GB. Other results of the agent, not repeated by me: one busy Python thread slows a real chain 7.4 times (the toy number was 40); the batch page polling at 1 Hz does not slow reductions, and reductions raise the page's p95 latency from 4 ms to 40 ms (1 page) or 220 ms (5 pages); passing outputs as files costs 0.5 to 3 % of a request; a cut of a 1 GB volume costs 0.1 ms in process and 25 ms from another process; `LokiAtLarmorWorkflow()` takes 0.65 s under the GIL and is built about 4 times per chain (35 % of a chain's wall time); each workspace thread's sciline call starts a dask pool of 24 threads; one I(Q) peaks at 7.5 to 9 GB on files with 458,752 pixels, while LoKI has 3.2 M.
  My reading: (1) the workspace's thread pool buys nothing for a workflow that is parallel inside, and costs memory in proportion; (2) the stage's gain is the shared work, not the threads; (3) worker processes are not justified by speed, only by isolation of a 24/7 process (GIL, crash, memory), and they need stage affinity; (4) memory, not cores, bounds concurrency. Proposed: default `workers=1` (decision for Simon); build the esssans workflow once per binding (small PR, started); worker processes deferred until the automatic reduction is built, together with the `started` event of `durable.md` item 5.
- 2026-11-13: Simon on step 2: 1 workspace thread "sounds like a good default for now"; the optimum depends on the instrument (file sizes, file counts, processing patterns). He asked whether the main reason for worker processes was not to keep the UI from freezing. My answer: that was the reason given, and the measurement speaks to it for the batch page (p95 4 ms to 40 ms with 1 page, worst request 0.35 to 0.54 s, no freeze), because the heavy work releases the interpreter lock; not measured for a Panel or Qt application, for 300 rows, or for a workflow with long pure-Python stretches. To do: default `workers=1`, settable per application; measure the page's latency again with it.
- 2026-11-13: PR #187 opened: the LoKI bindings build the esssans workflow once per process and copy it per request (chain 7.0-7.5 s to 4.0-4.1 s; 4 I(Q) on 1 thread 15 s to 12.2 s, on 4 threads 20 s to 14 s; memory unchanged). Four threads are still slower than one. Agent started on branch `184-one-workspace-thread`: default `workers=1`, `workers` keyword on `local()` and `open_folder()`, page latency measured again with 1 thread.
- 2026-11-13: Simon agrees to defer worker processes, and will read `durable.md` later. Step 2 ticked in #184.
- 2026-11-13: PR for the default of one workspace thread opened (branch `184-one-workspace-thread`): `workers=1`, keyword on `local()` and `open_folder()`, two persistence tests given two threads, page latency on one thread measured (p95 45-68 ms with 1 page, 63-204 ms with 5; no freeze). Not done: no option on `essapps-batch`. PR #187 has green CI. Both await Simon's go to merge.
- 2026-11-13: profiled `LokiAtLarmorWorkflow()` (0.73 s, 993 nodes, 94 assignments): about 90 % is `sciline DataGraph.__setitem__` -> `cyclebane Graph.__setitem__` -> `networkx.compose`, which rebuilds the whole graph per assignment; about half of the time is `typing.__hash__` of generic keys. Simon: the node count likely comes from generics, and sciline plans to handle them differently (not as networkx nodes), so nothing to do upstream now; keep #187, add a code comment to check again later, merge. No `PipelineBinding` change (my recommendation, he agreed with "keep #187"). Also Simon: go ahead with the `started` event (`durable.md` item 5); agent on branch `184-started-event`.
- 2026-11-13: PR #187 merged (5f006e8) with a comment saying when the shared workflow stops being worth it. `started` event implemented by an agent on branch `184-started-event` (commit 8d188df, not pushed): `Started` logged in `Workspace._run` under the lock before anything is read; `_restart` fails a started record with "the process ended while it ran"; format 2. Agent's findings to raise with Simon: `Workspace.close` waits for every record, so an orderly stop leaves nothing started; no process in the repository handles SIGTERM, so `docker stop` or systemd ends it as a kill and fails the running records; ADR 0002 ("After a restart") and `prior-art/aiida.md:75` now read wrongly. Fresh review started.
- 2026-11-13: `started` event reviewed (fresh agent, probes and mutations) and fixed (commit 655a7d6): started check before the cancel check, a test that pins the moment of the event, reason "the workspace ended while it ran", stale statements in ADR 0002, `batch.md`, H2, `aiida.md` corrected, SIGTERM sentence in H2. PR opened. Conflicts with #188 in `workspace.py` and `persist_test.py` are possible.
- 2026-11-13: Simon merged #188 and #189; `main` at bca647a. He noted the format bump to 2 was not needed (a format 1 log reads correctly without start events) but does no harm. PR #190 opened (README open question 10). Local merged branches deleted.
- 2026-11-13: step 4 groundwork: `batch-vs-rules.md` (agent; I ran five scripts and added `> Claude:` lines). With two additions to `Batch` (handled by the file at one column, read from records; a reduce that skips refused rows) and a loop of about 30 lines, every story E1 to E4 runs with the batch. The obvious loop (scan, reduce remaining) fails: a changed setting reduces old runs again, a failed row is resubmitted at every restart, staleness is lost at a restart, results are filtered by submitter. Goes under option A: `rules.py` (165 lines), `rules_test.py` (631 lines).
- 2026-11-13: Simon answered `durable.md`. Item 1: mostly agrees; wants a spec mismatch treated like a parameter change: rows show as outdated, the old result stays accessible, so results of two versions can be compared. Item 2: (c) with a fallback (path or filename). Item 3: no opinion, I settle it. Item 4: not confident; "something is odd if we propose to forget tweaks after restart"; think in a fresh session; issue #191 opened. Item 6: agrees; the store should be an abstraction that allows other formats later, perhaps per file. Item 7: a GUI option "clear on shutdown" is likely wanted; for automatic reduction he is not sure history is needed at all (results and provenance go to SciCat), though people will want the automatic configuration at a time T as a start for a batch. Follow-ups asked: whether an old output read as plain scipp data is enough for item 1; my choices for items 2, 3, 7.
- 2026-11-13: Simon on `durable.md` item 1: old result as plain scipp data and old params as the stored dict, neither usable as input to a new request, is enough ("sounds good, go ahead"). He merged #190. ADR 0009 drafted on branch `184-adr-what-is-durable` (`docs/developer/adr/0009-what-a-workspace-keeps-on-disk.md`), with ADR 0004 and the index brought in line (five lists; "old formats must stay readable" replaced). Fresh review running. After the ADR: implement the spec digest with "record of another spec" (item 1) and the file identities (item 2).
- 2026-11-13: ADR draft reviewed (fresh agent, probes) and reworked by decision: ADR 0009 (spec identity: name, version, digest; a record of another spec stays readable as stored data), ADR 0010 (file identity: catalogue identifier, else path), ADR 0004 edited (format header as a decision, older formats not read; starts list; drop rule limited to a history no person holds; title changed), ADR 0002 edited (store: interface, a file format per stored value). PR #192 open. Simon: continue as PRs stacked on the ADR PR. Plan: `184-adr-what-is-durable` <- `184-spec-digest` (digest in `ess.spec`, author's check, LoKI digests; agent running) <- workspace part (digest in the log, record of another spec readable) <- `184-file-identity`. Beware the stacked-PR merge trap: a stacked PR merges into its base branch.
- 2026-11-13: PR #193 opened, stacked on #192 (base `184-adr-what-is-durable`): `WorkflowSpec.digest`, `check_digests`/`write_digests` in `ess.spec.testing`, LoKI digests file. Reviewed by a fresh agent with probes; fixed: RootModel and other non-group classes described by bare name (false "same"), forward references hashed by text, `Annotated` serializers now refused, the check passed with no specs. My rulings there, told to Simon: requiredness of params stays in the digest; the role of a raw-file field stays; `Referenced[T]` counts by T's fields. Part 2 started on branch `184-record-of-another-spec` (stacked on `184-spec-digest`): digest in the log, record of another spec readable as stored data, refusals, format bump. Open for part 2: whether "a request made with a spec whose digest differs is refused" (my sentence in ADR 0009) can be done without changing `Request`; if not, remove the sentence.
- 2026-11-13: part 2 on `184-record-of-another-spec` (f769067, 94364de; not pushed): digest in the log (`FORMAT = 3`), record of another spec read as stored data, references refused, restart rule. Corrected: `readable` names such outputs; the refusal by spec object and its ADR sentence removed. Fresh review found: `Listing` raised `KeyError` for an output renamed under the same version (batch views could not open the folder); Panel plot crashed on a group read as stored; no public way to ask whether a record is of the bound spec; as-stored form of scalars and lists not stated; `Opened.specs` unnecessary. Fix agent running. NOTE: I told Simon I would bring the proposal for a public call before adding it, then had `Client.bound(records) -> list[bool]` added because the `Listing` crash needs it. Tell him, and mark it in the PR as his to decide.
- 2026-11-13: review fixes for part 2 committed (9a6fb08): `Client.bound`, `Listing` holds a spec only for bound records, Panel plot fix with tests, as-stored form in `Store.read`, `Opened.specs` dropped, ADR 0009 status "implemented, but for what Open names". PR opened, stacked on #193. Stack: main <- #192 <- #193 <- this. For Simon: `Client.bound` (added before asking), requiredness/role/`Referenced[T]` rules of the digest, drop rule rescoped in ADR 0004. Next: file identity (ADR 0010) on a branch stacked on `184-record-of-another-spec`, since it changes the log format again.
- 2026-11-13: file identity implemented on `184-file-identity` (d476866, 256f661), reviewed by a fresh agent, PR #195 stacked on #194. `FolderFiles(root=)`, `catalogue_identifier`, log format 4. Rulings of mine in it: a relative path a person writes is relative to the working directory everywhere; real paths (symlinks); a `pid` not of the form prefix/suffix is refused. Follow-ups not filed as issues: batch table file cells show `pid:...` (write the run number; needs metadata in `table_of`); exported `_record.json` with a relative identity does not name its folder; a catalogue dataset with several files cannot be listed. Step 3 now waits for Simon. Step 4 waits for his answers in `batch-vs-rules.md`.
- 2026-11-13: Simon merged #192 to #195; `main` at 5c32677, the four stack branches deleted locally, step 3 ticked in #184. Simon on step 4, before answering the six questions: `batch-vs-rules.md` is long; rules predate #174, where batch reduction became a short chain of table operations; can and should that chain be the automatic reduction (files appear as new rows, each mapping runs on what is not consumed yet)? My answer: yes for the model; "not consumed" is decided once, at the last step, by joining rows with the results under the label (not by a cursor per table, since pairing is a join over all files). Checked: with handled decided per row key (the row's files at `at` against those of the newest record with that member), `rules_arrival`, `rules_can_before`, `rules_series`, `rules_template`, `rules_restart` print the same as with handled by file over the label (`rules_bykey.py`).
- 2026-11-13: Simon: no preference between "recompute and join" and "a position per table", but automatic reduction must avoid quadratic cost; 10k to 100k files per proposal, a file every few seconds, is realistic. Measured (`rules_scale.py`, `rules_scale_changed.py`), one step with one new file at 100k reduced runs: batch chain as written 4.3 s (not usable), trigger loop today 0.27 s (it lists all files each step), rows recomputed with state and submit only for changed rows 0.33 s. All linear per step. Below linear needs a source that does not run over all files (an optional `group` per file). `FileSource` has no "new since a position" call; needed for either model. `client.submit` of one request grows from 1 ms (3k records) to 5 ms (100k); not looked into. `batch-vs-rules.md` rewritten as one page with three questions (trigger column, spec version, cost bound); the long version is `batch-vs-rules-long.md`.
- 2026-11-13: Simon answered the one-page `batch-vs-rules.md`. Q3: linear with a small constant is acceptable; he asked why below linear is hard (my reply: the source is one function over all files, so the loop cannot know which rows a new file touches; an optional `group` per file would tell it; not built). Q2: no rerun of old runs for a new spec version now, but keep it easy to add, "like controlling a topic offset in Kafka" (my reply: a label is the consumer group; a new label starts at the first file; a range in `Selector` gives a start; rerun on version is one more term of the predicate; none built). Q1: he floated a cut-off of the history by age or count to bound pairing; my reply argues against it as the mechanism (a sample measured again after the cut-off is reduced with part of its runs, silently) and for `group` later; awaiting his view. Implementation started by an agent (opus) on branch `184-automatic-as-batch`: loop in `ess.apps.batch.auto`, state for chosen keys in `Batch`, submitter filter dropped, rules removed, stories E and `automatic-reduction.md` rewritten. No push, no PR by the agent; fresh review before the PR.
- 2026-11-13: step 4, first part: PR #196 opened (branch `184-automatic-as-batch`, 7 commits): `ess.apps.batch.auto.Automatic(batch, at=, settings=)` with `step()`, `refused`, `run()`; `Batch.state(keys=)`; submitter filter dropped; `rules.py` and `rules_test.py` removed; stories E, `automatic-reduction.md`, `batch.md` rewritten. Fresh review (probes, mutations) found and the agent fixed: a step that raised lost rows (now what runs is derived from the records at every step, refused rows only suppress retries); a trigger cell naming a file was resubmitted for ever (no `resolve` on a client; the loop now raises `ValueError` after the first record and at every later step); false cost statements; `state(keys)` vs `state()`; untested promises. Step at 100k: 822 ms best, 1.4 s mean (loaded machine). Simon on cost: linear is acceptable; an interval between steps bounds the load (documented with the formula and an adaptive interval as a suggestion). Simon asked whether the source should be an object or small database behind an interface; my answer: the loop needs only "rows that changed"; behind it a pure function now, later a declared group or an index that records what each row asked for; not a stateful class written by authors. A Fable agent explores whether batch and automatic reduction should be one interface (`one-interface.md`, running). After #196 merges: rewrite #151, update #155 rows, restate #68, check #177 and #80 for closing, #184 step 4 remainder (one presenter, plot contract #149).

- 2026-11-13: CI green on #196. Fable exploration back: `one-interface.md` (spike in `.scratch/one-interface/`, run against the branch at 9c28338). Recommendation B, "one table, two calls": a row's state is read from its newest record (`RowState.changed` = parameters at which the row's resolved request differs from its result's; `RowState.outdated` = result of another spec version); "which rows run" is a predicate on that state (`remaining` for a person, `arrived(at)` for the loop); `Batch.reduce` and `Automatic.step` stay two calls (not C, one call with a flag). Needs one core call that resolves without submitting (`client.check(requests)`), and rests on "resolving a resolved value gives the same value" (held for toys, not tried on LoKI). Removes `_submitted`, `_tweaked`, `_held`, the guard of 9c28338; tweak becomes an edit at a parameter that is not a column; `--remaining` takes the page's meaning. It is in effect the proposal for #191. Seen on the branch: a person's page on the loop's `Batch` shows every reduced row as outdated (form writes defaults, requests compared as given); the guard stops all rows for one bad row. Five questions for Simon in the document.
- 2026-11-13: Simon asked whether the one-interface exploration covered the UI (would the web demo append rows as they arrive). It had not (server object only). Second exploration (opus), `one-interface-page.md`, run in a headless browser on the branch with a loop stepped under the server's lock: the page replaces the whole table at each new row (scroll jumps to the top), a row waiting for its partner never shows (the version moves only on record changes), usable at 300 rows, frozen at 3 000. Causes: `forms.cell` searches picker options linearly with pydantic equality (rows x files; 1.7 s per state at 1 000 rows, a defect for a person's batch too), Tabulator's 'basic' renderer with `replaceData`, whole state per poll (59 MB at 100k). With a dictionary lookup the server does 0.5 s per state at 10k; Tabulator's 'virtual' renderer handles 100k rows. Other findings: loading a table conflicts with the loop's scan; `Automatic.refused` never reaches the page; a settings change while following marks all earlier rows outdated and offers to reduce them all; the primary button is disabled while any row runs. View: one set of parts and a mode the server starts in, not a switch a person flips. Three questions for Simon in the document. Two leftover wait loops of that agent killed.
