# Spectroscopy

BIFROST, CSPEC, T-REX, MIRACLES and VESPA at ESS; only BIFROST has a reduction workflow (essspectroscopy), and it has run on simulated data only, since ESS plans first neutrons for early 2027.
The BIFROST code expects one run to hold a whole scan of sample angles; each event is normalised by the monitor and proton charge of its angle setting, then binned into a Q-E cut or written, observation by observation, to an SQW file for Horace.
Simon expects 4D volumes of hundreds of GB; at ISIS, Horace files reach 10 to 500 GB (136 GB for 276 runs).
Open: is that volume a fixed grid that each run adds to, or a list of observations that grows with every run, and how many runs does one experiment combine?

## Known

### Instruments

- BIFROST passed its safety readiness review in December 2025; like every ESS instrument, it has measured no neutrons yet ([data](data.md)). *[ESS, 2026-02-04](https://ess.eu/article/2026/02/04/ess-neutron-rainbow-instrument-bifrost-ready-receive-neutrons)*
- essspectroscopy has a workflow for BIFROST only; CSPEC, MIRACLES and T-REX have empty modules since 2026-10-01, and VESPA has none. *[ess.cspec](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/cspec/__init__.py), [ess.trex](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/trex/__init__.py), [ess.miracles](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/miracles/__init__.py)*
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

- Spectroscopy needs to accumulate into 4D volumes that can be hundreds of GB. *Simon, 2026-10-02*
- At ISIS, Horace SQW files range from 10 to 500 GB; a 276-run MERLIN data set gave 136 GB, and building a 142 GB file from 231 runs took 150 minutes. *[Ewings et al. 2016](https://arxiv.org/abs/1604.05895)*
- essspectroscopy writes SQW itself because Horace could not convert BIFROST NXSPE files: it needed a 141 GB array and failed on a 256 GB machine. *[ADR 0001](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/docs/developer/adr/0001-relegate-nxspe-support.md)*
- The SQW export writes one row of 9 float32 values (36 B) per pixel, angle setting and incident-energy bin, empty ones included: 49 MB per angle setting with 100 energy bins.
  It builds the whole file in memory from one run and "requires large amounts of memory". *[sqw.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L59-L69), [L201-L225](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L201-L225), [L320-L328](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L320-L328)*
- The binned image in the SQW file holds, per bin, the mean of the observations and their number, not their sum. *[sqw.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/sqw.py#L290-L296)*
- Live reduction (esslivedata) accumulates for BIFROST a Q-E cut of 5 arcs × 100 × 100 bins from the start of a run, resets it when a run starts or stops, and sets the proton charge to 1. *[factories.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/bifrost/factories.py#L138-L155), [specs.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/instruments/bifrost/specs.py#L151-L166), [workflow_spec.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/config/workflow_spec.py#L434-L441)*
- That live cut sums normalised event weights over all angle settings and keeps no count of how many settings cover a bin. *[live.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/live.py#L107-L166)*

## Assumed

- One BIFROST file holds a whole a3 scan rather than one angle; the code itself says "we currently do not know enough about how ESS NeXus files will be written for real measurements". *From [nexus.py](https://github.com/scipp/ess/blob/main/packages/essspectroscopy/src/ess/bifrost/io/nexus.py#L224-L229); check with the BIFROST instrument scientist and the first real files.*
- CSPEC and T-REX write one run per sample angle, so a rotation scan of 100 to 300 angles is 100 to 300 runs. *From ISIS practice (186 to 276 runs per Horace data set); check with the CSPEC and T-REX instrument scientists.*
- What users take home is the SQW file, which keeps every observation; a summed 4D grid serves as a quick look. *From ADR 0001 and Horace practice; check with the BIFROST instrument scientist.*
- A 4D grid summed over runs must keep the normalisation or the number of observations per bin next to the intensity, because angle settings cover bins unevenly. *From the SQW image (mean and count per bin); check with the BIFROST instrument scientist.*
- BIFROST produces 1e5 to 1e6 events per second and CSPEC 1e6 to 1e7, with 0.4 to 0.75 million CSPEC pixels. *From [esslivedata benchmark targets](https://github.com/scipp/esslivedata/blob/main/docs/about/ess_requirements.py#L53-L61) of 2023 with no stated origin, which give BIFROST 5,000 pixels against 13,500 in the code; check with the detector group.*
- Users want results combined across runs during an experiment, not only after it. *From the team's stories; no code combines BIFROST runs; check with the BIFROST instrument scientist.*

## Open

- Is the 4D volume of hundreds of GB a histogram on a fixed grid, or the list of all observations as in an SQW file? *A grid has a fixed size and each run adds to it; an observation list grows with every run and does not fit in memory. Ask: Simon, BIFROST instrument scientist.*
- How many runs does one BIFROST, CSPEC or T-REX experiment combine, and how long is one run? *Decides the number of inputs to one result and the time over which it grows. Ask: BIFROST, CSPEC and T-REX instrument scientists.*
- Into what grid do users bin a 4D volume, for a quick look and for keeping? *300 bins per axis with three float64 values per bin takes 194 GB; 100 bins per axis take 2.4 GB. Ask: BIFROST and CSPEC instrument scientists.*
- Do users look at a combined result while runs are still being added, and how often? *Decides whether a partial result must be readable while combining goes on. Ask: BIFROST instrument scientist.*
- Which vanadium, empty-can or background runs does a BIFROST result need, and how does a user pick them? *Decides which runs one result needs. Ask: BIFROST instrument scientist.*
- How many events does one run hold at 2 MW, and how large is its file? *At 1e7 events per second, one hour gives 3.6e10 events. Ask: detector group, CSPEC instrument scientist.*
- With repetition-rate multiplication, is each incident energy of one run a separate result? *Decides whether one run yields one result or several. Ask: CSPEC and T-REX instrument scientists.*
- Is combining runs into a 4D volume needed for the first release, with BIFROST, or later, with CSPEC and T-REX? *Decides when the largest results must be supported. Ask: Simon.*
- Should each new run be reduced automatically, for example to an SQW file, and for which instrument first? *Decides automatic reduction for spectroscopy, for the first release or later. Ask: BIFROST instrument scientist.*
- Do MIRACLES and VESPA need rotation scans or large volumes at all? *If not, their results stay small and per run. Ask: MIRACLES and VESPA instrument scientists.*
