# User stories

Companion to [architecture.md](architecture.md).
Each story is told from the user's side and says nothing about mechanism.
They exist to be run against the proposal: trace the story through the sketch, name the decisions it touches, and record whether the proposal determines what happens.
An outcome is one of *fits*, *gap* (the proposal does not say, or says something wrong), or *question* (a choice the team must make).
A gap becomes an open question or a change in the sketch; a story is not changed to fit the proposal.

Stories are grouped by the part of the proposal they exercise.
Each has the actor, the story, and the checks the run-through must answer.
The outcome line is filled in when the story is run.

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user with a local application or notebook.

1. Opens the application and lists the files in a chosen folder.
2. Adds a reference measurement by its SciCat PID.
3. Plots several local files against the reference.

Checks: listing creates nothing but records; a local file and a catalogue file are named the same way in a request; nothing is copied or downloaded before it is needed.

Outcome: gap, closed in the sketch. Listing, naming, and lazy copying all fit (D1). Step 3 does not: a view (D10) is defined over a declared array output, and a raw NeXus file is readable only by a workflow (D8), so the sketch does not say how a raw file is plotted before any workflow ran. Fix: state that raw files are viewable only through a workflow, and provide a per-instrument preview spec.

### A2. Run number instead of file

Actor: user at the instrument.

1. Types a run number into a reduction form.
2. Submits.

Checks: the run number resolves to one catalogue dataset at submission; the record names the dataset, not the number.

Outcome: fits. Stand-ins resolve at submission (D1); the record names the dataset. Minor: run-number resolution is said to be per instrument in D1 and within a proposal in D12; say which disambiguates.

### A3. Work without the facility mount

Actor: user on a laptop away from the facility.

1. References a catalogue dataset by PID.
2. Runs a reduction locally.

Checks: the file is fetched once, kept as a location, and the record's identity is still the PID.

Outcome: fits. The fetch creates a location, the PID stays the origin (D1, D3). Retention of the fetched copy in local mode is unstated but harmless: local mode has no retention.

### A4. Mistaken copy into the shared service

Actor: user of the shared web UI.

1. Copies a local file into the shared service by mistake and reduces it.
2. Asks for the file to be removed because it should not have left their machine.

Checks: the bytes can be dropped; the record and the runs that used it remain honest about what happened; nothing else breaks.

Outcome: question, now in open questions. The bytes can be dropped and dependents lose recomputability (Choice 1, Retention). The origin path remains on the record forever because records are never deleted. Decide whether an origin path is sensitive enough to need redaction.

## B. Manual and interactive reduction

### B1. Tune a SANS reduction in a notebook

Actor: user in a notebook.

1. Loads one sample run with background and direct beam.
2. Changes Q binning, wavelength range, and a detector mask several times, looking at I(Q) after each change within a second or two.
3. Saves the final parameters as a template for the beamtime.

Checks: which changes are cheap and who declares that; what the record listing shows afterwards, one entry or fifty; the template captures what was tuned.

Outcome: gap, closed in the sketch. Cheap parameters are declared on the spec (D8, D13) and a template comes from saving a request (D1), but the sketch does not say which fields are blanked when a request becomes a template. Fix: saving a request makes a template with data-reference fields blank and everything else literal, editable by the user.

### B2. Add a run to a sum, then remove one

Actor: user in a notebook.

1. Has runs 611 and 612 reduced as a sum.
2. Run 613 finishes; adds it and looks at the updated result.
3. Decides 612 was bad and removes it.

Checks: adding is fast; removing is correct even if slow; each state has a record that stands on its own.

Outcome: fits. One complete request per state; the growing list is the special case the wrapper accumulates; removal resets and recomputes (Choice 1, D8).

### B3. Compare two parameter sets side by side

Actor: user in a notebook or the web UI.

1. Is unsure whether a new mask helps.
2. Wants the result with and without it in one plot.
3. Keeps the better one and discards the other.

Checks: two variants can coexist without confusing the interactive series; "discard" means something concrete.

Outcome: gap, closed in the sketch. Two variants are two labels (D10), but the sketch does not say who assigns the second label when a user forks, and 'discard' has no meaning: records stay and eviction is automatic. Fix: forking is a client operation that assigns a new label; discard is dropping the label from the UI, nothing more.

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI.

1. Reduces a Bifrost run into an event array over Q and energy transfer.
2. Drags through 2D cuts.
3. Once a cut looks right, computes a proper cut with fitting from it.

Checks: the frontend never receives the volume; dragging creates no records; the chosen cut becomes an input of the next workflow with exact provenance.

Outcome: fits in a session, where the volume lives in the process serving the views (D10). In shared mode the partial-read layout that makes this responsive is explicitly deferred.

### B5. Notebook kernel dies mid-session

Actor: user in a notebook.

1. Has spent ten minutes tuning parameters.
2. The kernel dies.
3. Restarts and wants to continue from where they were.

Checks: what survives, what is recomputed, and how long that takes; nothing the user did is lost except time.

Outcome: gap, closed in the sketch. Records survive (D5) and the warm workflow is a cache (D2), but the sketch does not say where the local record store lives, that a restarted kernel reopens it, or how the user finds their last records: the record store lists only two queries. Fix: a default per-user store location, and listing by proposal, time, batch, and slot.

### B6. Find last week's result

Actor: user returning after a week.

1. Opens the notebook or web UI.
2. Wants the reduction made last Tuesday and the parameters it used.

Checks: records can be listed by proposal and time; a record shows its resolved parameters.

Outcome: fits. The record store lists by proposal and time (Components), and a record holds its resolved values (D1).

## C. Chaining and stage outputs

### C1. Beam centre feeds a sample reduction

Actor: user in the local or web application.

1. Runs the beam-centre finder on a run.
2. Uses its output as an input of a sample reduction, or of a batch of them.

Checks: the output is addressable as an input without export or import; the batch form can take it; provenance of the reduction reaches the run the beam centre came from.

Outcome: fits. The literal-or-reference union (D13) and references as inputs (D1) cover it; batch forms take a reference like any field.

### C2. Vanadium from the catalogue

Actor: user configuring single, batch, or automatic reduction.

1. References a processed vanadium run that was published to SciCat.

Checks: a published stage output is an ordinary input; the reduction does not depend on the vanadium's original record store being reachable.

Outcome: gap, closed in the sketch. A published stage output is an ordinary input (D1, D11), but the duplicate-PID check is stated only for the dataset source; a user typing our own published PID at submission may create a shallow file record instead of a reference to the run record. Fix: stand-in resolution checks PIDs against records too.

### C3. Per-bank diffraction results

Actor: DREAM user.

1. Reduces a run into per-bank d-spacing patterns.
2. Plots the mantle bank alone.
3. Feeds only the high-resolution bank into a refinement export.

Checks: one output with several members; a member is addressable as an input; the UI can show one member without loading all.

Outcome: gap, closed in the sketch. Collection outputs and element references are defined (D13, D1), but the sketch does not say whether one element can be stored and loaded without the rest. Fix: elements of a collection output are stored and served individually.

### C4. Reflectometry angle series

Actor: reflectometry user.

1. Measures four angles plus a reference.
2. Reduces them together so the curves are scaled against each other.
3. Exports one ORSO file with one dataset per angle.

Checks: the combine that feeds back into its members is expressible; the export carries per-angle metadata; the published file is the per-angle set, not one merged curve.

Outcome: fits. This is the worked example under D6; per-angle metadata and the ORSO file are the workflow's serializer (D8).

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook.

1. Adjusts the vanadium processing and immediately sees the effect on a sample reduction that uses it.

Checks: two workflows chained in memory; the vanadium output is still recorded so batch can reuse it later.

Outcome: fits. Two warm workflows chained in one session (Choice 1); the vanadium is a stage output with a record (D4).

## D. Batch

### D1. Temperature scan

Actor: instrument scientist.

1. Reduces 200 runs of a temperature scan with one template, one temperature per run.
2. Plots I(Q) at three chosen temperatures side by side.
3. Exports the whole series.

Checks: picking "the run at 250 K" without knowing record IDs; one corrupt file does not affect the other 199 and its failure is visible.

Outcome: gap, closed in the sketch. Member keys exist (D1) and failures are isolated (D6), but nothing lets a user find a member by key: the record store has no query for it. Fix: listing and lookup by batch ID and member key.

### D2. Overnight cluster batch

Actor: NMX user in the web UI.

1. Submits thirty multi-gigabyte runs, an hour each on a cluster node.
2. Closes the laptop.
3. Next day sees which finished, which failed and why, and reruns the failed ones.

Checks: results that finished while the backend was restarted are not lost; a rerun is traceable to the failed run.

Outcome: gap, closed in the sketch. Completion markers and reconciliation cover the restart (Failure handling), and a rerun links to the failed record (D1), but the run record has no failure-reason field although failure surfacing is in scope. Fix: add a structured failure reason to the record.

### D3. Cancel and resubmit

Actor: user in the web UI.

1. Submits a batch of 500.
2. After 20 have started, realises a shared parameter is wrong.
3. Cancels, fixes the parameter, resubmits without waiting for the running ones.

Checks: cancellation reaches running and queued members; the new batch is not blocked by the old; the old records stay as history.

Outcome: gap, closed in the sketch. The new batch is independent and old records stay, but cancel exists per request and per slot only; batch members are independent, so nothing cancels a batch. Fix: cancel by batch ID.

### D4. Typo caught before 500 failures

Actor: user in the web UI.

1. Fills in a batch form with a parameter combination the pydantic model rejects but the schema accepts.
2. Submits.

Checks: feedback arrives before the user leaves the form; no records are created for a request that cannot run.

Outcome: question, decided. Validate before leaving the form is D9 verbatim. A batch is now validated whole before any record is created; members are independent only at execution (D6).

### D5. Understand why a run failed

Actor: user in the web UI.

1. One member of a batch fails.
2. Reads why in the UI without opening logs.
3. Fixes the input and reruns that member.

Checks: a structured reason on the record; the rerun links to the failed run.

Outcome: fits. A failed record carries a structured reason (D1, Failure handling), and a retry is a new record linked to the old one. What the reason holds for an exception inside workflow code is a runner detail.

### D6. Rerun last year's batch with a new workflow version

Actor: instrument scientist.

1. The workflow package is upgraded to a new spec version.
2. Wants last year's batch reduced again with it, keeping the old results.

Checks: old records stay valid under their version; the template moves to the new version deliberately; a mismatched parameter set fails loudly.

Outcome: fits. Records are immutable and name their spec version; a template moves by copy and the rerun is a new batch linked to the old (D1); a mismatch fails loudly (D5). A recompute would refuse, since it is same-environment only, which is the right answer here.

## E. Automatic reduction

### E1. Series completes, reduction fires

Actor: reflectometry user during a beamtime.

1. An angle series of four runs plus a reference is measured; the last run arrives last.
2. Automatic reduction waits for the series, reduces it, and the curves appear in the web UI within minutes of the last run.

Checks: a rule can wait for a group; out-of-order and repeated dataset arrival do not fire it twice or early.

Outcome: question. The sketch names this as the open question 'Groups as the automatic-reduction unit' and recommends list-valued trigger rules; the base rule cannot express waiting for a series.

### E2. Automatic reduction goes quiet

Actor: instrument operator.

1. The workflow package is upgraded and the template no longer matches its spec version.
2. No runs are created for a day.

Checks: where the operator sees that the loop is refusing to fire, and why; nothing silently continues with a superseded workflow.

Outcome: fits. Fail loudly on a spec mismatch (D5) and the trigger loop's visible status (Components). Visibility is pull-only, so a UI must surface it.

### E3. Reduction of our own output

Actor: nobody; a failure mode.

1. An automatic-reduction result is published to SciCat.
2. The dataset source sees the new dataset.

Checks: the rule does not fire on the published output; the published dataset does not become a second raw-file record.

Outcome: fits. A rule never fires on records made from its own template, and the dataset source skips PIDs in publishing or published state (D11).

### E4. Template improved during a beamtime

Actor: instrument scientist.

1. Improves the instrument defaults template mid-beamtime.
2. New runs should use the new version; results already made stay as they are.

Checks: the loop moves to the new version deliberately; every record names the template version that made it.

Outcome: fits. Templates are immutable and versioned (D1); the trigger loop is bound to one version and moved deliberately (Components); records carry the template version.

## F. Publication and provenance

### F1. Publish, then trace six months later

Actor: user, then a colleague.

1. Inspects a reduced dataset in the web UI and publishes it.
2. Six months later the colleague opens the SciCat entry and asks what raw files, parameters, and software produced it.

Checks: the answer does not depend on any of our services still running; the provenance reaches raw data.

Outcome: gap, closed in the sketch. The publisher writes the output 'together with its provenance' but the sketch never says what that payload is, and the truth table still names our record as the truth for a published record. Fix: the SciCat entry carries a self-contained provenance snapshot (raw PIDs, resolved parameters, package versions, environment, spec identity), so no service of ours is needed to read it.

### F2. Reproduce after two upgrades

Actor: user.

1. Wants the reduced data behind a published figure.
2. The stored copy has been dropped and the environment has been upgraded twice.

Checks: the user learns that the exact result is not reproducible in the current environment before anything runs; a recompute in the current environment is a new, honestly labelled record.

Outcome: fits. A published output is downloaded, not recomputed (D1); a forced recompute in another environment is refused unless overridden and yields a new linked record.

### F3. Publish what was tuned interactively

Actor: user in a notebook.

1. Tunes a reduction in a warm session.
2. Publishes the result they are looking at.

Checks: what enters SciCat was computed in a way the record reproduces; a development binding of the workflow is refused or flagged.

Outcome: fits. A reused workflow object is recomputed cold before publication, and an in-process binding is refused unless overridden (D11).

### F4. Publish a corrected version

Actor: user.

1. A published result turns out to have used a bad mask.
2. Reduces again and publishes the correction.

Checks: the old entry cannot be removed; the new entry says what it supersedes; automatic reduction fires on neither.

Outcome: fits. A publication may name the PID it supersedes and the snapshot records it (D11); SciCat entries are never removed; the dataset source skips both PIDs.

## G. Roles and deployment

### G1. Instrument scientist prepares a beamtime

Actor: instrument scientist, then external users.

1. Before a beamtime, processes vanadium, finds the beam centre, and writes instrument defaults as a template.
2. Makes all three available to the external users of the coming proposal.
3. External users must not see other proposals' data.

Checks: artefacts from a long-lived proposal are readable across proposals on the instrument; everything else is scoped; the artefacts do not expire under the users' feet.

Outcome: gap, closed in the sketch. Instrument-shared artefacts cover vanadium and beam centre (D12) but templates are said to operate within a proposal, so an instrument default template made under commissioning is not shared by any rule. Fix: templates can be instrument-shared like artefacts.

### G2. Developer iterates on a workflow

Actor: workflow developer.

1. Modifies a sciline workflow in a notebook.
2. Runs it through the framework on real files to check outputs and plots.
3. Once satisfied, wants the runs recorded in a way that later distinguishes them from production runs.

Checks: a workflow can be bound without an installed package; such records are marked; they cannot masquerade as the installed spec.

Outcome: fits. In-process binding, the binding recorded on the record, and the no-shadowing rule (D8).

### G3. Local application, remote compute

Actor: user with a desktop application.

1. The expensive reduction stage runs on the cluster.
2. Tunes the cheap post-processing stage on the laptop with sub-second feedback and views the result locally.

Checks: a session can live in the user's process with the backend elsewhere; the stage output crosses once; views never leave the laptop.

Outcome: deferred by design. The sketch names this topology and lists client-hosted sessions under deferred and the remote-sessions open question; nothing in the model forecloses it.

### G4. Two notebooks on one machine

Actor: user with two notebooks open.

1. Both work on the same proposal's data on the same machine.
2. One wants to reference a result made in the other.

Checks: what the second notebook is, a second backend or a client of the first; whether records made in one are visible in the other.

Outcome: question, now in open questions. D5 says the second notebook is a client of the first, but local mode has no transport and HTTP is deferred, so the mechanism is unstated. Decide: per-notebook stores by default and cross-notebook references deferred, or a local transport now.

### G5. Reference across proposals refused

Actor: external user.

1. Submits a request that references an output of a record in another proposal, not instrument-shared.

Checks: refused at validation with a clear reason; no record is created.

Outcome: fits. The runnability layer requires every reference to resolve to a record the user may read (D8), submit refuses on any error (D9), and access is checked on every reference (Failure handling).

## H. Operations

### H1. Disk fills up

Actor: operator.

1. An instrument's data store reaches its disk quota during a beamtime.
2. Wants to know what can be dropped safely and what cannot.

Checks: retention says what is droppable and what is exempt; dropping loses bytes only, never provenance.

Outcome: question. Dropping loses bytes only and records stay (Choice 1, Retention); store copies of local files and instrument-shared outputs are exempt. The policy itself, and what the operator sees, is the open question on retention.

### H2. Backend upgrade with runs in flight

Actor: operator.

1. Deploys a new backend version while cluster jobs are running and a batch is queued.

Checks: records survive; dispatched runs are reconciled; queued ones are re-dispatched; a schema change is handled.

Outcome: fits, with one silence. Restart handling covers dispatched and queued runs (Failure handling) and the store carries a schema version (D5), but the sketch does not say how a schema migration is applied. Minor.
