# Users and ways of working

Users are visiting users, instrument scientists, workflow authors (about 12 people write the ess reduction packages), operators, and the team's own application developers.
They reduce data in three ways: by hand (interactively, in JupyterLab on a VISA machine or a laptop), in batches (hundreds of independent samples), and automatically (each new run as it arrives).
Batch and automatic reduction run as a long-running service for many users, so its memory use must be bounded by how it works, not by users freeing memory.
Results go to SciCat and must say what produced them, but the framework must not become a second catalogue.
Open: whether visiting users write Python, how many people reduce at once, and how long results must be kept.

## Known

### Who

- Workflow authors: about 12 people committed to the reduction packages of the ess monorepo in the year to October 2026, all writing Python. *[scipp/ess history](https://github.com/scipp/ess/commits/main/packages)*
- Workflow authors: the framework must not depend on how a reduction workflow is implemented (sciline pipeline or plain function), and a workflow must not depend on where it runs. *Simon, scoping.md; Simon, 2026-09-28*
- Instrument scientists choose calibration and background runs in different ways, and the framework must not prescribe how they operate their instruments. *Simon, 2026-09-28*
- Application developers: the team writes the user interfaces itself, in Python and with AI assistance, which favours web interfaces, although the team's plotting library plopp is not made for the web. *Simon, scoping.md*
- Today users reduce in Jupyter notebooks; reflectometry also has a Jupyter batch-reduction interface, and NMX a command-line reducer. *[reflectometry gui.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/gui.py#L323), [essnmx pyproject.toml](https://github.com/scipp/ess/blob/main/packages/essnmx/pyproject.toml#L51)*
- A user may belong to several proposals. *Simon, 2026-10-02*

### By hand

- A user configures a reduction workflow, applies it to one run or a few, and may inspect intermediate results interactively. *Simon, scoping.md*
- Interactive users work in JupyterLab on VISA machines ([systems](systems.md)) or on laptops. *Simon, 2026-10-02*
- The result of one reduction feeds others, for example a beam centre feeds the sample reductions, and a person may do the chaining by hand. *Simon, scoping.md*

### In batches

- One reduction workflow is applied to many runs, with most parameters shared and some set per run or per sample. *Simon, scoping.md*
- A batch can hold hundreds of independent samples, each possibly a sum of runs or angles; samples are reduced independently and never merged. *Simon, 2026-10-02*
- SANS and reflectometry measure hundreds of runs or samples per hour, and serving their batch reduction is essential. *Simon, 2026-10-02*
- Parameters may differ per run within one sum, and the framework must not require that summed runs be reduced alike. *Simon, 2026-09-28*

### Automatically

- A configured reduction workflow is applied to every new run. *Simon, scoping.md*
- Reducing a growing series again at every new run, such as all angles of one sample so far, must be possible, but not as the only behaviour. *Simon, 2026-09-28*
- Batch and automatic reduction, and large work spread over the cluster, run as a service, so leaks and peak memory must be bounded by how the service works, not by users freeing memory. *Simon, 2026-10-02*

### Provenance and the catalogue

- The configuration of batch reductions must give a provenance graph. *Simon, scoping.md*
- Input runs and results come from and go to SciCat, or another data catalogue ([systems](systems.md)). *Simon, scoping.md*
- The framework must not replicate SciCat. *Simon, 2026-09-30*

## Assumed

- Visiting users: many do not write Python and need forms or graphical interfaces, while some work in notebooks. *From the user stories (batch form, web interface); ask instrument scientists of the first instruments.*
- Instrument scientists prepare shared inputs, such as a reduced vanadium or a beam centre, often in a commissioning proposal, for the users of later proposals. *From user story C2; ask instrument scientists and the user office.*
- Operators: a DMSC team runs the batch and automatic-reduction service and must see, without reading logs, why automatic reduction reduced nothing. *From story E2; ask DMSC.*
- By hand: changing a binning or a mask shows the new result within about a second. *Design docs say "under a second"; measure on real LoKI files with the LoKI instrument scientist.*
- By hand: users look at slices of large results, such as cuts through a 4D volume, as fast as a slider moves, without moving the whole result to their screen. *Scoping asks for a data slicer, story B4; ask spectroscopy instrument scientists.*
- In batches: a night of long runs continues with the user's laptop closed, and the next morning the user reads why some failed and reruns them. *Story D2 (30 NMX runs); ask the NMX instrument scientist.*
- In batches: a batch of hundreds started with a wrong shared parameter can be stopped and restarted at once, and invalid parameters are refused before anything runs. *Stories D3 and D4; ask instrument scientists how often this happens.*
- Automatically: no run is reduced twice, also after a restart of the service or when a file arrives again. *Stories E1 and D7; ask instrument scientists whether a duplicate result does harm.*
- Provenance: every result, intermediate ones included, can say which raw runs, parameter values, and software versions produced it. *Design README and story S8; ask Simon whether this holds for every result or only for kept and published ones.*
- Provenance: a published result says what produced it six months later, without access to the framework. *Story F1; check with the SciCat team that derived entries can hold this.*
- Users read batch results the next morning and weeks later, and find them by what they know, such as sample and temperature, not by an identifier. *Stories D1, D2, D6; ask instrument scientists.*
- Later: a user can rerun a result in its original software environment after upgrades, or learns before running that this is impossible. *Story F2; ask Simon and DMSC.*
- Live reduction of the event stream stays in esslivedata, and this framework starts from runs written to files. *Inferred from scoping.md; ask Simon.*

## Open

- How many visiting users does ESS expect per year, and how many people reduce data at once per instrument? *Decides the load the service must handle. Ask: user office, instrument scientists.*
- Do visiting users write Python? *Decides whether a Python interface or forms and graphical interfaces come first. Ask: instrument scientists of the first instruments.*
- Which way of working must the first release serve, and for which instrument? *Earlier design docs disagree (automatic first, against interactive work deciding adoption). Ask: Simon.*
- How long must the history of what ran and the result files be kept, and does ESS set an embargo or retention period? *No public ESS policy was found. Ask: ESS data management.*
- Must a result combined over a day, such as a rotation scan, survive a crash of the user's notebook? *Decides whether it can live in the user's own process. Ask: CSPEC and BIFROST instrument scientists.*
- Do AI agents or other programs need an interface to inspect results? *Scoping lists it with a question mark. Ask: Simon.*
