# Scoping and investigation for ESS applications project

## Notes

Treat this as a living document.
Try to stay concise for now.

## Context and goal

We provide software for "data reduction" for ESS instruments.
This mainly lives in the scipp/ess monorepo (libraries and Jupyter notebooks) and scipp/esslivedata (Kafka stream processing, Panel/Holoviews GUI app).
The goal of this project/repo is to scope, architect, and eventually implement a framework and applications for data reduction.
This may eventually merge in to the scipp/ess monorepo, but for now we will keep it separate to allow for more flexibility.

Key scope to deliver eventually:

- Manual-reduction: User configures a workflow and runs it on a dataset, possibly with some interactive inspection of (intermediate) results.
- Batch-reduction: Same as manual reduction, but applied to many datasets, many paramaters shared, but some per-dataset individualization.
- Auto-reduction: Configre workflow auto-applied to all new datasets.
- Chaining and combining: an output of one workflow feeds another (beam centre, processed vanadium), runs are combined (angle series, sums over runs), and auto-reduction must be able to wait for a group of runs to be complete before it fires.

Key scope on another axis:

- Smooth interfacing with SciCat (or other data catalog) for input and output datasets.
- Architecture should allow for running compute locally or elsewhere (cluster, cloud, ...).

Our initial goal is to do high-level scoping, figure out key building blocks, and come up with technology recommendation.

## Technologies

- Our entire stack is so far built in Python.
- Team experience is mostly Python.
- We plan to lean on AI for building the actual user-interface, technology choice for this part should therefore take this into account.
  Key consideration: Playwright seems to be a good choice for AI-assisted UI development, nudging as towards web-based UI, but scientific plotting and the scipp/plopp library we maintain is not made for that.

## Items for consideration

For now this is random things that came to my mind, not in any order of importance or priority.

- workflow spec
  - scipp/ess#690 (ADR for workflow specs)
  - validation endpoint for workflow specs (if we cannot use the modal for validation in the frontend)
  - output specs should be same as input specs (for data), or at least outputs can fulfill inputs
- workflow runner -> hides where the workflow described by the spec runs (and hides what *implements* the workflow -- typically a scipp/sciline workflow for use, but app/framework does not need to know that)
- output (or input) handle (opaque: local, cluster, cloud, ...), facilities SciCat integration
- metrics, logging
- API (for agent access/inspection)?
- data service, holding workflow outputs (for inspection/plotting), or to be fed into follow-up workflows
  - outputs -> can be inputs
- plotting and output inspection
- SciCat integration (abstracted) <-> data handle?
- data-slicer -> compute/backend to handle slicing of large data so only small result needs to be sent to frontend?
workflow 
- config management (batch reduction) -> provenance graph; also non-batch reduction workflow chaining (may be manual), e.g., beam-center-finder-workflow -> reduciton-workflow
- viz + plotting
- look at Aiida or similar for inspiration