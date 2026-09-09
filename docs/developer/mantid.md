# What Mantid's ISIS batch interfaces say about the sketch

Companion to [architecture.md](architecture.md), [snakemake.md](snakemake.md), and [aiida.md](aiida.md).
Snakemake and AiiDA are engines; Mantid is the nearest thing to what this project builds: batch and automatic reduction interfaces used daily at a neutron facility, with fifteen years of release notes recording what was tried and removed.
The ISIS Reflectometry interface is the heaviest user and the main subject here; the ISIS SANS interface, the ISIS Powder scripts, and FIA, ISIS's current automatic-reduction service, are read for the same questions.
Facts were checked against the Mantid source and release notes and the FIA repositories on 2026-09-09; sources are listed at the end.
Analysis, not decisions; the last section says what was folded into the sketch and what was not.

## Mantid in the sketch's terms

Mantid's unit of data is a workspace in the Analysis Data Service, the ADS: an in-memory object addressed by name.
Every algorithm run on a workspace appends to the workspace's history, which is saved with it and can be replayed as a script.
A GUI is a Qt window that fills algorithm properties and reads the ADS.

The **ISIS Reflectometry interface** is a set of *batches*, one tab each, and a batch is one complete configuration: a *runs table*, experiment settings with a *lookup table*, instrument settings, event slicing, and save settings.
A *row* of the table is one reduction: sample run numbers, summed if several, an angle, transmission runs, a Q range and step, a scale, and an *options* column of free `key=value` algorithm properties.
Rows sit in *groups*; a group with more than one row is stitched after all its rows succeed.
The *lookup table* holds, per angle within a tolerance of 0.01 and optionally a title regex, the defaults a row gets when its cell is blank: transmission runs, Q range, processing instructions, detector regions of interest; one wildcard entry catches the rest, and a match on more than one entry is an error.
A row's algorithm properties are assembled in order, later overwriting earlier: event and experiment and instrument settings, then the matching lookup entry, then the row's own cells, then its options column.
Processing runs one algorithm at a time on one background thread, a group's rows and then its stitch, then the next group.
*Autoprocessing* polls the ISIS journal every 30 seconds for an investigation, parses each new run's title for `th=` to get the angle and the text before it to get the group, merges the run into the table, and processes what is not yet done; runs with the same title and angle are summed into one row, runs with the same title and another angle become another row of the same group, and any change to a group's membership resets the group so it is stitched again.
A *preview* tab lets a user draw regions on a detector image and a time-of-flight plot, runs the full reduction under them, and on Apply writes the regions into the matching lookup entry.
The whole batch, table with row states and output workspace names, search results with per-run exclusions and comments, settings and lookup table, saves as one JSON file.

The **ISIS SANS interface** is a table whose row names six runs by role, sample and can scatter, transmission and direct, an output name, a thickness, a few options, and optionally a *user file*, the complete settings document, which then overrides everything the GUI holds for that row.
The table round-trips to a CSV batch file; the settings are a versioned TOML file.
Every reduction is driven by one serialisable state object that a diagnostic tab can show per row.

**ISIS Powder** has no GUI: a Python object per instrument, configured in layers, code defaults, a YAML file, then call arguments, and a *cycle mapping* YAML keyed by run-number ranges that names the vanadium runs, empty runs, and calibration file for each range.
Processed vanadium is cached in the calibration folder under a name derived from its inputs.

**FIA** watches each instrument's last-run file, ingests a new file's metadata, and applies a per-instrument *specification*: a JSON document of rules, stored in the service's database and editable from the web UI, most of which inject constants such as a mask file or a vanadium run, and some of which look other runs up in the journal by title convention.
A job stores the reduction script text it executed, fetched from the head of a git repository with the commit recorded, the values injected into it, the container image with a pinned Mantid version, its state, output file names, and a stack trace.
A rerun is the user editing the script text and picking an image.
Outputs go to a directory per proposal and are not registered in the catalogue.

## What it learned the hard way

**Identity by name in a process.**
A row is complete when the model says so, and the model learns that its outputs are gone only through an ADS observer: delete a workspace and the row resets, rename one and the row follows, rename onto another row's output and that row resets, and replacing a workspace under its own name is not noticed at all.
Two batches that reduce the same run write the same workspace name.
The live-data monitor loses its link when the interface is closed, and a new one cannot start because the output name is taken.
Editing is forbidden while processing "because this would change the model that the reduction is running on".
Each of these is a rule the sketch does not need: a record is immutable and complete, its outputs are addressed by record ID, and a session's memory is a cache that nothing else depends on (D2).
This is the same lesson as esslivedata's, from a second codebase.

**Inputs and outputs in one cell.**
When a row's Q range is blank, the reduction computes one and the interface writes it back into the row's cells, so a saved batch cannot say which values were typed and which were computed.
The sketch keeps the request and the resolved values apart on the record; a UI may show the resolved value next to the blank field, but it stays on the record.

**Staleness by reset.**
Any settings change resets every row and group; a row edit resets the row; a membership change resets the group.
That is the Snakemake staleness model applied to a table, and it means a change to one lookup entry reprocesses a whole beamtime on the next resume, without saying which rows the change affected.
The sketch never marks a record stale; what it lacked was the operation users reach for here, and that is folded in below.

**Reproducibility from a script generator was not wanted.**
The 3.x interface could write a notebook of what it ran; the 4.1 rewrite removed it "as this was not used and not useful in its current state", and it did not return.
What users keep is the batch file, and what carries provenance is the workspace history inside saved outputs and, since 6.10, the reduction script embedded in the ORSO file.
FIA reached the same point from the other side: the executed script text is the record, and its `inputs` are display-only, so a rerun is an edit of code.
The sketch's structured request with resolved values and a code revision is the thing both were circling; a script is a rendering of it, not the record.

**A generic batch widget for one user.**
Release 3.7.1 refactored reflectometry onto a Data Processor Framework meant for SANS, Powder, and single-crystal too: columns mapped to algorithm properties, a pre-, main, and post-processing algorithm, an options column.
Its own guide said reflectometry was "the only used case at the moment"; nobody else adopted it, the 4.1 rewrite replaced it with reflectometry-specific presenters over a plain tree widget, and it was deleted in 2022.
Batch UIs differ per technique more than a column mapping can express.
The sketch's D9 says the same: the workflow contract and the view interface are the two contracts that must not foreclose UI options, and everything else in a UI is replaceable.

**Retry without a reason or a limit.**
Under autoprocessing every resume makes failed rows eligible again, so a run that fails for a lasting reason is retried at every poll.
The sketch's retry rule keys on a declared failure reason and carries a limit.

**A catalogue that lags the file.**
The interface recommends the journal over ICat because the catalogue search "is less reliable"; FIA polls a last-run file and reads journal XML, and never touches ICAT.
Both discover runs from the archive, not the catalogue, and FIA's outputs are consequently unknown to the catalogue.
The sketch's dataset source is abstracted (D7), but a file record's identity is the SciCat PID, so a filesystem-watching source cannot replace the catalogue, only get ahead of it and wait for the PID.
Whether SciCat ingestion lags the file by more than a beamtime can bear is an operational question to settle before phase 1; it is now on the open questions.

**Retrospective group reduction.**
FIA's stitch rules walk backwards from a new run over consecutive files with similar titles and submit a summed job over all of them, so a series of k runs is summed k-1 times; SANS pairs a scatter with its transmission by title and drops the run if the partner is not there yet, so whichever arrives last completes the pair.
The reflectometry interface resets a group whenever a row is added and stitches again.
None of them waits for a series to be complete, because nobody at the instrument can say when it is; the user decides to measure one more angle.
This is the answer to the sketch's open question on groups, taken up below.

## What paid off

**The lookup table.**
Parameters selected by what the data is, an angle within a tolerance, a title pattern, a wildcard, are the centre of the reflectometry interface; SANS has it as a per-row settings file, Powder as run-number ranges in a YAML file, FIA as a rule document per instrument, and the old ISIS autoreduction as variables per run-number range.
Five interfaces at one facility converged on the same shape: an ordered list of match criteria on dataset metadata, each supplying defaults, with one fallback, edited by the instrument scientist, saved with the batch, and applied to typed rows and automatically added rows alike.
The sketch had a template, one partial request, and a rule, a Python callable in the trigger loop, and nothing for manual batch beyond overrides the submitter writes by hand.
That put the per-angle table in code, invisible to the web UI and unversioned.
The lookup is now data in the sketch.
Two details from Mantid carry over: a match on more than one entry is an error found at validation, and the row shows which entry applied.

**Precedence in one order.**
Instrument defaults, then experiment settings, then the lookup entry, then the row, then the options column; a blank at any level falls through to the next.
The sketch's template plus lookup plus member override is the same ladder, and the record stores the resolved result.
One wart to avoid: Mantid's stitch reads only the wildcard entry for its parameters, because the group has no angle; in the sketch the combine is its own spec with its own template.

**Exclusions with a reason, beside the data.**
A user can exclude a run from autoprocessing with a reason, or comment on it, in the search results; the annotations survive re-searches and save with the batch, and a run deleted from the table without an exclusion comes back on the next poll.
The sketch had annotations on records only, so there was nowhere to say "not this run" before the run was ever referenced.
It now allows an annotation on a dataset's file record, and the trigger loop reports the exclusion as the reason it did not fire.

**Preview into the template.**
The preview tab runs the real reduction under interactively drawn regions and writes the regions into the lookup entry for that angle, so the next batch and the next automatic run use them.
That is D10's rule that a slice becomes a parameter, with one more step: the parameter lands in the template, not only in the next request.
The sketch's "saving a request makes a template" covers it, and the lookup gives it a place per angle.

**Multiple batches for one experiment.**
Some runs need one wavelength range and others another, so users keep two tabs.
Two templates, in the sketch.

**Group semantics.**
A group of one is not stitched; a failed row keeps its group from being stitched; the group is stitched only when every row succeeded.
D6 verbatim.

**A complete state object per row.**
The SANS interface builds one serialisable state per row, validates it whole, and can show it in a diagnostic tab; a row with its own user file ignores the GUI.
The sketch's request is that object; the validate operation separate from submit (D9) is the diagnostic tab.

**What FIA records.**
Executed script text with its commit, injected values, a container image with a pinned Mantid version, output names, a stack trace, an owner that is a proposal or a user; a watcher that declares a job stalled after thirty minutes without output.
Environment, code revision, logs, proposal scope, and liveness are all on the sketch's record already.

## Where the model does not transfer

Mantid executes in one process, one algorithm at a time, and the interface, the model, and the data share it; the sketch's runs are records executed elsewhere, and its interactive loop is a warm workflow in a session.
The ADS, workspace naming, Qt presenters, and algorithm property passing have no counterpart.
Mantid's provenance is per workspace and per algorithm, nested inside the output; the sketch's is per request, and the request is what Mantid's Python orchestration loses.
FIA's Kubernetes job per run with a pinned image is a launcher, and a good data point for the cluster launcher, not for the record model.

## What was folded into the sketch

| Lesson | Change to the sketch | Where |
|---|---|---|
| Five lookup tables | A lookup is stored, versioned data: ordered entries matching dataset metadata and supplying fills, one wildcard, ambiguity a validation error; used by batch and by the trigger loop; the record says which entry applied | Records and references; Components; Glossary |
| Nobody waits for a series | Recommendation on the groups question: key datasets into a group by metadata, submit the member and a fresh combine over the members so far, successive combines in one slot | Open questions; D10 |
| Staleness by reset | Moving a loop to a new template or lookup version offers, as a second deliberate operation, a previewed batch over the datasets the old version reduced; nothing reruns on its own | Components |
| Exclusions with a reason | An annotation may be attached to a dataset's file record, created for that purpose; the trigger loop honours an exclusion and reports it | Records and references; Components |
| The catalogue lagged the file | SciCat ingestion lag as an operational question for phase 1, with a filesystem source as the fallback that still waits for the PID | Open questions |
| The batch file is what users keep | A stored batch definition, template, lookup, members, exclusions, versioned by copy, as an open question for phase 2 | Open questions |

Not folded in, and why:

- **Resetting state on a settings change.** The Snakemake 7.8 story; the sketch keeps reruns explicit and now names the operation.
- **A free-form options column.** Both ISIS interfaces let a row override any algorithm property by name; the sketch's forms are generated from the spec, so every parameter is already reachable, and an untyped escape hatch would bypass validation.
- **Pause and resume of a batch.** Mantid's pause exists so users can edit the shared model; the sketch's requests are immutable, so editing is submitting, and cancel by batch covers the rest.
- **A cached vanadium keyed by its inputs.** Powder's implicit cache in the calibration folder; identical-request reuse was removed in the second review pass, and the opt-in shape in snakemake.md stands.
- **Run titles as the metadata channel.** Every ISIS match reads `th=` or `_TRANS` out of a title typed at the instrument. The sketch matches on SciCat metadata; that the acquisition writes the angle, sample, and role into it is a requirement on the instrument, not on this framework, and is worth stating to the instrument teams early.
- **Script text as the record.** FIA's choice; the sketch's request is structured and a script is a rendering of it.

## Sources

- Mantid source at `qt/scientific_interfaces/ISISReflectometry`: `Reduction/Item.cpp`, `Row.cpp`, `Group.cpp`, `ReductionJobs.cpp`, `LookupTable.cpp`, `LookupRow.h`; `GUI/Batch/RowProcessingAlgorithm.cpp`, `GroupProcessingAlgorithm.cpp`, `BatchJobManager.cpp`, `BatchPresenter.cpp`; `GUI/Runs/RunsPresenter.cpp`, `CatalogRunNotifier.h`; `GUI/Common/Encoder.cpp`; `GUI/Experiment/ExperimentPresenter.cpp`; `Reduction/ParseReflectometryStrings.cpp`
- [ISIS Reflectometry interface documentation](https://docs.mantidproject.org/nightly/interfaces/reflectometry/ISIS%20Reflectometry.html) and `dev-docs/source/ISISReflectometryInterface.rst`
- Release notes `docs/source/release/v3.7.1`, `v3.11.0`, `v3.13.0`, `v4.1.0`, `v4.2.0`, `v5.1.0`, `v6.4.0`, `v6.8.0`, `v6.10.0`, `v6.15.0`, `v6.16.0`, `reflectometry.rst`; the DataProcessorWidget removal in mantid PR 34599
- ISIS SANS: `scripts/SANS/sans/state/AllStates.py`, `sans/command_interface/batch_csv_parser.py`, `sans/sans_batch.py`, `qt/python/mantidqtinterfaces/mantidqtinterfaces/sans_isis/gui_logic/models/gui_state_director.py`; `docs/source/interfaces/isis_sans/`
- ISIS Powder: `scripts/Diffraction/isis_powder/routines/instrument_settings.py`, `run_details.py`, `yaml_parser.py`; [ISIS Powder tutorials](https://docs.mantidproject.org/nightly/techniques/ISISPowder-Tutorials.html)
- FIA: [file-watcher](https://github.com/fiaisis/file-watcher), [run-detection](https://github.com/fiaisis/run-detection) (`rundetection/rules/common_rules.py`, `mari_rules.py`, `sans_rules.py`, `inter_rules.py`, `specifications.py`), [FIA-API](https://github.com/fiaisis/FIA-API) (`fia_api/core/models.py`, `scripts/acquisition.py`, `scripts/transforms/`), [jobcontroller](https://github.com/fiaisis/jobcontroller) (`job_creator.py`, `job_watcher.py`), [db](https://github.com/fiaisis/db)
