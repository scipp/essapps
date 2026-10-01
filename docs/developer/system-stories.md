# System stories

These stories check what the system must provide beyond the API of [README.md](README.md): cost, placement, persistence, recovery, and operations.
Each has an actor, a goal, and the property to check.
Where a story shares a goal with an API story in [user-stories.md](user-stories.md), it has the same identifier.
[system.md](system.md) describes the part of the system that keeps history and values; the stories get code as the system document grows.

## S. Small stories

### S2. Tune one parameter

Actor: user in a notebook. Goal: a new binning comes back in a second or two.
Property: a call through a stage does not load the run again.

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user of a local application. Goal: browse a folder and the catalogue without waiting for transfers.
Property: listing a folder or the catalogue reads no data. A catalogue dataset is fetched only when a request reads it, and a local file is read where it is, not copied.

### A3. Work without the facility mount

Actor: user on a laptop. Goal: reduce a catalogue dataset twice without the facility file system.
Property: the backend fetches the dataset from the catalogue once, and the second request reads the fetched copy.

### A4. Mistaken copy into the shared service

Actor: user of the shared service. Goal: remove a file that should not have left their machine.
Property: a local file reaches the service only when a request that names it is submitted. After its removal, no copy of its bytes remains in the service: not in storage, caches, or session processes.

## B. Manual and interactive reduction

### B1. Tune a SANS reduction

Actor: user in a notebook. Goal: each change of binning or mask comes back in a second or two.
Property: a call through a stage whose blanks are `bins` and `threshold` does not load the run again.

### B2. Add a run to a sum

Actor: user in a notebook. Goal: after a new run finishes, the sum including it comes back quickly.
Property: pushing a contribution into an accumulator and computing it reduces only the new run and reads no earlier contribution again.

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI. Goal: cuts follow a slider as it is dragged.
Property: a view of an output returns in a fraction of a second, and only the slice leaves the backend; the volume is not copied to the UI.

### B5. Notebook kernel dies mid-session

Actor: user in a notebook. Goal: after a restart, nothing made before the crash is lost, and nothing stays held.
Property: records completed before the crash are kept, and requests submitted before it still complete. The backend ends the dead kernel's session and releases its memory, although the client never ended it.

### B6. Find last week's result

Actor: user after a week. Goal: last week's records are still there, and the outputs that were saved.
Property: records survive restarts of the client and of the backend for the retention period (H3). Saved outputs survive as long as their store keeps them.

## C. Chaining

### C2. Vanadium from the catalogue

Actor: user. Goal: use a vanadium result that another backend published.
Property: the backend reads the published output through the catalogue. It needs no access to the other backend's records or storage.

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: the sample reduction follows each vanadium change within a second or two.
Property: the vanadium output passes to the sample's stage in memory, within the session's process. Neither run is loaded again.

## D. Batch

### D2. Overnight cluster batch

Actor: NMX user. Goal: thirty long runs finish overnight while the laptop is closed.
Property: requests run to completion with no client connected, in parallel as far as the cluster allows.

### D3. Cancel and resubmit

Actor: user of the shared service. Goal: after 20 of 500 requests have started, a cancel frees the service for the corrected batch.
Property: a cancel stops the started requests within seconds and frees their workers. The resubmitted requests do not wait for the cancelled ones.

### D7. Rotation scan over a thousand angles

Actor: spectroscopy user. Goal: each run is reduced on its own node as it arrives, and the volume so far is ready within seconds of each angle.
Property: each `ANGLE` request starts when its run arrives, on any free node. Pushing a finished angle into the accumulator combines one contribution and reads no earlier one.
Each snapshot of the volume makes a record over every angle pushed so far. The storage these records take does not grow quadratically with the number of snapshots.
A request of `VOLUME` over a thousand elements, made outside a session, runs as a tree of partial sums, since its author declares that grouping does not change the sum. No process reads more than a configured number of contributions.

## F. Publication and provenance

### F2. Reproduce after two upgrades

Actor: user. Goal: reproduce a result exactly after the backend has been upgraded twice.
Property: the software environment recorded with a record is enough to install it again, so that a request can run in its record's environment.

## G. Roles and deployment

### G1. Instrument scientist prepares a beamtime

Actor: operator, for an instrument scientist. Goal: records of the commissioning proposal are readable by the users of the coming proposal.
Property: an operator lets one proposal read another's records and datasets by configuration. The backend enforces the grant.

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: edit a workflow in a notebook and see the result within seconds.
Property: a backend in the notebook's process runs a workflow bound there, and uses an edited binding for the next request without a restart. A hosted backend runs only installed workflows.

### G3. Local application, remote compute

Actor: user of a desktop application. Goal: the expensive reduction runs on the cluster; the cheap post-processing is tuned on the laptop with sub-second feedback.
Property: a session's process runs on the laptop while its stages read outputs of records made on the cluster. A stage fetches such an output once, and each call through it computes on the laptop. The records the session makes go to the cluster's backend.

### G5. Reference across proposals refused

Actor: operator. Goal: no user reads another proposal's data, whatever client they use.
Property: the backend checks every reference against the submitter's proposal and its grants. A modified client cannot get around the check.

## H. Operations

### H1. Disk fills up

Actor: operator. Goal: free disk space without losing provenance.
Property: stored outputs can be dropped by proposal, label, or age. Every record and its provenance stay for the retention period (H3). The operator sees the space used per proposal before the disk is full.

### H2. Backend upgrade with runs in flight

Actor: operator. Goal: deploy a new backend version while requests run and others wait on them.
Property: every record pending at the upgrade finishes after it, including requests that wait on a pending input. Records written before the upgrade stay readable, also when the new version stores records in another schema. A backend started on a log that another backend still holds is refused, so the old and the new version never write the same log.

### H3. Records expire

Actor: operator. Goal: history is kept while an experiment needs it and removed afterwards; what was published lasts.
Property: history is kept for a configured period that covers a running experiment. An older event is kept while a kept event depends on it, so a kept record's provenance is complete. A published entry still answers what produced it after the history behind it has expired.
