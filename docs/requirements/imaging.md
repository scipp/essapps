# Imaging

ODIN at ESS, plus the test beamline (TBL) and imaging at BEER; essimaging reduces camera images (ODIN, TBL, and the ESS test bed YMIR) and ODIN wavelength-resolved event data.
One normalised image needs a sample, an open-beam and a dark measurement, as three files (TBL) or as one file split by a time log (ODIN, YMIR); dark and open-beam frames are averaged, but each sample frame is normalised alone.
A stack of 361 camera images of 2048 × 2048 pixels, as in one YMIR example, holds 6 GB as 32-bit integers and 12 GB in float64; a Timepix3 wavelength cube of 4096 × 4096 pixels × 256 bins holds 34 GB.
Open: whether tomographic reconstruction or full-resolution wavelength cubes belong to reduction, and whether users watch normalised projections while a scan runs.

## Known

- The test beamline is "not foreseen to be made available for scientific experiments within the user programme"; BEER offers imaging from the first day with a detector borrowed from ODIN. *[Andersen et al. 2020, §3.3 and §3.1.2](https://doi.org/10.1016/j.nima.2020.163402)*

### Runs and normalisation

- A normalised image needs a sample, an open-beam (no sample, shutter open) and a dark (shutter closed) measurement. *[imaging/types.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/types.py#L39-L51)*
- TBL writes the three as separate files, "the way TBL would like to operate (at least initially)"; ODIN and YMIR write them into one file and the workflow splits them by an image-key log over time. *[make-tbl-images-from-ymir.ipynb](https://github.com/scipp/ess/blob/main/packages/essimaging/tools/make-tbl-images-from-ymir.ipynb) cell 0, [odin/workflows.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/odin/workflows.py#L127-L140), [ymir/io.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/io.py#L62-L67)*
- Dark frames are averaged and subtracted from every frame; each frame is divided by the proton charge at its time; open-beam frames are then averaged, and each sample frame is divided by that mean. *[normalization.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/normalization.py#L33), [L101-L105](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/normalization.py#L101-L105), [orca.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/orca.py#L50-L53)*
- The result keeps one image per sample frame. *[normalization.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/normalization.py#L101-L105)*
- The YMIR workflow scales every frame by the ratio of the mean open-beam counts to the mean counts of the whole sample stack, so it needs the complete stack first. *[ymir/workflow.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/workflow.py#L83-L107), [ymir/normalize.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/normalize.py#L164-L178)*

### Tomography and wavelength cubes

- YMIR gives each sample frame a rotation angle from a motor log in the same run. *[ymir/io.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/io.py#L150-L165), [L255-L261](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/io.py#L255-L261)*
- Normalised frames are exported as TIFF files, one per frame. *[ymir/io.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/io.py#L308-L312)*
- The camera images in essimaging (YMIR, TBL) have 2048 × 2048 pixels; one example of 361 such images overflowed a 32-bit sum, and 400 images "couldn't be done in the regular laptop". *[ymir/normalize.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/ymir/normalize.py#L157-L162), [histogram_mode_detector.ipynb](https://github.com/scipp/ess/blob/main/packages/essimaging/docs/ymir/histogram_mode_detector.ipynb) cell 4, [tbl/data.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/tbl/data.py#L42-L47)*
- The ODIN Timepix3 detector, a second detector besides the camera, has 4096 × 4096 pixels and detects single neutron events; the documented Bragg-edge reduction sums all pixels into one spectrum of 300 wavelength bins and divides it by the open beam's. *[odin-data-reduction.ipynb](https://github.com/scipp/ess/blob/main/packages/essimaging/docs/odin/odin-data-reduction.ipynb) cells 18 and 24*
- essimaging can export the events of the whole Timepix3 detector as one (time, y, x) histogram to a TIFF file. *[imaging/io.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/io.py#L28-L81)*

## Assumed

- A tomography scan is one run holding every projection, as in the YMIR test file. *From one YMIR test file; check with the ODIN instrument scientist.*
- Each frame should be divided by the proton charge accumulated during its exposure; the code divides by one log value and asks in a comment how to do it right. *From [orca.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/imaging/orca.py#L42-L48); check with the ODIN instrument scientist.*

## Open

- Does reduction produce a reconstructed volume, or only normalised projections for another program? *Decides whether one result needs all projections of a scan at once. Ask: ODIN instrument scientist.*
- How many projections, frames per projection and runs does a tomography scan have at ODIN, and how long does it take? *Decides the size of the inputs to one result. Ask: ODIN instrument scientist.*
- Do users look at normalised projections, or a partial reconstruction, while the scan still runs? *Decides whether partial results are needed. Ask: ODIN instrument scientist.*
- At what spatial and wavelength binning do users want Bragg-edge cubes, and is the full 4096 × 4096 resolution needed? *At full resolution with 256 bins one cube holds 4.3e9 values, 34 GB in float64. Ask: ODIN instrument scientist.*
- How many events per second does the ODIN Timepix3 detect, and how large is one run? *Decides memory per reduction; esslivedata has no rate for ODIN. Ask: ODIN instrument scientist, detector group.*
- Will ODIN always write open-beam and dark frames into the sample run's file, or will users pick them from earlier runs? *Decides whether one result reads one run or several. Ask: ODIN instrument scientist.*
- Should each new imaging run be normalised automatically? *Decides whether imaging needs automatic reduction. Ask: ODIN instrument scientist.*
