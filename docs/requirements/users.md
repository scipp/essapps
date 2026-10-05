# Users and ways of working

Users are visiting users, instrument scientists, workflow authors (about 12 people write the ess reduction packages), the team's own application developers, and programs such as AI agents.
They reduce data in three ways: by hand (interactively, in JupyterLab on a VISA machine or a laptop), in batches (hundreds of independent samples), and automatically (each new run as it arrives).
Batch and automatic reduction run in long-running services for many users, so their memory use must be bounded by how they work, not by users freeing memory.
One to three people per instrument work in the reduction software at once.
Results go to SciCat and must say what produced them, but the framework must not become a second catalogue.

## Known

### Who

- Workflow authors: about 12 people committed to the reduction packages of the ess monorepo in the year to October 2026, all writing Python. *[scipp/ess history](https://github.com/scipp/ess/commits/main/packages)*
- Workflow authors: the framework must not depend on how a reduction workflow is implemented (sciline pipeline or plain function), and a workflow must not depend on where it runs. *Simon, 2026-09-04 and 2026-09-28*
- A reduction that needs a change its workflow does not offer, such as a replaced geometry, falls back to a Jupyter notebook; a recurring need becomes an optional input that the workflow authors add. *Simon, 2026-10-05*
- Instrument scientists prepare inputs that the users of later proposals reuse, such as masks, detector calibration, or a direct-beam function; a reduced vanadium, reflectometry reference, or beam centre is not one of them, as each experiment makes its own. *Simon, 2026-10-05*
- Users configure batch reduction, with support from instrument scientists; instrument scientists or users configure automatic reduction. DMSC, perhaps the team that develops the services, keeps them running but configures no reduction. *Simon, 2026-10-05*
- Visiting users: many do not write Python and need forms or graphical interfaces, while some work in notebooks; this holds most for SANS and reflectometry and least for spectroscopy. *Simon, 2026-10-05*
- ESS expects more than 1000 experiments a year, each lasting several days with a couple of users, who reduce data at ESS and after they leave. *Simon, 2026-10-05*
- At most one to three people per instrument work in the reduction software at once; on average far fewer start reductions or call the API at the same time. *Simon, 2026-10-05*
- Application developers: the team writes the user interfaces itself, in Python and with AI assistance, which favours web interfaces, although the team's plotting library plopp is not made for the web. *Simon, 2026-09-04*
- Today users reduce in Jupyter notebooks; reflectometry also has a Jupyter batch-reduction interface, and NMX a command-line reducer. *[reflectometry gui.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/gui.py#L323), [essnmx pyproject.toml](https://github.com/scipp/ess/blob/main/packages/essnmx/pyproject.toml#L51)*
- A user may belong to several proposals. *Simon, 2026-10-02*
- Programs use the framework through an API: AI agents inspect results, and NICOS might one day start a reduction and wait for its result. *Simon, 2026-10-02 and 2026-10-05*

### By hand

- A user configures a reduction workflow, applies it to one run or a few, and looks at the results. *Simon, 2026-09-04*
- The framework has no notion of an intermediate result: a user sees the outputs a workflow exposes, and an application may chain several workflows to show the results between them. A value inside a workflow that it does not expose needs a plain notebook. *Simon, 2026-10-05*
- Interactive users work in JupyterLab on VISA machines ([systems](systems.md)) or on laptops. *Simon, 2026-10-02*
- The result of one reduction feeds others, for example a beam centre feeds the sample reductions, and a person may do the chaining by hand. *Simon, 2026-09-04*

### In batches

- One reduction workflow is applied to many runs, with most parameters shared and some set per run or per sample. *Simon, 2026-09-04*
- A batch can hold hundreds of independent samples, each possibly a sum of runs or angles; samples are reduced independently and never merged. *Simon, 2026-10-02*
- SANS and reflectometry measure hundreds of runs or samples per hour, and serving their batch reduction is essential. *Simon, 2026-10-02*
- Parameters may differ per run within one sum, and the framework must not require that summed runs be reduced alike. *Simon, 2026-09-28*
- Users must find the results of a batch reduction they started the day before. *Simon, 2026-10-05*

### Automatically

- A configured reduction workflow is applied to every new run. *Simon, 2026-09-04*
- Reducing a growing series again at every new run, such as all angles of one sample so far, must be possible, but not as the only behaviour. *Simon, 2026-09-28*
- Batch and automatic reduction, and large work spread over the cluster, run as services, so leaks and peak memory must be bounded by how the services work, not by users freeing memory. *Simon, 2026-10-02*

### Provenance and the catalogue

- The configuration of batch reductions must give a provenance graph; the result of one reduction sometimes feeds later ones, but most reductions are independent, so the graph is mostly many small separate trees. *Simon, 2026-09-04 and 2026-10-05*
- The framework must not replicate SciCat: the lasting history of what ran belongs in SciCat, and the framework only writes to SciCat what that history needs.
  A published result therefore says what produced it without access to the framework. *Simon, 2026-09-30 and 2026-10-05*
- Files and provenance are kept in the long term only in SciCat. Provenance inside result files depends on the technique: reflectometry's ORSO format records part of it, and many formats record none. *Simon, 2026-10-05*

## Assumed

- Instrument scientists who configure automatic reduction must see, without reading logs, why it reduced nothing. *Story E2; ask instrument scientists.*
- By hand: changing a binning or a mask shows the new result within about a second. *Design docs say "under a second"; measure on real LoKI files with the LoKI instrument scientist.*
- By hand: users look at slices of large results, such as cuts through a 4D volume, as fast as a slider moves, without moving the whole result to their screen. *Scoping asks for a data slicer, story B4; ask spectroscopy instrument scientists.*
- In batches: a night of long runs continues with the user's laptop closed, and the next morning the user reads why some failed and reruns them. *Story D2 (30 NMX runs); ask the NMX instrument scientist.*
- In batches: a batch of hundreds started with a wrong shared parameter can be stopped and restarted at once, and invalid parameters are refused before anything runs. *Stories D3 and D4; ask instrument scientists how often this happens.*
- Automatically: no run is reduced twice, also after a restart of the automatic-reduction service or when a file arrives again. *Stories E1 and D7; ask instrument scientists whether a duplicate result does harm.*
- Provenance: every result the framework keeps, not only published ones, can say which raw runs, parameter values, and software versions produced it. *Design README and story S8; ask Simon.*
- Users find results by what they know, such as sample and temperature, not by an identifier. *Stories D1, D2, D6; ask instrument scientists.*
- Later: a user can rerun a result in its original software environment after upgrades, or learns before running that this is impossible. *Story F2; ask Simon and DMSC.*

## Open

- Nothing at present.
