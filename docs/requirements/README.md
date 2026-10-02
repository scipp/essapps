# Requirements and context

What we know about the problem that essapps, the data-reduction framework built in this repository, must solve; what we only assume; and what we do not know yet.
These pages describe the world, not our design, and use no design terms.
Each mechanism of the design ([../developer/](../developer/README.md)) should trace to an item here; a mechanism that traces to nothing is a candidate for removal.

## Goal

Run the reduction workflows of the ess packages on ESS data in three ways: by hand (interactively), in batches, and automatically on each new run. *Simon, scoping.md*
Read inputs from and write results to SciCat or another catalogue, run locally or on a cluster, and let every result say where it came from. *Simon, scoping.md*
Stay simple: predictable behaviour, and nothing that no stated need forces. *Simon, 2026-09-30*

Simon is Simon Heybrock, who leads the project; "Simon, scoping.md" cites his scoping notes in [../developer/scoping.md](../developer/scoping.md) (2026-09-04).
Terms such as VISA, DMSC, can, or transmission run are explained in the [glossary](glossary.md).

## In one minute

- ESS plans first neutrons for early 2027, so every ESS size and rate here comes from simulations, test runs, or other facilities.
- No file says what role a run plays (sample, can, transmission, open beam). Today a person pairs runs by hand for every technique; ISIS pairs them by run-title conventions.
- "Combining runs" means a different operation per technique: a sum of counts and of normalisations, divided once (SANS), a concatenation of events (reflectometry, runs at one angle), a joint fit (reflectometry angles, NMX scaling), or nothing yet (powder, imaging). Several methods need all inputs at once.
- One run is not always one file or one result: the BIFROST code expects one file to hold a whole angle scan, an ODIN file holds sample, open-beam and dark frames, a simulated FREIA run holds three angles, and fast kinetics may split one run into hundreds of results.
- Results grow with events and frames, not only with bins: an imaging stack takes 12 GB and a Horace file at ISIS up to 500 GB, while a standard VISA machine has 64 GB.
- A run number alone does not name a run; the per-run UUID does. SciCat holds metadata and file paths, not file bytes.

## Numbers

| What | Value | Source | Page |
|---|---|---|---|
| Reflectometry batch reduction | more than 1000 runs per hour | Simon; Amor tutorial runs came 11 to 47 min apart | [reflectometry](reflectometry.md) |
| Batch size | hundreds of independent samples | Simon | [users](users.md) |
| Spectroscopy 4D volume | up to hundreds of GB | Simon; grid or list of observations is open | [spectroscopy](spectroscopy.md) |
| Horace SQW files at ISIS | 10 to 500 GB | publication | [spectroscopy](spectroscopy.md) |
| Imaging stack, 361 frames of 2048² pixels | 12 GB in float64 | code | [imaging](imaging.md) |
| ODIN wavelength cube at full resolution | 34 GB | computed | [imaging](imaging.md) |
| One reduced NMX orientation | 2 GB uncompressed | measured | [diffraction](diffraction.md) |
| Existing runs (LoKI test at ISIS, Amor) | 0.2 to 13 M detector events, 20 to 180 MB | files | [sans](sans.md), [reflectometry](reflectometry.md) |
| ESS test runs of about 10 minutes | 0.1 to 5 GB | measured, simulated data | [data](data.md) |
| Planned event rates | 1e7 (LoKI) to 7.5e7 (DREAM) per second | assumed, origin unknown | [data](data.md) |
| Standard VISA machine | 64 GB, 6 CPUs | documented | [systems](systems.md) |
| Workflow authors | about 12 people | git history | [users](users.md) |

## Questions that block the design

1. Is the 4D volume of hundreds of GB a fixed grid that each run adds to, or a list of observations that grows with every run? *Ask: Simon, BIFROST instrument scientist.*
2. Where are runs summed: before reduction into merged files, inside one reduction, or across separate reductions, and for which techniques? *Ask: Simon, LoKI instrument scientist.*
3. What lies behind "more than 1000 runs per hour": short kinetic runs, time slices of one run, or reprocessing? *Ask: Simon.*
4. How will ESS files or the catalogue say what role a run plays, and which runs belong together? *Ask: NICOS team, instrument scientists.*
5. What must users find again, for how long, and where: in files, in SciCat, or in the framework? *Ask: Simon, ESS data management.*
6. Which way of working, and which instrument, must the first release serve? *Ask: Simon.*
7. Do visiting users write Python, and how many people reduce at once? *Ask: instrument scientists, user office.*

## Non-goals

- A second catalogue of results next to SciCat. *Simon, 2026-09-30*
- Windows. *Simon, 2026-09-30*
- Knowing technique details such as normalisation, or prescribing how instrument scientists choose calibration and background runs. *Simon, 2026-09-28*
- Knowing whether a reduction workflow is a sciline pipeline or a plain function. *Simon, scoping.md; Simon, 2026-09-28*
- Splitting one reduction into parts by values found only in the data, such as angles read from a log; a technique that needs this runs a first reduction to find them. *Simon, 2026-09-28*
- Steps of one technique, such as applying the reflectometry scale factors back to the curve of each angle; an application built on the framework does that. *Simon, 2026-09-28*

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
- A source is a person and a date, a public document, or code on GitHub. The design docs and user stories are not sources: a claim only they make is Assumed.
- Write about runs, samples, files, and results, not about the design (no record, request, stage, accumulator, and so on).
- Each fact has one home page; other pages link to it. A page that outgrows 100 lines becomes a folder with its own README.
- The repository is public: name people by role, except Simon.
