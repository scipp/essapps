# Tensions between needs

Some needs pull against each other, and how each pull is settled shapes the framework more than any single need does.
Settled: results kept in memory only in interactive work, a large volume held once, the technique deciding where runs are summed, applications pairing runs, only parameters that can be written down, a simple system, and results kept only while users work with them.
Open: whether one reduction may use data of several proposals, where large results are cut down for a web interface, whether data must be erasable when no run may be reduced twice, and whether results reach SciCat automatically.

Each tension has a line with its resolution or question, and one sub-bullet for each side.
A settled tension names the decision that settles it; the decision itself lives in the design ([ADRs](../developer/adr/index.md)).

## Settled

- **Fast interactive work versus bounded memory.** Interactive work, such as a notebook, runs in the user's own process, which keeps results in memory and frees them when it ends; batch and automatic reduction write each result to a file as soon as it is computed and keep nothing. *Simon, 2026-10-02 and 2026-10-05; [ADR 0002](../developer/adr/0002-the-client-is-the-lifetime.md)*
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
  - Today a person pairs runs by hand, and ISIS pairs them by run-title conventions; whether ESS files or the catalogue will say what role a run plays is unknown. *[data](data.md), [sans](sans.md)*
- **Describing every reduction versus parameters given as Python functions.** The framework accepts only parameters it can write down and runs no Python code that a user enters; where a workflow needs a function, such as a mask, its authors offer a list to choose from. Any other change a workflow does not offer runs in a plain notebook ([users](users.md)). *Simon, 2026-10-05*
  - Batch configuration must give a provenance graph, and parameters should be choices from a list rather than Python functions. *Simon, 2026-09-04 and 2026-09-28*
  - Powder, imaging, and reflectometry workflows take masks and corrections as Python functions. *[powder](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L208-L216), [reflectometry](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/types.py#L134-L135), [imaging](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/types.py#L76)*
- **A simple system versus every technique's way of working.** Keep only what a stated need forces, and trace each mechanism to an item on these pages. *Simon, 2026-09-30*
  - The goal is a simple system with predictable behaviour; anything not bound by a strict requirement goes. *Simon, 2026-09-30*
  - The framework must serve all cases: some runs merged and others not, per-run parameters in a sum, a series reduced again at each new run but not only that. *Simon, 2026-09-28*
- **Finding results again versus a second catalogue.** The framework keeps results while the user considers the work running, as with a long-running application, which means days to weeks for batch and automatic reduction; a limit is acceptable, and a result needed for longer goes to SciCat. *Simon, 2026-10-05; [ADR 0001](../developer/adr/0001-history-as-an-event-log.md) keeps history for the life of the proposal and must be amended, [essapps#31](https://github.com/scipp/essapps/issues/31)*
  - Reductions can take hours or read data arriving over days, users must find the results of a batch reduction started the day before, and they process their data at ESS and after they leave ([users](users.md)). *Simon, 2026-10-05*
  - The framework must not replicate SciCat, which alone keeps files and provenance in the long term, and should keep as little bookkeeping as it can ([users](users.md)). *Simon, 2026-09-30 and 2026-10-05*

## Open

- **May one reduction use data of several proposals?** *Deferred to the design of the services (2026-10-02); decides how access is checked. Ask: user office, instrument scientists.*
  - ESS grants access to data per proposal, and a catalogue dataset belongs to one proposal. *[systems](systems.md)*
  - A user may belong to several proposals, and a strict limit of one proposal everywhere was loosened. *Simon, 2026-09-28 and 2026-10-02*
- **Where are large results cut down for a web interface?** *Decides whether the framework must serve parts of a result to a remote screen. Ask: Simon, spectroscopy instrument scientists.*
  - AI-assisted interface development favours web interfaces. *Simon, 2026-09-04*
  - plopp is not made for the web, and large data should be sliced next to the computation so that only a small part reaches the screen. *Simon, 2026-09-04*
- **Never reducing a run twice, or removing data?** *Ask DMSC whether data that should not have been uploaded must be erasable, history included.*
  - Automatic reduction must never reduce a run twice, even after a restart, so it must remember which runs it handled. *User stories E1 and H3 only.*
  - A user can remove a file that should not have reached a hosted service, with no copy left. *User story A4 only.*
- **Results in SciCat automatically, or only those a person chose?** *Ask: Simon, the SciCat team, instrument scientists.*
  - Batch reduction must put its results into SciCat, or at least let users do so ([systems](systems.md)). *Simon, 2026-10-05*
  - Ordinary users likely cannot delete a catalogue entry, so a wrong automatic entry stays ([systems](systems.md)).
