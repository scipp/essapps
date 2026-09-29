# Questions from the story findings

Each question refers to a finding at the end of [user-stories.md](../user-stories.md) and gives a recommendation.
Comment under each `> Simon:` line; an empty line means "go with the recommendation".
A "Follow-up" is my reading of your comment, for you to confirm.

## Part 1: decisions

### Q1. The record an accumulator makes (finding 26, D7)

Computing an accumulator after each of 1000 arrivals makes 1000 records that together name 500,500 elements.

- (a) Keep it: each record is the accumulator spec over every element pushed so far. It equals the plain request over the same elements, and removing an element is a request over fewer. Storing a growing list without repeating it is left to the system.
- (b) Each record is the accumulator spec over the previous record's output and the elements pushed since. It is still a plain request, and provenance still reaches every element, through a chain of records. It no longer equals the flat request, and removing an element needs a flat request anyway.

Recommendation: (a). A record that says "the sum of these 1000 angles" is worth more than the saved references, and the driver decides how often it reads.

> Simon: I do worry about quadritic growth here. We should ask what we need the records for. Should it only be kept if the output is referenced, e.g., if FINALIZE also ran 1000 times? In any case, this feels like an implementation problem, we should avoid making "the system always keeps record X in case Y" a *requirement*. 

Follow-up: the API keeps only the meaning: the record of an accumulator is the record of the plain request over its elements. Which records are kept, for how long, and how they are stored is the system's, and system story D7 gets the property "storage does not grow quadratically with the number of reads".
This also removes the implied guarantee that every record is kept. Proposed wording for core-api.md: "Records are working state for running experiments and are kept for the medium term; long-term provenance is what `publish` puts in the catalogue." Is that the right promise, or should the API promise nothing about lifetime at all?

> Simon: I think this is a key question we should think about more -- is there any good reason for keeping records *outside* sessions? What purpose does the store actually serve? If not within a session, would it be reasonable to assume that we can just as well reference an output file produced by a run if SPEC_A in SPEC_B? But I don't want to open Pandora's box here if you think this is a distraction.

Answer: I think records must outlive sessions, and the reason is that most work runs without one.
A session exists only to hold stages and accumulators. A batch, an automatic reduction, and a plain `compute` run without a session, and the stories need their results later, by someone else, or by a program:

- C1: a beam centre found today is an input of tomorrow's batch.
- D2, D5: the next day, a user finds which members of a batch failed, and why. A failure produces no output file, so only a record says it happened.
- E1 to E4: the trigger loop has no session, and must know what it already reduced.
- G4, B6: another notebook, or the same user a week later, reads a result by its label.
- F1: publishing happens after the work, and the entry needs the chain of requests that made the output.

Your alternative, referencing an output file made by SPEC_A in SPEC_B, is what a reference already is: `centre.ref('centre')` names an output. The question is only what the reference points at.
A path points at bytes that may move, be overwritten, or be deleted, and says nothing about how they were made.
A record says how they were made, and it also exists for pending and failed requests, which have no file.

The store could still be very plain: output files, each with the request that made it written next to it, plus a small index for labels, pending requests, and failures.
That is a system decision, and the API does not change with it. So I do not think it opens Pandora's box, as long as core-api.md promises only the medium-term lifetime and leaves the store's form to the system.

> Simon:

### Q2. Provenance through a published output (finding 28, C2)

A record that reads a published output names a dataset, so its provenance stops there.

- (a) Provenance continues through the published entry, which carries its own provenance anyway (F1).
- (b) Provenance stops, and the guarantee becomes "reaches raw datasets or published entries".

Recommendation: (a).

> Simon: If we reference a published dataset, I would argue that provenance then belongs to SciCat (or the dataset source in general). We should not pull provenance data from SciCat and rebuild the full provenance graph.

Follow-up: provenance stops at every dataset, raw or published; what lies behind a dataset belongs to its source. The guarantee becomes "the provenance of a record reaches every dataset it read, through all its inputs". C2 keeps its assertion that provenance ends at the published dataset.

### Q3. Records of a dropped proposal that others reference (finding 29, G1, H3)

When the commissioning proposal is dropped, a p2 record that read a commissioning record loses a step of provenance.

- (a) A dropped proposal's records stay while another proposal's records reference them; their outputs may go.
- (b) A proposal cannot be dropped while referenced.
- (c) Accept the break.

Recommendation: (a). Records are small, and provenance is the point of keeping them.

> Simon: Not sure what "dropped proposal" means. Our records should be considered to have a "medium" lifetime -- enough to support running experiments, not throw-away afterwards -> long-term provenance must live in SciCat.

Follow-up: "dropped proposal" was the old H3's term for deleting a proposal's records and outputs when the proposal ends. With records as medium-term working state, H3 becomes a system story about records expiring, and G1's cross-proposal reference simply loses its provenance step when the commissioning records expire; the published entry carries what lasts.

### Q4. A session whose client dies (finding 24, B5, H2)

A dying kernel never leaves `with client.session()`.

Recommendation: the backend ends a session whose client is gone (for example after a missed heartbeat), and a call through a holder of an ended session raises a defined error that says to open a new session. The guarantee in core-api.md gets this second clause.

> Simon: Yes, cleanup will be needed, but seems like implementation "detail".

Follow-up: it leaves the API. The guarantee in core-api.md stays "ending their session releases them"; system story B5 already asks for the cleanup.

### Q5. Where a session runs (finding 25, G3)

A desktop application wants its session on the laptop and its reductions on the cluster.

- (a) `client.session()` takes a placement, for example `client.session(where='local')`. The session's process runs on the laptop but belongs to the cluster's backend, and records go to the cluster's store.
- (b) The application runs its own backend, whose records reference records in the cluster's backend.

Recommendation: (a). (b) needs references across backends, which nothing else needs.

> Simon: agree with (a)

Follow-up: core-api.md gets a placement argument on `client.session()`; its name and values are open.

### Q6. Published outputs listed as datasets (finding 13, E3)

A published result must stay usable as an input (C2) without triggering rules over raw data (E3).

Recommendation: a dataset shows whether it is raw, and a selector matches only raw datasets unless it asks for others.

> Simon: agree, we would use different selectors for raw vs derived data (or masks, or calibration files, ...)

### Q7. Labels and members (findings 14, 15)

Recommendation:
- A record shows its `label` and `member`. They are not part of the request, since they do not change the result.
- `apply` and `Rule` take `member_field=`, the dataset field whose value becomes the member.
- `client.members(label)` returns `{member: latest record}`.

> Simon: no opinion

Follow-up: I will adopt the recommendation, marked as tentative in core-api.md.

### Q8. Records do not name their template (finding 19, E4)

E4's goal says every record names the template version it came from. A record holds the request, not the template.

Recommendation: drop that part of the goal. A template stays unnamed plain data, and the request says everything that determines the result.

> Simon: agree

### Q9. List-valued outputs (finding 7, C3, C4)

`ess.reduce.spec` already allows `list[...]` and `dict[str, ...]` data fields, and `OutputRef.key` names one element.

Recommendation: a reference to a whole list output fills a list parameter when the elements agree; `ref('scaled', key)` names one element. Drop C3, which checks nothing beyond S1 with fixed per-bank outputs, or rewrite it with a varying number of banks.

> Simon: agree, I think list-valued outputs are unlikely in practice, or we might want to avoid them, do not complicate the design for this "feature"

Follow-up: no list-valued output semantics in the design. C3 is dropped, and C4 is rewritten so that STITCH outputs only the stitched curve and the export reads that.

### Q10. A series under a rule (finding 21, E1)

Story E1: during a beamtime, a reflectometry sample is measured at several angles, one run per angle, arriving over hours.
After each arrival the user wants the stitched curve over all angles of that sample so far: one curve per sample, updated as angles arrive.
A stitch fits scale factors over all angles at once, so it is one request over a list of runs, `STITCH(runs=[...])`, recomputed each time; it is not an accumulation.

A rule as core-api.md describes it makes one request per arriving dataset, with the dataset filling one blank.
E1 needs a rule that, on each arrival, collects every dataset with the same value of a field (here `sample`) and fills them, in run order, into the template's list blank:

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
# run 4 of sample 'si' arrives -> STITCH(runs=[r2, r3, r4]) under label 'reflectivity', member 'si'
```

Recommendation: `series=` as above. It belongs to the sub-design for batch and automatic reduction (Part 3).

> Simon:

### Q11. The trigger loop (finding 22, E1 to E4)

Recommendation:
- `loop.step()` handles what arrived since the last step and returns the records it submitted; `run()` repeats it.
- `loop.status(rule)` gives the reason a rule submitted nothing.
- The loop checks a rule's template when it is given the rule.
- The loop reads which datasets it has handled from the records under the rule's label, so that a restarted or replaced loop needs no memory of its own.

> Simon: This seems like an implementation detail of the auto-reduction service, can this be moved to a sub-design or does it influence the core API and backend design? Maybe this is a more general comment, can we partiation this into "independent" sub-designs, or some that build on each other?

Follow-up: the trigger loop touches the core in two places only. Records show their label and member (Q7), so that a loop can find what it has handled, and datasets can be listed and watched (finding 10 below). Everything else, `step`, `status`, the template check, series, and lookups, is inside the sub-design for batch and automatic reduction. Part 3 proposes the partition you asked for.

### Q12. Where templates and rules live (finding 23, B1, E4; core-api open question 4)

"The beamtime's template" lives only in the notebook, and a changed rule needs a new loop.

- (a) Templates and rules are plain data that serialize to JSON. A user keeps them in files or code; a driving server has its own API to add, replace, and list rules. The core API has no store for them.
- (b) The backend stores named, versioned templates and rules, reached through the client.

Recommendation: (a). Records do not name templates (Q8), so nothing in the backend needs them.

> Simon: Agree

### Q13. Removing a dataset (finding 12, A4)

A user removes a file that should not have reached the service. Its records stay.

Open: whether outputs derived from it are removed too, since they may be as sensitive as the file.
Recommendation: `client.remove(dataset)` also drops the outputs of every record that read it; the records stay.

> Simon: Seems like a detail that can be added later?

Follow-up: deferred; A4 stays in the stories with its Gap line.

### Q14. A dropped output (finding 34, H1)

Recommendation: `client.output` on a dropped output raises a defined error, and a new request that references it fails at submission. Computing it again is a new request the user makes.

> Simon: ok

## Part 2: missing calls, explained

Each item names the stories that need it, shows the code they use, says what core-api.md lacks, and proposes a form.
They are grouped by the sub-designs of Part 3. Comment where you disagree or where it is still unclear.

### Core: requests and records

**P1. Submitting several requests at once (finding 1; S4, C1).**
S4 submits two contributions and their sum in one call, so that the sum waits for them without the notebook waiting:

```python
parts = [Request(CONTRIBUTE, {'run': r}) for r in (r611, r612)]
total = Request(PARTS_SUM, {'numerator': [p.ref('numerator') for p in parts],
                            'denominator': [p.ref('denominator') for p in parts]})
c611, c612, summed = client.submit([*parts, total])
```

core-api.md shows `client.submit([...])` but not what it returns, nor that `p.ref(...)` on a request not yet submitted becomes a reference to the record it turns into.
Proposal: it returns the records, pending, in request order; references to requests in the same call become references to their records.

> Simon: Agree. But maybe we should support submitting a `dict` of requests instead, and return a dict in that case?

Answer: agreed. `client.submit` takes a request, a list, or a dict of requests, and returns records in the same shape.
`client.apply` then returns a dict keyed by member, so a batch's records come back keyed by member too:

```python
records = client.submit(client.apply(template, datasets, label='scan', member_field='temperature'))
records['250K']
```

> Simon:

**P2. Status and failure (finding 2; D1, D2, D3, D5).**
In D1 one of five runs is corrupt. The story waits for the batch and then checks each member:

```python
assert client.latest('scan', member='260K').status == 'failed'
assert failed.failure.message == 'file signature not found'
```

core-api.md names no statuses, says nothing of `record.failure`, and does not say whether `client.wait` raises when a record failed. It also says "a record never changes", while a pending record becomes completed.
Proposal: statuses `pending`, `completed`, `failed`, `cancelled`; `record.failure.message`; `wait` returns failed records rather than raising; "a *finished* record never changes".

> Simon: agree

**P3. Errors at submission (finding 3; A2, D4, D6, G5).**
D4: a batch form has a typo, `threshold='2,5'`, for 500 runs. The user must learn it before 500 records fail:

```python
requests = client.apply(Template(IOFQ, params={'threshold': '2,5'}, blanks=('run',)), runs)
with pytest.raises(SubmitError, match='threshold'):
    client.submit(requests)
assert client.records() == []
```

The same exception serves an unknown run number (A2), a spec version that no longer exists (D6), and a reference to another proposal's record (G5). core-api.md defines no exceptions.
Proposal: `SubmitError`, naming the field at fault, raised before any record exists. The check could already run at `client.apply`, so that a form shows the error before the user presses submit.

> Simon: agree

**P4. Cancel (finding 4; D3).**
D3: 20 of 500 requests have started when the user sees a wrong shared parameter. They cancel the rest and resubmit with the fix.
Proposal: `client.cancel(records)`; unfinished records end with status `cancelled`.

> Simon: agree

**P5. When a record was made (finding 5; B6).**
B6: a week later, the user looks for "the reduction I made last Tuesday". The story filters with `client.records(since=tuesday)`, but a record shows no time, so it cannot narrow to that day.
Proposal: `record.created`, and `until=` beside `since=`.

> Simon: agree

**P6. Intermediate values (finding 6; S3).**
S3: the user wants to see the counts after masking, a value inside the reduction. The old API let a request select outputs (`outputs=('masked',)`); the new stories only read an output the author declared:

```python
result = client.compute(IOFQ, {'run': run, 'threshold': 2.5})
client.output(result, 'masked')        # exists because IOFQ declares it
```

Proposal: no output selection on a request. An intermediate is visible only if the author declares it as an output, and then every record of the spec has it; whether an output nobody reads is stored is the system's business.

> Simon: agree, inspection of other intermediates would need to be done by running underlying workflow in a notebook

**P7. Reading part of an output (finding 8; B4).**
B4: a spectroscopy user drags a slider through cuts of a large 4D output in a web UI. Each position must show a cut without making a record, and without sending the whole volume:

```python
cuts = [client.output(volume, 'counts', index=i) for i in range(4)]
```

`client.output` reads a whole output.
Proposal: a read of part of an output that makes no record; its exact form waits for the plotting work.

> Simon: agree, making cuts must be efficient and produce no records

### Datasets

**P8. Naming datasets (finding 9; A1, A2, C2).**
core-api.md shows only `dataset(run=60339)`. A1 opens local files and a catalogue reference, and C2 reads a result another backend published:

```python
dataset(path='/home/user/data/run1.h5')
dataset(pid='20.500.12269/vanadium')
```

A2 checks that the record names the dataset itself, not the run number the user typed.
Proposal: the three forms above; the record keeps the dataset's identity, not what was typed.

> Simon: agree

**P9. Listing and watching datasets (finding 10; A1, S7, C1, D7, E1).**
The stories get dataset references from the return value of `measure`, because nothing lists datasets. A batch form needs "every sample run of this proposal":

```python
samples = client.datasets(Selector(role='sample'))
```

D7's driver loops over `client.watch(Selector(scan='17'))`. core-api.md does not say whether `watch` first yields datasets that already exist, or yields a dataset again when its file is written again; in the second case an accumulator would count a run twice.
Proposal: `client.datasets(selector)`; `watch` yields the existing datasets first, then new ones, each dataset once.

> Simon: seems reasonable, but does this belong into design of auto/batch reduction subsystem, or is it needed on the client API? Should client be dependency-injected into a higher-level object?

Answer: the backend needs datasets only to resolve a name such as `dataset(run=4711)` into an identity and to read the data; that stays in the core.
Listing, watching, and reading metadata are catalogue queries. They serve batch forms (D), browsing (A1), lookups (S7), and drivers (D7, E1), but no request needs them.
So I would move them into a dataset source object that higher-level objects take next to the client:

```python
datasets = catalogue(instrument='loki', proposal='p1')      # a dataset source
samples = datasets.list(Selector(role='sample'))
loop = TriggerLoop(client, datasets, rules=[rule])
for run in datasets.watch(Selector(scan='17')): ...
datasets.metadata(run)['sample']
```

The client stays about requests and records, and a test injects a fake source.
Where a notebook gets a dataset source from, and whether it must agree with the backend's, is a deployment question.

> Simon: agree, sounds like we could later "serve" the dataset-source

**P10. Current metadata (finding 11; A5).**
A5: after reduction, the catalogue corrects the sample name of a run. The user wants to see the corrected name next to the old result:

```python
(named,) = reduced.request.datasets()
client.metadata(named)['sample']       # 'heavy water', the catalogue's current value
```

Proposal: `client.metadata(dataset)`.

> Simon: agree

### Batch and automatic reduction

**P11. Listing labels (finding 16; B3).**
B3: the user keeps two variants under the labels `iofq` and `iofq-masked`. A UI that shows the user's variants needs to list labels.
Proposal: `client.labels()`; hiding a label is the UI's business.

> Simon: agree

**P12. A template from a tuned record (finding 17; B1).**
B1: the user tunes binning and mask in a stage, then saves the final values as the template for the rest of the beamtime:

```python
final = client.latest('iofq')
beamtime = Template.from_request(final.request, blanks=('run',))
```

Proposal: `Template.from_request(request, blanks=...)`.

> Simon: Does `Template` "need to know" about requests, or should it just be made from the input-model instance?

Answer: it does not. A template is a spec, some values, and blanks, so it can be made from the params of any request, with no special constructor:

```python
final = client.latest('iofq')
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))
```

Naming a field as a blank drops the value given for it. `Template.from_request` goes.

> Simon: agree

**P13. Changing a template (finding 18; D3, D6, E4).**
D3 fixes a shared parameter, D6 moves a template to a new spec version, and E4 improves a rule's template during a beamtime. The stories write `template.revise(params={'threshold': 0.5})`.
Proposal: `Template` and `Rule` are frozen dataclasses, and `dataclasses.replace` does this; no `revise` method.

> Simon: agree

**P14. Lookups (finding 20; S7).**
S7: every sample subtracts the can measured most recently before it, without the user naming the can:

```python
template = Template(IOFQ, blanks=('run', 'can'))
cans = Lookup(can=LastBefore(Selector(role='can')))
client.apply(template, samples, lookup=cans, label='iofq')
```

core-api.md mentions lookups only in prose.
Proposal: a lookup type as above; `apply` and `Rule` take the same `lookup=`; each dataset fills the one blank the lookup leaves.

> Simon: agree

### Provenance and publication

**P15. Reading provenance (finding 27; S8, F1).**
S8: a sample reduction used a vanadium result, and the user wants the vanadium's parameter values. F1: a colleague reads which software versions made a published result:

```python
(upstream,) = client.provenance(result).records()   # the records `result` read, through all inputs
upstream.request.params['scale']
client.provenance(result).software['scipp']
```

The reading calls offer only `.datasets()`.
Proposal: provenance is plain data with `.datasets()`, `.records()`, `.software`, and the record's own request, so that a publisher can store it. With Q2, it stops at datasets.

> Simon: agree

**P16. Recompute in the original environment (finding 30; F2).**
F2: after two upgrades, the user wants to know before anything runs that a result cannot be reproduced exactly. A plain `compute` of the same request runs in the current environment.
Proposal: `client.recompute(record)` runs the request in the record's environment, or refuses before running. This could also wait for a later sub-design.

> Simon: wait

**P17. Publishing a correction (finding 31; F4).**
F4: a published result was wrong. The user publishes a corrected one that names the entry it replaces; the old entry stays.
Proposal: `client.publish(ref, 'scicat', supersedes=old_pid)`.

> Simon: I don't know how SciCat/Scitecean handles this, important, but seems like a detail we can mostly omit for now

**P18. A workflow defined in a notebook (finding 32; G2).**
G2: a developer edits a workflow in a notebook and runs it without installing the package. Such records must be marked and must not be published:

```python
dev = connect(bind={IOFQ: draft_workflow})
result = dev.compute(IOFQ, {'run': run})      # marked as bound in a notebook; publish refuses it
```

Proposal: as above. The binding of specs to code is otherwise not in the API.

> Simon: I don't think we want a hosted backend to support custom workflows right now. But it should be possible when running in-process locally. I fear that forbidding to publish will cause trouble in practice, so we should be careful about what we hard-code. Is enforcing provenance the task of our system?

Answer: agreed on all points. The WorkflowSpec already has `code_revision` "so that a record made from a development branch is honest about what ran"; that is the right level.
Our system records what ran, honestly: the spec, the code revision, and that the implementation was bound in the process. It does not refuse to publish; whoever reads the published entry sees the same information.
A hosted backend runs only installed workflows. A backend in the notebook's process may bind a workflow defined there.

> Simon: agree

**P19. F3 repeats S2 and F1 (finding 33).**
F3 checks that a record made through a stage can be published like the plain request's record.
Proposal: make that one assertion in F1 and drop F3.

> Simon: agree

## Part 3: partition into sub-designs

Your comment on Q11 asks whether the design splits into sub-designs that are independent or build on each other. The stories and findings suggest this split:

| Sub-design | Contents | Builds on | Stories |
|---|---|---|---|
| Core | specs, requests, records, references, pending outputs, labels, statuses, errors | none | S1–S6, S8, C1, C2, C5, D4, D5 |
| Holders | sessions and their placement, stages, accumulators | core | S2, B1–B3, B5, C5, D7 |
| Datasets | naming, listing, watching, metadata, selectors for raw and derived data | core | A1–A5 |
| Batch and automatic reduction | templates, `apply`, lookups, rules, series, the trigger loop and its driving server | core, datasets; holders for a growing sum | S7, D1–D3, D6, E1–E4 |
| Provenance and publication | reading provenance, publishing, corrections, recompute, workflows bound in a notebook | core | S8, F1–F4, G2 |
| System | storage, record lifetime, scheduling, placement, session cleanup, access control, deployment | all | system-stories.md |

What each sub-design needs from the core is small: batch and automatic reduction need records to show their label and member (Q7), and datasets to be listable and watchable (P9).
core-api.md would hold the core and the holders, which are designed, and one paragraph on each other sub-design. Each sub-design gets its own document when its turn comes, with its stories.

> Simon: I think the first couple of rows might all belong into "Core", as the system is too incomplete without. I think the remaining split looks right.

Answer: I read "the first couple of rows" as core, holders, and datasets. Then the core is requests, records, references, labels, sessions with stages and accumulators, and naming datasets. Listing, watching, and metadata go to the dataset source object (P9), which I would also count as core, since batch, automatic reduction, and browsing all need it.
core-api.md then covers the core in full, and has one paragraph each on batch and automatic reduction, and on provenance and publication.

> Simon: seems reasonable, but feel free to restructure during writing if it turns out to be more useful to change partitioning.
