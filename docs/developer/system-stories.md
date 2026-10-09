# System stories

These stories check what the system must provide beyond the API of [README.md](README.md): cost, placement, persistence, recovery, and operations.
Each has an actor, a goal, and the property to check.
Where a story shares a goal with an API story in [user-stories.md](user-stories.md), it has the same identifier.
How the backend keeps history and values is described in the backend's docstrings (`ess.dispatch.backend`).

## S. Small stories

### S2. Tune one parameter

Actor: user in a notebook. Goal: a new binning comes back in a second or two, and so does each change of binning and mask in user story B1.
Property: a call through a stage does not load the run again, with one blank such as `bins` or with several such as `bins` and `threshold`.

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user of a local application. Goal: browse a folder and the catalogue without waiting for transfers.
Property: listing a folder or the catalogue reads no data. A catalogue dataset is fetched only when a request reads it, and a local file is read where it is, not copied.

### A3. Work without the facility mount

Actor: user on a laptop. Goal: reduce a catalogue dataset twice without the facility file system.
Property: the backend fetches the dataset from the catalogue once, and the second request reads the fetched copy.

### A4. Mistaken copy into the shared service

Actor: user of the shared service. Goal: remove a file that should not have left their machine.
Property: a local file reaches the service only when a request that names it is submitted. After its removal, no copy of its bytes remains in the service: not in storage, caches, or the processes that run stages.

## B. Manual and interactive reduction

### B2. Add a run to a sum

Actor: user in a notebook. Goal: after a new run finishes, the sum including it comes back quickly.
Property: with a binding that has a held state of its own, such as `Summing`, pushing a run into an accumulator reduces only that run, and reading the sum computes the outputs from the held state, without reading any earlier run again. With the held state that keeps the rows, each read reduces every run so far again.

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI. Goal: cuts follow a slider as it is dragged.
Property: a selection of an output returns in a fraction of a second, and only the slice leaves the backend; the volume is not copied to the UI. On the service, the volume is held in memory under the client's cap and written only if persisted.

### B6. Find last week's result

Actor: user after a week. Goal: last week's records are still there, with their outputs.
Property: records survive restarts of the client and of the backend, until the proposal has been idle for the retention period (H3). Their persisted outputs are in the store, which drops them with the proposal's history; outputs that were not persisted are gone once their client ends, and their records say so.

## C. Chaining

### C2. Vanadium from the catalogue

Actor: user. Goal: use a vanadium result that another backend published.
Property: the backend reads the published output through the catalogue. It needs no access to the other backend's records or storage. A result of another proposal on the same backend is read the same way: no proposal reads another's records.

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: the sample reduction follows each vanadium change within a second or two.
Property: the vanadium output passes to the sample's stage in memory, within one process. Neither run is loaded again.

## D. Batch

### D2. Overnight cluster batch

Actor: NMX user. Goal: thirty long runs finish overnight while the laptop is closed.
Property: requests run to completion with no client connected, in parallel as far as the cluster allows. The batch is persisted at submission, so each record's outputs are written to the store before it completes, and the next day's client reads it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).

### D3. Cancel and resubmit

Actor: user of the shared service. Goal: after 20 of 500 requests have started, a cancel frees the service for the corrected batch.
Property: a cancel stops the started requests within seconds and frees their workers. The resubmitted requests do not wait for the cancelled ones.

Gap: a cancel ends the records, but a workflow that has started runs on and keeps its worker until it returns. Its outputs are then dropped. The resubmitted requests wait for those workers.

### D7. Rotation scan over three hundred angles

Actor: spectroscopy user. Goal: each run is added to the volume as it arrives, and a cut through the volume so far is ready within seconds of each run.
Property: with a binding that has a held state of its own and adds in place, such as `Summing`, each push reduces its run and adds it to the held state, reading no earlier run. The volume is held once; where the service holds it, and how it bounds its memory, is open ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md), Open). A cut copies only its selection and makes no record; a cut kept as a record makes the record of its state too, which history stores as the accumulator and its number of pushes ([ADR 0004](adr/0004-history-is-append-only-lists.md)). History therefore grows in proportion to the number of runs. The frozen volume is written once, and no copy of it is made.
Each cut checks the plain request of its state, which takes time in proportion to the rows so far: about 1.3 ms at 300 rows, so its total over the scan grows with the square of the number of runs ([ADR 0003](adr/0003-accumulators-add-in-place.md)).

Gap: reducing the runs of one scan on several nodes needs a merge of two held states (README.md open question 1).

## F. Publication and provenance

### F2. Reproduce after two upgrades

Actor: user. Goal: reproduce a result exactly after the backend has been upgraded twice.
Property: the software environment recorded with a record is enough to install it again, so that a request can run in its record's environment.

## G. Roles and deployment

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: edit a workflow in a notebook and see the result within seconds.
Property: a backend in the notebook's process runs a workflow bound there, and uses an edited binding for the next request without a restart. The service runs only installed workflows.

### G5. Reference across proposals refused

Actor: DMSC. Goal: no user reads another proposal's data, whatever client they use.
Property: the backend checks every reference against the submitter's proposal. A modified client cannot get around the check.

## H. Operations

### H1. Disk fills up

Actor: DMSC. Goal: free disk space without losing provenance.
Property: stored outputs can be dropped by proposal, label, or age. Every record and its provenance stay until its proposal's history is dropped (H3). DMSC sees the space used per proposal before the disk is full.

### H2. Backend upgrade with runs in flight

Actor: DMSC. Goal: deploy a new backend version while requests run and others wait on them.
Property: every record pending at the upgrade with a persist request finishes without anyone submitting it again, and so do the records it reads. Whether the old instance finishes them before it stops, or the new one runs them again, is open ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md), Open); a record that reads an accumulator's held state finishes only if the old instance finishes it. The upgrade ends every client of the old instance, and pending work that nothing persists is cancelled. Records and persisted values written before the upgrade stay readable, also when the new version stores records in another schema. A backend started on history that another backend still writes is refused, so the old and the new version never write the same history.

### H3. History ends when the work does

Actor: user. Goal: the records of a proposal are there while the user works with them, and for days to weeks after; what was published lasts.
Property: once a proposal has been idle for the retention period, with no client open, no record pending, and no write pending, its history is dropped as a whole, never in part. So a kept record's provenance is complete. A running trigger loop keeps its proposal from being idle, so it never reduces a handled dataset again. A published entry still answers what produced it after the history behind it is dropped.
