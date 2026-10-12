# Tensions between needs

Some needs pull against each other, and how each pull is settled shapes the framework more than any single need does.
Settled: results kept in memory while users work with them and written only when someone asks, a large volume held once, the technique deciding where runs are summed, applications pairing runs, only parameters stored as values, a simple system, and results that someone asked to keep found again for days to weeks, not longer.
Open: whether one reduction may use data of several proposals, where large results are cut down for a web interface, whether data must be erasable when no run may be reduced twice, and whether results reach SciCat automatically.

Each tension has a line with its resolution or question, and one sub-bullet for each side.
A settled tension names the decision that settles it; the decision itself lives in the design ([ADRs](../developer/adr/index.md)).

## Settled

- **Fast interactive work versus bounded memory.** Results stay in memory while a user works with them: in the user's own process for interactive work, such as a notebook, and on the service under a limit for each notebook or application connected to it. A result is written to a file only when someone asks to keep it. Batch and automatic reduction ask for the results they keep as they submit, so they keep nothing in memory but what runs are still being added to, such as a growing volume (next tension), with its memory bounded by how the service works. *Simon, 2026-10-02 and 2026-10-05; [ADR 0002](../developer/adr/0002-a-value-lives-while-something-keeps-it.md)*
  - A user looks at the results of a reduction while deciding what to do next ([users](users.md)). *Simon, 2026-09-04*
  - Batch and automatic reduction must bound leaks and peak memory by how the services work ([users](users.md)). *Simon, 2026-10-02*
- **A large volume held once versus looking at it while it grows.** Hold one copy and add each run in place; a cut through the volume can be made at any time without racing the addition, and no earlier state of the volume is kept. *Simon, 2026-10-02 and 2026-10-05; [ADR 0003](../developer/adr/0003-accumulators-add-in-place.md)*
  - Spectroscopy accumulates 4D volumes of up to hundreds of GB ([spectroscopy](spectroscopy.md)). *Simon, 2026-10-02*
  - Users look at 1D or 2D cuts through the volume while the scan runs; the cut may be fixed when the reduction starts, chosen anew each time, or both. *User story D7; Simon, 2026-10-05*
- **Where runs are summed.** The technique decides, so the framework prescribes none of: summing before reduction into merged files, inside one reduction, or across separate reductions. *Simon, 2026-10-05*
  - Summing numerators and denominators over runs and normalising once must be efficient for several instruments, without the framework knowing about normalisation; to grow such a sum without computing it again, both must be kept until the division. *Simon, 2026-09-28 and 2026-10-05*
  - SANS run merging might be a pre-processing step that writes merged "raw" files ([sans](sans.md)). *Simon, 2026-10-02*
- **Pairing runs without knowing the technique.** Applications built on the framework, including interfaces that configure batch and automatic reduction, know the technique and pair each sample with its can, transmission, reference, or open-beam runs, from metadata by criteria they choose; the framework knows none of this. *Simon, 2026-10-02 and 2026-10-05*
  - The framework must not know technique details, nor prescribe how scientists choose calibration and background runs ([non-goals](README.md#non-goals)). *Simon, 2026-09-28*
  - Automatic reduction in the first release must find the background and normalisation runs of each new sample run ([README](README.md#goal)); today a person pairs runs by hand. *Simon's plan, 2026-10-05; [data](data.md)*
- **Recording every reduction versus parameters given as Python functions.** The framework accepts only parameters it can store as values, such as numbers, text, files, or a choice from a list, and runs no Python code that a user enters; where a workflow needs a function, such as a mask, its authors offer a list to choose from. Any other change a workflow does not offer runs in a plain notebook ([users](users.md)). *Simon, 2026-10-05*
  - Batch configuration must give a provenance graph, and parameters should be choices from a list rather than Python functions. *Simon, 2026-09-04 and 2026-09-28*
  - Powder, imaging, and reflectometry workflows take masks and corrections as Python functions. *[powder](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L208-L216), [reflectometry](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/types.py#L134-L136), [imaging](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/types.py#L76)*
- **A simple system versus every technique's way of working.** Keep only what a stated need forces, and trace each mechanism to an item on these pages. *Simon, 2026-09-30*
  - The goal is a simple system with predictable behaviour; anything not bound by a strict requirement goes. *Simon, 2026-09-30*
  - The framework must serve all cases: some runs merged and others not, per-run parameters in a sum, a series reduced again at each new run but not only that. *Simon, 2026-09-28*
- **Finding results again versus a second catalogue.** The framework keeps the results someone asked to keep for as long as users consider their work ongoing, as with an application they leave open; for batch and automatic reduction that means days to weeks. Other results are kept only while a user works with them. A limit is acceptable, and a result needed for longer goes to SciCat. *Simon, 2026-10-05; [ADR 0004](../developer/adr/0004-history-is-append-only-lists.md)*
  - Reductions can take hours or read data arriving over days, users must find the results of a batch reduction started the day before, and they process their data at ESS and after they leave ([users](users.md)). *Simon, 2026-10-05*
  - The framework must not replicate SciCat, through which alone results and their provenance are found in the long term, and should keep as little bookkeeping as it can ([users](users.md)). *Simon, 2026-09-30 and 2026-10-05*

## Open

- **May one reduction use data of several proposals?** *Deferred to the design of the services (2026-10-02); decides how access is checked. Ask: user office, instrument scientists.*
  - ESS grants access to data per proposal, and a catalogue dataset belongs to one proposal. *[systems](systems.md)*
  - A user may belong to several proposals, and users of later proposals reuse inputs that instrument scientists prepared, such as masks or a direct-beam function ([users](users.md)). *Simon, 2026-10-02 and 2026-10-05*
- **Where are large results cut down for a web interface?** *Decides whether the framework must serve parts of a result to a remote screen. Ask: Simon, spectroscopy instrument scientists.*
  - AI-assisted interface development favours web interfaces ([users](users.md)). *Simon, 2026-09-04*
  - plopp is not made for the web, and large data should be sliced next to the computation so that only a small part reaches the screen. *Simon, 2026-09-04*
- **Never reducing a run twice, or removing data?** *Decides what may remain of a removed file. Ask: DMSC, whether data that should not have been uploaded must be erasable.*
  - Automatic reduction must never reduce a run twice, even after a restart, so it must remember which runs it handled ([users](users.md)). *User story E1 and system story H3 only.*
  - A user can remove a file that should not have reached the batch- or automatic-reduction service, with no copy left. *System story A4 only.*
- **Results in SciCat automatically, or only those a person chose?** *Decides whether batch and automatic reduction create catalogue entries without a person. Ask: Simon, the SciCat team, instrument scientists.*
  - Batch reduction must put its results into SciCat, or at least let users do so ([systems](systems.md)). *Simon, 2026-10-05*
  - Ordinary users likely cannot delete a catalogue entry, so a wrong automatic entry stays. *Inferred from [systems](systems.md): only configured groups may delete.*
