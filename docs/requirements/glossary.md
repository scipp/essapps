# Glossary

Facility and technique terms used on these pages, in a few words each.
Physics terms such as Q, I(Q), or S(Q, E) are not explained.

## Measurements

- **Run**: one measurement, written by the file writer into one NeXus file.
- **Proposal**: an accepted application for beam time; access to data is granted per proposal.
- **Event**: one detected neutron, stored as a pixel ID and a time.
- **Proton charge**: the number of protons that hit the target during a measurement; intensities are divided by it.
- **Monitor**: a detector in the beam before the sample, counting the incoming neutrons.
- **Sample** and **can**: the sample is what is studied; the can is its empty container, measured alone to subtract its scattering.
- **Vanadium**: a sample that scatters equally in all directions, measured to correct for detector efficiency (diffraction, spectroscopy).
- **Rotation scan**: one sample measured at many orientations, combined into one volume (single crystal, spectroscopy).

## Techniques

- **Transmission run** (SANS): measures how much of the beam passes the sample, to correct for absorption.
- **Empty beam** (SANS): a run without a sample, the reference for transmission runs.
- **Direct-beam function** (SANS): the detector efficiency per wavelength, computed from a standard sample of known I(Q).
- **Beam centre** (SANS): where the direct beam hits the detector, found from the data.
- **Reference** or **supermirror** (reflectometry): a sample of known reflectivity, measured to normalise the sample's curve.
- **Direct beam** (reflectometry): the beam measured without a sample, used for normalisation at FREIA and Offspec.
- **Stitching** (reflectometry): joining the curves measured at several angles into one curve.
- **Open beam** and **dark** (imaging): images without a sample, and with the shutter closed.
- **Horace** and **SQW**: Horace is the spectroscopy analysis program from ISIS; an SQW file holds every observation of an experiment in (Q, E).

## Facility and software

- **NeXus**: the HDF5-based file format for neutron data.
- **Kafka**: the message system that carries events and logs from the instrument to the file writer and to live reduction.
- **File writer**: the ESS program (kafka-to-nexus) that writes Kafka streams into NeXus files.
- **NICOS**: the experiment control software; it starts and stops runs.
- **ECDC**: the ESS group that develops NICOS and the file writer.
- **SciCat**: the catalogue of datasets, holding metadata and file paths; **scitacean** is its Python client.
- **esslivedata**: live reduction and dashboards while a run is measured.
- **ess packages**: the reduction workflows in the scipp/ess repository (esssans, essreflectometry, ...), built with sciline on scipp.
- **VISA**: ESS remote desktops (virtual machines) with JupyterLab, created by a user for a proposal.
- **DMSC**: the ESS Data Management and Software Centre; it runs the cluster and the data services.
- **Service**: a long-running program, run by operators, that reduces data for many users; batch and automatic reduction run there.
- **ISIS**, **SNS**, **PSI**: neutron sources in the UK, the USA, and Switzerland; Amor (PSI) and Larmor, Sans2d, Zoom, Offspec (ISIS) run ess code today.
