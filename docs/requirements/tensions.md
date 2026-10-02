# Tensions between needs

Some needs pull against each other, and how each pull is settled shapes the framework more than any single need does.
Settled: a history of which reductions were done instead of a second catalogue, results kept in memory only in interactive work, and a large volume held once.
Open: where runs are summed, whether a result can grow by adding runs when some methods need all inputs at once, and how runs are paired without knowing the technique.
Most important open question: are runs summed before reduction into merged files, inside one reduction, or across separate reductions?

Each tension has a line with its resolution or question, and one sub-bullet for each side.
A settled tension names the decision that settles it; the decision itself lives in the design ([ADRs](../developer/adr/index.md)).

## Settled

- **Finding results again versus a second catalogue.** Keep a history of which reductions were done, with no results in it, for the life of the proposal; a result that must last is written to a file once or published to SciCat. *Simon, 2026-09-30 and 2026-10-02; [ADR 0001](../developer/adr/0001-history-as-an-event-log.md), [essapps#23](https://github.com/scipp/essapps/issues/23)*
  - The result of one reduction feeds later ones, and batch configuration must give a provenance graph. *Simon, scoping.md*
  - The framework must not replicate SciCat, and several places where results live are hard to explain to users. *Simon, 2026-09-30*
- **Fast interactive work versus bounded memory.** Interactive work keeps results in memory in the user's own process, whose end frees them; batch and automatic reduction write each result to a file as soon as it is computed and keep nothing. Open: how the service frees the memory of a user whose notebook ended without saying so. *Simon, 2026-10-02; [ADR 0002](../developer/adr/0002-the-client-is-the-lifetime.md), [essapps#27](https://github.com/scipp/essapps/issues/27)*
  - A user inspects intermediate results interactively. *Simon, scoping.md*
  - Batch and automatic reduction must bound leaks and peak memory by how the service works. *Simon, 2026-10-02*
- **A large volume held once versus looking at it while it grows.** Hold one copy and add each run in place; a look at the volume is valid until the next run is added, and keeping an earlier state takes an explicit copy. *Simon, 2026-10-02; [ADR 0003](../developer/adr/0003-accumulators-add-in-place.md)*
  - Spectroscopy accumulates 4D volumes of up to hundreds of GB, and a standard VISA machine has 64 GB. *Simon, 2026-10-02; [systems](systems.md)*
  - Users look at cuts through the volume after each angle while the scan runs. *User story D7 only; ask CSPEC and BIFROST instrument scientists whether they look here or in esslivedata.*
- **A simple system versus every technique's way of working.** Keep only what a stated need forces, and trace each mechanism to an item on these pages. *Simon, 2026-09-30*
  - The goal is a simple system with predictable behaviour; anything not bound by a strict requirement goes. *Simon, 2026-09-30*
  - The framework must serve all cases: some runs merged and others not, per-run parameters in a sum, a series reduced again at each new run but not only that. *Simon, 2026-09-28*

## Open

- **Where are runs summed: before reduction into merged files, inside one reduction, or across separate reductions?** *Decides whether the framework needs sums across reductions, and how provenance reaches through a merged file to its runs. Ask: LoKI instrument scientist, Simon.*
  - Summing numerators and denominators over runs and normalising once must be efficient for several instruments, without the framework knowing about normalisation. *Simon, 2026-09-28*
  - SANS run merging might be a pre-processing step that writes merged "raw" files. *Simon, 2026-10-02*
- **Can a result grow by adding runs, when some methods need all inputs at once?** *Decides whether a partial result can be updated by adding, or must be computed again from all inputs so far. Ask: Simon, instrument scientists.*
  - Spectroscopy accumulates into 4D volumes, and a SANS sum does not depend on the order of its runs. *Simon, 2026-10-02; [sans](sans.md)*
  - Reflectometry scales all angles of a sample by one joint fit, NMX fits one scale curve to all orientations, and YMIR scales by the mean of the whole stack, so a new input changes earlier results. *[reflectometry](reflectometry.md), [diffraction](diffraction.md), [imaging](imaging.md)*
- **How can a framework that knows no technique pair each sample with its can, transmission, reference, or open-beam runs?** *Decides how partner runs are found; Simon wants them found from metadata and leaves the criteria open (2026-10-02). Ask: instrument scientists, NICOS team.*
  - The framework must not know technique details, nor prescribe how scientists choose calibration and background runs. *Simon, 2026-09-28*
  - No ESS file says what role a run plays; today a person pairs runs by hand, and ISIS pairs them by run-title conventions. *[data](data.md), [sans](sans.md)*
- **Must a quick reduction in a notebook be fully described, even when it passes masks or corrections as Python functions?** *Decides whether the framework accepts parameters it cannot write down, or leaves such reductions outside. Ask: Simon, authors of the powder, imaging, and reflectometry workflows.*
  - Batch configuration must give a provenance graph, and parameters should be choices from a list rather than Python functions. *Simon, scoping.md; Simon, 2026-09-28*
  - Powder, imaging, and reflectometry workflows take masks and corrections as Python functions. *[powder](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L208-L216), [reflectometry](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/types.py#L134-L135), [imaging](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/types.py#L76)*
- **Where do users look at a result while it grows: here or in esslivedata?** *Decides whether offline reduction needs partial results at all. Ask: Simon, BIFROST and LoKI instrument scientists.*
  - Users look at cuts through a volume while a scan continues. *User story D7 only.*
  - esslivedata already accumulates BIFROST Q-E cuts, DREAM I(d), and LoKI I(Q) during a run, without full normalisation. *[spectroscopy](spectroscopy.md), [diffraction](diffraction.md), [sans](sans.md)*
- **May one reduction use data of several proposals?** *Deferred to the design of the service (2026-10-02); decides how access is checked. Ask: user office, instrument scientists.*
  - ESS grants access to data per proposal, and a catalogue dataset belongs to one proposal. *[systems](systems.md)*
  - A user may belong to several proposals, and a strict limit of one proposal everywhere was loosened. *Simon, 2026-09-28 and 2026-10-02*
- **Where are large results cut down for a web interface?** *Decides whether the framework must serve parts of a result to a remote screen. Ask: Simon, spectroscopy instrument scientists.*
  - AI-assisted interface development favours web interfaces. *Simon, scoping.md*
  - plopp is not made for the web, and large data should be sliced next to the computation so that only a small part reaches the screen. *Simon, scoping.md*
- **Dropping a bad run from a sum, or starting over?** Simon leans to starting over (2026-09-30, as a question). *Ask instrument scientists how often a run is dropped from a large sum, and how long the sum takes to redo.*
  - A user drops a bad run from a sum without reducing the other runs again. *User story B2 only.*
- **Never reducing a run twice, or removing data?** *Ask DMSC whether data that should not have been uploaded must be erasable, history included.*
  - Automatic reduction must never reduce a run twice, even after a restart, so it must remember which runs it handled. *User stories E1 and H3 only.*
  - A user can remove a file that should not have reached the shared service, with no copy left. *User story A4 only.*
- **Automatic publication, or only results a person chose?** *Ordinary users likely cannot delete a catalogue entry, so a wrong automatic entry stays. Ask: Simon, the SciCat team, instrument scientists.*
