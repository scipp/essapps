# Tensions between needs

Some needs pull against each other, and how each pull is settled shapes the framework more than any single need does.
Settled: results kept in memory only in interactive work, a large volume held once, the technique deciding where runs are summed, an application pairing runs, starting over instead of dropping a run from a sum, and only parameters that can be written down.
Open: how long users find results through the framework rather than in SciCat, whether a result can grow by adding runs when some methods need all inputs at once, and where users look at a result while it grows.
Most important open question: how long must users find results through the framework, and what goes to SciCat?

Each tension has a line with its resolution or question, and one sub-bullet for each side.
A settled tension names the decision that settles it; the decision itself lives in the design ([ADRs](../developer/adr/index.md)).

## Settled

- **Fast interactive work versus bounded memory.** Interactive work keeps results in memory in the user's own process, whose end frees them; batch and automatic reduction write each result to a file as soon as it is computed and keep nothing. Open: how a hosted service frees the memory of a user whose notebook ended without saying so. *Simon, 2026-10-02; [ADR 0002](../developer/adr/0002-the-client-is-the-lifetime.md), [essapps#27](https://github.com/scipp/essapps/issues/27)*
  - A user inspects intermediate results interactively. *Simon, 2026-09-04*
  - Batch and automatic reduction must bound leaks and peak memory by how the services work. *Simon, 2026-10-02*
- **A large volume held once versus looking at it while it grows.** Hold one copy and add each run in place; a look at the volume is valid until the next run is added, and keeping an earlier state takes an explicit copy. *Simon, 2026-10-02; [ADR 0003](../developer/adr/0003-accumulators-add-in-place.md)*
  - Spectroscopy accumulates 4D volumes of up to hundreds of GB. *Simon, 2026-10-02; [spectroscopy](spectroscopy.md)*
  - Users look at cuts through the volume after each angle while the scan runs; the cut may be fixed when the reduction starts, chosen anew each time, or both. *User story D7; Simon, 2026-10-05; ask CSPEC and BIFROST instrument scientists whether they look here or in esslivedata.*
- **Where runs are summed.** The technique decides, so the framework prescribes none of: summing before reduction into merged files, inside one reduction, or across separate reductions. *Simon, 2026-10-05*
  - Summing numerators and denominators over runs and normalising once must be efficient for several instruments, without the framework knowing about normalisation. *Simon, 2026-09-28*
  - SANS run merging might be a pre-processing step that writes merged "raw" files. *Simon, 2026-10-02*
- **Pairing runs without knowing the technique.** The application built on the framework pairs each sample with its can, transmission, reference, or open-beam runs, from metadata by criteria it chooses. *Simon, 2026-10-02 and 2026-10-05*
  - The framework must not know technique details, nor prescribe how scientists choose calibration and background runs. *Simon, 2026-09-28*
  - Today a person pairs runs by hand, and ISIS pairs them by run-title conventions; whether ESS files or the catalogue will say what role a run plays is unknown. *[data](data.md), [sans](sans.md)*
- **Dropping a bad run from a sum, or starting over.** Start over; nothing supports dropping one run from a sum. *Simon, 2026-10-05*
  - A user drops a bad run from a sum without reducing the other runs again. *User story B2 only.*
- **Describing every reduction versus parameters given as Python functions.** The framework accepts only parameters it can write down; a reduction that needs a Python function, or another change its workflow does not offer, runs in a plain notebook, and a recurring need becomes an optional input of the workflow. *Simon, 2026-10-05; [users](users.md)*
  - Batch configuration must give a provenance graph, and parameters should be choices from a list rather than Python functions. *Simon, 2026-09-04 and 2026-09-28*
  - Powder, imaging, and reflectometry workflows take masks and corrections as Python functions. *[powder](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L208-L216), [reflectometry](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/types.py#L134-L135), [imaging](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/types.py#L76)*
- **A simple system versus every technique's way of working.** Keep only what a stated need forces, and trace each mechanism to an item on these pages. *Simon, 2026-09-30*
  - The goal is a simple system with predictable behaviour; anything not bound by a strict requirement goes. *Simon, 2026-09-30*
  - The framework must serve all cases: some runs merged and others not, per-run parameters in a sum, a series reduced again at each new run but not only that. *Simon, 2026-09-28*

## Open

- **Finding results again versus a second catalogue.** How long must users find results through the framework, and what goes to SciCat? *Decides what history the framework keeps and for how long; [ADR 0001](../developer/adr/0001-history-as-an-event-log.md) keeps it for the life of the proposal, which is not defined. Ask: Simon, ESS data management.*
  - Users must find the results of a batch reduction started the day before, and they process their data at ESS and after they leave. *Simon, 2026-10-05*
  - The framework must not replicate SciCat, whose job is the lasting history of what ran, and several places where results live are hard to explain to users. *Simon, 2026-09-30 and 2026-10-05*
  - The result of one reduction sometimes feeds later ones, and batch configuration must give a provenance graph; many reductions are independent, so the graph is mostly many small separate trees. *Simon, 2026-09-04 and 2026-10-05*
- **Can a result grow by adding runs, when some methods need all inputs at once?** *Decides whether a partial result can be updated by adding, or must be computed again from all inputs so far. Ask: Simon, instrument scientists.*
  - Spectroscopy accumulates into 4D volumes (BIFROST perhaps only 2D), and a SANS sum does not depend on the order of its runs. *Simon, 2026-10-02 and 2026-10-05; [sans](sans.md)*
  - Reflectometry scales all angles of a sample by one joint fit, NMX fits one scale curve to all orientations, and YMIR scales by the mean of the whole stack, so a new input changes earlier results. *[reflectometry](reflectometry.md), [diffraction](diffraction.md), [imaging](imaging.md)*
- **Where do users look at a result while it grows: here or in esslivedata?** *Decides whether offline reduction needs partial results at all. Ask: Simon, BIFROST and LoKI instrument scientists.*
  - Users look at cuts through a volume while a scan continues. *User story D7 only.*
  - esslivedata already accumulates BIFROST Q-E cuts, DREAM I(d), and LoKI I(Q) during a run, without full normalisation. *[systems](systems.md)*
- **May one reduction use data of several proposals?** *Deferred to the design of the services (2026-10-02); decides how access is checked. Ask: user office, instrument scientists.*
  - ESS grants access to data per proposal, and a catalogue dataset belongs to one proposal. *[systems](systems.md)*
  - A user may belong to several proposals, and a strict limit of one proposal everywhere was loosened. *Simon, 2026-09-28 and 2026-10-02*
- **Where are large results cut down for a web interface?** *Decides whether the framework must serve parts of a result to a remote screen. Ask: Simon, spectroscopy instrument scientists.*
  - AI-assisted interface development favours web interfaces. *Simon, 2026-09-04*
  - plopp is not made for the web, and large data should be sliced next to the computation so that only a small part reaches the screen. *Simon, 2026-09-04*
- **Never reducing a run twice, or removing data?** *Ask DMSC whether data that should not have been uploaded must be erasable, history included.*
  - Automatic reduction must never reduce a run twice, even after a restart, so it must remember which runs it handled. *User stories E1 and H3 only.*
  - A user can remove a file that should not have reached a hosted service, with no copy left. *User story A4 only.*
- **Automatic publication, or only results a person chose?** *Ordinary users likely cannot delete a catalogue entry, so a wrong automatic entry stays. Ask: Simon, the SciCat team, instrument scientists.*
