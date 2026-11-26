# System stories

For someone who checks what the system must provide beyond the API of [README.md](README.md): cost, placement, persistence, recovery, and operations.
Each story has an actor, a goal, and the property to check.
A story that shares a goal with an API story in [user-stories.md](user-stories.md) has the same identifier.
*The service* is a hosted workspace, which the users of a proposal share.

## S. Small stories

### S2. Tune one parameter

Actor: user in a notebook. Goal: a new binning comes back in a second or two, and so does each change of binning and mask in user story B1.
Property: a call through a stage does not load the run again, whether it changes one value such as `bins` or several such as `bins` and `threshold`.

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user of a local application. Goal: browse a folder and the catalogue without waiting for transfers.
Property: listing a folder or the catalogue reads no data. A catalogue file is fetched only when a request reads it. A local file is read where it is, without a copy.

### A3. Work without the facility mount

Actor: user on a laptop. Goal: reduce a catalogue file twice without the facility file system.
Property: the workspace fetches the file from the catalogue once, and the second request reads the fetched copy.

### A4. Mistaken copy into the shared service

Actor: user of the shared service. Goal: remove a file that should not have left their machine.
Property: a local file reaches the service only when a request that refers to it is submitted. After its removal, no copy of its bytes remains in the service: not in storage, caches, or the processes that run stages.

## B. Manual and interactive reduction

### B2. Add a run to a sum

Actor: user in a notebook. Goal: after a new run finishes, the sum including it comes back quickly.
Property: with a driver that sums, pushing a run into an accumulator reduces only that run. Reading the sum computes the outputs from the driver, and reads no earlier run again. A driver that holds the rows instead reduces every run so far again at each read.

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI. Goal: cuts follow a slider as it is dragged.
Property: a selection of an output returns in a fraction of a second, and only the slice leaves the workspace. The volume is not copied to the UI.

### B6. Find last week's result

Actor: user after a week. Goal: last week's records are still there, with their outputs.
Property: records survive restarts of the client and of the workspace, until the proposal has been idle for the retention period (H3). Their persisted outputs are in the store until a client clears them, or until the store drops them with the proposal's history. Outputs that were not persisted are gone once a client releases them or the workspace closes, and their records show this.

## C. Chaining

### C2. Vanadium from the catalogue

Actor: user. Goal: use a vanadium result that another workspace published.
Property: the workspace reads the published output through the catalogue. It needs no access to the other workspace's records or storage. A result of another proposal on the same service is read the same way: no proposal reads another's records.

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: the sample reduction follows each vanadium change within a second or two.
Property: the vanadium output passes to the sample's stage in memory, within one process. Neither run is loaded again.

## D. Batch

### D2. Overnight cluster batch

Actor: NMX user. Goal: thirty long runs finish overnight while the laptop is closed.
Property: requests run to completion with no client connected, in parallel as far as the cluster allows. The batch is persisted at submission, so each record's outputs are written to the store before it completes, and the next day's client reads them.

### D3. Cancel and resubmit

Actor: user of the shared service. Goal: after 20 of 500 requests have started, a cancel frees the service for the corrected batch.
Property: a cancel stops the started requests within seconds and frees their workers. The resubmitted requests do not wait for the cancelled ones.

### D7. Rotation scan over three hundred angles

Actor: spectroscopy user. Goal: each run is added to the volume as it arrives, and a cut through the volume so far is ready within seconds of each run.
Property: with a driver that adds in place, each push reduces its run and adds it to the driver, reading no earlier run. The volume is held once. A cut copies only its selection and creates no record. History grows by one push per run and one record for the freeze, which history stores as the accumulator and its number of pushes ([ADR 0004](adr/0004-history-is-append-only-lists.md)). The frozen volume is written once, and no copy of it is made.

## F. Publication and provenance

### F2. Reproduce after two upgrades

Actor: user. Goal: reproduce a result exactly after the framework has been upgraded twice.
Property: the software environment recorded with a record is enough to install it again, so that a request can run in its record's environment.

## G. Roles and deployment

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: edit a workflow in a notebook and see the result within seconds.
Property: a workspace in the notebook's process runs a workflow bound there, and uses an edited binding for the next request without a restart. The service runs only installed workflows.

### G5. Reference across proposals refused

Actor: DMSC. Goal: no user reads another proposal's data, whatever client they use.
Property: a workspace holds the history of one proposal, so a reference can refer only to records of that proposal, and the workspace checks every file against its proposal. The service lets a user reach only the workspaces of their proposals. A modified client cannot get around either check.

## H. Operations

### H1. Disk fills up

Actor: DMSC. Goal: free disk space without losing provenance.
Property: stored outputs can be dropped by proposal, label, or age. Every record and its provenance stay until its proposal's history is dropped (H3). DMSC sees the space used per proposal before the disk is full.

### H2. Upgrade with runs in flight

Actor: DMSC. Goal: deploy a new version of the service while requests run and others wait on them.
Property: every record pending at the upgrade with a persist request finishes without anyone submitting it again, and so do the records it reads.
If the old instance is killed instead of closed, a record that was running fails at the next start, since running it again could end the process again. A record that had not started runs if what it reads is there. A record that reads an accumulator's driver finishes only if the old instance finishes it.
The upgrade closes every workspace of the old instance, and pending work that nothing persists and that had not started is cancelled.
Records and persisted values written before the upgrade stay readable, also when the new version stores records in another schema.
A workspace started on a history that another workspace still writes raises, so the old and the new version never write the same history.

### H3. History ends when the work does

Actor: user. Goal: the records of a proposal are there while the user works with them, and for days to weeks after. What was published lasts.
Property: once a proposal has been idle for the retention period, with no call and no record pending, its history is dropped as a whole, never in part. So the provenance of a record that remains is complete. A running automatic reduction keeps its proposal from being idle, so it never reduces a row again for a file it has reduced. A published entry still states what produced it after the history behind it is dropped.
