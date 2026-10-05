# Systems the framework works with

At ESS the framework meets the catalogue (SciCat), the file writer and Kafka, live reduction (esslivedata), NICOS, VISA machines, the DMSC cluster, and the ess Python packages.
SciCat holds metadata and file paths, not file bytes, and lists each raw file once the file writer finishes it; every dataset belongs to one proposal.
Interactive reduction runs in notebooks on VISA machines (64 GB and 6 CPUs as standard, larger for instruments with large files); batch and automatic reduction, and large work spread over the DMSC cluster, run in hosted services.
NICOS might eventually start a reduction through the framework and wait for its result.
Open: whether the batch- and automatic-reduction services may run cluster jobs and write catalogue entries for a proposal.

## Known

### SciCat

- SciCat stores metadata about datasets; the files stay on disk, and archiving to tape or retrieving from it are jobs that the operator may enable. *[SciCat docs, Jobs](https://www.scicatproject.org/documentation/Users/Jobs.html)*
- scicat-ingestor creates a raw dataset when the file writer reports a finished file, skips files whose writing failed, and reads the proposal from the file. *[scicat-ingestor README](https://github.com/SciCatProject/scicat-ingestor/blob/main/README.md), [scicat_kafka.py](https://github.com/SciCatProject/scicat-ingestor/blob/main/src/scicat_kafka.py#L114-L121), [ess-fallback.imsc.yml](https://github.com/SciCatProject/scicat-ingestor/blob/main/src/fallback_metadata_schema/ess-fallback.imsc.yml#L8-L23)*
- The ingestor can compute and store a blake2b checksum per file; its sample configuration turns this on. *[config.sample.yml](https://github.com/SciCatProject/scicat-ingestor/blob/main/resources/config.sample.yml#L22-L29)*
- A SciCat dataset is `raw` or `derived`; a derived dataset lists its input datasets by identifier and the software used. *[scitacean dataset.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/dataset.py#L378-L421), [model.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/model.py#L175-L218)*
- Creating a dataset needs a type, contact email, creation time, owner, owner group and source folder.
  At ESS the owner group is the one proposal ID, and uploaded files go to `/ess/data/<proposal>/<instrument>/upload`. *[model.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/model.py#L175-L181), [_ess.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/_profile/_ess.py#L84-L105)*
- SciCat assigns a dataset identifier, or accepts an unused one from the uploader; creating a dataset never overwrites one. *[scitacean create_dataset_model](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/client.py#L1021-L1032)*
- SciCat can change a dataset's metadata after creation, and lets users in configured groups delete a dataset. *[SciCat datasets.controller.ts](https://github.com/SciCatProject/backend/blob/master/src/datasets/datasets.controller.ts#L1170), [configuration.ts](https://github.com/SciCatProject/backend/blob/master/src/config/configuration.ts#L12)*
- Users search datasets in the SciCat web interface; programs log in with an access token that the user copies from its settings page. *[DMSC school, SciCat](https://github.com/ess-dmsc-dram/dmsc-school/blob/main/6-scicat/6-scicat.md)*
- scitacean moves files by link or copy where `/ess` is mounted, and by SFTP to `sftp.esss.dk` elsewhere. *[_ess.py](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/_profile/_ess.py#L41-L60)*
- scitacean finds datasets by exact match on fields such as the proposal, and calls this search experimental. *[scitacean query_datasets](https://github.com/SciCatProject/scitacean/blob/main/src/scitacean/client.py#L749-L800)*

### Live reduction and NICOS

- esslivedata reduces neutron events and logs from Kafka while a run is measured, shows the results on a dashboard, and publishes some of them to NICOS. *[esslivedata glossary](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/glossary.md), [ADR 0006](https://github.com/scipp/esslivedata/blob/main/docs/developer/adr/0006-nicos-derived-devices.md)*
- When it cannot keep up, esslivedata drops the oldest event data to stay current, so its results can be incomplete. *[rate_aware_batcher.py](https://github.com/scipp/esslivedata/blob/main/src/ess/livedata/core/rate_aware_batcher.py#L28-L41)*
- esslivedata keeps no results after a run, so offline reduction from files is the only complete reduction. *Simon, 2026-10-05*
- NICOS is the experiment control software; it starts and stops runs ([data](data.md)).
  Users count and scan in NICOS against quantities that esslivedata derives from the live data. *[NICOS file_writer.py](https://github.com/ess-dmsc/nicos/blob/main/nicos_ess/devices/datasinks/file_writer.py#L454-L461), [esslivedata ADR 0006](https://github.com/scipp/esslivedata/blob/main/docs/developer/adr/0006-nicos-derived-devices.md)*
- NICOS prefers a static, version-controlled list of what it reads, with stable names, over configuration published at runtime. *[esslivedata ADR 0006](https://github.com/scipp/esslivedata/blob/main/docs/developer/adr/0006-nicos-derived-devices.md)*
- NICOS might eventually want to call the framework to start a reduction and get back a result. *Simon, 2026-10-02*

### Compute

- VISA provides remote desktops (virtual machines) with JupyterLab; the end of a machine cleans up everything on it. *Simon, 2026-10-02*
- Users sign in to VISA with ORCID and create a machine for a proposal. *[DMSC school guide](https://github.com/ess-dmsc-dram/dmsc-school/blob/main/guides/day1-student-guide/main.tex)*
- The standard VISA machine has 64 GB of memory and 6 virtual CPUs; instruments with large files will use larger machines. *[essnmx workflow.ipynb](https://github.com/scipp/ess/blob/main/packages/essnmx/docs/user-guide/workflow.ipynb); Simon, 2026-10-05*
- The ESS data file system (`/ess/data`, `/ess/raw`) is mounted on cluster nodes, on hosts that could run the batch- and automatic-reduction services, and on VISA machines, where the user's proposal folder is mounted automatically; SciCat gives each file's path.
  scitacean's fallback to SFTP serves laptops. *Simon, 2026-10-05*
- Interactive reduction runs inside the notebook or application process, on VISA or a laptop.
  Batch and automatic reduction, and large work spread over the cluster, run in hosted services ([users](users.md)). *Simon, 2026-10-02*
- During construction, the DMSC runs a mid-size cluster: 120 compute nodes with 4092 cores, 891 TB of storage and a batch system, hosted near Copenhagen and in Lund. *[ESS, Computing Centre](https://ess.eu/data-management-software/computing-centre)*
- The DMSC cluster runs Slurm. *Simon, 2026-10-05*
- Compute should be able to run locally or elsewhere (cluster, cloud), and the catalogue should be SciCat or another one. *Simon, scoping.md (2026-09-04)*

### Software

- ess reduction workflows are sciline pipelines over scipp data; essreduce requires sciline 25.11 and scipp 26.3.1 or later. *[essreduce pyproject.toml](https://github.com/scipp/ess/blob/main/packages/essreduce/pyproject.toml#L32-L41)*
- scipp data cannot be pickled, so Python's multiprocessing cannot pass it between processes; the issue asking for support is open. *[scipp/scipp#3940](https://github.com/scipp/scipp/issues/3940)*
- The ess packages require Python 3.12 or later and are tested on 3.12 to 3.14. *[essreduce pyproject.toml](https://github.com/scipp/ess/blob/main/packages/essreduce/pyproject.toml#L20-L28)*
- Users install ess packages with pip or from conda-forge; in practice environments are conda environments. *[essreduce installation.md](https://github.com/scipp/ess/blob/main/packages/essreduce/docs/user-guide/installation.md); Simon, 2026-09-28*
- The ess packages had 55 releases from January to September 2026, 14 of them essreduce. *[scipp/ess tags](https://github.com/scipp/ess/tags)*
- The software stack is Python, and so is most of the team's experience. *Simon, scoping.md (2026-09-04)*
- The batch- and automatic-reduction services need not run on Windows, but GUI applications should probably run on Windows too. *Simon, 2026-09-30 and 2026-10-05*

## Assumed

Nothing at present.

## Open

- Can the batch- and automatic-reduction services submit cluster jobs as the user who asked, or only under a service account? *Decides how batch and automatic reduction reach the cluster. Ask: DMSC cluster administrators.*
- May the automatic-reduction service create derived datasets in SciCat for a proposal, and who may delete datasets at ESS? *Decides who publishes automatic-reduction results and whether a published identifier stays valid. Ask: DMSC SciCat team.*
- Can VISA machines and cluster nodes reach SciCat, Kafka, and the hosts of the services inside ESS? *Decides where each part of a reduction can run. Ask: ESS IT.*
- Who builds and updates the conda environments on VISA and the cluster, and are they named and versioned? *Decides how a result names the software that made it. Ask: DMSC.*
- What would NICOS send to start a reduction, and what result must come back, how soon? *Decides the interface for a non-human caller. Ask: NICOS team, Simon.*
