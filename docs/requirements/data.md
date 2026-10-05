# Runs and files

A run is one measurement: NICOS asks the file writer to write the instrument's Kafka streams (neutron events, logs, chopper settings) into one NeXus (HDF5) file, named `<proposal>_<run>.hdf`.
A file is complete when the file writer reports it finished; the catalogue (SciCat) then lists it as `20.500.12269/<run UUID>`.
An event takes 8 bytes, so at the highest event rate estimated for DREAM one hour would be about 2 TB.
No ESS instrument has measured neutrons yet; ESS plans first neutrons for early 2027.
Open: what the run number is unique within in user operation, and how large runs will be.

## Known

### What a run is

- No ESS instrument has measured neutrons yet; ESS plans first neutrons for early 2027 and user experiments by the end of 2027.
  All file evidence below comes from test runs with simulated or commissioning data, so a field missing there may still appear in user operation. *[ESS, 2026-07-07](https://ess.eu/article/2026/07/07/ess-completes-beam-dump-2-advancing-toward-first-neutrons), [ESS, 2026-01-19](https://ess.eu/article/2026/01/19/road-science-update)*
- NICOS starts and stops a run by sending start and stop messages to the file writer over Kafka; the file writer can keep writing after the stop message. *[NICOS file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L454-L461), [NICOS filewriter.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/commands/filewriter.py#L30-L36)*
- When it closes a file, the file writer sends a "finished writing" message that names the file and says whether writing failed. *[kafka-to-nexus commands.md](https://github.com/ess-dmsc/kafka-to-nexus/blob/main/documentation/commands.md), [wrdn schema](https://github.com/ess-dmsc/streaming-data-types/blob/master/schemas/wrdn_finished_writing.fbs)*
- Raw files on ESS machines sit on read-only network file systems, synced from the machine that writes them.
  A file can still change while it is read if the file writer has not finished it. *[essreduce _nexus_loader.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/nexus/_nexus_loader.py#L217-L238)*
- A neutron event arrives as a 32-bit time offset and a 32-bit pixel ID, so it takes 8 bytes without compression. *[ev44 schema](https://github.com/ess-dmsc/streaming-data-types/blob/master/schemas/ev44_events.fbs)*
- The best available event-rate estimates are 1e6 per second at BIFROST, 1e7 at LoKI and CSPEC, 5e7 at BEER and 7.5e7 at DREAM; 1e7 per second is 0.3 TB of events per hour, 7.5e7 is 2.2 TB.
  Simon gathered them from instrument scientists years ago; they have likely changed, but nothing better exists. *[esslivedata benchmark targets](https://github.com/scipp/esslivedata/blob/main/docs/about/ess_requirements.py#L48-L90); Simon, 2026-10-05*
- One file can hold data of several roles: an ODIN camera file holds sample, open-beam and dark frames, told apart by an `image_key` log over time. *[odin/workflows.py](https://github.com/scipp/ess/blob/main/packages/essimaging/src/ess/odin/workflows.py#L116-L140)*
- Splitting a run by time or by a sample-environment log adds a dimension to its result; it does not make many results. *Simon, 2026-10-05*
- Batch and automatic reduction need a configurable way to decide the role of a file: catalogue metadata, NeXus fields, or frame indices within one HDF5 dataset. *Simon, 2026-09-28*

### Naming, finding and access

- The run number is a counter that NICOS keeps in a file under its data root and increments for every file; it is written to `entry/entry_identifier` and the proposal to `entry/experiment_identifier`. *[file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L333-L343), [nexus_structure.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/nexus_structure.py#L169-L176)*
- In the CODA test setup one NICOS drives several instruments, so consecutive run numbers go to different instruments (ESTIA 13947, FREIA 13948). *[scicat-ingestor scicat_kafka.py](https://github.com/SciCatProject/scicat-ingestor/blob/main/src/scicat_kafka.py#L155-L166)*
- NICOS names the file `<proposal>_<run>.hdf`, with an 8-digit run number, in the folder `<instrument>/<proposal>/raw/`. *[file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L541-L552), [experiment.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/experiment.py#L259-L267)*
- Each file carries a per-run UUID (`entry/entry_identifier_uuid`) built from proposal, run number and a random part.
  The catalogue ingestor's published ESS schemas make it the catalogue identifier `20.500.12269/<uuid>`. *[file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L422-L436), [ess-fallback.imsc.yml](https://github.com/SciCatProject/scicat-ingestor/blob/main/src/fallback_metadata_schema/ess-fallback.imsc.yml#L8-L18)*
- A local file may carry no run number. *Simon, 2026-09-28*
- Users name data by file path today: every ess reduction workflow reads a run from a `Filename` parameter. *[essreduce nexus/types.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/nexus/types.py#L281)*
- In the ESS catalogue a dataset's owner group is its single proposal ID, so who may read it follows the proposal. *[scitacean _ess.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/_profile/_ess.py#L84-L90)*
- ESS intends data to become open: after an embargo, anyone may access it.
  This is part of making data FAIR, which requires that each result carry its provenance. *[ESS, 2020-07-06](https://ess.eu/article/2020/07/06/driven-data); Simon, 2026-10-05*

### Other input files

- Wavelength lookup tables will be computed from the chopper settings in each file; today they are simulated beforehand and downloaded from `public.esss.dk`. *Simon, 2026-10-05; [essreduce _registry.py](https://github.com/scipp/ess/blob/main/packages/essreduce/src/ess/reduce/data/_registry.py#L21-L27)*
- In user operation, masks, calibration files and direct-beam functions will be in the catalogue; the mid-term goal is to read every input of a reduction from SciCat. *Simon, 2026-10-05*

## Assumed

- In user operation, raw files land at `/ess/raw/<instrument>/<year>/<proposal>/raw/<proposal>_<run>.hdf`. *Seen in the `file_name` attribute of a 2025 ODIN commissioning file; confirm with the ECDC file-writer team.*
- A file does not change after the file writer reports it finished. *Simon, 2026-10-05; confirm with the ECDC file-writer team.*
- Users will name the runs of every role by run number or a SciCat identifier; this may change over time. *Simon, 2026-10-05; ask instrument scientists.*
- The embargo lasts 3 years, and raw files are kept forever. *Simon, 2026-10-05, from memory; confirm with the ESS data policy owner.*

## Open

- Does each instrument have its own NICOS and run counter in user operation, and can a run number repeat? *Decides whether instrument and run number name a run, or the proposal or UUID is needed. Ask: NICOS team.*
- How will ESS files or the catalogue say what role a run plays (sample, can, transmission, empty beam, vanadium, reference, direct beam, open beam, dark), and which runs belong together, such as the angles of one sample? *Decides how an application pairs runs without a person ([tensions](tensions.md)). Ask: NICOS team, SciCat team, instrument scientists.*
- How long after the file writer finishes does the catalogue list the run? *Decides how automatic reduction learns of new runs. Ask: DMSC SciCat team.*
- How large is a typical and a largest run per instrument in user operation? *Decides memory per reduction, and which VISA machine size an instrument needs ([systems](systems.md)). Ask: instrument scientists.*
- Who may read a proposal's data during its embargo? *Decides who may see results and their inputs. Ask: ESS data policy owner.*
- Is a wavelength lookup table computed inside the reduction of each run, or by a separate reduction whose result a person checks first, and does this differ between automatic and interactive reduction? *Decides whether the reduction of a run depends on another result. Ask: Simon, essreduce authors.*
