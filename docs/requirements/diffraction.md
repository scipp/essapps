# Diffraction

DREAM and HEIMDAL (powder), BEER (engineering), MAGiC and NMX (single crystal) at ESS, and DREAM also measures single crystals and has a SANS detector; essdiffraction reduces DREAM and BEER, and essnmx reduces NMX.
A powder result needs a sample, a vanadium and an empty-can run, each read from one file and normalised by its own proton charge or monitor.
essnmx reduces each NMX run alone to an image file of about 2 GB uncompressed, and DIALS, outside ess, combines the files of all crystal orientations.
Open: how many runs one powder sample or one rotation scan has, and whether anyone wants a combined result while runs still arrive.

## Known

- DREAM measures mostly powder but also single crystals, and has a SANS detector bank that needs a SANS workflow; none exists, and the goal is to reuse most of esssans. *Simon, 2026-10-05*

### Powder and engineering

- The DREAM and BEER powder workflows read three runs, sample, vanadium and empty can, one file each; DREAM also reads a calibration file and pixel masks. *[powder/types.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L48-L55), [dream/workflows.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/workflows.py#L123-L131), [dream/parameters.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/parameters.py#L34-L48)*
- Each run is divided by its own proton charge or monitor; sample and empty can are then each divided by the vanadium, and the empty can is subtracted last. *[correction.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L286-L304), [L169-L200](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L169-L200), [L389-L393](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L389-L393)*
- DREAM keeps events up to the result by default, and the empty-can subtraction concatenates the events of both runs, so memory grows with the number of events, not bins. *[dream/workflows.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/dream/workflows.py#L155-L160), [correction.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/correction.py#L389-L393)*
- Vanadium peaks are removed by a fit that "a human should inspect"; the processed vanadium is meant to be saved to a file and reused for sample runs. *[vanadium_processing.ipynb](https://github.com/scipp/ess/blob/main/packages/essdiffraction/docs/user-guide/common/vanadium_processing.ipynb) cells 0 and 20*
- The DREAM detector of the current buildout has 1,286,144 pixels: 491,520 in the mantle, 229,376 in the endcaps, 270,336 in the high-resolution bank and 294,912 in the SANS bank; upgrades might add mantle and endcap pixels for more solid-angle coverage. *[esslivedata dream/views.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/dream/views.py#L38-L72), [L103-L110](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/dream/views.py#L103-L110); Simon, 2026-10-05, from memory*
- A BEER bank has 12 million pixels (12 panels of 1000 × 1000) in real files; the McStas data in essdiffraction has 100,000 per bank. *[esslivedata beer/views.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/beer/views.py#L18), [beer/workflow.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/beer/workflow.py#L38-L41); Simon, 2026-10-05*
- DREAM and BEER aim at ms to sub-second time resolution, and DREAM has a cryo-furnace sample changer from the first day. *[Andersen et al. 2020, §3.1.1, §4.1.2-4.1.3](https://doi.org/10.1016/j.nima.2020.163402)*
- BEER's modulation mode finds peaks in a histogram of all events of a run and sorts each event into a peak; it needs the whole run at once. *[beer/clustering.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/beer/clustering.py#L8-L55)*

### Single crystal

- The essnmx result is one (x, y, time-of-flight) histogram per detector panel, 3 panels of 1280 × 1280 pixels, written in NXlauetof format for DIALS. *[executables.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/executables.py#L276-L298), [workflow_test.py](https://github.com/scipp/ess/blob/main/packages/essnmx/tests/mcstas/workflow_test.py#L67-L68), [workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb) cell 3*
- One reduced NMX file of the test data is 1966 MB uncompressed and 17 MB with the default compression, measured on a standard VISA machine ([systems](systems.md)). *[workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb) cell 11*
- DIALS imports the image files of all orientations together, then finds spots, indexes, refines and integrates; scaling and merging follow in other programs. *[data_workflow_overview.md](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/about/data_workflow_overview.md#L26-L43)*
- essnmx's own scaling concatenates the reflection files of all orientations and fits one wavelength curve to all of them. *[mtz_io.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/mtz_io.py#L214-L217), [scaling.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/scaling.py#L48-L52)*
- NMX reduction reads sample runs only: no vanadium, and no monitor, since "NMX simulations or experiments do not have monitors". *[workflows.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/workflows.py#L222-L227), [executables.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/executables.py#L303-L307)*
- "NMX does not expect users to use python interface directly"; essnmx is a command-line program. *[workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb) cells 0 and 3*
- MAGiC collects a complete half-polarised data set on a 1 mm³ crystal in about 20 minutes, with two detector banks of about 490,000 and 130,000 voxels. *[Andersen et al. 2020, §4.3.3](https://doi.org/10.1016/j.nima.2020.163402); [esslivedata magic/specs.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/magic/specs.py#L66-L76)*
- MAGiC can measure all combinations of polarisation states, more than two; its XYZ setup separates nuclear and magnetic scattering in about 40 minutes on a 10 mm³ crystal. *Simon, 2026-10-05; [Andersen et al. 2020, §4.3.3](https://doi.org/10.1016/j.nima.2020.163402)*
- The essreduce polarisation correction ([sans](sans.md)) needs all four spin channels of a result together, and models two states per polarising device, so the XYZ setup does not fit it. *[correction.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/polarization/correction.py#L179-L214), [types.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/polarization/types.py#L10-L12), [methodology cells 1, 8](https://github.com/scipp/ess/blob/main/packages/essreduce/docs/user-guide/polarization/sans-polarization-analysis-methodology.ipynb)*

## Assumed

- One NMX run is one crystal orientation. *The code reads one rotation per file ([workflows.py](https://github.com/scipp/ess/blob/main/packages/essnmx/src/ess/nmx/workflows.py#L120-L146)); check with the NMX instrument scientist.*
- An NMX data set has tens of orientations of hours each. *From the team's research report, citing a 2026 NMX simulation paper; check with the NMX instrument scientist.*
- A MAGiC rotation scan is combined into one 3D map in reciprocal space, summing data and normalisation separately, as at the CORELLI instrument at SNS. *From the team's research report; check with the MAGiC instrument scientist.*
- The combined MAGiC 3D map is smaller than a spectroscopy 4D volume, even if its Q axes have finer bins. *Simon, 2026-10-05; check with the MAGiC instrument scientist.*
- A MAGiC scan has thousands of orientations. *Simon, 2026-10-05, from instrument scientists years ago; check with the MAGiC instrument scientist.*
- DREAM runs will be split by time or by a sample-environment log: events are sorted by the sample-environment value at their wall-clock time, which adds a dimension to the result ([data](data.md)).
  Sub-second resolution means one data point per sub-second interval, such as one sample-environment value, with many points in one file. *Simon, 2026-10-05; ask the DREAM instrument scientist what ms resolution refers to.*
- A BEER strain scan is one file, not one run per point. *Simon, 2026-10-05; ask the BEER instrument scientist.*

## Open

- How many sample runs does one powder result combine, and are repeated runs of one sample summed or kept as a series (temperature, time)? *Decides whether powder needs combining of runs. Ask: DREAM instrument scientist.*
- How many sample runs share one vanadium, and how would automatic reduction find the vanadium and empty-can runs of a new sample run? *Decides how matching runs are found for automatic reduction. Ask: DREAM instrument scientist.*
- Should every DREAM or HEIMDAL run be reduced automatically, for example with a default vanadium? *Decides whether powder needs automatic reduction, and with which vanadium. Ask: DREAM instrument scientist.*
- How many events does one DREAM run hold, and how long does its reduction take with events kept? *Decides memory and time per reduction. Ask: DREAM instrument scientist, detector group.*
- How many points does a BEER strain scan have? *Decides the size of one result. Ask: BEER instrument scientist.*
- How many orientations does an NMX data set have, and does anyone combine or index images before the last orientation is measured? *Decides whether NMX needs anything beyond one reduction per run. Ask: NMX instrument scientist.*
- Do users look at the combined MAGiC map while the scan runs? *Decides whether single-crystal maps need partial results. Ask: MAGiC instrument scientist.*
- Are MAGiC's polarisation states measured in one run, switched by a flipper log, or in separate runs? *Decides whether one result needs several runs at once. Ask: MAGiC instrument scientist.*
