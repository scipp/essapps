# Diffraction

DREAM and HEIMDAL (powder), BEER (engineering), MAGiC and NMX (single crystal) at ESS; essdiffraction reduces DREAM and BEER, MAGiC is a placeholder, HEIMDAL has no code, essnmx reduces NMX, and their documentation reduces simulated data only.
A powder result needs a sample, a vanadium and an empty-can run, each read from one file and normalised by its own proton charge or monitor; no code sums several runs of one role.
essnmx reduces each NMX run alone to an image file of about 2 GB uncompressed, and DIALS, outside ess, combines the files of all crystal orientations.
Open: how many runs one powder sample or one rotation scan has, and whether anyone wants a combined result while runs still arrive.

## Known

- The DREAM documentation uses McStas and Geant4 simulations, and all BEER data is McStas output. *[dream-powder-reduction.ipynb](https://github.com/scipp/ess/blob/main/packages/essdiffraction/docs/user-guide/dream/dream-powder-reduction.ipynb), [beer/data.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/beer/data.py)*
- The MAGiC workflow "provides no reduction providers yet", and HEIMDAL has no workflow in ess and no configuration in esslivedata. *[magic/workflow.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/magic/workflow.py#L15-L23)*

### Powder and engineering

- The DREAM and BEER powder workflows read three runs, sample, vanadium and empty can, one file each; DREAM also reads a calibration file and pixel masks. *[powder/types.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L48-L55), [dream/workflows.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/workflows.py#L123-L131), [dream/parameters.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/parameters.py#L34-L48)*
- Each run is divided by its own proton charge or monitor; sample and empty can are then each divided by the vanadium, and the empty can is subtracted last. *[correction.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L286-L304), [L169-L200](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L169-L200), [L389-L393](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L389-L393)*
- No code combines several runs of one role; the only combining across inputs is over detector banks, and the DREAM SANS bank "requires a different workflow". *[dream-advanced-powder-reduction.ipynb](https://github.com/scipp/ess/blob/main/packages/essdiffraction/docs/user-guide/dream/dream-advanced-powder-reduction.ipynb) cells 26 and 30, [grouping.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/grouping.py#L238-L255)*
- DREAM keeps events up to the result by default, and the empty-can subtraction concatenates the events of both runs, so memory grows with the number of events, not bins. *[dream/workflows.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/workflows.py#L155-L160), [correction.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L389-L393)*
- Vanadium peaks are removed by a fit that "a human should inspect"; the processed vanadium is meant to be saved to a file and reused for sample runs. *[vanadium_processing.ipynb](https://github.com/scipp/ess/blob/main/packages/essdiffraction/docs/user-guide/common/vanadium_processing.ipynb) cells 0 and 20*
- DREAM has 1.29 million pixels in five banks. *[esslivedata dream/views.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/dream/views.py#L14-L18), [dream/workflows.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/workflows.py#L48-L72)*
- DREAM and BEER aim at ms to sub-second time resolution, and DREAM has a cryo-furnace sample changer from the first day.
  No code splits a run by time or by a sample-environment log. *[Andersen et al. 2020, §3.1.1, §4.1.2-4.1.3](https://doi.org/10.1016/j.nima.2020.163402); [filtering.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/filtering.py)*
- BEER's modulation mode finds peaks in a histogram of all events of a run and sorts each event into a peak; it needs the whole run at once. *[beer/clustering.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/beer/clustering.py#L8-L55)*
- Live reduction accumulates DREAM I(d) from the start of each run with one fixed vanadium file and a proton charge faked from the counts. *[esslivedata dream/factories.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/dream/factories.py#L111-L141)*

### Single crystal

- essnmx reduces one input file at a time; summing several files is described but raises "only a single input file is supported". *[configurations.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/configurations.py#L15-L20), [executables.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/executables.py#L80-L82)*
- The essnmx result is one (x, y, time-of-flight) histogram per detector panel, 3 panels of 1280 × 1280 pixels, written in NXlauetof format for DIALS. *[executables.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/executables.py#L276-L298), [workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb) cell 3*
- One reduced NMX file of the test data is 1966 MB uncompressed and 17 MB with the default compression, measured on a standard VISA machine ([systems](systems.md)). *[workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb) cell 11*
- DIALS imports the image files of all orientations together, then finds spots, indexes, refines and integrates; scaling and merging follow in other programs. *[data_workflow_overview.md](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/about/data_workflow_overview.md#L26-L43)*
- essnmx's own scaling concatenates the reflection files of all orientations and fits one wavelength curve to all of them. *[mtz_io.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/mtz_io.py#L214-L217), [scaling.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/scaling.py#L48-L52)*
- NMX reduction reads sample runs only: no vanadium, and no monitor, since "NMX simulations or experiments do not have monitors". *[workflows.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/workflows.py#L222-L227), [executables.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/executables.py#L303-L307)*
- "NMX does not expect users to use python interface directly"; essnmx is a command-line program. *[workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb) cells 0 and 3*
- Live reduction shows NMX panels at 640 × 640, because a full-resolution panel "publishes 26 MB per update". *[esslivedata nmx/specs.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/nmx/specs.py#L24-L29)*
- MAGiC collects a complete half-polarised data set on a 1 mm³ crystal in about 20 minutes, with two detector banks of about 490,000 and 130,000 voxels. *[Andersen et al. 2020, §4.3.3](https://doi.org/10.1016/j.nima.2020.163402); [esslivedata magic/specs.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/magic/specs.py#L66-L76)*

## Assumed

- One NMX run is one crystal orientation. *The code reads one rotation per file ([workflows.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/workflows.py#L120-L146)); check with the NMX instrument scientist.*
- An NMX data set has tens of orientations of hours each. *From the team's research report, citing a 2026 NMX simulation paper; check with the NMX instrument scientist.*
- A MAGiC rotation scan is combined into one 3D map in reciprocal space, summing data and normalisation separately, as at the CORELLI instrument at SNS. *From the team's research report; check with the MAGiC instrument scientist.*
- DREAM produces 1.3e6 to 7.5e7 events per second and BEER 3e5 to 5e7. *From [esslivedata benchmark targets](https://github.com/scipp/esslivedata/blob/main/docs/about/ess_requirements.py#L48-L69) of 2023 with no stated origin, which also give DREAM 4 to 12 million pixels; check with the detector group.*

## Open

- How many sample runs does one powder result combine, and are repeated runs of one sample summed or kept as a series (temperature, time)? *Decides whether powder needs combining of runs. Ask: DREAM instrument scientist.*
- How does a user pick the vanadium and empty-can runs for a sample run, and how many sample runs share one vanadium? *Decides how matching runs are found for batch and automatic reduction. Ask: DREAM instrument scientist.*
- Should DREAM runs be split by time or by a sample-environment log into many results? *Decides whether one run yields one result or hundreds. Ask: DREAM and HEIMDAL instrument scientists.*
- Should every DREAM or HEIMDAL run be reduced automatically, for example with a default vanadium? *Decides automatic reduction for powder, for the first release or later. Ask: DREAM instrument scientist.*
- How many events does one DREAM run hold, and how long does its reduction take with events kept? *Decides memory and time per reduction. Ask: DREAM instrument scientist, detector group.*
- How many points does a BEER strain scan have, is each point a run, and how many pixels does a BEER bank have (100,000 in essdiffraction, 12 million in esslivedata)? *Decides batch size and memory. Ask: BEER instrument scientist.*
- How many orientations does an NMX data set have, and does anyone combine or index images before the last orientation is measured? *Decides whether NMX needs anything beyond one reduction per run. Ask: NMX instrument scientist.*
- How many orientations does a MAGiC scan have, how large is the combined map, and do users look at it while the scan runs? *Decides memory and partial results for single-crystal maps. Ask: MAGiC instrument scientist.*
- Are the two MAGiC polarisation states measured in one run or in separate runs? *Decides whether one result needs pairs of runs. Ask: MAGiC instrument scientist.*
