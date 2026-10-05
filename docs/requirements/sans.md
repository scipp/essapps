# SANS

LoKI and SKADI at ESS; LoKI has 3.2 M pixels, and SKADI has no reduction code yet.
One I(Q) needs runs in up to five roles (sample, can, a transmission run for each, empty beam) plus a direct-beam function, a beam centre and masks; today a person names every run by hand.
The runs of one sample are summed; the 9 LoKI detector banks give 9 separate curves today, and instrument scientists want them merged into one as well.
Samples are independent of each other, and a batch may hold hundreds of them.
Open: events per run at LoKI, and what automatic reduction should produce for each new run.

## Known

### Instruments and sizes

- LoKI has 9 detector banks at about 1.5 m, 3 m and 5 to 10 m from the sample, with 3,211,264 pixels in total; SKADI has 3 banks. *[loki/workflow.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/loki/workflow.py#L31-L41), [LoKI test file](https://public.esss.dk/groups/scipp/ess/loki/3/loki-coda-5-pulses.hdf), [Andersen et al. 2020, §2.1.2, §2.2.2](https://doi.org/10.1016/j.nima.2020.163402)*
- LoKI is designed for single-shot kinetic measurements on sub-second time scales. *[Andersen et al. 2020, §2.1.1](https://doi.org/10.1016/j.nima.2020.163402)*

### Runs and their roles

- esssans knows five run roles: sample, can, empty beam, and a transmission run for sample and for can; sample and can take a list of runs, the others one run each. *[types.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/sans/types.py#L42-L62), [parameters.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/sans/parameters.py#L81-L95)*
- One run can fill several roles: the LoKI tutorial uses run 60392 as can transmission and as empty beam. *[loki/data.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/loki/data.py#L95-L100)*
- The direct-beam function is itself a result: 6 iterations of reducing a standard sample of known I(Q), with its own sample, can, transmission and empty-beam runs. *[loki-direct-beam.ipynb cells 0, 11, 19](https://github.com/scipp/ess/blob/main/packages/esssans/docs/user-guide/loki/loki-direct-beam.ipynb)*
- The tutorial computes the beam centre from the sample runs, by their centre of mass, before it reduces them. *[loki-iofq.ipynb cells 9, 23](https://github.com/scipp/ess/blob/main/packages/esssans/docs/user-guide/loki/loki-iofq.ipynb)*
- Polarised SANS (a SKADI option) corrects four spin channels together, with an analyser transmission fitted from runs spread over the experiment (8 runs at 4 times in the essreduce example with ISIS data).
  The precise cell opacity needs a run on the depolarised cell at the end of its life, and users may redo their results with it. *[polarisation methodology cells 1, 5, 6](https://github.com/scipp/ess/blob/main/packages/essreduce/docs/user-guide/polarization/sans-polarization-analysis-methodology.ipynb), [zoom polarisation cells 2, 13](https://github.com/scipp/ess/blob/main/packages/essreduce/docs/user-guide/polarization/zoom.ipynb)*

### Combining

- The runs of one sample are combined inside one reduction: numerators summed (events concatenated), denominators summed, one division at the end, one transmission run for all.
  N copies of a run give the same I(Q) as one. *[workflow.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/sans/workflow.py#L41-L45), [L97-L141](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/sans/workflow.py#L97-L141), [iofq_test.py](https://github.com/scipp/ess/blob/main/packages/esssans/tests/loki/iofq_test.py#L244-L273)*
- SANS run merging may happen as a pre-processing step that writes merged "raw" files, as ISIS does with its "add files". *Simon, 2026-10-02; [Mantid Sum Runs](https://github.com/mantidproject/mantid/blob/main/docs/source/interfaces/isis_sans/Sum%20Runs.rst)*
- Instrument scientists want the LoKI banks merged into one I(Q), before normalisation, and per-bank results too, as a batch of independent reductions with one result each.
  Some runs may need merging and others not. *Simon, 2026-09-28 and 2026-10-05*

### Batch and automatic

- In every esssans notebook a person types the file of each role; can, empty beam and direct-beam function are set once for 4 samples. *[loki-iofq.ipynb cells 4-7](https://github.com/scipp/ess/blob/main/packages/esssans/docs/user-guide/loki/loki-iofq.ipynb)*
- At ISIS, a batch file row names up to six runs for one I(Q).
  ISIS automatic reduction pairs runs by title (`{sample}_{can}_SANS`, `_TRANS`), takes the most recent "direct" or "empty" run as empty beam, and skips a run whose partner is missing. *[Mantid batch file](https://github.com/mantidproject/mantid/blob/main/docs/source/interfaces/isis_sans/Batch%20File%20Format.rst), [FIA run-detection code](https://github.com/fiaisis/run-detection/blob/main/rundetection/rules/sans_rules.py#L114-L206)*

## Assumed

- All runs of a sample exist before its reduction starts, as in every notebook. *Ask the LoKI instrument scientist whether users want a sample's sum updated as its runs arrive.*
- One beam centre serves all samples of one experiment in one instrument configuration ([users](users.md)). *Ask the LoKI instrument scientist.*
- Each sample needs its own thickness for absolute scale; esssans has none today. *ISIS batch rows carry `sample_thickness`; ask the LoKI instrument scientist.*
- Results are histograms; events are not kept to bin again in Q. *LoKI notebooks set `ReturnEvents = False`, ISIS notebooks keep events; ask the LoKI instrument scientist.*

## Open

- How many events per second reach the LoKI detector, and how long is a run? *Decides memory per reduction and runs per hour. Ask: LoKI instrument scientist; read [Detector rates for the SANS instruments at ESS (arXiv:1805.12334)](https://arxiv.org/abs/1805.12334).*
- How many runs per sample, samples per day, and samples per can are typical? *Decides batch size and how often one can serves many samples. Ask: LoKI instrument scientist.*
- What should automatic reduction produce on each new run: a full I(Q) with can and transmission, or a per-run curve like the live one? *Decides whether it must wait for partner runs. Ask: LoKI instrument scientist.*
- Will kinetic measurements be split into time slices of one run, and from when? *Decides the size of one result ([data](data.md)). Ask: LoKI and SKADI instrument scientists.*
- How long does one LoKI reduction of all 9 banks take, and how much memory does it need? *Decides where reductions can run. Measure on real LoKI files once they exist.*
- When will SKADI need reduction, and must polarised results be redone after the end-of-cell run? *Decides whether a later calibration run triggers reductions again. Ask: SKADI instrument scientist.*
