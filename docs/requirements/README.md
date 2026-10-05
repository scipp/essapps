# Requirements and context

What we know about the problem that essapps, the data-reduction framework built in this repository, must solve; what we only assume; and what we do not know yet.
These pages describe the world, not our design, and use no design terms.
Each mechanism of the design ([../developer/](../developer/README.md)) should trace to an item here; a mechanism that traces to nothing is a candidate for removal.

## Goal

Run the reduction workflows of the ess packages on ESS data in three ways: by hand (interactively), in batches, and automatically on each new run. *Simon, 2026-09-04*
Read inputs from and write results to SciCat or another catalogue, run locally or on a cluster, and let every result say where it came from. *Simon, 2026-09-04*
Stay simple: predictable behaviour, and nothing that no stated need forces. *Simon, 2026-09-30*

The first release combines no runs and offers: *Simon's plan, 2026-10-05*

- automatic reduction of each new sample run, which must still find the background and normalisation runs it needs;
- batch reduction of many sample runs with one workflow, with parameters tweaked per run;
- an application to select and configure a predefined workflow, run it, look at the results and their plots, and upload them to SciCat, without loops or interactive tuning of parameters.

Simon is Simon Heybrock, who leads the project; "Simon, 2026-09-04" cites his [scoping notes](https://github.com/scipp/essapps/blob/e72c374bffac264ca95b39c6acd5e0afc894187d/docs/developer/scoping.md).
Terms such as VISA, DMSC, can, or transmission run are explained in the [glossary](glossary.md).

## In one minute

- ESS plans first neutrons for early 2027, so every ESS size and rate here comes from simulations, old estimates, test files, or other facilities.
- Today a person pairs runs by hand (sample with can, transmission, reference); ISIS pairs them by run-title conventions, and whether ESS files will say what role a run plays is unknown.
- "Combining runs" means a different operation per technique: summing the counts and, separately, the normalisation of all runs, then dividing once (SANS); binning into one fixed 4D grid (spectroscopy); concatenating events (reflectometry, runs at one angle); a joint fit (reflectometry angles, NMX scaling); or nothing yet (powder, imaging). A sum grows run by run only if counts and normalisation are kept apart until the division, and a joint fit needs all inputs at once.
- A run is one file, but not always one angle, one role, or one result: one file may hold a whole BIFROST angle scan, the sample, open-beam and dark frames of ODIN, or FREIA's up to three angles interleaved in time, and splitting a run by time or by a sample-environment log adds a dimension to its result.
- Results can be large: a spectroscopy grid up to hundreds of GB, an imaging stack 12 GB, and a Horace file at ISIS, which lists every observation, up to 500 GB.
- The framework keeps results for days to weeks, while users work with them; only results registered in SciCat are found later, and SciCat holds their metadata and file paths, not the files. Instrument and run number are assumed to name a run.

## Numbers

| What | Value | Source | Page |
|---|---|---|---|
| Reflectometry batch reduction | many hundreds of runs per hour, perhaps more | an instrument scientist, via Simon | [reflectometry](reflectometry.md) |
| Batch size | hundreds of independent samples | Simon | [users](users.md) |
| Experiments per year | more than 1000, a couple of users each | Simon | [users](users.md) |
| People in the reduction software at once | at most 1 to 3 per instrument | Simon | [users](users.md) |
| Results kept by the framework | days to weeks | Simon | [tensions](tensions.md) |
| Spectroscopy 4D grid over Q and ΔE | up to hundreds of GB | Simon | [spectroscopy](spectroscopy.md) |
| Horace SQW files at ISIS | 10 to 500 GB | publication | [spectroscopy](spectroscopy.md) |
| Imaging stack, 361 frames of 2048² pixels | 12 GB in float64 | code | [imaging](imaging.md) |
| ODIN wavelength cube at full resolution | 34 GB | computed for 256 bins | [imaging](imaging.md) |
| One reduced NMX orientation | 2 GB uncompressed | measured | [diffraction](diffraction.md) |
| Amor runs at PSI | samples 0.2 to 4 M events, 23 to 48 MB; reference 13 M, 124 MB | files | [reflectometry](reflectometry.md) |
| One LoKI reduction | seconds, little memory | Simon (assumed) | [sans](sans.md) |
| Estimated event rates | 5e4 (LoKI) to 7.5e7 (DREAM) per second | Simon, from instrument scientists years ago | [data](data.md) |
| VISA machine | 64 GB, 6 CPUs as standard; larger for instruments with large files | documented; Simon | [systems](systems.md) |
| Workflow authors | 7 people | git history | [users](users.md) |

## Questions that block the design

- How will ESS files or the catalogue say what role a run plays, and which runs belong together? Automatic reduction in the first release needs this to find background and normalisation runs. *Ask: NICOS team, SciCat team, instrument scientists ([data](data.md)).*

## Non-goals

- A second catalogue of results next to SciCat. *Simon, 2026-09-30*
- Windows for the batch- and automatic-reduction services; GUI applications should probably run on Windows too. *Simon, 2026-09-30 and 2026-10-05*
- The framework knowing technique details such as normalisation, or prescribing how instrument scientists choose calibration and background runs, which each does differently. Applications built on the framework know them, including the interfaces that configure batch and automatic reduction. *Simon, 2026-09-28 and 2026-10-05*
- Knowing whether a reduction workflow is a sciline pipeline or a plain function. *Simon, 2026-09-04 and 2026-09-28*
- Starting a separate reduction for each value found only in the data, such as each angle read from a log; a technique that needs this runs a first reduction to find the values. Splitting events by a log inside one reduction is not excluded ([data](data.md)). *Simon, 2026-09-28*
- Steps of one technique, such as applying the reflectometry scale factors back to the curve of each angle; an application built on the framework does that. *Simon, 2026-09-28*
- Tomographic reconstruction, which other software does; the framework might one day drive that software. *Simon, 2026-10-05*
- Continuing a reduction after the user's process crashed: crashes come from lack of memory or from bugs, which a restart does not fix. *Simon, 2026-10-05*

## Pages

| Page | What it holds |
|---|---|
| [users](users.md) | who reduces data, and how: by hand, in batches, automatically |
| [tensions](tensions.md) | needs that pull against each other, settled or open |
| [sans](sans.md), [reflectometry](reflectometry.md) | runs and their roles, combining, sizes, batch and automatic needs |
| [spectroscopy](spectroscopy.md), [diffraction](diffraction.md), [imaging](imaging.md) | the same for the large-volume techniques |
| [data](data.md) | runs and files: what a run is, how it is named, other input files |
| [systems](systems.md) | SciCat, NICOS, live reduction, VISA, the cluster, the ess packages |
| [glossary](glossary.md) | the facility and technique terms these pages use |

## How to write here

- A page has at most 100 lines (checked at commit) and opens with a summary of at most 5 lines.
- A topic page has three sections, in this order: `Known` (has a primary source), `Assumed` (nobody confirmed it; says how to check), `Open` (a question; says what it decides and whom to ask). The tensions page uses `Settled` and `Open`.
- Include an item only if the design would change were it false. One item per bullet, at most two sentences, its source in italics.
- A source is a person and a date, a public document, or code on GitHub. The design docs and the [user](../developer/user-stories.md) and [system](../developer/system-stories.md) stories (cited by ID, such as D7) are not sources: a claim only they make is Assumed.
- Write about runs, samples, files, and results, not about the design (no record, request, stage, accumulator, and so on).
- Each fact has one home page; other pages link to it. A page that outgrows 100 lines becomes a folder with its own README.
- A tension needs two stated needs that pull against each other; a question with no opposing need belongs on a topic page.
- What happens inside a workflow, such as a correction or sorting events by angle, belongs here only if it changes which runs go in or which results come out.
- Leave out version numbers and the current state of other systems, such as what esslivedata computes today; both change too often to constrain the design.
- What the framework itself provides, such as how long it keeps results, the team decides, not outside groups.
- Files from CODA hold generated data: do not use them for sizes, or to show that a field is missing.
- That no code does something today is no evidence that nobody needs it.
- Sizes measured at other facilities may indicate ESS sizes; their rates do not.
- The repository is public: name people by role, except Simon.
