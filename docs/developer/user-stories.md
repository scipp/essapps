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

Outcome:

### A2. Run number instead of file

Actor: user at the instrument.

1. Types a run number into a reduction form.
2. Submits.

Checks: the run number resolves to one catalogue dataset at submission; the record names the dataset, not the number.

Outcome:

### A3. Work without the facility mount

Actor: user on a laptop away from the facility.

1. References a catalogue dataset by PID.
2. Runs a reduction locally.

Checks: the file is fetched once, kept as a location, and the record's identity is still the PID.

Outcome:

### A4. Mistaken copy into the shared service

Actor: user of the shared web UI.

1. Copies a local file into the shared service by mistake and reduces it.
2. Asks for the file to be removed because it should not have left their machine.

Checks: the bytes can be dropped; the record and the runs that used it remain honest about what happened; nothing else breaks.

Outcome:

## B. Manual and interactive reduction

### B1. Tune a SANS reduction in a notebook

Actor: user in a notebook.

1. Loads one sample run with background and direct beam.
2. Changes Q binning, wavelength range, and a detector mask several times, looking at I(Q) after each change within a second or two.
3. Saves the final parameters as a template for the beamtime.

Checks: which changes are cheap and who declares that; what the record listing shows afterwards, one entry or fifty; the template captures what was tuned.

Outcome:

### B2. Add a run to a sum, then remove one

Actor: user in a notebook.

1. Has runs 611 and 612 reduced as a sum.
2. Run 613 finishes; adds it and looks at the updated result.
3. Decides 612 was bad and removes it.

Checks: adding is fast; removing is correct even if slow; each state has a record that stands on its own.

Outcome:

### B3. Compare two parameter sets side by side

Actor: user in a notebook or the web UI.

1. Is unsure whether a new mask helps.
2. Wants the result with and without it in one plot.
3. Keeps the better one and discards the other.

Checks: two variants can coexist without confusing the interactive series; "discard" means something concrete.

Outcome:

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI.

1. Reduces a Bifrost run into an event array over Q and energy transfer.
2. Drags through 2D cuts.
3. Once a cut looks right, computes a proper cut with fitting from it.

Checks: the frontend never receives the volume; dragging creates no records; the chosen cut becomes an input of the next workflow with exact provenance.

Outcome:

### B5. Notebook kernel dies mid-session

Actor: user in a notebook.

1. Has spent ten minutes tuning parameters.
2. The kernel dies.
3. Restarts and wants to continue from where they were.

Checks: what survives, what is recomputed, and how long that takes; nothing the user did is lost except time.

Outcome:

## C. Chaining and stage outputs

### C1. Beam centre feeds a sample reduction

Actor: user in the local or web application.

1. Runs the beam-centre finder on a run.
2. Uses its output as an input of a sample reduction, or of a batch of them.

Checks: the output is addressable as an input without export or import; the batch form can take it; provenance of the reduction reaches the run the beam centre came from.

Outcome:

### C2. Vanadium from the catalogue

Actor: user configuring single, batch, or automatic reduction.

1. References a processed vanadium run that was published to SciCat.

Checks: a published stage output is an ordinary input; the reduction does not depend on the vanadium's original record store being reachable.

Outcome:

### C3. Per-bank diffraction results

Actor: DREAM user.

1. Reduces a run into per-bank d-spacing patterns.
2. Plots the mantle bank alone.
3. Feeds only the high-resolution bank into a refinement export.

Checks: one output with several members; a member is addressable as an input; the UI can show one member without loading all.

Outcome:

### C4. Reflectometry angle series

Actor: reflectometry user.

1. Measures four angles plus a reference.
2. Reduces them together so the curves are scaled against each other.
3. Exports one ORSO file with one dataset per angle.

Checks: the combine that feeds back into its members is expressible; the export carries per-angle metadata; the published file is the per-angle set, not one merged curve.

Outcome:

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook.

1. Adjusts the vanadium processing and immediately sees the effect on a sample reduction that uses it.

Checks: two workflows chained in memory; the vanadium output is still recorded so batch can reuse it later.

Outcome:

## D. Batch

### D1. Temperature scan

Actor: instrument scientist.

1. Reduces 200 runs of a temperature scan with one template, one temperature per run.
2. Plots I(Q) at three chosen temperatures side by side.
3. Exports the whole series.

Checks: picking "the run at 250 K" without knowing record IDs; one corrupt file does not affect the other 199 and its failure is visible.

Outcome:

### D2. Overnight cluster batch

Actor: NMX user in the web UI.

1. Submits thirty multi-gigabyte runs, an hour each on a cluster node.
2. Closes the laptop.
3. Next day sees which finished, which failed and why, and reruns the failed ones.

Checks: results that finished while the backend was restarted are not lost; a rerun is traceable to the failed run.

Outcome:

### D3. Cancel and resubmit

Actor: user in the web UI.

1. Submits a batch of 500.
2. After 20 have started, realises a shared parameter is wrong.
3. Cancels, fixes the parameter, resubmits without waiting for the running ones.

Checks: cancellation reaches running and queued members; the new batch is not blocked by the old; the old records stay as history.

Outcome:

### D4. Typo caught before 500 failures

Actor: user in the web UI.

1. Fills in a batch form with a parameter combination the pydantic model rejects but the schema accepts.
2. Submits.

Checks: feedback arrives before the user leaves the form; no records are created for a request that cannot run.

Outcome:

## E. Automatic reduction

### E1. Series completes, reduction fires

Actor: reflectometry user during a beamtime.

1. An angle series of four runs plus a reference is measured; the last run arrives last.
2. Automatic reduction waits for the series, reduces it, and the curves appear in the web UI within minutes of the last run.

Checks: a rule can wait for a group; out-of-order and repeated dataset arrival do not fire it twice or early.

Outcome:

### E2. Automatic reduction goes quiet

Actor: instrument operator.

1. The workflow package is upgraded and the template no longer matches its spec version.
2. No runs are created for a day.

Checks: where the operator sees that the loop is refusing to fire, and why; nothing silently continues with a superseded workflow.

Outcome:

### E3. Reduction of our own output

Actor: nobody; a failure mode.

1. An automatic-reduction result is published to SciCat.
2. The dataset source sees the new dataset.

Checks: the rule does not fire on the published output; the published dataset does not become a second raw-file record.

Outcome:

## F. Publication and provenance

### F1. Publish, then trace six months later

Actor: user, then a colleague.

1. Inspects a reduced dataset in the web UI and publishes it.
2. Six months later the colleague opens the SciCat entry and asks what raw files, parameters, and software produced it.

Checks: the answer does not depend on any of our services still running; the provenance reaches raw data.

Outcome:

### F2. Reproduce after two upgrades

Actor: user.

1. Wants the reduced data behind a published figure.
2. The stored copy has been dropped and the environment has been upgraded twice.

Checks: the user learns that the exact result is not reproducible in the current environment before anything runs; a recompute in the current environment is a new, honestly labelled record.

Outcome:

### F3. Publish what was tuned interactively

Actor: user in a notebook.

1. Tunes a reduction in a warm session.
2. Publishes the result they are looking at.

Checks: what enters SciCat was computed in a way the record reproduces; a development binding of the workflow is refused or flagged.

Outcome:

## G. Roles and deployment

### G1. Instrument scientist prepares a beamtime

Actor: instrument scientist, then external users.

1. Before a beamtime, processes vanadium, finds the beam centre, and writes instrument defaults as a template.
2. Makes all three available to the external users of the coming proposal.
3. External users must not see other proposals' data.

Checks: artefacts from a long-lived proposal are readable across proposals on the instrument; everything else is scoped; the artefacts do not expire under the users' feet.

Outcome:

### G2. Developer iterates on a workflow

Actor: workflow developer.

1. Modifies a sciline workflow in a notebook.
2. Runs it through the framework on real files to check outputs and plots.
3. Once satisfied, wants the runs recorded in a way that later distinguishes them from production runs.

Checks: a workflow can be bound without an installed package; such records are marked; they cannot masquerade as the installed spec.

Outcome:

### G3. Local application, remote compute

Actor: user with a desktop application.

1. The expensive reduction stage runs on the cluster.
2. Tunes the cheap post-processing stage on the laptop with sub-second feedback and views the result locally.

Checks: a session can live in the user's process with the backend elsewhere; the stage output crosses once; views never leave the laptop.

Outcome:

### G4. Two notebooks on one machine

Actor: user with two notebooks open.

1. Both work on the same proposal's data on the same machine.
2. One wants to reference a result made in the other.

Checks: what the second notebook is, a second backend or a client of the first; whether records made in one are visible in the other.

Outcome:
