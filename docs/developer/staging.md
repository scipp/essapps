# Staging the implementation

Companion to [architecture.md](architecture.md), which this note reads against a three-phase delivery order.
It asks, for each phase, which parts of the sketch are needed, which can wait, and which might never be needed.
A second companion, [stateless.md](stateless.md), works out how much would disappear if the framework never kept state between runs, and what that would cost.
Both are analysis, not decisions; where a judgment is given it is marked as one.

## The three phases

The phases are the delivery order the team has in mind, not the order in which the sketch was written.

1. **Automatic reduction.**
   A rule fires on new datasets in the catalogue, a template is instantiated, the run executes unattended, the result is published, and a simple web page shows what fired, what failed and why, and a plot of each result.
   Nobody tunes anything while it runs.
   Done when one instrument's beamtime is reduced automatically end to end against the real SciCat, with an operator able to see a refusal or a failure without reading logs.
2. **Batch reduction.**
   A user fills in a form once, applies it to many datasets with per-dataset differences, watches progress, cancels, reruns the failures, and saves the form as a template.
   The interface is a web page that may grow forms, record browsing, comparison plots, and template management.
   Done when a two-hundred-member batch can be submitted, monitored, cancelled, and partially rerun from the web page, and its members found by a meaningful key.
3. **Per-technique applications.**
   Interactive reduction with sub-second feedback on cheap parameters, plot selections that become parameters, exploration of large volumes, and whatever each technique turns out to need.
   Scope is the least known of the three.

Phases 1 and 2 are the sketch's *shared mode* with throwaway processes only.
Phase 3 is where the sketch's *sessions* first do anything.

## What each phase needs from the sketch

The table goes through the decisions D1 to D13 and the failure-handling rules.
"Core" means needed from phase 1.
A number means the phase where the part is first needed.
"3 only" means nothing before phase 3 uses it.
"Optional" means the sketch itself says it can be left out.

| Decision | Part | First needed | Note |
|---|---|---|---|
| D1 | Requests, records, references to completed records | Core | The foundation; every phase stands on it. |
| D1 | File records for catalogue datasets by PID | Core | The trigger loop creates them. |
| D1 | File records for local paths, checksum on first read | 3 only | Or phase 2 if the web page accepts uploads; see "Candidates for removal". |
| D1 | Run-number resolution | 2 | A form field; the trigger loop already has the PID. |
| D1 | Recompute of a dropped copy | 2 | Nothing is dropped in phase 1 unless a quota is hit. |
| D1 | Records dropped by proposal, export bundle | 2 | An operations task; phase 1 keeps everything. |
| D1 | Templates | Core | Phase 1 needs version-controlled files only; saving a request as a template is phase 2. |
| D1 | Batch, member keys | 2 | The whole of phase 2. |
| D2 | Requests are stateless | Core | |
| D2 | Sessions, session invariants | 3 only | |
| D3 | Registry of disk copies, disk tier | Core | The data store in phases 1 and 2 is exactly this. |
| D3 | Private memory caches, two execution shapes, one shape per group | 3 only | Phases 1 and 2 have one shape. |
| D3 | Retention of disk copies | 2 | Disk fills during phase 2 batches; the policy is an open question. |
| D4 | Split a workflow where an output is reused | Core | Processed vanadium and beam centre in an automatic template. |
| D4 | Split for iteration without a session | Core in phases 1 and 2 | Without sessions this is the only rule and it always applies. |
| D5 | Own record store, single writer, schema version | Core | |
| D6 | Pending outputs as inputs, groups, cycle check, failure propagation | Optional; first concrete need probably 2 | Phase 1 sequences chains through trigger events on completed records, not through groups; see below. |
| D7 | Dataset source, fake implementation | Core | |
| D7 | Real SciCat dataset source | Core | The sketch defers real SciCat; phase 1 cannot. |
| D8 | One callable, entry points, materialization by kind | Core | |
| D8 | Three validation layers | Core | The trigger loop's refusals need structured errors from day one. |
| D8 | Warm reuse, cheap parameters, warm-equals-cold helper, accumulation | 3 only | |
| D8 | In-process binding, no-shadowing rule | 3 only | Phases 1 and 2 bind through installed packages; developers install editable. |
| D9 | The client interface is the API; validate separate from submit | Core | |
| D9 | Notebook as the first client | Reordered | The first clients are the trigger loop and a web page in the backend process. |
| D9 | HTTP transport | 2, as a decision | Needed as soon as a client lives outside the backend process: a JavaScript frontend or a notebook submitting batches. |
| D10 | Views as plain arrays over dense outputs | Core, trivially | Phase 1 views are whole small outputs; slicing and overlays come with phase 2 plots. |
| D10 | Event data never viewed, dense twin outputs | Core | A rule on workflow authors, free for the framework. |
| D10 | Slots, latest-by-label, evict-superseded-first, fork, cancel predecessors, diff | 3 only | |
| D11 | Publication with a provenance snapshot, real SciCat publisher | Core | Automatic reduction that publishes nothing is invisible. |
| D11 | Cold recompute before publishing a reused result; refusal of in-process bindings | 3 only | Every run in phases 1 and 2 is cold. |
| D12 | Instrument plus proposal on every record; one backend per instrument | Core | |
| D12 | Instrument-shared artefacts and templates | Core, or replaced | First needed when external users' proposals consume commissioning artefacts; see "Candidates for removal". |
| D13 | Data-reference parameters, typed outputs, collections | Core | DREAM produces per-bank outputs and reflectometry consumes lists of runs in phase 1 already. |
| D13 | Cheap parameters on the spec | 3 only | |
| D13 | Code revision on the spec | Core | One optional field; keeps development records honest. |
| Failure | State machine, completion marker, reconciliation on restart, liveness, structured failure reason, trigger status | Core | Unattended operation is what phase 1 is. |
| Failure | Cancel by batch | 2 | |
| Failure | Session loss | 3 only | |
| Failure | Access checked on every reference | Core if external users see phase 1; else 2 | |

The same by component.

| Component | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Backend | Validate, submit one, dispatch, poll, retry | Plus group submit if adopted, cancel by batch, recompute | Unchanged |
| Client interface | In-process, used by the trigger loop and the web page | Plus HTTP transport if the UI or notebooks leave the process | Plus slots, direct scipp access in notebooks |
| Launcher | Subprocess on the backend host | Plus cluster when volume demands | Plus session, and later remote session |
| Runner | Cold: construct, call once, marker | Unchanged | Plus keep the callable |
| Data store | Disk tier plus registry | Plus retention and drop | Plus private caches, write-out on demand |
| Record store | Records, references, list by proposal and time | Plus list by batch and member key | Plus slot column and latest-by-label |
| Dataset source | Real SciCat plus fake | Unchanged | Unchanged |
| Trigger loop | On datasets and on completed records; group rules | Unchanged | Unchanged |
| Publisher | Real SciCat plus fake | Unchanged | Plus cold recompute rule |
| Templates | Version-controlled files, one version bound per loop | Plus saved from a request, derived by copy | Unchanged |
| Views | Whole small outputs | Slicing, reduction, overlays, a read cache in the service | Volumes, served from session memory |
| Web UI | Status page, record list, plot | Forms with live validation, batch monitor, template editor | Per-technique applications |
| Sessions, warm workflows, slots | Absent | Absent | The whole of the addition |

## Phase 1 in detail

**What it needs from the sketch.**
Records, references, file records by PID, templates from files, the record store, the backend without groups, the subprocess launcher, the cold runner, the disk tier with its registry, the dataset source, the trigger loop, the publisher, trivial views, and all of failure handling.
That is the sketch with Choice 1 reduced to its first sentence, Choice 3 without its warm half, and Choice 4 without slots.

**What it needs that the sketch defers or underweights.**
This list matters more than the previous one, because it is the critical path.

- The real SciCat dataset source and the real publisher.
  The sketch lists both under "explicitly deferred"; phase 1 is defined by them.
- The trigger loop firing on completed records as well as on new datasets.
  The sketch's component description says both; the skeleton's loop handles datasets only.
  With both, a chain such as vanadium then sample is two rules and needs no pending outputs.
- Group rules.
  The open question "groups as the automatic-reduction unit" is a phase 1 question, because a reflectometry beamtime reduced automatically is nothing but angle series.
  A rule that waits for a series and submits one request with a list-valued parameter needs no scheduler support beyond what a single request has.
- A deployment: the backend as a long-running service on one host per instrument, with the lock, restart reconciliation, logs, and something that pages an operator.
- Authentication for the web page, mapping a login to SciCat proposal membership, unless the phase 1 page is restricted to instrument staff.
- A web UI framework choice.
  The sketch chooses after the skeleton; phase 1 ships a page.
  A Python web framework running in the backend process keeps the client interface as the only API and needs no HTTP transport.
- A quota alarm, because retention is not designed yet and a beamtime of automatic reduction fills disks.

**What phase 1 can do without and should not build.**
Sessions, warm workflows, cheap parameters, the private cache, the second execution shape, slots, local file records, in-process binding, batch, user-saved templates, run-number resolution, recompute, groups, the HTTP transport.

**Two simplifications available in phase 1 that the sketch does not mention.**

- A template can reference its artefacts as published PIDs rather than as outputs of records in a commissioning proposal.
  Then no reference crosses a proposal and the instrument-shared rules are not needed yet.
- Views can be the whole output, because automatic reduction produces small final results.
  The view vocabulary is designed when phase 2 plots need it.

## Phase 2 in detail

**Additions.**
Batch with member keys, validated whole and cancelled whole; templates saved from requests and derived by copy; run-number resolution; a record browser by proposal, time, batch, and member key; recompute; retention and drop; cluster launcher when one host is not enough; the view vocabulary for slicing and overlays; live validation in forms.

**Decisions that fall due.**

- *HTTP transport.*
  As long as the web page is a Python framework in the backend process, and notebooks either run their own throwaway backend or are not used, no transport is needed.
  The first JavaScript frontend or the first notebook that submits to the shared service forces it.
  The stateless core makes the transport trivial: every object is plain data and no request needs to reach a particular process.
- *Pending outputs.*
  The first concrete need is a user submitting a stage and its consumers together, or a map-combine such as a temperature scan followed by a combine.
  Until a workflow asks for it, "submit the stage, wait, submit the batch" costs the user one wait and the framework nothing.
  When it is adopted, the skeleton already has it.
- *Local uploads.*
  Whether the shared web page accepts files from a user's disk.
  Refusing them removes local file records, checksums on read, the per-proposal quota, the retention exemption, and the redaction question of story A4 from phases 1 and 2 entirely.
- *Cluster launcher.*
  Brings heartbeats instead of process polling, jobs under the submitting user, download tokens, and the "no broker" question.
  Story D2, thirty multi-gigabyte NMX runs overnight, is the first that needs it.

**Still absent.**
Everything in the phase 3 list below.
A "manual" reduction from the web page in phase 2 is a batch of one; a rerun with a changed parameter is a fresh process and a new record linked to the old one by a retry derivation.

## Phase 3: what appears only here

The direct answer to "is there anything phases 1 and 2 would not require but phase 3 would":

- Sessions, and the two invariants that keep them safe: no session identity in a record, and everything in a session recomputable from records.
- The warm workflow, the sciline wrapper with its frontier computation, the declaration of cheap parameters, the warm-equals-cold test helper, and the accumulation special case for growing lists.
- The `reused` flag on the record, and the rule that a reused result is recomputed cold before publication.
- Private memory caches, the rule that the registry knows disk copies only, the memory lifetime, and the cache-coherence argument for keeping caches private.
- Two execution shapes, placement as a launcher decision, `needs_disk_inputs`, write-out on demand, and the rule that a group runs in one shape.
- Slots: the label, latest-by-label, evict superseded first, cancel a slot's predecessors, forking into a second label, and the inspection tooling that shows the diff between successive records.
- In-process binding, the no-shadowing rule, and the refusal to publish an in-process record.
- Local file records with checksum on first read, store copies of local files exempt from retention, and the per-proposal quota, unless phase 2 accepts uploads.
- Per-notebook record stores, the two-notebooks question, and upload of a private record store to the shared backend.
- Session loss as a failure event, and the absence of a liveness timeout for session runs.
- Remote sessions, the session launcher, idle timeout, session cap, and the interpreter-sharing cost of a one-process application.
- Views served from the process holding a copy rather than from the service.
- The second reason in D4 and the sentence that it disappears inside a session.

Measured against the skeleton and the sketch, with the caveat that the boundary is a judgment:

| | Total | Phase 3 only | Share |
|---|---|---|---|
| Skeleton source lines | 2651 | about 380 | about one seventh |
| Architecture text lines | 644 | about 130 | about one fifth |

The share of the text is larger than the share of the code because the phase 3 parts carry the subtlest invariants and the longest justifications.
The code share is small because the skeleton isolates them well: a launcher class, a wrapper module, a flag on the runner, a column in the store.
That isolation is a merit of the sketch and is what makes staging cheap.

Phase 3 also needs a decision that the sketch takes for granted: whether interactive work is modelled as sessions over records at all.
[stateless.md](stateless.md) lays out the alternatives.

## Candidates for removal or simplification

Grouped by how confident the judgment is.
Each says what goes and what it would cost to lose.

**Park until phase 3, and keep out of the core documents until then.**
Everything in the phase 3 list.
Nothing in phases 1 and 2 becomes harder without them, and the skeleton shows the retrofit is additive: a second launcher, a keep flag, a cache in front of the disk tier, a column.

**Defer until a workflow asks.**

- Pending outputs and groups (D6).
  The trigger loop's completed-record event covers automatic chains, and a user can wait for a stage before submitting a batch.
  Keep the design; do not make it part of phase 1's acceptance.
- The view vocabulary beyond "the whole output".
- Instrument-shared artefacts and templates (D12).
  Needed the first time an external user's proposal consumes a commissioning artefact that is not published.
  Publishing the artefact and referencing its PID is the alternative; it costs a SciCat entry per artefact version and fits "only finalized data enters SciCat" only if commissioning artefacts count as finalized.
  Keep the marker in the design, but it is three rules (access, retention exemption, templates) and none of them is needed while instrument staff are the only users.

**Consider dropping outright.**

- Store copies of local files in shared mode, with their quota, retention exemption, and the redaction question.
  Refuse uploads in the shared service; a local application has no retention and needs none of the rules.
  Cost: a user away from the facility with an uncatalogued file cannot use the shared service for it, which is arguably correct.
- The rule that a group runs in one execution shape.
  It exists only because two shapes exist; with sessions parked it is not a rule, and when sessions arrive the simpler rule "slot runs execute in the submitter's session, everything else in a throwaway process" may be enough.
- Slot inspection, replay, and diff as framework features.
  If phase 3 keeps sessions, the label is one column and the latest-by-label query is one line; the tooling on top of it is application code and should be designed with the application.

**Considered and not recommended.**

- Replacing file records with a second reference form that names a PID directly.
  It removes the built-in `file` spec and the create-on-first-reference rule, but adds a second kind of reference to every walk, and loses the one query "which runs used this dataset" that comes free from references being records.
- Removing the retry and recompute distinction.
  It is one enum value and the record browser will want the word.

## Where the sketch's build order differs from the phase order

The sketch's "Next step" builds local mode first, with the notebook as the first client and sessions in the walking skeleton, and defers SciCat, HTTP, the UI, and deployment.
That order is right for testing the riskiest ideas, which is what the skeleton was for.
It is the reverse of the delivery order: phase 1 is shared mode, unattended, against SciCat, with a page, and without sessions.

Recommended reordering of the next steps, as a judgment:

1. Keep the skeleton as it is, but make the throwaway launcher the default configuration and treat the session launcher as an experiment that stays out of the phase 1 and 2 acceptance tests.
2. Build the real dataset source and publisher against SciCat behind the existing fakes, since phase 1 cannot be shown without them.
3. Extend the trigger loop to completed-record events and group rules, which closes open question E1 for phase 1.
4. Choose the web framework for the status page and run it in the backend process against the client interface.
5. Deploy for one instrument, and let phase 2 start from what the operators ask for.

## A suggested split of the architecture document

The document would be easier to review against the phases if it were two layers rather than one.

- A **core** that is complete in itself: D1, D5, D6 marked optional, D7, D8 without its warm half, D9, D10 without slots, D11 without the cold-recompute rule, D12, D13 without cheap parameters, and failure handling without session loss.
  This is the design of phases 1 and 2, and a reader can check that it closes with no forward reference to a session.
- An **interactive extension** that adds sessions, warm workflows, cheap parameters, slots, private caches, the second execution shape, and local file records, each stated as an addition to a named part of the core.
  It should open with the decision that [stateless.md](stateless.md) puts on the table, because the extension is one of three ways to do phase 3 and not obviously the right one.

The user stories can be tagged the same way; A1, B1 to B5, C5, F3, G2, G3, and G4 are the extension's stories, and every other story runs against the core alone.
