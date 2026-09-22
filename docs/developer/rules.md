# Batch and automatic reduction: templates, lookups, and rules

This document covers how requests are made when nobody fills a form: the stored data they are made from, the one operation that makes them, and the loop that calls it as data arrives.
Batch reduction and automatic reduction are the same mechanism, because a rule is to a batch what a template is to a request.
The overview is in [architecture.md](architecture.md#batch-and-automatic-reduction).

## A temperature scan

A sample was measured at two temperatures, and both runs are to be reduced with the instrument's defaults.
The person picks the stored template, keys the two members by temperature, and submits:

```python
template = Template(name='load-defaults', spec=LOAD.id,
                    params={'scale': 2.0}, blanks=('run',))

group = apply(client, template, label='scan',
              pinned={'300K': {'run': dataset_ref(instrument='dream', run=2)},
                     '310K': {'run': dataset_ref(instrument='dream', run=3)}})

reports = {key: client.validate(request) for key, request in group.items()}
records = client.submit_group(group)
batch_table(client, 'scan')
```

`apply` fills the template once per member and returns a group, which is validated and submitted whole, so nothing exists before the submit.
`batch_table` returns what was reduced with which values: one row per member, with the record, its status, the spec, the template, rule, and lookup version, and the lookup entry that applied.
The value columns are the fields that differ per member, which are the template's blanks and every field a member pinned.
Each shows the value the request was made with, whoever supplied it, and the `pinned` column names the fields of the row a person pinned.
A field that holds a model is one column per leaf, `q.start` and `q.num_bins`, which is how a form would lay it out.
A rule's member keys are dataset identities; `dataset_table` is the frame of the datasets and their fields under the same key, and joining it onto the batch table says which sample each member is.
That table is a query over the records, not a stored object.
Automatic reduction is this scan plus a lookup, a selector, and a loop that calls `apply` when a dataset arrives.

## Templates and lookups

**A template is a stored, immutable, versioned partial request.**
It comes from a version-controlled file, such as the instrument defaults, or from a user saving a request with `Template.from_request`, which blanks the data-reference fields and keeps every other field literal.
A template moves to a new version, or to a new spec version, by copy through `Template.revise`, and the records say which version filled them.
Its parameters, like a lookup's fills and the pinned values on a record, are held in the plain JSON form a request's parameters have, whatever objects the author passed, so that stored data compares and displays alike whether it was just made or read back.
A batch rerun under the copy is a new batch whose records link to the old ones.

**A lookup is stored, versioned data beside a template that supplies fills per dataset.**
It is an ordered list of entries, each matching dataset metadata by a value within a tolerance (`Near`), a glob pattern (`Like`), or a run-number range open at either end (`Between`).
At most one entry is the wildcard, which applies to what nothing else matched, and a dataset matching more than one entry is a validation error rather than a choice.
An entry may match only on the fields the dataset source declares for the instrument, an angle, a sample name, or a run's role.
Every ISIS batch interface converged on this table under a different name, and [mantid.md](prior-art/mantid.md) says why it must be data rather than code: the instrument scientist edits it, the UI shows it, and a batch file carries it.

**An as-of fill is resolved against the member, not against the clock.**
A fill is either a literal or an `AsOf`, which holds criteria on dataset metadata and resolves to the nearest dataset before the member that matches them.
This is how a sample gets the last can, dark frame, or empty-beam run measured before it, which FIA does by walking back through the journal by title.
Because the fill is anchored to the member's own dataset, the backlog and a reprocess give a sample the same can the live loop gave it, as `test_backlog_and_reprocess_resolve_the_same_can_as_the_live_loop` checks, and the record holds the reference it resolved to.
A member with no matching dataset before it is refused, visibly, in the trigger status.

**Precedence is one ladder: template, then lookup entry, then the values the submitter pinned.**
A blank at any rung falls through to the next.
The record stores the resolved result, and its `Origin` keeps apart from that result the template version, the rule version, the lookup version and its entry that applied, and the pinned values.
That is what lets a reprocess under a new template or lookup version carry forward what was pinned and fill again what was filled.

## Labels, batches, and slots

**A request may carry a label and a member key, and the latest record under a label and member key supersedes the earlier ones.**
That is one field on the request, one query in the record store, and one eviction rule: the outputs of superseded records drop first.
A rule's records carry the rule's name as their label, stable across its versions, and the dataset as their member key.

**A batch is the records under one label, and nothing else is stored.**
The members are those records, so the table is a query: the latest record per member key, with a rule's exclusions as rows without a record.
The ISIS batch file is a rendering of that table, and cancelling a batch is cancelling the queued and running records under its label.

**The latest record is the one no other record supersedes.**
The backend writes on each record under a label the record that was latest when it accepted the request.
As the single writer it serializes two requests under one label and member key, so the order holds when a rule, a retry, and a person's correction submit from hosts whose clocks disagree.

**A rule's label is reserved.**
The trigger loop reserves it with the backend, which then refuses a request under it that the rule did not fill, so a batch a person happens to name after a rule cannot land in the rule's table.
A run the selector missed is added by calling `apply` on the rule by hand, which sets the rule on the origin and so passes the reservation.
A member a person corrects is applied again with pinned values, which supersedes the rule's record under the same member key.

A **slot** is a label with no member key, owned by one interactive tool, so that hundreds of reruns of one plot are one thing a person sees.
[stages.md](stages.md) describes slots.

## Rules

**A rule is stored, versioned data that makes requests from datasets.**

| Field | What it holds |
|---|---|
| `template` | the partial request to fill, with its version |
| `lookup` | the table beside it, with its version |
| `selector` | metadata criteria that pick the datasets, and a lower `Bound` |
| `retry` | the failure reasons on which a failed record is resubmitted, and a limit |
| `series` | optional: the dataset field that keys a series, and the combine template |
| `exclusions` | member keys the rule must not fire on, each with a reason |
| `active` | whether the rule fires at all |

This rule subtracts from each sample the can measured before it, and is the `as_of_rule` fixture of `batch_test.py`:

```python
Rule(name='subtract',
     template=Template(name='subtract-defaults', spec=SUBTRACT.id,
                       blanks=('sample', 'can'), dataset_field='sample'),
     lookup=Lookup(name='cans', entries=(
         LookupEntry(name='can', fills={'can': AsOf(match={'role': Like(pattern='can')})}),
     )),
     selector=Selector(match={'role': Like(pattern='sample')}))
```

The bound is a run number, or a creation time where the source knows no run number.
`Rule.over` sets it to the newest dataset the source knows at creation, so a rule made mid-beamtime does not reduce the whole beamtime by surprise.

**Exclusions and the active flag are mutable state, everything else changes by copy.**
Those two change over a beamtime, while a change to the template, the lookup, or the selector is a new rule version through `Rule.revise`, and the records say which version made them.
A paused rule fires on nothing, and on resume the datasets that arrived meanwhile are fired on like any other, because the rule's promise is that every matching dataset after its bound is reduced.
A user who wants a gap left alone excludes it or moves the bound.

## The trigger loop

**The trigger loop runs the active rules, and nothing else does.**
It fires a rule on a candidate, a new dataset or a completed record, when all of these hold:

- the rule is active,
- the selector matches the candidate's metadata,
- the candidate lies after the rule's bound,
- the candidate is not in the rule's exclusions,
- no record exists under the rule's label with the candidate as member key, unless the latest one failed for a reason the retry policy names and the records under that member key are fewer than the limit,
- the candidate's SciCat entry does not carry our provenance snapshot.

`trigger_status` answers this for one dataset and returns the reason, so the loop's decision and the status a user reads are one function.
The last clause keeps automatic reduction off its own output: a published output is recognized by the snapshot in its SciCat entry, not by a table of ours.

**The loop keeps no memory.**
Every clause is a query over the records and the sources, so a restart loses nothing and repeats nothing: what arrived while the backend was down is fired on when it returns, and the record under the label prevents a second firing.
A retry is the same query once more, not a second mechanism.
Refusals are kept as a log for a user, never read by the loop itself.

## Backlog, reprocess, and retry

Three deliberate operations call `apply` with a query instead of with a dataset.
Each returns a group that `validate` shows before anything is created, and none of them runs on its own.

- **`backlog`**: the datasets before a new rule's bound that its selector matches, offered when the rule is created.
- **`reprocess`**: the members whose latest record under the rule's label came from an older rule version, offered when the rule moves to a new template or lookup version.
- **`retry`**: the members under a label whose latest record failed or was cancelled, the by-hand form of the trigger loop's retry policy.
  A member with a record in flight is not offered.

**A reprocess is a rebase of the pinned values onto the new template and lookup.**
It keeps what the submitter pinned and fills again what the template and the lookup filled.
The ladder would therefore keep, silently, a Q range a user pinned for one member over the Q range the instrument scientist has since corrected for that member's angle.
`shadowed` is the three-way compare the ladder does not make: per member and field it reports the pinned value, the fill under the old versions, and the fill under the new ones, and the person decides whether the pinned value stands.
A pinned value replaces its field whole, so a Q range pinned to move its lower edge also pins the number of bins; `shadowed` therefore reports per leaf, under the names the batch table uses, and names `q.num_bins` when only that default changed.

**The backend never skips a request because an equal one completed earlier.**
A run that silently did not happen is a decision the user cannot see, and [snakemake.md](prior-art/snakemake.md) records what that cost elsewhere.

## Series

There are two kinds of batching.
Batching for convenience is many independent requests from one template, made by a person or by a rule without a series key.
Batching for merging is an aggregation: one request per member, and a combine request whose collection parameter references the members' contributions.

**A rule with a series key submits a member request and a combine request per arrival.**
Its `Series` field names the dataset field whose value keys datasets into a series, the combine template, the member output that is the contribution, and the collection parameter of the combine spec that takes it.
The combine request references the contribution of every current member of the series, or the previous combine plus what that combine does not cover when chaining is valid, for which the condition is in [aggregation.md](aggregation.md#when-chaining-is-valid).
Nothing on the rule says whether chaining is allowed, because that is a property of the combine's code, which the person writing a rule cannot know.
Successive combines of one series supersede each other under the rule's label, with the series value as their member key, so a UI shows one curve per sample that grows.
A series of k runs therefore costs k-1 combines, and the superseded ones are the first evicted.

**A rule never waits for a series to be complete**, because nobody at the instrument can say when it is: the user decides to measure one more angle, and none of ISIS's interfaces waits either.
A series of fixed roles, a scatter and its transmission, is the same rule with the combine fired only when every role is present.
The rule says whether its combine is published, and by default it is not.

**Series membership is not stored.**
The members are the records under the rule's label, and which series each belongs to is asked of the dataset source when a combine is submitted.
A metadata correction at the instrument therefore moves a run between series and the next combine reflects it, while earlier records are untouched because they hold resolved references.
An exclusion added after a member's record exists drops that member from the next combine the same way.

## The dataset source

**A dataset source yields the datasets of a proposal and persists nothing.**
Each dataset comes as an identity, a PID or an instrument and run number, plus the metadata fields the source declares for the instrument.
Those declared fields are the only ones a lookup entry or a selector may match on, and they are declared here because the source knows what the acquisition writes into the catalogue.
Arrival may be out of order and repeated, and the interface promises no monotonic cursor, which is why the trigger loop asks queries rather than holding a position in a stream.

A dataset enters the record store only as a reference in the requests a rule submits, like any other stand-in.
Which datasets a rule has already decided on is a query over the records, not memory in the source or the loop.
Three implementations exist: SciCat for a facility, a folder for the local application (`FolderSource`), and a fake for tests, and having more than one from the start keeps the tests off SciCat.

## In pandas terms

The batch table is a frame, and the pieces above are how it is built:

| Here | In pandas terms |
|---|---|
| Batch table | a frame: one row per member, the member key as index, parameters as columns |
| Template | column defaults, one row broadcast over the frame |
| Lookup | a join against the dataset metadata on a value within a tolerance, a pattern, or an interval, the wildcard as fallback; two matches are an error, not the nearest |
| As-of fill | `merge_asof` of each member against the datasets matching the criteria, direction backward |
| Precedence ladder | `pinned.combine_first(lookup).combine_first(template)`; a blank is a NaN falling through |
| Pinned values beside resolved values | keeping the source frames next to the result frame, instead of writing the result back into the cells |
| Selector | a boolean mask over the dataset metadata frame, which is `dataset_table` |
| Series key | `groupby(series_key)` |
| Chained combine | a cumulative reduction within the group; the superseded partials are its intermediate values |
| Latest per label and member key | `groupby(member_key).last()` over the records, where last follows the supersedes links, not the clock |

The picture is exact for the view and wrong for the store.
A frame is a stored, mutable table, and Mantid's runs table was one, which is where staleness by reset and the write-back into cells came from.
Here the records are the append-only log and the frame is a query over them.

The client interface speaks the picture anyway: `batch_table` returns a `DataFrame` and `apply` accepts one, with the member key as index and the pinned values as columns, which is the ISIS batch CSV on disk.
pandas stays at the client.
Two words clash: a series here is a groupby group, not a `pandas.Series`, and `apply` here is a merge and a fill, not `DataFrame.apply`.

## Alternatives considered

**A separate autoreduction service with its own state.**
It would keep its own definitions, status table, and notion of what it has already done, beside the batch machinery a person uses.
Mantid's reflectometry interface shows the two are one thing: a batch tab is this rule, with settings, a lookup table, an autoprocessing search, exclusions, and one runs table, and autoprocessing is the mode of that batch which appends rows.

**A stored batch object, a slot object, and a rule status table.**
Each would be a second copy of what the records already say, able to go stale against them.
One label field with an optional member key serves all three, so one query lists, supersedes, cancels, and evicts for all three.

**The rule and the record in one row.**
This is how ISIS autoreduction did it, and correcting the rule rewrote history.
The template, the lookup, and the rule are what a person edits, and the records are what happened.

**A cursor, or a table of the datasets the loop has seen.**
The loop would hold state that a restart can lose or double-count, and that would have to agree with the records anyway.
Every condition is already a query over the records, so the table adds a second truth and no information.

**Ordering the records under a label by creation time.**
Clocks on different hosts disagree, so a person's correction could lose to a retry stamped a second earlier.
Each record instead links to the record it supersedes.

**A fill that names whatever dataset is newest at submission.**
This is simpler than an as-of fill and right for the live loop only.
Every backlogged sample would get the latest can rather than the can measured before it, so the backlog and a reprocess would disagree with the live loop.

**Skipping a request when an equal one already completed.**
An up-to-date check of this kind, as build systems and Snakemake make it, hides a decision from the user, because the run that did not happen is invisible.
Requests are therefore always made, and a superseding record shows the repeat.

**Waiting for a series to be complete before combining.**
Completeness is not knowable at the instrument, since the user decides to measure one more angle.
Each arrival therefore combines what exists.

**Fan-out in the scheduler.**
Splitting a completed output into one request per key, with the keys known only after reading the data, could be a scheduler feature.
Snakemake put it in the scheduler as checkpoints and it became the most confusing part of the tool.
No current workflow needs it, and if one arises it is a rule on the completed producer, one template instantiation per key.

## Costs

- Reprocessing after a template change is a client operation over a query, not a stored diff.
- A rule's bound is one more thing to get right at creation.
  Its default, the newest dataset the source knows, means a rule made mid-beamtime reduces the backlog only when asked.
- An as-of fill has nothing to resolve to until the first can of a beamtime is measured, so the samples before it are refused, visibly, until a person fills them by hand.
- The acquisition must write the fields a lookup or a selector matches on into the catalogue, which is a requirement on the instrument to be stated to the instrument teams early.
- A series a person defines by hand, "these runs, and keep combining as more arrive", has no place here.
  It would be a rule with members a person lists instead of a selector, and it is left out until someone asks for it.
