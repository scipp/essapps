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

`apply` fills the template once per member and returns a `Group`, which is validated and submitted whole, so nothing exists before the submit.
Each member is a run request that holds every filled value in `params`.
The group carries the template's blanks, here `run`, as `group.vary`, and `submit_group` passes them on as what the members vary.
The two members above agree on everything but their blanks, so in a session they share one held stage.
`batch_table` returns what was reduced with which values: one row per member, with the record, its status, the spec, the template, rule, and lookup version, and the lookup entries that applied.
The value columns are the fields that differ per member, which are the template's blanks and every field a member pinned.
Each shows the value the request was made with, whoever supplied it, and the `pinned` column names the fields of the row a person pinned.
A field that holds a model is one column per leaf, `q.start` and `q.num_bins`, which is how a form would lay it out.
A rule's member keys are dataset identities; `dataset_table` is the frame of the datasets and their fields under the same key, and joining it onto the batch table says which sample each member is.
That table is a query over the records, not a stored object.
Automatic reduction is this scan plus a lookup, a selector, and a loop that calls `apply` when a dataset arrives.

## Templates and lookups

**A template is a partial request: the spec, the values set, the blanks each use fills, and the outputs to compute.**
Its blanks are the inputs of a stage, so the requests made from one template name one stage, and the stage behind a notebook slider is a template too ([stages.md](stages.md#who-names-the-stage)).
The batch form and the slider therefore use one concept.
The template of a batch or a rule is stored, immutable, and versioned.
Records made from a template carry its name as their label unless the caller gives another, and a rule's records carry the rule's name.
A template comes from a version-controlled file, such as the instrument defaults, or from a user saving a request with `Template.from_request`, which blanks the data-reference fields and the fields the caller names, and keeps every other field literal, the tuned ones included.
A template moves to a new version, or to a new spec version, by copy through `Template.revise`, and the records say which version filled them.
Its parameters, like a lookup's fills and the pinned values on a record, are held in the plain JSON form a request's parameters have, whatever objects the author passed, so that stored data compares and displays alike whether it was just made or read back.
A batch rerun under the copy is a new batch whose records link to the old ones.

**A lookup is stored, versioned data beside a template that supplies fills per dataset.**
It is an ordered list of entries, each matching dataset fields by a value within a tolerance (`Near`), a glob pattern (`Like`), or a run-number range open at either end (`Between`).
At most one entry is the wildcard, which applies to what nothing else matched, and a dataset matching more than one entry is a validation error rather than a choice.
An entry may match only on the fields the instrument's field extractor derives, an angle, a sample name, or a run's role ([The dataset source](#the-dataset-source)).
Every ISIS batch interface converged on this table under a different name, and [mantid.md](prior-art/mantid.md) says why it must be data rather than code: the instrument scientist edits it, the UI shows it, and a batch file carries it.

**A nearest fill is resolved against the member, not against the clock.**
A fill is either a literal or a `Nearest`, which resolves to the dataset nearest the member that matches its criteria:

```python
Nearest(match={'role': Like(pattern='can')})                        # the last can before
Nearest(match={'role': Like(pattern='can')}, direction='after')     # the first can after
Nearest(match={'role': Like(pattern='can')}, direction='either',    # the nearer of the two,
        same=('sample_holder',))                                    # in the same holder
```

It picks a can, dark frame, or empty-beam run for a sample, however the instrument is operated.
`direction='before'`, the default, is what FIA does by walking back through the journal by title.
`same` names fields whose value the match shares with the member.
"Nearest" is in the order of the instrument's datasets ([The dataset source](#the-dataset-source)), and `'either'` compares distances in that order, a tie going to the match before.
A fixed run, or one run for the whole proposal, is a literal fill, and a run a person picks is a pinned value.
Because the fill is anchored to the member's own dataset, the backlog and a reprocess give a sample the same can the live loop gave it, as `test_backlog_and_reprocess_resolve_the_same_can_as_the_live_loop` checks, and the record holds the reference it resolved to.
A member with no match before it is refused, visibly.
A member with no match after it *waits*: `trigger_status` says so, and a later pass fires on it once the match is measured.
`'either'` waits for the match after as well, so the answer depends only on the datasets that exist, not on when it is asked.

**Precedence is one ladder: template, then lookup entry, then the values the submitter pinned.**
A blank at any rung falls through to the next.
Every filled value goes into the member's `params`, and the members vary the template's blanks.
A lookup entry that fills a value per dataset, such as a Q range per angle, or a value a person pinned for one member, therefore gives that member a held stage of its own.
The record stores the resolved result, and its `Origin` keeps apart from that result the template version, the rule version, the lookup version and the entry that applied to each dataset, and the pinned values.
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

**A rule is stored, versioned data that makes requests from datasets, or from the completed records of another rule.**

| Field | What it holds |
|---|---|
| `template` | the partial request to fill, with its version |
| `lookup` | the table beside it, with its version |
| `selector` | field criteria that pick the candidates, and a lower `Bound` |
| `retry` | the failure reasons on which a failed record is resubmitted, and a limit |
| `series` | optional: the dataset field that keys a series, and when the series fires |
| `follows` | optional: the label of another rule whose completed records are the candidates, instead of datasets |
| `exclusions` | dataset identities the rule must not fire on or list in a series, each with a reason |
| `active` | whether the rule fires at all |

This rule subtracts from each sample the can measured before it, and is `subtract_rule()` of `batch_test.py`:

```python
Rule(name='subtract',
     template=Template(name='subtract-defaults', spec=SUBTRACT.id,
                       blanks=('sample', 'can'), dataset_field='sample'),
     lookup=Lookup(name='cans', entries=(
         LookupEntry(name='can', fills={'can': Nearest(match={'role': Like(pattern='can')})}),
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
It fires a rule on a candidate, a new dataset or a completed record of the rule it follows, when all of these hold:

- the rule is active,
- the selector matches the candidate's fields,
- the candidate lies after the rule's bound,
- the candidate is not in the rule's exclusions,
- no record exists under the rule's label with the candidate as member key, unless the latest one failed for a reason the retry policy names and the records under that member key are fewer than the limit,
- the candidate's SciCat entry does not carry our provenance snapshot.

For a series, and for a completed record, a record counts only if it references the candidate.
`trigger_status` answers this for one candidate and returns the reason, so the loop's decision and the status a user reads are one function.
Where the clauses hold but the request cannot be made yet, because a nearest fill looks after the member or a series fires once complete, the status is *waiting*, and the loop lists it in `TriggerLoop.waiting`.
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
Batching for convenience is many independent requests from one template, made by a person or by a rule without a series.
Batching for merging is one request over a list of runs ([aggregation.md](aggregation.md)).

**A rule with a series submits one request, whose dataset field lists every current run of the series, on each arrival or once the series is complete.**

```python
rule = Rule(
    name='sans-series',
    template=Template(name='sans-defaults', spec=NORMALIZE.id,
                      params={'floor': 1.5}, blanks=('runs',)),
    selector=Selector(match={'role': Like(pattern='sample')}),
    series=Series(key='sample'),
)
```

`Series(key, fire)` names the dataset field whose value keys datasets into a series, and when the rule fires.
The current runs of a series are the datasets with the same value of the key that the selector matches and that are not excluded, in the order the sources list them.
The series value is the member key, so successive requests of one series supersede each other under the rule's label, and the batch table has one row per sample, whose curve grows.
A run that arrives again, or out of order, is listed once.
The bound does not apply to the runs of a series, so a series that began before the bound is one series.
A series of k runs therefore costs k requests, the k-th of which reduces k runs, and the superseded ones are the first evicted.

**Each run of a series is filled from its own lookup entry.**
When the template's dataset field is a column of a list of rows, each run is one row, and a fill named for another column goes into that run's row:

```python
template = Template(spec=FLOORED.id, params={'scale': 2.0}, blanks=('runs',),
                    dataset_field='runs.run')
lookup = Lookup(name='floors', entries=(
    LookupEntry(name='noisy', match={'mode': Like(pattern='noisy')},
                fills={'runs.floor': 3.0}),     # the floor of this run only
))
```

A fill of any other field goes into the request, which has one value per field.
If two runs of one series fill such a field differently, `apply` refuses the series and names the runs.
Before that, `apply` checks each fill name of the lookup against the template's spec.
A name must be a parameter, or a column of the rows the dataset is one of, and must not be the dataset field itself.
A row ignores a name it does not declare, so a misspelt column would otherwise leave its default in every row and in the record.
`Origin.entries` records the entry each run matched.

**A run that cannot be read fails the whole series request, visibly.**
The operator excludes the run, and `retry` submits the series without it.

**A series fires on each arrival, or once complete.**

```python
Series(key='sample')                                                 # fire='each'
Series(key='sample', fire=Complete(count=4))                         # once 4 runs exist
Series(key='sample', fire=Complete(roles=('scatter', 'transmission')))  # once each role is there
```

`fire='each'`, the default, reduces what exists on every arrival, which fits a series whose length the user decides while measuring.
`Complete` waits until the series has `count` runs, or a run of each of `roles`, the values of the runs' `role` field, and then fires on each arrival like `'each'`.
Until then the series waits, visibly in `trigger_status`.
Completion is a question of the datasets that exist, so the loop stays without memory.
Firing after a quiet period needs a clock the loop does not have, and is left out.
The rule says whether the results of a series are published, and by default they are not.

**Series membership is not stored.**
Which runs belong to a series is asked of the dataset source when a series request is made.
A metadata correction at the instrument therefore moves a run between series and the next request reflects it, while earlier records are untouched because they hold resolved references.
An exclusion drops a run from the next request the same way.

What the workflow does with the list is its own business, so a sum and a stitch look the same to the rule ([aggregation.md](aggregation.md#combinations-that-are-not-accumulations)).

## Rules over completed records

**A rule that follows another fires on its completed records instead of on datasets.**
`Follows(label)` makes the candidates the latest completed record per member key under the other rule's label.
A candidate has the fields of the dataset its member key names, so selectors, lookups, and series keys work as on datasets.
It fills the template's dataset field with a row of references to every output of the record, by output name.
This splits a sum into one request per run and one combine over the records ([aggregation.md](aggregation.md#reducing-the-runs-of-a-sum-on-separate-nodes)):

```python
contribute = Rule(name='contribute',
                  template=Template(spec=CONTRIBUTE.id, params={'floor': 1.5}, blanks=('run',)))
combine = Rule(name='combine',
               template=Template(spec=COMBINE.id, params={'scale': 2.0}, blanks=('parts',)),
               follows=Follows(label='contribute'),
               series=Series(key='sample'))
TriggerLoop(client, contribute, combine)
```

`COMBINE`'s `parts` is a list of rows whose model is `CONTRIBUTE`'s output model, so each completed contribution is one row.
The combine is fired again as the sample gets more runs, and its latest record equals the one request over the list, as `test_a_combine_follows_the_contributions_of_a_series_as_it_grows` checks.
A contribution that failed is not a candidate: the combine leaves it out, and the failure shows in the contribute rule's batch table.

**`Follows(label, output=...)` is the second phase of a fan-out.**
Each candidate fills a reference to that output, and a collection output gives one candidate per key, so the second rule makes one request per key the first phase found:

```python
first = Rule(name='first', template=Template(spec=SUM.id, blanks=('runs',)),
             series=Series(key='sample', fire=Complete(count=2)))
second = Rule(name='second', template=Template(spec=EXPORT.id, blanks=('data',)),
              follows=Follows(label='first', output='per_run'))    # member keys sio2[0], sio2[1]
```

A candidate's member key is the record's member key, with `[key]` for an element.
A record counts as fired on only when a record under the following rule's label references it, so a new record under the same member key, a correction or a grown series, is fired on again.
That keeps the loop without memory: the state is the records.

## The dataset source

**A dataset source yields the datasets of a proposal and persists nothing.**
Each dataset comes as an identity, a PID or an instrument and run number, plus the fields the instrument's field extractor derives.
Arrival may be out of order and repeated, and the interface promises no monotonic cursor, which is why the trigger loop asks queries rather than holding a position in a stream.

**A field extractor is instrument code; rules stay data.**
Where a run's role, sample, or angle is written differs between instruments: a title prefix, a NeXus field, a variable of the acquisition script.
The instrument team writes a function from what the source has, the catalogue entry and the file, to the fields, and an installed package registers it for the instrument, as it registers specs:

```python
def loki_fields(entry: Mapping[str, Any], path: Path) -> Mapping[str, Any]:
    role, _, sample = entry['title'].partition(': ')
    return {'role': role, 'sample': sample, 'start': entry['start_time']}

LOKI_FIELDS = FieldExtractor(loki_fields, order='start')
# pyproject.toml: [project.entry-points."ess.apps.fields"] loki = "ess.loki.apps:LOKI_FIELDS"
```

The source applies it; lookups, selectors, series keys, and completion criteria use the fields it returns, and fills and firing stay data.
Nothing is required of acquisition.
An instrument without one keeps the catalogue entry's own fields.
`order` names the field that orders the instrument's datasets, typically the start time, which is what "before" and "after" mean to a nearest fill; without it, datasets are ordered by run number.
A role that is a frame range inside one file is not a dataset field: splitting the file is the workflow's.

A dataset enters the record store only as a reference in the requests a rule submits, like any other stand-in.
Which datasets a rule has already decided on is a query over the records, not memory in the source or the loop.
Three implementations exist: SciCat for a facility, a folder for the local application (`FolderSource`), and a fake for tests, and having more than one from the start keeps the tests off SciCat.

## In pandas terms

The batch table is a frame, and the pieces above are how it is built:

| Here | In pandas terms |
|---|---|
| Batch table | a frame: one row per member, the member key as index, parameters as columns |
| Template | column defaults, one row broadcast over the frame |
| Lookup | a join against the dataset fields on a value within a tolerance, a pattern, or an interval, the wildcard as fallback; two matches are an error, not the nearest |
| Nearest fill | `merge_asof` of each member against the datasets matching the criteria, `by=same`, direction backward, forward, or nearest |
| Precedence ladder | `pinned.combine_first(lookup).combine_first(template)`; a blank is a NaN falling through |
| Pinned values beside resolved values | keeping the source frames next to the result frame, instead of writing the result back into the cells |
| Selector | a boolean mask over the dataset fields frame, which is `dataset_table` |
| Series key | `groupby(series_key)` |
| Request of a series | a reduction over the group so far, done again on each arrival; the superseded requests are its earlier values |
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
This is simpler than a nearest fill and right for the live loop only.
Every backlogged sample would get the can that is newest when the backlog runs, so the backlog and a reprocess would disagree with the live loop.

**Skipping a request when an equal one already completed.**
An up-to-date check of this kind, as build systems and Snakemake make it, hides a decision from the user, because the run that did not happen is invisible.
Requests are therefore always made, and a superseding record shows the repeat.

**Waiting for a series to be complete as the only behaviour.**
A series whose length the user decides while measuring, such as one more angle, would have no result before its last run.

**Member and finalize requests per arrival as the default for a series.**
Each arrival submits a request for the new run, whose outputs are the values that add, and a finalize request that accumulates the outputs of every current member.
The k-th finalize reads k stored contributions and not k runs.
Two rules, one following the other, build exactly this ([Rules over completed records](#rules-over-completed-records)), but as the default it costs too much.
Nothing checks that the members fit, which needs the graph, and a member that failed is left out of the finalize, visible only in the other rule's table.
One request over the list has neither problem.

**A combine template on the series.**
The series names a second template, for a combine spec over a list of references to the members' contributions, with the output that is the contribution and the parameter that takes it.
The rule then holds two templates that share most of their values and can disagree.
Two rules say the same with one template each.

**Fan-out in the scheduler.**
Splitting a completed output into one request per key, with the keys known only after reading the data, could be a scheduler feature.
Snakemake put it in the scheduler as checkpoints and it became the most confusing part of the tool.
Fan-out whose keys come from the data takes two phases instead: a first run whose output holds the keys, then one request per key, made by hand or by a rule that follows the first.

## Costs

- Reprocessing after a template change is a client operation over a query, not a stored diff.
- A rule's bound is one more thing to get right at creation.
  Its default, the newest dataset the source knows, means a rule made mid-beamtime reduces the backlog only when asked.
- A nearest fill looking before the member has nothing to resolve to until the first can of a beamtime is measured, so the samples before it are refused, visibly, until a person fills them by hand.
  One looking after the member, or either way, waits, and the samples after the last can wait until a person pins one.
- A series request over k runs reduces all k runs in a throwaway process ([aggregation.md](aggregation.md#a-series-under-a-rule)).
- One run that cannot be read fails its series request until someone excludes it.
- A series a person defines by hand, "these runs, and keep accumulating as more arrive", has no place here.
  It would be a rule with members a person lists instead of a selector, and it is left out until someone asks for it.
