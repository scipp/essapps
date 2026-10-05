# Spectroscopy

BIFROST, CSPEC, T-REX, MIRACLES and VESPA at ESS; essspectroscopy so far reduces BIFROST only.
The BIFROST code expects one run to hold a whole scan of sample angles; each event is normalised by the monitor and proton charge of its angle setting, then binned into a Q-E cut or written, observation by observation, to an SQW file for Horace.
Simon expects fixed 4D grids over the Q vector and the energy transfer ΔE, of up to hundreds of GB, that each run adds to; at ISIS, Horace files, which list every observation, reach 10 to 500 GB.
Open: how finely users bin the 4D volume, and how many runs one experiment combines.

## Known

### Instruments

- The BIFROST detector has 13,500 pixels in 45 tube triplets; the workflow reads each triplet separately and concatenates their events within one run. *[detector.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/detector.py#L63-L72), [workflow.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/workflow.py#L151-L161)*
- CSPEC and T-REX use repetition-rate multiplication: up to 10 incident energies per source pulse.
  CSPEC's neighbouring energies "will often" be combined; T-REX's "cannot be straightforwardly combined". *[Andersen et al. 2020, §5 and §5.1.2](https://doi.org/10.1016/j.nima.2020.163402)*

### Runs and normalisation

- The BIFROST workflow reads one sample run and no vanadium, empty-can or background run. *[workflow.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/workflow.py#L137-L145)*
- One BIFROST run can hold a whole scan: the workflow reads the sample angle (a3) and detector-tank angle (a4) as time logs from the file and groups the events by angle setting.
  The test file holds 180 a3 × 2 a4 settings. *[cutting.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/cutting.py#L18-L64), [workflow_test.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/tests/bifrost/workflow_test.py#L73-L81)*
- BIFROST needs two detector-tank positions to cover its Q-E range. *[Andersen et al. 2020, §5.3 and Fig. 28](https://doi.org/10.1016/j.nima.2020.163402)*
- Each event weight is divided by the monitor spectrum and by the proton charge of its run or angle setting before anything is histogrammed or summed. *[normalization.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/normalization.py#L20-L80)*

### Volumes

- Spectroscopy needs to accumulate into 4D volumes of up to hundreds of GB: fixed grids over the Q vector and ΔE that each run adds to, not lists of observations; BIFROST may need only 3D (two Q axes and ΔE). *Simon, 2026-10-02 and 2026-10-05*
- At ISIS, Horace SQW files range from 10 to 500 GB; a 276-run MERLIN data set gave 136 GB, and building a 142 GB file from 231 runs took 150 minutes. *[Ewings et al. 2016](https://arxiv.org/abs/1604.05895)*
- essspectroscopy writes SQW itself because Horace could not convert BIFROST NXSPE files: it needed a 141 GB array and failed on a 256 GB machine. *[ADR 0001](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/docs/developer/adr/0001-relegate-nxspe-support.md)*
- The SQW export writes one row of 9 float32 values (36 B) per pixel, angle setting and incident-energy bin, empty ones included: 49 MB per angle setting with 100 energy bins.
  It builds the whole file in memory from one run and "requires large amounts of memory". *[sqw.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L59-L69), [L201-L225](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L201-L225), [L320-L328](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L320-L328)*
- The binned image in the SQW file holds, per bin, the mean of the observations and their number, not their sum. *[sqw.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L290-L296)*

## Assumed

- A rotation scan of 100 to 300 angles may be one file, as the BIFROST code expects, or one file per angle, as at ISIS (186 to 276 runs per Horace data set); either way the workflow reads one angle at a time, from a file or a section of one. *Simon, 2026-10-05; the BIFROST code says "we currently do not know enough about how ESS NeXus files will be written for real measurements" ([nexus.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/nexus.py#L224-L229)); check with the first real files.*
- With repetition-rate multiplication, one run gives one result that merges all incident energies; variants may give one output per energy, for diagnostics, or keep the energies as substructure of the one result. *Simon, 2026-10-05; ask the CSPEC and T-REX instrument scientists.*
- Besides the 4D grid, users take home an SQW file, which keeps every observation. *From essspectroscopy's ADR 0001 and Horace practice; check with the BIFROST instrument scientist.*
- A 4D grid summed over runs must keep the normalisation or the number of observations per bin next to the intensity, because angle settings cover bins unevenly. *From the SQW image (mean and count per bin); check with the BIFROST instrument scientist.*

## Open

- How many runs does one BIFROST, CSPEC or T-REX experiment combine, and how long is one run? *Decides the number of inputs to one result and the time over which it grows. Ask: BIFROST, CSPEC and T-REX instrument scientists.*
- Into what grid do users bin the 4D volume? *Decides the size of the combined result. ΔE likely has fewer bins than each Q axis (Simon, 2026-10-05); with three float64 values per bin, 300 bins per Q axis and 100 in ΔE take 65 GB, 100 bins on every axis 2.4 GB. Ask: BIFROST and CSPEC instrument scientists.*
- How often do users look at the combined volume while runs are still being added? *Decides how often a cut is taken from a growing volume. Ask: BIFROST instrument scientist.*
- Which vanadium, empty-can or background runs does a BIFROST result need, and how does a user pick them? *Decides which runs one result needs. Ask: BIFROST instrument scientist.*
- How many events does one run hold at 2 MW, and how large is its file? *Decides memory per reduction: at 1e7 events per second, one hour gives 3.6e10 events. Ask: detector group, CSPEC instrument scientist.*
- Should each new run be reduced automatically, for example to an SQW file? *Decides whether spectroscopy needs automatic reduction. Ask: BIFROST instrument scientist.*
