# User stories

Each story is told from the user's side and says nothing about mechanism.
They exist to be run against the design: trace the story through the design documents and record whether the design determines what happens.
An outcome is one of *fits*, *gap* (the design does not say, or says something wrong), or *question* (a choice the team must make).
A story is never changed to fit the design.
Every gap and every question is also listed in [open-issues.md](open-issues.md).
[roadmap.md](roadmap.md) says which stories belong to which delivery phase.

Stories are grouped by the part of the design they exercise.
Each has the actor, the story, the checks the run-through must answer, and the outcome.

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user with a local application or notebook.

1. Opens the application and lists the files in a chosen folder.
2. Adds a reference measurement by its SciCat PID.
3. Plots several local files against the reference.

Checks: listing creates nothing but records; a local file and a catalogue file are named the same way in a request; nothing is copied or downloaded before it is needed.

Outcome: fits.
A raw file is viewable only through a workflow, and a quick look at a run just measured goes through a per-instrument preview spec ([stages.md](stages.md#views)).

### A2. Run number instead of file

Actor: user at the instrument.

1. Types a run number into a reduction form.
2. Submits.

Checks: the run number resolves to one catalogue dataset at submission; the record names the dataset, not the number.

Outcome: fits.
Stand-ins resolve at submission, and a run number is unique within an instrument and proposal ([records.md](records.md#datasets)).

### A3. Work without the facility mount

Actor: user on a laptop away from the facility.

1. References a catalogue dataset by PID.
2. Runs a reduction locally.

Checks: the file is fetched once, kept as a location, and the record's identity is still the PID.

Outcome: fits.
The fetch makes a location and the PID stays the identity ([records.md](records.md#datasets)).

### A4. Mistaken copy into the shared service

Actor: user of the shared web UI.

1. Copies a local file into the shared service by mistake and reduces it.
2. Asks for the file to be removed because it should not have left their machine.

Checks: the bytes can be dropped; the record and the runs that used it remain honest about what happened; nothing else breaks.

Outcome: question.
The bytes can be dropped, and the dependent records stay and lose recomputability.
Whether the local path on the record's submission is sensitive enough to redact is undecided ([open-issues.md](open-issues.md#open-questions)).

### A5. Metadata corrected after the fact

Actor: instrument scientist.

1. Reduces ten runs during a beamtime.
2. A week later the sample name of one run is corrected in SciCat.
3. Lists the runs in the web UI and expects the corrected name.

Checks: the UI shows what SciCat says now; the record is unchanged; nothing in our store had to be updated.

Outcome: fits.
Nothing is stored per dataset, so the UI asks SciCat ([operations.md](operations.md#the-record-store-is-not-a-catalogue)).

## B. Manual and interactive reduction

### B1. Tune a SANS reduction in a notebook

Actor: user in a notebook.

1. Loads one sample run with background and direct beam.
2. Changes Q binning, wavelength range, and a detector mask several times, looking at I(Q) after each change within a second or two.
3. Saves the final parameters as a template for the beamtime.

Checks: which changes rerun only the post-processing and who decides that; what the record listing shows afterwards, one entry or fifty; the template captures what was tuned.

Outcome: fits.
The session takes the stage inputs from the fields a person changes ([stages.md](stages.md#who-chooses-the-stage-inputs)), and saving a request as a template blanks the data-reference fields and keeps every other field literal ([rules.md](rules.md#templates-and-lookups)).

### B2. Add a run to a sum, then remove one

Actor: user in a notebook.

1. Has runs 611 and 612 reduced as a sum.
2. Run 613 finishes; adds it and looks at the updated result.
3. Decides 612 was bad and removes it.

Checks: adding is fast; removing is correct even if slow; each state has a record that stands on its own.

Outcome: fits.
Adding a run is a member request plus a combine over the previous combined contribution and the new member, and removing one is a combine over the remaining members ([aggregation.md](aggregation.md#when-chaining-is-valid)).

### B3. Compare two parameter sets side by side

Actor: user in a notebook or the web UI.

1. Is unsure whether a new mask helps.
2. Wants the result with and without it in one plot.
3. Keeps the better one and discards the other.

Checks: two variants can coexist without confusing the interactive series; "discard" means something concrete.

Outcome: fits.
Two variants are two slots, the second assigned when the user forks, and discarding one drops its label from the UI and changes nothing else ([stages.md](stages.md#slots)).

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI.

1. Reduces a Bifrost run into an event array over Q and energy transfer.
2. Drags through 2D cuts.
3. Once a cut looks right, computes a proper cut with fitting from it.

Checks: the frontend never receives the volume; dragging creates no records; the chosen cut becomes an input of the next workflow with exact provenance.

Outcome: fits.
A view returns a small array from the process that holds the output and creates no record, and the slice a user settles on becomes a parameter of the next request ([stages.md](stages.md#views)).

### B5. Notebook kernel dies mid-session

Actor: user in a notebook.

1. Has spent ten minutes tuning parameters.
2. The kernel dies.
3. Restarts and wants to continue from where they were.

Checks: what survives, what is recomputed, and how long that takes; nothing the user did is lost except time.

Outcome: fits.
Every notebook is its own backend with a record store at a per-user default location that the restarted kernel reopens, and what the session held is recomputable from the records ([records.md](records.md#the-record-store)).

### B6. Find last week's result

Actor: user returning after a week.

1. Opens the notebook or web UI.
2. Wants the reduction made last Tuesday and the parameters it used.

Checks: records can be listed by proposal and time; a record shows its resolved parameters.

Outcome: fits.
A record carries its proposal, its timestamps, and the parameter values the run resolved to ([records.md](records.md#requests-and-records)).

## C. Chaining and stage outputs

### C1. Beam centre feeds a sample reduction

Actor: user in the local or web application.

1. Runs the beam-centre finder on a run.
2. Uses its output as an input of a sample reduction, or of a batch of them.

Checks: the output is addressable as an input without export or import; the batch form can take it; provenance of the reduction reaches the run the beam centre came from.

Outcome: fits.
A value that other requests reference is an output of a record of its own, and a field may hold a literal or a reference ([records.md](records.md#reuse-means-a-workflow-boundary)).

### C2. Vanadium from the catalogue

Actor: user configuring single, batch, or automatic reduction.

1. References a processed vanadium run that was published to SciCat.

Checks: a published stage output is an ordinary input; the reduction does not depend on the vanadium's original record store being reachable.

Outcome: fits.
A PID typed at submission resolves to the run record named in its provenance snapshot while the store still has it, and to a dataset reference otherwise ([records.md](records.md#datasets)).

### C3. Per-bank diffraction results

Actor: DREAM user.

1. Reduces a run into per-bank d-spacing patterns.
2. Plots the mantle bank alone.
3. Feeds only the high-resolution bank into a refinement export.

Checks: one output with several members; a member is addressable as an input; the UI can show one member without loading all.

Outcome: fits.
Elements of a collection output are stored and served individually, and a reference may name one of them by key ([workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690)).

### C4. Reflectometry angle series

Actor: reflectometry user.

1. Measures four angles plus a reference.
2. Reduces them together so the curves are scaled against each other.
3. Exports one ORSO file with one dataset per angle.

Checks: the combine that feeds back into its members is expressible; the export carries per-angle metadata; the published file is the per-angle set, not one merged curve.

Outcome: fits.
The stitch is a combine spec without `chain`, recomputed over all members on each arrival ([aggregation.md](aggregation.md#combines-that-are-not-additive)).

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook.

1. Adjusts the vanadium processing and immediately sees the effect on a sample reduction that uses it.

Checks: two workflows chained in memory; the vanadium output is still recorded so batch can reuse it later.

Outcome: fits.
Each workflow contributes a stage, and the first one's output is a record's output that the session holds in memory ([stages.md](stages.md#more-than-one-workflow)).

## D. Batch

### D1. Temperature scan

Actor: instrument scientist.

1. Reduces 200 runs of a temperature scan with one template, one temperature per run.
2. Plots I(Q) at three chosen temperatures side by side.
3. Exports the whole series.

Checks: picking "the run at 250 K" without knowing record IDs; one corrupt file does not affect the other 199 and its failure is visible.

Outcome: fits.
The batch table is a query over the records, the latest one per member key, and the members are independent ([rules.md](rules.md#labels-batches-and-slots)).

### D2. Overnight cluster batch

Actor: NMX user in the web UI.

1. Submits thirty multi-gigabyte runs, an hour each on a cluster node.
2. Closes the laptop.
3. Next day sees which finished, which failed and why, and reruns the failed ones.

Checks: results that finished while the backend was restarted are not lost; a rerun is traceable to the failed run.

Outcome: fits.
Completion markers reconcile a restart, and a failed record carries a structured reason that the rerun's record links back to ([operations.md](operations.md#failure-handling)).

### D3. Cancel and resubmit

Actor: user in the web UI.

1. Submits a batch of 500.
2. After 20 have started, realises a shared parameter is wrong.
3. Cancels, fixes the parameter, resubmits without waiting for the running ones.

Checks: cancellation reaches running and queued members; the new batch is not blocked by the old; the old records stay as history.

Outcome: fits.
A batch is cancelled whole by its label, queued and running members alike, and the cancelled records stay ([operations.md](operations.md#failure-handling)).

### D4. Typo caught before 500 failures

Actor: user in the web UI.

1. Fills in a batch form with a parameter combination the pydantic model rejects but the schema accepts.
2. Submits.

Checks: feedback arrives before the user leaves the form; no records are created for a request that cannot run.

Outcome: fits.
A UI calls validate on every change, and a group is validated whole before any record is created ([operations.md](operations.md#the-client-interface)).

### D5. Understand why a run failed

Actor: user in the web UI.

1. One member of a batch fails.
2. Reads why in the UI without opening logs.
3. Fixes the input and reruns that member.

Checks: a structured reason on the record; the rerun links to the failed run.

Outcome: fits.
A failed record carries a structured failure reason, and a retry is a new record that links to the failed one ([operations.md](operations.md#failure-handling)).

### D6. Rerun last year's batch with a new workflow version

Actor: instrument scientist.

1. The workflow package is upgraded to a new spec version.
2. Wants last year's batch reduced again with it, keeping the old results.

Checks: old records stay valid under their version; the template moves to the new version deliberately; a mismatched parameter set fails loudly.

Outcome: fits.
A template moves to a new spec version by copy, the records say which version filled them, and a stored parameter set that no longer matches its spec version fails loudly ([rules.md](rules.md#templates-and-lookups)).

## E. Automatic reduction

### E1. Series grows, reduction follows

Actor: reflectometry user during a beamtime.

1. An angle series is measured one run at a time, plus a reference.
2. Nobody can say in advance how many angles there will be.
3. After each run the stitched curve in the web UI grows by one angle, within minutes of the run.

Checks: a rule can key runs into a group; every arrival reduces the member and combines the members so far; out-of-order and repeated dataset arrival do not produce a duplicate combine; the UI shows one curve per sample, not one per arrival.

Outcome: fits.
A rule with a series key submits a member request and a combine request on every arrival, and successive combines supersede each other under the series value as member key ([rules.md](rules.md#series)).

### E2. Automatic reduction goes quiet

Actor: instrument operator.

1. The workflow package is upgraded and the template no longer matches its spec version.
2. No runs are created for a day.

Checks: where the operator sees that the loop is refusing to fire, and why; nothing silently continues with a superseded workflow.

Outcome: fits.
`trigger_status` answers for one dataset why the rule did not fire, and a spec-version mismatch fails loudly rather than defaulting ([rules.md](rules.md#the-trigger-loop)).

### E3. Reduction of our own output

Actor: none. The story is a failure mode.

1. An automatic-reduction result is published to SciCat.
2. The dataset source sees the new dataset.

Checks: the rule does not fire on the published output; the published dataset is not listed as a raw dataset.

Outcome: fits.
A rule fires on no dataset whose SciCat entry carries our provenance snapshot, and on no record made from its own template ([rules.md](rules.md#the-trigger-loop)).

### E4. Template improved during a beamtime

Actor: instrument scientist.

1. Improves the instrument defaults template mid-beamtime.
2. New runs should use the new version; results already made stay as they are.

Checks: the rule moves to the new version deliberately; every record names the template version that made it.

Outcome: fits.
A rule names one template version and moves by copy, and what the old version made is reprocessed only when a person asks for it ([rules.md](rules.md#backlog-reprocess-and-rerun)).

## F. Publication and provenance

### F1. Publish, then trace six months later

Actor: user, then a colleague.

1. Inspects a reduced dataset in the web UI and publishes it.
2. Six months later the colleague opens the SciCat entry and asks what raw files, parameters, and software produced it.

Checks: the answer does not depend on any of our services still running; the provenance reaches raw data.

Outcome: fits.
The SciCat entry carries a provenance snapshot of the raw PIDs, the resolved parameters, the spec identity, the package versions, and the environment, readable without any service of ours ([operations.md](operations.md#publication)).

### F2. Reproduce after two upgrades

Actor: user.

1. Wants the reduced data behind a published figure.
2. The stored copy has been dropped and the environment has been upgraded twice.

Checks: the user learns that the exact result is not reproducible in the current environment before anything runs; a recompute in the current environment is a new, honestly labelled record.

Outcome: fits.
A published output is downloaded rather than recomputed, and a recompute outside the record's environment is refused unless the client overrides ([records.md](records.md#datasets)).

### F3. Publish what was tuned interactively

Actor: user in a notebook.

1. Tunes a reduction in a session.
2. Publishes the result they are looking at.

Checks: what enters SciCat was computed in a way the record reproduces; a development binding of the workflow is refused or flagged.

Outcome: fits.
A result that a held stage served is recomputed in a throwaway process before publication, and a record bound to workflow code in process is refused unless the client overrides ([operations.md](operations.md#publication)).

### F4. Publish a corrected version

Actor: user.

1. A published result turns out to have used a bad mask.
2. Reduces again and publishes the correction.

Checks: the old entry cannot be removed; the new entry says what it supersedes; automatic reduction fires on neither.

Outcome: fits.
A publication may name the PID it supersedes, an entry in SciCat is never removed, and the trigger loop skips both entries ([operations.md](operations.md#publication)).

## G. Roles and deployment

### G1. Instrument scientist prepares a beamtime

Actor: instrument scientist, then external users.

1. Before a beamtime, processes vanadium, finds the beam centre, and writes instrument defaults as a template.
2. Makes all three available to the external users of the coming proposal.
3. External users must not see other proposals' data.

Checks: artefacts from a long-lived proposal are readable across proposals on the instrument; everything else is scoped; the artefacts do not expire under the users' feet.

Outcome: fits.
Artefacts, templates, and lookups from a commissioning proposal are marked instrument-shared, and their disk copies are exempt from retention ([operations.md](operations.md#scope-instrument-plus-proposal)).

### G2. Developer iterates on a workflow

Actor: workflow developer.

1. Modifies a sciline workflow in a notebook.
2. Runs it through the framework on real files to check outputs and plots.
3. Once satisfied, wants the runs recorded in a way that later distinguishes them from production runs.

Checks: a workflow can be bound without an installed package; such records are marked; they cannot masquerade as the installed spec.

Outcome: fits.
A notebook binds a spec in process unless an installed package provides that name and version, and the record says which binding ran ([workflow-contract.md](workflow-contract.md#spec-and-binding)).

### G3. Local application, remote compute

Actor: user with a desktop application.

1. The expensive reduction stage runs on the cluster.
2. Tunes the cheap post-processing stage on the laptop with sub-second feedback and views the result locally.

Checks: a session can live in the user's process with the backend elsewhere; the stage output crosses once; views never leave the laptop.

Outcome: question.
Nothing in the model forecloses this topology, and no request needs to reach a particular process.
Whether interactive work uses sessions at all, and where a session then runs, is undecided ([open-issues.md](open-issues.md#open-questions)).

### G4. Two notebooks on one machine

Actor: user with two notebooks open.

1. Both work on the same proposal's data on the same machine.
2. One wants to reference a result made in the other.

Checks: what the second notebook is, a second backend or a client of the first; whether records made in one are visible in the other.

Outcome: question.
Each notebook is its own backend with its own record store, so a reference from one to the other needs a local transport that does not exist ([open-issues.md](open-issues.md#open-questions)).

### G5. Reference across proposals refused

Actor: external user.

1. Submits a request that references an output of a record in another proposal, not instrument-shared.

Checks: refused at validation with a clear reason; no record is created.

Outcome: fits.
Runnability requires every reference to resolve to a record the submitter may read, and submit refuses a request with any error ([workflow-contract.md](workflow-contract.md#validation)).

## H. Operations

### H1. Disk fills up

Actor: operator.

1. An instrument's data store reaches its disk quota during a beamtime.
2. Wants to know what can be dropped safely and what cannot.

Checks: retention says what is droppable and what is exempt; dropping loses bytes only, never provenance.

Outcome: question.
Dropping loses bytes only, the records stay, and store copies of local files and instrument-shared outputs are exempt.
The retention policy itself, and what the operator is shown when the quota is reached, is undecided ([open-issues.md](open-issues.md#open-questions)).

### H2. Backend upgrade with runs in flight

Actor: operator.

1. Deploys a new backend version while cluster jobs are running and a batch is queued.

Checks: records survive; dispatched runs are reconciled; queued ones are re-dispatched; a schema change is handled.

Outcome: gap.
Restart handling covers dispatched and queued runs, and the record store carries a schema version.
How an existing store is migrated to a new schema is not stated ([open-issues.md](open-issues.md#what-the-design-does-not-solve)).

### H3. Proposal ends

Actor: operator.

1. A proposal's analysis window closes.
2. Wants the instrument's store to stop carrying it, without losing what was published.
3. A user asks a year later what parameters produced a published result.

Checks: records and copies go together; nothing dangles; the published entry answers the question on its own.

Outcome: fits.
A proposal's records and disk copies are dropped together after an export, references do not cross proposals except into long-lived commissioning ones, and the SciCat entry carries its own provenance ([records.md](records.md#lifetimes)).

## Summary

Of the 38 stories, 33 fit, four raise a question, and one is a gap.

| Story | Title | Outcome |
|---|---|---|
| A1 | Browse a local folder next to a catalogue reference | fits |
| A2 | Run number instead of file | fits |
| A3 | Work without the facility mount | fits |
| A4 | Mistaken copy into the shared service | question |
| A5 | Metadata corrected after the fact | fits |
| B1 | Tune a SANS reduction in a notebook | fits |
| B2 | Add a run to a sum, then remove one | fits |
| B3 | Compare two parameter sets side by side | fits |
| B4 | Explore a 4D volume | fits |
| B5 | Notebook kernel dies mid-session | fits |
| B6 | Find last week's result | fits |
| C1 | Beam centre feeds a sample reduction | fits |
| C2 | Vanadium from the catalogue | fits |
| C3 | Per-bank diffraction results | fits |
| C4 | Reflectometry angle series | fits |
| C5 | Vanadium and sample tuned together | fits |
| D1 | Temperature scan | fits |
| D2 | Overnight cluster batch | fits |
| D3 | Cancel and resubmit | fits |
| D4 | Typo caught before 500 failures | fits |
| D5 | Understand why a run failed | fits |
| D6 | Rerun last year's batch with a new workflow version | fits |
| E1 | Series grows, reduction follows | fits |
| E2 | Automatic reduction goes quiet | fits |
| E3 | Reduction of our own output | fits |
| E4 | Template improved during a beamtime | fits |
| F1 | Publish, then trace six months later | fits |
| F2 | Reproduce after two upgrades | fits |
| F3 | Publish what was tuned interactively | fits |
| F4 | Publish a corrected version | fits |
| G1 | Instrument scientist prepares a beamtime | fits |
| G2 | Developer iterates on a workflow | fits |
| G3 | Local application, remote compute | question |
| G4 | Two notebooks on one machine | question |
| G5 | Reference across proposals refused | fits |
| H1 | Disk fills up | question |
| H2 | Backend upgrade with runs in flight | gap |
| H3 | Proposal ends | fits |
