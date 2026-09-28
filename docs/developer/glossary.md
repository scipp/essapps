# Glossary

The terms of the design, in alphabetical order, each with a pointer to the document that develops it.
[architecture.md](architecture.md) is the overview these terms come from.
Where esslivedata uses a word differently, the clash is noted, because the two projects are discussed together often enough for it to matter.

- **Accumulation key**: sciline's name for a value at which the members of an aggregation are accumulated, such as a numerator.
  It need not be exposed in the spec, because it never leaves the run.
- **Accumulator**: sciline's object that takes values by `push` and holds their accumulation as its value.
  An aggregation makes one per accumulation key, and `Buffered` makes one from a function.
  A stage over a list of runs holds its accumulators between calls.
- **Adapter**: the code, in ess.reduce, that turns a sciline pipeline into a workflow, and each run into a call of a `sciline.Stage`.
  It maps fields to keys, names the form of each data reference, and gives for each list parameter the package's function that builds its `sciline.Aggregation`.
  See [workflow-contract.md](workflow-contract.md#the-sciline-adapter).
- **Aggregation**: in the framework, one run request whose list parameter holds the members, such as the runs of a sum; the binding contributes each member through the package's aggregation and accumulates.
  In sciline, the object that composes a contribute stage, accumulators, and a finalize stage in one process, over the rows of a member table.
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
- **Blank**: a parameter a template leaves for each use to fill, and varies from use to use.
  The blanks of a template are the inputs of its stage.
  See [stages.md](stages.md#who-names-the-stage).
- **Client interface**: the backend's Python interface, including validate, apply, views, and the picker.
  The API.
  See [operations.md](operations.md#the-client-interface).
- **Collection**: a list or dict of values of one declared type, usable as a parameter and as an output.
  A reference may name one element of a collection output by key.
- **Contribute stage**: the stage of an aggregation from one member to the values that add, built by the adapter inside one run.
- **Data reference**: a field type: a parameter or output declared to hold a reference to a file or an array rather than a literal.
  Easy to confuse with *reference*, which is the value such a field holds.
- **Data store**: where the bytes of large outputs live: a registry of disk copies and a disk tier, owned by the backend.
  Each process that holds data also has a private memory cache, which the store serves from but never registers.
  See [records.md](records.md#the-data-store).
- **Dataset**: data the framework did not compute: a SciCat dataset or a file on a user's disk.
  Identified by its PID, else the UUID its NeXus file carries, else the sha256 of its bytes; its run number is a field, not its identity.
  Belongs to one or more proposals.
  The second form of reference.
  Not a record: no request, no status, nothing to recompute.
- **Dataset source**: where datasets are discovered and listed, with the fields the instrument's field extractor derives.
  Persists nothing.
  SciCat, a folder of one proposal in local mode, a fake for tests.
- **Field extractor**: an instrument's code that derives a dataset's fields, role, sample, angle, start time, from its catalogue entry and file, and names the field that orders the instrument's datasets.
  Registered per instrument under the entry-point group `ess.apps.fields`.
  See [rules.md](rules.md#the-dataset-source).
- **Finalize stage**: the stage of a sciline aggregation from the accumulated values to the outputs.
  The adapter does not use it: it builds its own final stage, which also reads the varied parameters.
- **Group**: several requests submitted atomically that may reference each other's outputs before those exist.
  `apply` returns a `Group`, which also carries the template's blanks as what its members vary.
  See [records.md](records.md#scheduling-pending-outputs-as-inputs).
- **Input**: a parameter of data-reference type.
  Not the same as a *stage input*, which may also be a literal parameter.
  In esslivedata, inputs are data streams and genuinely differ from parameters; here they do not.
- **Intermediate**: an output the spec exposes that a plain run does not compute, a value inside the pipeline such as a detector image.
  A request computes it when it names it as an output; over a sum it is the accumulated value.
  See [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).
- **Label**: a field on a request, with an optional member key.
  Each record under a label names the record it superseded, and the latest is the one nothing supersedes.
  A rule's name for the records it makes, a name a submitter picks for a batch, and a slot for an interactive tool are all labels.
  See [rules.md](rules.md#labels-batches-and-slots).
- **Launcher**: decides where a run executes and starts it there: session, subprocess, or cluster.
- **Local mode**: client, backend, launcher, session, and data store in one Python process.
  **Shared mode**: the backend as a service used by many people.
  The **shared service** is that backend's process, which also holds a memory cache.
- **Lookup**: stored, versioned data beside a template: ordered entries that match dataset fields and supply template fills, literal or **nearest**, the dataset nearest the member, before, after, or either, that matches criteria, with at most one wildcard.
  What ISIS calls a lookup table, a cycle mapping, or a per-row user file.
  See [rules.md](rules.md).
- **Member**: one record under a label, identified by its **member key**: a name a person chose for a batch made by hand, the dataset identity for a rule's batch.
  The members of a sum are the elements of its list parameter, one row each of sciline's **member table**: a value of the one member key, or a **row**, a model whose fields are the member keys.
- **Origin**: the field on a run request that says where its values came from: the template version, the rule version when a rule filled it, the lookup version and the entry that applied to each dataset, and the values pinned beyond template and lookup.
  Explanation, not provenance, which is the data the run read.
- **Pending output**: an output of a record that has not completed yet, usable as input to another request.
- **Picker**: the client query behind an input field: candidates of matching format from the record store and from every dataset source, as rows of one shape.
- **Pinned values**: the values of a request set for one member beyond the template and the lookup, the top rung of the precedence ladder.
  The origin keeps them apart from the request's parameters, so a reprocess under a new template or lookup version fills the rest again and leaves them alone.
  See [rules.md](rules.md#templates-and-lookups).
- **Plain run**: a run that varies nothing, computing the spec's results, as `client.run(spec, params)` submits it.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
  A record has one owning proposal and may reference datasets and records of any proposal its submitter may read.
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
- **Rule**: stored, versioned data that makes requests from datasets, or from the completed records of a rule it follows: a selector with a lower bound, a template and a lookup, a retry policy, exclusions, an active state, and optionally a series.
  A rule is to a batch what a template is to a request, and its label is reserved for it.
  See [rules.md](rules.md).
- **Run record**: a run request plus what happened to it: status, outputs, versions.
  Its execution is called a run, never a job, except for the launcher's own job IDs.
  In esslivedata a job is a running streaming workflow.
  See [records.md](records.md#requests-and-records).
- **Run request**: one run of a spec: the spec, every parameter value in `params`, and the outputs to compute.
  Everything needed to run it.
  The backend fills the spec's defaults into `params` at submit, for every parameter not given, and records every value in the form the params model gives it.
  A call of a stage is one run request, made by filling the blanks of a template.
  See [records.md](records.md#requests-and-records).
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **SciCat**: the facility's data catalogue.
  **PID**: SciCat's persistent identifier for a dataset.
- **Series**: the datasets a rule keys together by a field value.
  `Series(key, fire)` on a rule submits one request whose dataset field lists every current run of the series, on each arrival or once the series is `Complete`.
  See [rules.md](rules.md#series).
- **Session**: a runner plus a private memory cache, belonging to one client, keeping the outputs of its run records, and the stages it built for them, in memory.
  A cache over records.
  See [records.md](records.md#where-runs-execute-and-where-data-lives).
- **Slot**: a label an interactive tool owns, with no member key.
  The unit of interactive work, and what tools list and replay.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs, and which outputs are intermediates.
  The signature of a pipeline: every parameter and every value a caller may ask for.
  Defined in scipp/ess#690 with the extensions in [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).
- **Stage**: the part of a pipeline from named stage inputs to named outputs, cut from a spec with parameters set, like `sciline.Stage`.
  It may hold whatever its inputs cannot affect.
  The caller names it as a template; the workflow builds it; the session holds it under the name its run requests and their varied names give it.
  See [stages.md](stages.md).
- **Stage inputs**: the values a stage takes on each call: the parameters a request varies.
  A template names them as its blanks, whether a client cut it for a slider or a rule holds it.
  See [stages.md](stages.md#who-names-the-stage).
- **Stand-in**: what a person may type in place of a dataset's identity, a run number `run:<instrument>/<n>` or a path `path:<path>`.
  Resolved at submission to the identity of the one dataset it names, which is what the record's parameters hold.
- **Template**: a partial request: a spec, the values set, the blanks each use fills, and the outputs to compute.
  The requests made from one template name one stage, so a template is both the stage a client names for a slider and what a batch or a rule fills.
  Records made from a template carry its name as their label.
  The templates of batches and rules are stored and versioned.
  `cut` derives another template over the same spec and values.
  A rule is to a batch what a template is to a request.
  See [rules.md](rules.md#templates-and-lookups) and [stages.md](stages.md#who-names-the-stage).
- **Throwaway process**: a subprocess or cluster job that runs one request and exits.
  The execution shape of shared mode.
- **Trigger loop**: applies the active rules to new datasets and completed records.
  Keeps no state: every decision is a query over the records and the datasets.
  A member that cannot be made yet, because a fill or a series completion depends on a dataset still to come, is **waiting**.
  See [rules.md](rules.md#the-trigger-loop).
- **Vary**: the names of the parameters a caller varies from run to run, given with the submission beside the requests, each with its value in `params`.
  They are the blanks of the request's template.
  A hint for the session: it does not change the result, and the record does not hold it.
  See [stages.md](stages.md#who-names-the-stage).
- **View**: a small piece of an output's data for display, computed by the process holding a copy.
  Not a run.
  See [stages.md](stages.md#views).
- **Vocabulary**: the set of types the workflow spec allows for parameters and outputs.
- **Workflow**: the scientific code behind a spec: it builds the stages a session holds, and knows the graph.
  A plain function `(params, inputs) -> outputs` is one.
  Typically a sciline pipeline behind an adapter; the framework does not care.
  See [workflow-contract.md](workflow-contract.md#the-workflow-protocol).
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).
