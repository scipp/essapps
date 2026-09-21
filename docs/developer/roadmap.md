# Delivery phases

This document maps the design onto the order in which the team plans to deliver it.
It says which parts each phase needs, what the walking skeleton lacks for the first phase, and which parts can wait or might be dropped.
The design itself is in [architecture.md](architecture.md).
Where this document gives a judgment and not a fact, it says so.

## The three phases

1. **Automatic reduction.**
   A rule fires on new datasets in the catalogue, the run executes unattended, and the result is published.
   A simple web page shows what fired, what failed and why, and a plot of each result.
   Done when one instrument's beamtime is reduced automatically against the real SciCat, and an operator can see a refusal or a failure without reading logs.
2. **Batch reduction.**
   A user fills in a form once, applies it to many datasets with differences per dataset, watches progress, cancels, reruns the failures, and saves the form as a template.
   Done when a batch of two hundred members can be submitted, monitored, cancelled, and partially rerun from the web page.
3. **Applications per technique.**
   Interactive reduction with feedback in under a second, plot selections that become parameters, and exploration of large volumes.
   The scope of this phase is the least known of the three.

Phases 1 and 2 are shared mode with throwaway processes only.
Phase 3 is where sessions are first used.
The skeleton was built in the opposite order, local mode and sessions first, because those held the riskiest ideas.

## What each phase needs

| Part of the design | Phase 1 | Phase 2 adds | Phase 3 adds |
|---|---|---|---|
| [Records and references](records.md) | requests, records, output references, dataset references by PID | run-number resolution, recompute, dropping a proposal with its export bundle | local files as datasets with checksums |
| [Data store](records.md#the-data-store) | disk tier and registry, a quota alarm | retention and drop | private memory caches, the second execution shape, write-out on demand |
| [Scheduling](records.md#scheduling-pending-outputs-as-inputs) | none required; see below | pending outputs and groups, when a workflow asks | unchanged |
| [Workflow contract](workflow-contract.md) | the callable, entry points, path or object, three validation layers, typed outputs, collections, code revision | unchanged | the stage offer, in-process binding |
| [Aggregation](aggregation.md) | member requests and a combine request over all members, for angle series | contribute and combine specs with `chain`, for sums over runs | the fold, only if a series arrives faster than its contribution can be read and written |
| [Rules](rules.md) | templates from files, lookups, rules, `apply`, labels and member keys, the trigger loop, trigger status, cancel by label, real SciCat dataset source | the batch form, templates saved from requests | unchanged |
| [Interactive work](stages.md) | views of whole small outputs, dense twins of event outputs | view vocabulary for slicing and overlays, a read cache in the service | sessions, held stages, slots, views from session memory |
| [Client interface](operations.md#the-client-interface) | in-process, used by the trigger loop and a web page in the backend process | HTTP, once a client lives outside the backend process | direct scipp access in notebooks |
| [Publication](operations.md#publication) | provenance snapshot, real SciCat publisher | unchanged | recompute before publishing a result that a stage served |
| [Failure handling](operations.md#failure-handling) | all of it except session loss | unchanged | session loss |
| Launcher | subprocess on the backend host | cluster, when one host is not enough | session, later remote session |
| Web UI | the rule's batch table, record list, plots | forms with live validation, per-member overrides, template editor | applications per technique |

Batch and automatic reduction use nothing from the phase 3 column.
[stages.md](stages.md#what-sessions-cost-in-concepts) lists what the phase 3 column amounts to, about one seventh of the skeleton's source.

## The critical path for phase 1

The skeleton does not contain these, and phase 1 cannot be shown without them:

- **A SciCat dataset source and a SciCat publisher**, behind the interfaces that the folder source and the fake publisher implement today.
- **A trigger loop that fires on completed records** as well as on new datasets.
  A chain such as vanadium then sample is then two rules and needs no pending outputs.
- **A deployment**: the backend as a long-running service on one host per instrument, with the store lock, reconciliation at restart, logs, and an alert to an operator.
- **Authentication for the web page**, mapping a login to SciCat proposal membership, unless the page is restricted to instrument staff.
- **A web framework for the status page.**
  A Python web framework in the backend process keeps the client interface the only API and needs no HTTP transport.
- **A quota alarm**, because retention is not designed and a beamtime of automatic reduction fills disks.

Two simplifications are available in phase 1:

- A template can reference its artefacts, such as a direct beam, as published PIDs and not as outputs of records in a commissioning proposal.
  No reference then crosses a proposal, and the rules for instrument-shared artefacts are not needed yet.
- A view can be the whole output, because automatic reduction produces small final results.

## Decisions that fall due in phase 2

- **HTTP transport.**
  The first JavaScript frontend, or the first notebook that submits to the shared service, forces it.
  Every object is plain data and no request needs to reach a particular process, so the transport is thin.
- **Pending outputs.**
  The first need is a user who submits a vanadium reduction and its consumers together, or a temperature scan followed by a combine.
  Until then, "submit, wait, submit the batch" costs the user one wait. The skeleton already implements pending outputs.
- **Local uploads.**
  Refusing files from a user's disk in the shared service removes local files as datasets, checksums, the quota per proposal, and the retention exemption from phases 1 and 2.
  The cost is that a user away from the facility cannot use the shared service for a file that is not in the catalogue.
- **Cluster launcher.**
  It brings heartbeats in place of process polling, jobs under the submitting user, download tokens, and the question of a message broker.
  Thirty NMX runs of several gigabytes overnight is the first user story that needs it.

In phase 2 a manual reduction from the web page is a batch of one, and a rerun with a changed parameter is a throwaway process and a new record.

## Candidates for deferral or removal

These are judgments.

**Defer until a workflow asks**: pending outputs and groups, the view vocabulary beyond the whole output, and instrument-shared artefacts, which are three rules (access, retention exemption, templates) that are not needed while instrument staff are the only users.

**Consider dropping**:

- Store copies of local files in shared mode, by refusing uploads as above.
- The rule that a group runs in one execution shape.
  "Slot runs execute in the submitter's session, everything else in a throwaway process" may be enough.
- Slot inspection, replay, and diff as framework features.
  The label is one column and the latest-per-label query is one line. The tooling on top is application code and should be designed with the application.

**Considered and kept**: the distinction between retry and recompute.
It is one enum value, and a record browser needs the word.

## Recommended build order

As a judgment:

1. Keep the skeleton, make the throwaway launcher its default, and keep the session launcher out of the acceptance tests of phases 1 and 2.
2. Build the SciCat dataset source and publisher.
3. Extend the trigger loop to completed records.
4. Choose the web framework for the status page and run it in the backend process.
5. Deploy for one instrument, and let phase 2 start from what the operators ask for.

Phase 3 also needs a decision that is still open: whether interactive work uses sessions at all.
See [stages.md](stages.md#sessions-are-one-of-three-models) and [open-issues.md](open-issues.md#open-questions).

The [user stories](user-stories.md) A1, B1 to B5, C5, F3, G2, G3, and G4 exercise phase 3.
All others run against phases 1 and 2 alone.
