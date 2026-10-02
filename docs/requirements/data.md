# Runs and files

A run is one measurement: NICOS asks the file writer to write the instrument's Kafka streams (neutron events, logs, chopper settings) into one NeXus (HDF5) file, named `<proposal>_<run>.hdf`.
A file is complete when the file writer reports it finished; the catalogue (SciCat) then lists it as `20.500.12269/<run UUID>`.
An event takes 8 bytes: 2026 test runs of about 10 minutes were 0.1 to 5 GB, and at the highest event rate assumed for DREAM one hour would be about 2 TB.
No ESS instrument has measured neutrons yet; ESS plans first neutrons for early 2027.
Open: what the run number is unique within in user operation, and whether a file can change after the catalogue lists it.

## Known

### What a run is

- No ESS instrument has measured neutrons yet; ESS plans first neutrons for early 2027 and user experiments by the end of 2027.
  All file evidence below comes from test runs with simulated or commissioning data. *[ESS, 2026-07-07](https://ess.eu/article/2026/07/07/ess-completes-beam-dump-2-advancing-toward-first-neutrons), [ESS, 2026-01-19](https://ess.eu/article/2026/01/19/road-science-update)*
- NICOS starts and stops a run by sending start and stop messages to the file writer over Kafka; the file writer can keep writing after the stop message. *[NICOS file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L454-L461), [NICOS filewriter.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/commands/filewriter.py#L30-L36)*
- When it closes a file, the file writer sends a "finished writing" message that names the file and says whether writing failed. *[kafka-to-nexus commands.md](https://github.com/ess-dmsc/kafka-to-nexus/blob/main/documentation/commands.md), [wrdn schema](https://github.com/ess-dmsc/streaming-data-types/blob/master/schemas/wrdn_finished_writing.fbs)*
- Raw files on ESS machines sit on read-only network file systems, synced from the machine that writes them.
  A file can still change while it is read if the file writer has not finished it. *[essreduce _nexus_loader.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/nexus/_nexus_loader.py#L217-L238)*
- A neutron event arrives as a 32-bit time offset and a 32-bit pixel ID, so it takes 8 bytes without compression. *[ev44 schema](https://github.com/ess-dmsc/streaming-data-types/blob/master/schemas/ev44_events.fbs)*
- One file can hold data of several roles: an ODIN camera file holds sample, open-beam and dark frames, told apart by an `image_key` log over time. *[odin/workflows.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/odin/workflows.py#L116-L140)*
- Batch and automatic reduction need a configurable way to decide the role of a file: catalogue metadata, NeXus fields, or frame indices within one HDF5 dataset. *Simon, 2026-09-28*

### Naming, finding and access

- The run number is a counter that NICOS keeps in a file under its data root and increments for every file; it is written to `entry/entry_identifier` and the proposal to `entry/experiment_identifier`. *[file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L333-L343), [nexus_structure.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/nexus_structure.py#L169-L176)*
- In the ECDC test setup one NICOS drives several instruments, so consecutive run numbers go to different instruments (ESTIA 13947, FREIA 13948). *[scicat-ingestor scicat_kafka.py](https://github.com/SciCatProject/scicat-ingestor/blob/main/src/scicat_kafka.py#L155-L166)*
- NICOS names the file `<proposal>_<run>.hdf`, with an 8-digit run number, in the folder `<instrument>/<proposal>/raw/`.
  Test files land in `/ess/raw/coda/999999/raw/` with an extra `coda_<instrument>_` prefix. *[file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L541-L552), [experiment.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/experiment.py#L259-L267)*
- Each file carries a per-run UUID (`entry/entry_identifier_uuid`) built from proposal, run number and a random part.
  The catalogue ingestor's published ESS schemas make it the catalogue identifier `20.500.12269/<uuid>`. *[file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L422-L436), [ess-fallback.imsc.yml](https://github.com/SciCatProject/scicat-ingestor/blob/main/src/fallback_metadata_schema/ess-fallback.imsc.yml#L8-L18)*
- A local file may carry no run number. *Simon, 2026-09-28*
- Users name data by file path today: every ess reduction workflow reads a run from a `Filename` parameter. *[essreduce nexus/types.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/nexus/types.py#L281)*
- The only run-number entry today is the Amor GUI, which reads every file in a folder and maps a run number to a PSI file name (`amor<year>n<run>.hdf`). *[essreflectometry gui.py](https://github.com/scipp/ess/blob/main/packages/essreflectometry/src/ess/reflectometry/gui.py#L938-L944)*
- In the ESS catalogue a dataset's owner group is its single proposal ID, so who may read it follows the proposal. *[scitacean _ess.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/_profile/_ess.py#L84-L90)*
- ESS intends data to become open: after an embargo, anyone may access it. *[ESS, 2020-07-06](https://ess.eu/article/2020/07/06/driven-data)*

### Other input files

- Wavelength lookup tables are simulated from chopper settings and downloaded, checksum-verified, from `public.esss.dk` on first use; production live reduction does the same. *[essreduce _registry.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/data/_registry.py#L21-L27), [esslivedata deployment.md](https://github.com/scipp/esslivedata/blob/main/docs/user-guide/deployment.md#runtime-reference-data)*
- Besides runs, reductions read files made elsewhere: SANS a direct-beam function computed by an earlier reduction and pixel-mask files, DREAM an optional calibration file.
  The LoKI tutorial mask is a Mantid XML file from an ISIS test. *[esssans types.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/sans/types.py#L136-L156), [loki/data.py](https://github.com/scipp/ess/blob/main/packages/esssans/src/ess/loki/data.py#L155-L171), [powder/types.py](https://github.com/scipp/ess/blob/main/packages/essdiffraction/src/ess/powder/types.py#L59-L60)*
- Live reduction reads detector geometry from separate files, several per instrument, each valid for a date range. *[esslivedata detector_data.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/preprocessors/detector_data.py#L89-L90)*

## Assumed

- In user operation, raw files land at `/ess/raw/<instrument>/<year>/<proposal>/raw/<proposal>_<run>.hdf`. *Seen in the `file_name` attribute of a 2025 ODIN commissioning file; confirm with the ECDC file-writer team.*
- Event rates reach 1e7 per second at LoKI, 5e7 at BEER and 7.5e7 at DREAM, which is 0.3, 1.4 and 2.2 TB per hour of uncompressed events. *Rates from [esslivedata benchmark targets](https://github.com/scipp/esslivedata/blob/main/docs/about/ess_requirements.py#L48-L90) of unstated origin; ask instrument scientists.*
- Test runs on 2026-09-22 held 0.12 GB (FREIA) to 4.8 GB (NMX, 563 million events in 13 minutes), with events stored uncompressed. *Measured on ECDC test files with simulated detector data; production sizes may differ.*
- Files carry a sample name, but no field for the run's role (sample, can, direct beam) and no reflectometry angle at entry level. *Seen in 2025 and 2026 test files; ask the ECDC and NICOS teams what user-operation files will carry.*

## Open

- Does each instrument have its own NICOS and run counter in user operation, and can a run number repeat? *Decides whether instrument and run number name a run, or the proposal or UUID is needed. Ask: NICOS team.*
- Can a file change after the file writer reports it finished, for example a repair or a rewrite? *Decides whether a result must store a checksum of each input file. Ask: ECDC file-writer team.*
- How long after the file writer finishes does the catalogue list the run? *Decides how automatic reduction learns of new runs. Ask: DMSC SciCat team.*
- How large is a typical and a largest run per instrument in user operation, and will the file writer compress events? *Decides whether one run fits in the memory of a standard VISA machine ([systems](systems.md)). Ask: instrument scientists, ECDC.*
- How long is the embargo, who may read a proposal's data during it, and how long are raw files kept on disk? *Decides how long results and their inputs stay readable. Ask: ESS data policy owner, DMSC.*
- Where will masks, calibration files and direct-beam functions live in user operation: in the catalogue, in the proposal folder, or in a package's file registry? *Decides how a user names them and whether results can trace them. Ask: instrument scientists, DMSC.*
- Is the geometry in a run file always right, or must a reduction sometimes replace it from a separate file? *Decides whether geometry is an input of offline reduction. Ask: ECDC, instrument scientists.*
