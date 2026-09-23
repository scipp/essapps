# Glossary

The terms of the design, in alphabetical order, each with a pointer to the document that develops it.
[architecture.md](architecture.md) is the overview these terms come from.
Where esslivedata uses a word differently, the clash is noted, because the two projects are discussed together often enough for it to matter.

- **Accumulate**: a supplied intermediate whose value is the accumulation of the outputs it lists, `Accumulate(accumulate=[...])`.
  The binding resolves it with the accumulator its author gave for that intermediate; the framework never adds anything.
  See [aggregation.md](aggregation.md).
- **Accumulation key**: sciline's name for an intermediate at which the members of an aggregation are accumulated.
  This documentation calls it an intermediate to accumulate.
- **Accumulator**: sciline's object that takes values by `push` and holds their accumulation as its value.
  The binding gives one per intermediate that may be accumulated, and `Buffered` makes one from a function.
  Every accumulator must be associative, so that a finalize may accumulate onto a value an earlier finalize accumulated.
  A session holds accumulators between finalize calls.
- **Adapter**: the code, in ess.reduce, that turns a sciline pipeline into a workflow, and each run into a call of a `sciline.Stage`.
  It maps fields to keys, names the form of each data reference, and gives the accumulators by sciline key, the same dictionary a `sciline.Aggregation` takes.
  See [workflow-contract.md](workflow-contract.md#the-sciline-adapter).
- **Aggregation**: in the framework, member run records plus a finalize run record that accumulates their intermediates, all agreeing on the parameters they set outside what they vary.
  In sciline, the object that composes a contribute stage, accumulators, and a finalize stage in one process.
  It has no spec of its own.
  See [aggregation.md](aggregation.md).
- **Annotations**: labels and notes attached to a record after the fact.
  Mutable, outside provenance, read by nothing in the framework.
- **Apply**: the client operation that fills a template through a lookup for a set of datasets and returns a group to preview and submit whole.
  Called by a batch form, by the trigger loop per arrival, and by the backlog, reprocess, and retry operations.
  In a notebook it accepts a DataFrame, member key as index and pinned values as columns.
  See [rules.md](rules.md).
- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results.
  In esslivedata, "backend services" are the Kafka worker processes, which are unrelated.
- **Batch**: the records under one label, made by a person from a template or by a rule.
  Not a stored unit.
  In esslivedata a batch is a bundle of messages, which is unrelated.
- **Client interface**: the backend's Python interface, including validate, apply, views, and the picker.
  The API.
  See [operations.md](operations.md#the-client-interface).
- **Collection**: a list or dict of values of one declared type, usable as a parameter and as an output.
  A reference may name one element of a collection output by key.
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
- **Finalize**: the stage of an aggregation from the accumulated intermediates to the outputs.
  A finalize run request supplies one `Accumulate` per intermediate.
  A **chained** finalize also outputs the accumulated values, and the next finalize accumulates onto them instead of every member they cover.
  See [aggregation.md](aggregation.md#when-chaining-is-valid).
- **Group**: several requests submitted atomically that may reference each other's outputs before those exist.
  See [records.md](records.md#scheduling-pending-outputs-as-inputs).
- **Input**: a parameter of data-reference type.
  Not the same as a *stage input*, which may also be a literal parameter or a supplied intermediate.
  In esslivedata, inputs are data streams and genuinely differ from parameters; here they do not.
- **Intermediate**: an output the spec exposes that a plain run does not compute, a value inside the pipeline such as a numerator or a beam centre.
  A request may name it as an output, or supply it in place of what computes it.
  A supplied value is a reference to an output of a run record, or an `Accumulate`.
  See [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).
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
- **Member**: one record under a label, identified by its **member key**: a name a person chose for a batch made by hand, the dataset identity for a rule's batch.
  An aggregation's members are the run records whose intermediates its finalize accumulates.
- **Origin**: the field on a run request that says where its values came from: the template version, the rule version when a rule filled it, the lookup version and the entry that applied, and the values pinned beyond template and lookup.
  Explanation, not provenance, which is the data the run read.
- **Pending output**: an output of a record that has not completed yet, usable as input to another request.
- **Picker**: the client query behind an input field: candidates of matching format from the record store and from every dataset source, as rows of one shape.
- **Pinned values**: the values of a request set for one member beyond the template and the lookup, the top rung of the precedence ladder.
  The origin keeps them apart from the resolved parameters, so a reprocess under a new template or lookup version fills the rest again and leaves them alone.
  See [rules.md](rules.md#templates-and-lookups).
- **Plain run**: a run that varies nothing and supplies nothing, computing the spec's results, as `client.run` and `wf.stage().compute()` submit it.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
  See [operations.md](operations.md#scope-instrument-plus-proposal).
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
  The graph obtained by following references.
- **Recompute**: an explicit operation that runs a run record's request again and yields a new record linked to the old one.
- **Record store**: the database of records.
  Records are never deleted one at a time; a proposal's records are dropped together.
  See [records.md](records.md#the-record-store).
- **Reference**: a value naming output X of run record Y, optionally with an element key, or a dataset.
  The only way a request names data.
  See [records.md](records.md#references).
- **Retention**: how long a disk copy is kept within its proposal's lifetime.
  It applies to bytes, never to single records.
- **Results**: the outputs of a spec that are not intermediates; what a plain run computes.
- **Rule**: stored, versioned data that makes requests from datasets: a selector with a lower bound, a template and a lookup, a retry policy, exclusions, an active state, and optionally a series.
  A rule is to a batch what a template is to a request, and its label is reserved for it.
  See [rules.md](rules.md).
- **Run record**: a run request plus what happened to it: status, outputs, resolved parameters, versions.
  Its execution is called a run, never a job, except for the launcher's own job IDs.
  In esslivedata a job is a running streaming workflow.
  See [records.md](records.md#requests-and-records).
- **Run request**: one run of a spec: the spec, every parameter value in `params`, the supplied intermediates, the outputs to compute, and the names in `vary`.
  Everything needed to run it.
  The backend fills the spec's defaults into `params` at submit, for every parameter not given.
  A call of a stage is one run request.
  See [records.md](records.md#requests-and-records).
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **SciCat**: the facility's data catalogue.
  **PID**: SciCat's persistent identifier for a dataset.
- **Series**: the datasets a rule keys together by a metadata value.
  `Series(key, accumulate, outputs)` on a rule makes each arrival submit a member run request and a finalize run request.
  See [rules.md](rules.md#series).
- **Session**: a runner plus a private memory cache, belonging to one client, keeping the outputs of its run records, and the stages and accumulators its requests name, in memory.
  A cache over records.
  See [records.md](records.md#where-runs-execute-and-where-data-lives).
- **Slot**: a label an interactive tool owns, with no member key.
  The unit of interactive work, and what tools list and replay.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs, and which outputs are intermediates.
  The signature of a pipeline: every parameter and every value a caller may ask for.
  Defined in scipp/ess#690 with the extensions in [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).
- **Stage**: the part of a pipeline from named stage inputs to named outputs, cut from a spec with parameters set, like `sciline.Stage`.
  It may hold whatever its inputs cannot affect.
  The caller names it; the workflow builds it; the session holds it under the name its run requests give it.
  See [stages.md](stages.md).
- **Stage inputs**: the values a stage takes on each call: the parameters a request varies, and the intermediates it supplies.
  The caller names them with `wf.stage(inputs=...)`, and a rule's template names them as its blanks.
  See [stages.md](stages.md#who-names-the-stage).
- **Supplied intermediate**: an intermediate a run request holds in `supplied`, a reference or an `Accumulate`, in place of what computes it.
  Only supplied intermediates and `params` change what a run computes.
- **Template**: a saved, versioned request with some fields left blank.
  `apply` makes each member vary the blanks.
  See [rules.md](rules.md).
- **Throwaway process**: a subprocess or cluster job that runs one request and exits.
  The execution shape of shared mode.
- **Trigger loop**: applies the active rules to new datasets and completed records.
  Keeps no state: every decision is a query over the records.
  See [rules.md](rules.md#the-trigger-loop).
- **Vary**: the field of a run request that names the parameters a caller varies from run to run, each with its value in `params`.
  A hint for the session, like a label: it does not change the result, it is not part of the workflow ID, and provenance does not rely on it.
  See [records.md](records.md#requests-and-records).
- **View**: a small piece of an output's data for display, computed by the process holding a copy.
  Not a run.
  See [stages.md](stages.md#views).
- **Vocabulary**: the set of types the workflow spec allows for parameters and outputs.
- **Workflow**: the scientific code behind a spec: it builds the stage a run request cuts, and gives accumulators.
  A plain function `(params, inputs) -> outputs` is one.
  Typically a sciline pipeline behind an adapter; the framework does not care.
  See [workflow-contract.md](workflow-contract.md#the-workflow-protocol).
- **Workflow ID**: a hash of a run request's spec, the parameters it does not vary, instrument, and proposal, which names the configured pipeline the stage is cut from, like a `sciline.Pipeline` with parameters set.
  Derived from the request, never stored as a record of its own; a session names held stages by it.
  See [records.md](records.md#requests-and-records).
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).
