# Automatic reduction as the batch's chain of tables

Replaces the long version (`batch-vs-rules-long.md`, with the stories and scripts behind the claims here).
Direction, as I read your message of 2026-11-13: one model. Rules (`ess.apps.rules`: `Rule`, `RuleStatus`, `TriggerLoop`) predate the chain of #174 and go.

## 1. The model

```python
files   = file source                    # the only table that grows on its own
rows    = source(files)                  # e.g. pair_by_sample: pure, plain data in and out
results = records under the batch's label, by member        # the history, read as a table
state   = rows left-joined with results on key == member    # Batch.state()
todo    = [key for key in state if policy(row, result)]
submit(todo)                             # appends to the history, so results changes
```

A rule's `submit(client, file)` is `source`, the join, and the submit written as one function per file.
A chain (transmission, then sample) is a second batch whose source also takes the `state` of the first.

Nothing is "consumed" by a position in a table. A row is consumed when the records say so, so a restart needs no state of its own.

## 2. A person and the automatic loop differ in one predicate

| A row runs if | no result | failed or cancelled | a setting or another cell changed | a new file at the trigger column |
|---|---|---|---|---|
| page, "reduce remaining" | yes | yes | yes (stale) | yes |
| `essapps-batch reduce --remaining` | yes | yes | no | no |
| automatic | yes | no | no | yes |

The trigger column is one column of the batch, such as `sample`, or `runs` for a growing series.
Automatic, as code:

```python
def new_at(row, at, held):       # held: the files that records with this row's key hold at `at`
    return bool(files(row.values.get(at)) - held)
```

Run: five scripts of the long version (arrival, can before, series, template, restart) print the same with this per-row check as with rules' check, which collects the files of every record under the label.

## 3. Cost at 10k to 100k files

One step with one new file, N runs already reduced, toy workflow, times in ms (`rules_scale.py`, `rules_scale_changed.py`):

| N | chain as written: scan, full join, reduce | trigger loop today | rows recomputed, state and submit only for changed rows |
|---|---|---|---|
| 3 000 | 67 | 6 | 15 |
| 30 000 | 985 | 88 | 100 |
| 100 000 | 4 292 | 272 | 333 |

All three grow linearly with N per step, so the sum over a beamtime is quadratic. The constants decide:

- **Chain as written**: about 40 µs per file. At 100k a step takes longer than the 2 s between files. Not usable.
- **Trigger loop today**: about 3 µs per file, because it lists all files at every step. `FileSource.list(selector)` has no "new since" form.
- **Changed rows only**: `source` over all files (2 µs per file) and a comparison with the rows of the last step (1 µs per file). State, problems, and submission run for the rows that changed. Summed over a proposal of 100k files: about 4.6 h of one core, spread over the 55 h in which the files arrive.

The third column is this step:

```python
changed  = client.changes(after=cursor)                 # records: only what changed
held[k] |= files_at(record, at)                         # for each changed record of this label
rows     = {r.key: r for r in source(all_files)}        # the join over all files, 2 µs per file
look_at  = [k for k in rows if rows[k] != last.get(k)]  # new rows and rows whose cells changed
todo     = [k for k in look_at if new_at(rows[k], at, held[k]) and not problems(rows[k])]
```

What the loop keeps in memory (the rows of the last step, `held`, the position in the changes) is rebuilt from the files and the records at a start.
A row that waits for its partner is looked at again when the partner arrives, because its cells change. Rows that were refused are looked at again when the settings change.

To get below linear, the source must not run over all files. A source could name a group for each file, and the loop would recompute the rows of the groups that got a file:

```python
Batch(..., source=pair_by_sample, group=lambda f: f.metadata.get('sample'))   # optional
```

A source that pairs by time ("the can measured before") has no such group and stays linear. I would not build this before an instrument needs it.

Needed in any case, for either model: the file source reports what is new since a position, as `client.changes` does for records. Listing 100k files takes 280 ms in the fake source, and a catalogue cannot be listed whole at each step.

Seen on the way: `client.submit` of one request grows from 1 ms at 3k records to 5 ms at 100k. Not looked into.

## 4. What changes in the code

- Goes: `rules.py` (165 lines), `rules_test.py` (631 lines, about 10 tests ported), the rule code of `e_automatic_test.py`, `automatic-reduction.md` rewritten.
- Added: the loop of section 3 in `ess.apps.batch.auto`, and state for chosen keys in `Batch`, so that a step does not join all rows.
- Stays: `ess.apps.batch`. A technique writes its pairing once, as a row source.

I would settle these myself unless you object:

- One refused row does not hold back the others in automatic mode: the loop submits, drops the rows the refusal names, and submits the rest. `Batch.reduce` stays all or none for a person.
- The filter by submitter in the join goes: one folder or one service holds one history.
- The page of the automatic reduction and the loop using one `Batch` from two threads is settled with the application of #151.

## 5. Questions

1. Automatic mode: a row runs again only for a new file at the trigger column. A later can, a changed setting, or a failed record does not reduce old runs again (story E4, and what rules do).

> Claude: yes.

> Simon: Let us be clear, `pair_by_sample` would in principle need to run only until a successful pairing was found, candidates that were consumed can be removed from the candidate list. Except that may not be enough: (a) the same can run might get used by multiple sample, (b) the same sample might be measured multiple times and the "pairing" needs to build a list of sample runs that all feed into the same. My assumption would be that some pragmatic solution such as limiting the "history" we look at might solve most problems, e.g., time-based or file-count-based?

> Claude: I read this as a comment on cost, not as an objection to the rule of the question. Say so if I am wrong.
> Whether a file can be dropped once it is consumed depends on its part in a row:
>
> | The file's part in a row | After the row is reduced | Example |
> |---|---|---|
> | the trigger, one file in the cell | not needed again | a sample run |
> | the trigger, one of a list | needed again when the list grows | your (b): a sample measured again, the angles of a series |
> | a partner | needed by any later row | your (a): a can; a transmission run |
>
> So only the first kind can leave the candidates.
> A cut-off by age or by count bounds the cost of a step, but it changes results in two ways. A partner older than the cut-off: the row waits for ever, which a page shows as `incomplete`. A sample measured again after the cut-off: its row holds only the later runs and is reduced with those, and nothing shows it.
> So I would not make a cut-off the mechanism that bounds the cost. Question 3 has what gives the bound without it.
> A cut-off that a person sets on purpose ("only the files since Tuesday") is the "offset" of question 2: a filter on the files given to the source.

2. A new version of the spec during a beamtime: are the old runs reduced again by the automatic loop?

> Claude: no, as for a changed setting. A person who wants them reduced again makes a batch with its own label. This means the join for the automatic loop does not filter by spec.

> Simon: Interesting. I see no reason for supporting this right now, but it seems really useful -- a bit like controlling a topic offset in Kafka. Can we design it so it will be easy to add this in the future, without doing it now?

> Claude: yes. The picture fits: a label is a consumer group, since "consumed" is defined by the records under a label and by nothing else.
>
> ```python
> iofq_batch(client, label='auto/iofq')      # has consumed runs 1 to 5000
> iofq_batch(client, label='auto/iofq-2')    # a new label has consumed nothing: starts at the first file
> Batch(..., label='auto/iofq-2', selector=...)   # "start at run 4000": the files given to the source
> ```
>
> The last line needs a range in `Selector`, which matches equal values only today.
> "Reduce the old runs again when the version changes", under the same label, is one more term of the predicate, because the join gives it the old result with its spec (name, version, digest; ADR 0009):
>
> ```python
> def runs(row, result):
>     return new_at(row, at, held) or result.spec != spec    # the second term only if wanted later
> ```
>
> What must hold now so that both stay small additions: consumed is read from the records under the label and from nothing else; the join for the automatic loop does not filter by spec; the files reach a source through the batch's selector. All three are in the plan. Nothing is built for it now.

3. Which bound on the cost of a step do you want now: linear with a small constant (third column: 0.33 s at 100k files), or below linear (the `group` of section 3)?

> Claude: the third column now, and the `group` when an instrument's numbers ask for it. The requirement on the file source ("what is new since a position") is written down now, since it holds for either.

> Simon: I think linear with small column would be acceptable, but I do not fully understand the challenge in making it sub-linear. Is the pair-finding the problem, since it may need to look back into the past "arbitrarily" long?

> Claude: yes, the look back is the reason, and it goes both ways. A new sample run needs a partner of any age. And a new file can change a row made long ago: a late transmission run completes a sample that waits, a new run joins the list of a sample measured before.
> Looking back is cheap with an index. What is missing is knowing which rows a new file can touch: the source is one function over all files, so the loop runs it over all files and compares the rows with those of the last step. If the source says which files belong together, the loop knows:
>
> ```python
> group = lambda f: None if f.metadata['role'] == 'can' else f.metadata['sample']   # None: shared by all groups
> # a new file of sample 'water': rows = source(shared files + the files of 'water'), of any age, found by dictionary
> # a new shared file (a can):    only the rows that wait are made again; an old row does not run again for a can
> ```
>
> A step then costs the files of the touched groups, the shared files, and the rows that wait, with no cut-off, and it covers your (a) and (b).
> The author's side of it: `source` on the shared files and whole groups gives the same rows as on all files, which a helper in `ess.apps.batch` can check on a list of files.
> Not prototyped. Not built now: it adds an optional argument, and `source` stays `files -> rows`.

## Next

With your three answers the plan is: the step of section 3 (linear, 0.33 s at 100k) in `ess.apps.batch.auto`, state for chosen keys in `Batch`, rules removed, stories E and `automatic-reduction.md` rewritten. Not built: `group`, a range in `Selector`, running again for a new version, "new since a position" in `FileSource` (written down as a requirement). On branch `184-automatic-as-batch`, as a PR for you.

## 6. Not checked

- `rules_loki.py` and `rules_cost.py` were not rerun with the per-row check.
- Section 3 is a prototype on the public interface with a toy workflow, a fake file source, and an in-memory log and store. Not measured: a folder workspace, a restart at 100k records (every row is looked at once), memory.
- A page that shows 100k rows: `Batch.state()` takes 0.8 s at 100k. A separate question.
- Stale for a person's batch is compared with what `Batch` holds in memory, and is lost at a restart (#191). The automatic loop does not use it.
