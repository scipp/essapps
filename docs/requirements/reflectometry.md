# Reflectometry

ESTIA and FREIA at ESS; Amor at PSI and Offspec at ISIS run essreflectometry today, and FREIA has code for simulated data only.
A sample is measured at several angles, and each angle's curve is divided by a normalisation run; runs repeated at one angle are concatenated, and one joint fit scales the curves of all angles, so a new angle changes every factor.
Simon gives more than 1000 runs per hour for reflectometry batch reduction; Amor tutorial runs started 11 to 47 minutes apart, so the source of that rate is open.
Amor runs hold 0.2 to 4 M events (22 to 48 MB), and ESTIA aims at measurements of a few seconds.
Open: events per run at ESTIA and FREIA, whether fast measurements are separate runs or time slices of one run, and whether automatic reduction should stitch angles as they arrive.

## Known

### Instruments and pace

- ESTIA is expected to be the first ESS reflectometer in operation, with commissioning with neutrons from 2026. *[ESS Indico, ESTIA early science workshop](https://indico.ess.eu/event/3850/)*
- One ESTIA angle covers Q_max = 2.85 Q_min at 14 Hz, and 0.01 to 0.14 Å⁻¹ with the chopper at a third of the source frequency. *[Andersen et al. 2020, Table 4, §2.3.3](https://doi.org/10.1016/j.nima.2020.163402)*
- ESTIA measures a 1 cm² sample in a few seconds and a 1 mm² sample in a few hours; FREIA measures a full curve in seconds (10 to 15 min at high resolution); both aim at sub-second time resolution for kinetics. *[Andersen et al. 2020, §2.3.1, §2.3.3, §2.4.2, §2.4.3](https://doi.org/10.1016/j.nima.2020.163402)*
- essreflectometry cannot split a run into time slices; ISIS Mantid can, by time or by a log value. *[essreflectometry](https://github.com/scipp/ess/tree/main/packages/essreflectometry/src/ess), [Mantid ISIS Reflectometry](https://github.com/mantidproject/mantid/blob/main/docs/source/interfaces/reflectometry/ISIS%20Reflectometry.rst)*
- Amor tutorial runs 608 to 611 hold 4.0, 1.6, 0.56 and 0.21 M events in files of 48, 31, 23 and 23 MB, and started 11, 28 and 47 minutes apart. *[Amor run 608](https://public.esss.dk/groups/scipp/ess/amor/2/amor2023n000608.hdf) to [611](https://public.esss.dk/groups/scipp/ess/amor/2/amor2023n000611.hdf)*

### Runs and normalisation

- One curve is the sample's events divided by a normalisation run: a supermirror reference at ESTIA and Amor, a direct beam without sample at FREIA and Offspec.
  At FREIA the direct beam must be measured with the same slit and chopper settings as the sample. *[focused reflectometry](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/focused-reflectometry-data-reduction.md), [freia/workflow.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/freia/workflow.py#L99-L108), [offspec notebook cell 3](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/offspec/offspec_reduction.ipynb)*
- The supermirror reference is reduced once and serves every angle and sample; it can be saved to a file and loaded again. *[amor-reduction.ipynb cell 6](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/amor/amor-reduction.ipynb), [load.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/load.py#L100-L127)*
- The reference run is larger than a sample run (Amor: 13.1 M events in 124 MB); reduced, it is a histogram over detector pixel and wavelength, with 2000 wavelength bins in the tutorials. *[focused reflectometry](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/focused-reflectometry-data-reduction.md), [Amor run 614](https://public.esss.dk/groups/scipp/ess/amor/2/amor2023n000614.hdf), [normalization.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/normalization.py#L66-L88)*
- Values in files can be wrong: the Amor tutorial corrects every sample rotation by 0.05° and the chopper phase by hand. *[amor/data.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/amor/data.py#L12-L26)*
- Polarised ESTIA measures each sample at 4 flipper settings, corrected together, and calibrates with 2 reference samples at 4 settings each. *[simulated-spin-flip-sample.ipynb cell 0](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/estia/simulated-spin-flip-sample.ipynb)*

### Combining runs and angles

- Every tutorial measures one sample at 4 angles: Amor runs 608 to 611 at 0.85° to 5.05°, and 3 simulated ESTIA samples at 4 rotations each. *[amor/data.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/amor/data.py#L12-L24), [estia/data.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/estia/data.py#L107-L130)*
- FREIA switches between up to three incident angles without moving the sample; the simulated FREIA run holds three beams at once, each reduced to its own curve from its own detector region, with no scale fitted. *[Andersen et al. 2020, §2.4.2](https://doi.org/10.1016/j.nima.2020.163402), [freia-reflectivity.ipynb cells 0, 9-11](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/freia/freia-reflectivity.ipynb)*
- Runs at one angle are concatenated as events in one reduction, and the first run's sample rotation is used for all; reference runs can be concatenated the same way. *[workflow.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/workflow.py#L25-L88), [tools.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/tools.py#L565-L651), [gui.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/gui.py#L1034-L1044)*
- The curves of one sample's angles are scaled by one maximum-likelihood fit over all overlaps, anchored on the lowest-Q curve or on a critical-edge interval. *[tools.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/tools.py#L394-L490)*
- The Amor tutorial writes the scaled curves into one ORSO file, one dataset per angle; averaging them onto one Q grid (500 bins in the tutorial) is optional. *[amor-reduction.ipynb cells 16-20](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/amor/amor-reduction.ipynb), [amor-reduction-advanced.ipynb cell 19](https://github.com/scipp/ess/blob/main/packages/essreflectometry/docs/user-guide/amor/amor-reduction-advanced.ipynb), [tools.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/tools.py#L491-L564)*

### Batch and automatic

- Reflectometry is a heavy user of batch reduction, producing more than 1000 runs per hour. *Simon, 2026-10-02*
- The Amor batch GUI groups runs by sample name and angle, both read from the file; the user marks reference runs and exclusions by hand, and the reference run carries the same sample name (SM5) as the sample runs. *[gui.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/gui.py#L719-L826), [Amor run 614](https://public.esss.dk/groups/scipp/ess/amor/2/amor2023n000614.hdf)*
- At ISIS, runs with the same title and angle (`th=` in the title) are summed, and runs with the same title at another angle are stitched; automatic reduction polls for new runs and processes each group again when a run joins it. *[Mantid ISIS Reflectometry](https://github.com/mantidproject/mantid/blob/main/docs/source/interfaces/reflectometry/ISIS%20Reflectometry.rst)*
- Live reduction at ESTIA gives intensity over wavelength, Q, and angle and wavelength, corrected for footprint and proton current but not divided by a reference; FREIA has detector views only. *[esslivedata estia/specs.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/estia/specs.py#L161-L187), [freia/specs.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/freia/specs.py#L59-L73)*

## Assumed

- ESTIA still needs several angles per sample to cover a full curve. *One angle covers a factor 2.85 in Q; ask the ESTIA instrument scientist.*
- Users repeat runs at one angle and expect them summed. *Seen at ISIS and in the Amor GUI; the Amor "611+612" example joins runs at 5.05° and 0.65°, so it only demonstrates the code; ask the ESTIA instrument scientist.*
- Runs are grouped by sample name and by angle within a tolerance (run 611 logs 5.0 and 4.999; ISIS uses 0.01). *Ask the ESTIA instrument scientist.*
- One reduced reference serves all samples of an instrument configuration. *From the tutorials and the Amor GUI; ask the ESTIA instrument scientist how often the reference is remeasured.*
- The 1000 runs per hour come from short kinetic runs or from reprocessing many runs, not from one instrument at Amor's pace. *Ask Simon which.*

## Open

- How many events does one ESTIA or FREIA run hold, and how long is it? *Decides memory per reduction and runs per hour. Ask: ESTIA and FREIA instrument scientists.*
- Are fast and kinetic measurements separate runs or time slices of one run? *Time slices make one run give hundreds of results. Ask: ESTIA and FREIA instrument scientists.*
- Should automatic reduction scale and stitch a sample's angles as they arrive, or produce one curve per angle? *Decides whether a new run changes earlier results. Ask: ESTIA instrument scientist.*
- How will a run be marked as sample, reference or direct beam, and its sample and angle group, at ESS? *Decides whether runs can be grouped without a person. Ask: ESTIA instrument scientist, NICOS team.*
- Does FREIA always split one run into up to three curves, and are those stitched? *Decides whether one run yields several results. Ask: FREIA instrument scientist.*
- How long does one reduction take at ESTIA scale? *1000 runs per hour leave 3.6 s per run on one core. Measure on ESTIA files once they exist.*
