# Glossary

The terms of the design, in alphabetical order, each with a pointer to the document that develops it.
[architecture.md](architecture.md) is the overview these terms come from.
Where esslivedata uses a word differently, the clash is noted, because the two projects are discussed together often enough for it to matter.

- **Accumulation key**: a node of a workflow at which the members' intermediates are added.
  Usually two, a numerator and a denominator, so that normalisation comes after the sum.
  One accumulator sits at each.
  sciline's term.
- **Accumulator**: sciline's object that takes values by `push` and holds their combination as its value, one per accumulation key.
  `Buffered` makes one from a combine function.
  A running total holds one array instead of one per member.
- **Adapter**: the code, in ess.reduce, that turns a sciline pipeline into the callables of one or more specs.
  It maps fields to keys, names the form of each data reference, and for an aggregation the accumulation keys, the accumulators, and the member parameters.
  It checks its specs against the graph when it is built.
  See [workflow-contract.md](workflow-contract.md#the-callable).
- **Aggregation**: in the framework, a group of one member request per row of a member table plus one combine request that references the members' contributions.
  In sciline, the object that composes a contribute stage, accumulators, and a finalize stage in one process.
  Being a composition of two callables rather than a callable, it has no spec.
  See [aggregation.md](aggregation.md).
- **Annotations**: labels and notes attached to a record after the fact.
  Mutable, outside provenance, read by nothing in the framework.
- **Apply**: the client operation that fills a template through a lookup for a set of datasets and returns a group to preview and submit whole.
  Called by a batch form, by the trigger loop per arrival, and by the backlog, reprocess, and rerun operations.
  In a notebook it accepts a DataFrame, member key as index and pinned values as columns.
  See [rules.md](rules.md).
- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results.
  In esslivedata, "backend services" are the Kafka worker processes, which are unrelated.
- **Batch**: the records under one label, made by a person from a template or by a rule.
  Not a stored unit.
  In esslivedata a batch is a bundle of messages, which is unrelated.
- **Chain**: a field of a combine spec mapping one of its collection parameters to one of its own outputs.
  It says that this output may be passed back as an element of that parameter, where it stands for everything it was combined from.
  A **chained** combine request is one that uses this, referencing the previous combine's contribution instead of every member it covers.
  See [aggregation.md](aggregation.md).
- **Client interface**: the backend's Python interface, including validate, apply, views, and the picker.
  The API.
  See [operations.md](operations.md#the-client-interface).
- **Collection**: a list or dict of values of one declared type, usable as a parameter and as an output.
  A reference may name one element of a collection output by key.
- **Combine spec**: the spec of the part of a pipeline from the accumulation keys to the results.
  It takes a collection parameter of references to contributions plus the parameters only the finalize stage reads, and its outputs are the results and the combined contribution.
  A **combine request** is a request of such a spec.
  See [aggregation.md](aggregation.md).
- **Contribute spec**: the spec of the part of a pipeline from the contribute parameters to the accumulation keys.
  See [aggregation.md](aggregation.md).
- **Contribution**: the one output of a contribute spec, a scipp data group with one entry per accumulation key.
  Opaque to the framework, and it also carries the contribute parameters that all members of a combine must share.
- **Data reference**: a field type: a parameter or output declared to hold a reference to a file or an array rather than a literal.
  Easy to confuse with *reference*, which is the value such a field holds.
- **Data store**: where the bytes of large outputs live: a registry of disk copies and a disk tier, owned by the backend.
  Each process that holds data also has a private memory cache, which the store serves from but never registers.
  See [records.md](records.md#the-data-store).
- **Dataset**: data the framework did not compute: a SciCat dataset, identified by its PID, or a file on a user's disk, identified by the instrument and run number it carries or else by its path.
  The second form of reference.
  Not a record: no request, no status, nothing to recompute.
- **Dataset source**: where datasets are discovered and listed.
  Persists nothing.
  SciCat for a proposal, a folder in local mode, a fake for tests.
- **Group**: several requests submitted atomically that may reference each other's outputs before those exist.
  See [records.md](records.md#scheduling-pending-outputs-as-inputs).
- **Input**: a parameter of data-reference type.
  In esslivedata, inputs are data streams and genuinely differ from parameters; here they do not.
- **Label**: a field on a request, with an optional member key.
  Each record under a label names the record it superseded, and the latest is the one nothing supersedes.
  A rule's name for the records it makes, a name a submitter picks for a batch, and a slot for an interactive tool are all labels.
  See [rules.md](rules.md#labels-batches-and-slots).
- **Launcher**: decides where a run executes and starts it there: session, subprocess, or cluster.
- **Local mode**: client, backend, launcher, session, and data store in one Python process.
  **Shared mode**: the backend as a service used by many people.
  The **shared service** is that backend's process, which also holds a memory cache.
- **Lookup**: stored, versioned data beside a template: ordered entries that match dataset metadata and supply template fills, literal or **as-of**, the nearest earlier dataset matching criteria, with at most one wildcard.
  What ISIS calls a lookup table, a cycle mapping, or a per-row user file.
  See [rules.md](rules.md).
- **Member**: one row of a member table: one request of an aggregation, whose output the combine request references.
  Its **member key** labels it under the batch's label.
- **Member parameters**: the parameters that differ from member to member of an aggregation, such as the run.
  Only the adapter deals with them.
  sciline calls these member keys, which this document does not, because a member key here is the label of a batch member.
- **Pending output**: an output of a record that has not completed yet, usable as input to another request.
- **Picker**: the client query behind an input field: candidates of matching format from the record store and from every dataset source, as rows of one shape.
- **Pinned values**: the values of a request set for one member beyond the template and the lookup, the top rung of the precedence ladder.
  The submission keeps them apart from the resolved parameters, so a reprocess under a new template or lookup version fills the rest again and leaves them alone.
  See [rules.md](rules.md#templates-and-lookups).
- **Predecessor**: the request a new request is compared with when the session chooses the stage inputs.
  It is the request the new one supersedes, or, for a new member of a batch, the latest request under the same label.
  A request without a predecessor has nothing to differ from.
  See [stages.md](stages.md#who-chooses-the-stage-inputs).
- **Proposal**: the experiment allocation that owns data and defines who may access it.
  See [operations.md](operations.md#scope-instrument-plus-proposal).
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
  The graph obtained by following references.
- **Recompute**: an explicit operation that runs a record's request again and yields a new record linked to the old one.
- **Record store**: the database of records.
  Records are never deleted one at a time; a proposal's records are dropped together.
  See [records.md](records.md#the-record-store).
- **Reference**: a value naming output X of record Y, optionally with an element key, or a dataset.
  The only way a request names data.
  See [records.md](records.md#references).
- **Retention**: how long a disk copy is kept within its proposal's lifetime.
  It applies to bytes, never to single records.
- **Rule**: stored, versioned data that makes requests from datasets: a selector with a lower bound, a template and a lookup, a retry policy, exclusions, an active state, and optionally a series key and a combine clause.
  A rule is to a batch what a template is to a request, and its label is reserved for it.
  See [rules.md](rules.md).
- **Run record**: a run request plus what happened to it.
  Called "run", never "job", except for the launcher's own job IDs.
  In esslivedata a job is a running streaming workflow.
- **Run request**: everything needed to execute a workflow once.
  See [records.md](records.md#references).
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **SciCat**: the facility's data catalogue.
  **PID**: SciCat's persistent identifier for a dataset.
- **Series**: the datasets a rule keys together by a metadata value, aggregated again whenever one joins.
- **Session**: a runner plus a private memory cache, belonging to one client, keeping the outputs of its runs and the stages of its workflows in memory.
  A cache over records.
  See [records.md](records.md#where-runs-execute-and-where-data-lives).
- **Slot**: a label an interactive tool owns, with no member key.
  The unit of interactive work, and what tools list and replay.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs.
  The signature of one callable, and a record is one call of it.
  Defined in scipp/ess#690 with the extensions in [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).
- **Stage**: a callable with the signature of a workflow that accepts every request equal to a given one outside its stage inputs, and may hold whatever those inputs cannot affect.
  A workflow may offer stages, and the session builds, holds, and drops them.
  See [stages.md](stages.md).
- **Stage inputs**: the set of parameter field names a stage accepts changes in.
  The session chooses them from the fields in which a request differs from its predecessor, so they are neither part of the spec nor the workflow author's choice.
  See [stages.md](stages.md#who-chooses-the-stage-inputs).
- **Stage output**: an output that one spec produces and another takes as input, kept as a record.
  The word names a role, not a kind.
  See [records.md](records.md#reuse-means-a-workflow-boundary).
- **Submission**: the field on a run record that says how its request was made: the template version, the rule version when a rule filled it, the lookup version and the entry that applied, and the values the submitter pinned beyond template and lookup.
  Explanation, not provenance.
- **Template**: a saved, versioned run request with some fields left blank.
  See [rules.md](rules.md).
- **Throwaway process**: a subprocess or cluster job that runs one request and exits.
  The execution shape of shared mode.
- **Trigger loop**: applies the active rules to new datasets and completed records.
  Keeps no state: every decision is a query over the records.
  See [rules.md](rules.md#the-trigger-loop).
- **View**: a small piece of an output's data for display, computed by the process holding a copy.
  Not a run.
  See [stages.md](stages.md#views).
- **Vocabulary**: the set of types the workflow spec allows for parameters and outputs.
- **Workflow**: the scientific code that turns inputs into results: a stateless callable whose signature is a spec.
  Typically a sciline pipeline behind an adapter; the framework does not care.
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).
