# One interface for batch and automatic reduction?

Question: could and should a person's batch and the automatic reduction be the same interface, given that the records under a label play the part of a committed offset?

**Answer: one table, two calls.**

- One table. Read a row's state from its newest record, for a person and for the loop alike, and make "which rows run" a function of that state: `remaining` for a person, `arrived(at)` for the loop. Today three places decide this, each from other data (section 1).
- Two calls. Keep `Batch.reduce`, which runs once, all or none, and raises, and `Automatic.step`, which runs again and again, row by row, and keeps the reasons. Merging them saves one name and costs a flag (candidate C).
- The command, the page, and the configuration of the automatic reduction do not exist yet. Build them as the batch's, not as new ones (section 3, B).

Levels looked at:

| Level | Looked at how |
|---|---|
| Python (`Batch`, `Automatic`) | spiked, every situation of section 4 run |
| command (`essapps-batch reduce --remaining`) | the function behind it run with the new predicate; `--follow` not built |
| page | the web page's server (`ess.apps.batch.web`, unchanged) run on a batch that a loop steps; the Panel view and a switch not built |
| configuration | the loop reading the settings of the page's form run; layered files not built |

Everything is measured on `184-automatic-as-batch` at `9c28338`. The branch got four commits while this was written; the first runs were at `2f8f67f`.
The spike and the scripts are in `.scratch/one-interface/` (section 8).

**Terms.** As in `batch.md`: row, key, column, settings, source, scan, edit, label. A row's *result* is the newest record under the label with the row's key. New here:
a *predicate* picks the rows to run, as a function from the states of rows to keys;
to *resolve* a value is to bring it into the form a record holds, with file names as identities and defaults filled in;
*the loop* is `Automatic`.

## 1. What decides "this row needs to run" today

| | Reads | Kept in memory | After a restart |
|---|---|---|---|
| page, "reduce remaining" | status; requests as given, in `Batch._submitted` and `Batch._tweaked` | yes | stale and tweaked are forgotten (#191) |
| `essapps-batch reduce --remaining` | status of the newest record of this spec version | no | same |
| the loop | the files at `at` of every record of the key, of any spec | `_held`, rebuilt from the changes | same |

Three things follow, each run or read on the branch:

1. **The page of a person's batch cannot show the loop's batch** (`page_branch.py`). On the loop's `Batch` object, every row the loop reduced shows `outdated`, and the button says "Reduce 2 outdated": the form writes defaults out, and requests are compared as given. With a `Batch` object of its own the page never shows a row as outdated, since that object submitted nothing.
2. **Two answers to "does this row have a result"** (`situations_branch.py`). After a new spec version, `Batch.state` shows every row as `not reduced`, and the loop treats the same rows as reduced.
3. **The loop got a guard at `9c28338`**: it raises if a source names a file by `file(run=...)`, because the record holds the identity, so the row would run at every step. This is the mismatch of #191, given against resolved, now in the loop (`_unheld`, `_check`, 30 lines).

## 2. The Kafka reading

| Kafka | Here |
|---|---|
| topic | the files of a proposal |
| consumer group | a label: "consumed" is what the records under it say |
| committed offset | per row: the request of the row's result |
| lag | per row: where the row's request differs from its result's request |
| seek | `reduce(settings, keys)` for chosen rows, or a new label |

So the insight, as one line: **the request of a row's result is what the row has consumed.**
"What is left to do" is then a comparison of two requests, and each predicate is a filter on it:

```python
changed = {name for name in now if now[name] != was.get(name)}   # now: the row's request, resolved; was: its result's
remaining:    no result, or failed, or changed               # a person: any lag
arrived(at):  no result, or at in changed                    # the loop: lag at the trigger column only
```

Where the analogy breaks:

- An offset is one number, because messages are consumed in order. A row is made from files of any age (a late transmission run completes an old row), so "consumed" is per row. Taking the offset literally, as a position in the files, is not a candidate for that reason.
- Processing and committing are one step here: the record is the result and the commit. A failed record is a commit for the loop and not for a person.
- A consumer does not edit its input. A person does: edits, rows removed, settings. That part of a batch is a document, not a stream (section 6, #191).
- A seek moves the offset back. A rerun appends: the old record stays in the history.

## 3. Candidates

### A. Two mechanisms on one table (the branch)

```python
batch = Batch(client, IOFQ, columns=('sample', 'sample_transmission'), label='iofq', source=pair_by_sample)

batch.scan()                                               # a person
batch.reduce(settings, remaining(batch.state(settings)))   # all or none; SubmitError names the rows
batch.tweak('60339', params)                               # one row with other settings

auto = Automatic(batch, at='sample', settings=load)        # the loop; load() gives the settings
auto.step()                                                # row by row; auto.refused says why a row did not run
```

```sh
essapps-batch reduce iofq.yaml runs.csv -l water --remaining     # leaves out the rows completed
```

Page: Scan, Reduce all, Reduce remaining, Reduce selected, a form for a tweak. No page for the automatic reduction.

### B. One row state, two predicates

```python
state = batch.state(settings)['60339']
state.record     # the row's result: the newest record under the label with the row's key, of any spec version
state.changed    # {'q'}: the parameters at which the row's request now differs from its result's
state.outdated   # True if the result is of another version of the spec

def remaining(states):          # a person
    return [k for k in ready(states)
            if states[k].status in (None, FAILED, CANCELLED) or states[k].changed or states[k].outdated]

def arrived(at):                # the loop
    return lambda states: [k for k in ready(states) if states[k].record is None or at in states[k].changed]

batch.reduce(settings, remaining(batch.state(settings)))             # a person: once, all or none
Automatic(batch, runs=arrived('sample'), settings=load).step()       # the loop: row by row
batch.rows = batch.rows.edit('60339', {'q': {'num_bins': 50}})       # one row with another setting: an edit
```

`changed` is read from the record, so `Batch` keeps no copy of what it submitted, and the loop keeps no `_held`.
A value is compared as given first. Only a value that differs as given is resolved, which needs a call that `ess.apps` lacks (section 5).
`at='sample'` can stay as the spelling of `runs=arrived('sample')` until a loop needs a second predicate.

```sh
essapps-batch reduce iofq.yaml runs.csv -l water --remaining             # `remaining`: also the rows whose cells or settings changed
essapps-batch reduce iofq.yaml --rows my.pkg:pair_by_sample -l auto/iofq --follow sample   # the loop; not built
```

Page: the same page for both. A switch "reduce runs as they arrive" makes the server step the loop under its lock, with the settings of the form. The primary button stays: "Retry 1 failed", "Reduce 3 outdated".
Configuration: the settings of the automatic reduction are a `Template` file, the one that "Save settings" writes and `essapps-batch reduce` reads.

### C. One call

B, and `Automatic` goes:

```python
batch.reduce(settings, remaining)                       # a person: raises if a row is refused
batch.reduce(settings, ['60339'])                       # chosen rows
batch.reduce(settings, arrived('sample'), each=True)    # one step of the loop; batch.refused says why a row did not run
```

Command, page, and configuration as B: C differs from B only in Python.
What the loop keeps (the rows and settings of its last step, the reasons of refusals) moves into every `Batch`. Without it, each step works out the state of every row: 1.5 s at 100 000 rows (section 5).

## 4. The situations

A and B are run (`situations_branch.py`, `situations_one.py`, `page_branch.py`, `page_one.py`); toy workflows, 3 or 4 rows.
For C read B with `batch.reduce(settings, remaining)` in place of `batch.reduce(settings, remaining(batch.state(settings)))`, and `batch.reduce(load(), arrived('sample'), each=True)` in place of `auto.step()`. C is not run.

| Situation | A: the branch | B: the spike |
|---|---|---|
| a person reduces a table once | `batch.reduce(s, ready(batch.state()))` | same |
| the remaining rows after a failure | `batch.reduce(s, remaining(batch.state(s)))` submits the failed row | same |
| a setting changed, reduce the stale rows | the same call submits all 3. After a restart it submits none, and the rows show `completed`. The command with `--remaining` submits none | the same call submits all 3, also after a restart. The command submits all 3, then none |
| tweak one row | `batch.tweak('3', params)`; `tweaked` is forgotten at a restart | `batch.rows.edit('3', {'binning': {'bins': 8}})`, then `reduce`. It is an edit: kept and lost as edits are |
| edit a cell by hand | stale only if this `Batch` object submitted the row | `changed == {'thickness'}`, whoever submitted it |
| E1, a series grows | 2 records, the newest holds runs 2, 3, 4 | same |
| E2, goes quiet | `auto.refused == {'1': 'unknown spec sans-iofq/v1'}` | same |
| E3, our own output | not run for any candidate: publication is not implemented. The source gets raw files only, which no candidate changes | |
| E4, settings improved | run 3 only. The results made before: a filter over the records, written by hand | run 3 only. The results made before: `[k for k, s in batch.state(settings).items() if s.changed]` |
| restart of the loop | the first step submits nothing | same |
| a scientist starts from the automatic configuration | `iofq_batch(own_client, label='mine')` with the same settings, in their own folder; removes a row; reduces | same |
| watching on a page | rows `outdated`, button "Reduce 2 outdated" (section 1) | rows `done`. A failed row: "Retry 1 failed", and the loop does not run it again. A setting changed on the page: the loop uses it for the next run, the earlier rows show `outdated`, "Reduce 3 outdated". The same after a restart, once the form holds the settings again |
| again from run 4000 with the new version | a person: `v2.reduce(settings, keys)`. The other rows show `not reduced` | the same call, or one more term in `runs=`. The other rows show `outdated`, and their old result is read |

"One more term", as run:

```python
def from_run_4000(states):
    new = set(arrived('sample')(states))
    return [k for k in ready(states) if k in new or (states[k].outdated and int(k) >= 4000)]
```

The loop looks at every row once when it starts, and an upgrade restarts it, so the term applies then.
The start of a new label at a run, which `batch-vs-rules.md` leaves to a range in `Selector`, is the same kind of term.

## 5. What each candidate removes, adds, breaks, and costs

| | A | B | C |
|---|---|---|---|
| memory that decides what runs | `_submitted`, `_tweaked`, `_held`, `_unheld` | none: the records | none |
| toolbox code (`reduction`, `state`, `auto`), lines without comments and docstrings | 411 | about 377 (counted on the spike) | B, with about 60 lines moved from `Automatic` into `Batch` (inferred) |
| core | | a call that resolves without submitting: `Checks.template` exists in the workspace, no client call reaches it. Estimate 30 to 50 lines; not written | same |
| names that go | | `tweak`, `tweak_params`, `RowState.tweaked`, `RowState.stale`, `Automatic.at` | and `Automatic`, `step` |
| names that come | | `RowState.changed`, `RowState.outdated`, `arrived`, `Automatic.runs`, the resolve call | and `each=`, `Batch.refused` |
| the branch's 55 tests of `Batch` and `Automatic` | pass | 45 pass unchanged but for the spelling `runs=arrived(...)`; 5 changed where B behaves differently (below); 3 of `tweak` rewritten; 2 dropped (run: `ported/`) | all that call `reduce` or `Automatic` change (inferred) |

What B changes in behaviour:

- `--remaining` also reduces the rows whose cells or settings changed. Today's guide says it does not.
- `Automatic.refused` no longer lists a row that waits for a partner or for its running record. Its `RowState` says so, as on a person's page.
- The loop runs a row whose cell at `at` differs from the newest record's. Today: a cell that holds a file no record of the key holds. Two cases differ (`differs.py`, and a ported test): a run that leaves a list reduces the row again without it, and a file inside a group at `at` counts.
- A tweak follows the other settings, and stays until the edit is reset. Today it ignores the settings until the row is reduced with them.
- A changed default of a spec shows as `changed`, since the row's request no longer means what its record holds (inferred).
- The views read `tweak`, `stale`, and `tweaked` in 45 lines of Python, and in `app.js`. The web server ran unchanged on the spike with `stale` kept as a property; its tweak form was not run.

Cost at 100 000 runs reduced, toy workflow, fake files, history in memory, times in ms (`scale.py`). The machine was under load from other sessions (load average 8 to 30 on 24 cores), so read differences below 20% as noise.

| | A | B |
|---|---|---|
| a step with one new file, best of 10 | 816 | 774 |
| a step with nothing new | 782 | 761 |
| the first step after a restart (at 30 000 rows) | 2 559 (270) | 4 020 (1 136) |
| the state of one row | 23 | 0.03 |
| the state of all rows with settings, as a page asks for | 1 900 | 1 500 |

A step is the scan in both: listing the files and the source over all of them.
The state of one row lists every record under the label on the branch, and reads the batch's own records from the changes in B.
The first step after a restart is slower in B by 15 to 30 µs per row, once: it works out the state of every row, and half of that is asking the workspace for the status and the spec of each row's record (profiled at 20 000 rows).
A value that differs as given from its record costs one resolve, about 0.2 ms with the toy spec, kept until the row changes. Rows from a source hold files by identity and need none. A table of 10 000 rows typed by hand costs about 2 s once (inferred from the 0.2 ms).

## 6. Recommendation

**B, and not C.** In this order, each a pull request, after the branch is merged:

1. The resolve call in the workspace and the client. It is the first proposal of #191.
2. `RowState.changed` and `outdated` from the records; the result of a row is its newest record of any spec; `_submitted` goes; `--remaining` is `remaining`.
3. `arrived(at)` on the same state; `_held` and the guard of `9c28338` go.
4. A tweak is an edit at a parameter that is not a column; `tweak`, `tweak_params`, `_tweaked` go. Needs your ruling (question 2). Until then step 2 keeps `_tweaked`, or "reduce remaining" would reduce a tweaked row again, since its request differs from the settings.
5. When the automatic reduction gets a page: the batch's page, the loop stepped by its server, the settings in a `Template` file.

Without step 1, step 3 alone behaves as the branch does, guard included (run: the ported tests of the loop pass without a resolve call, but for the one of `file(run=...)`).

Not C: the two calls differ in what a refusal does (none run and an error, or the others run and a reason is kept), in what they look at (the rows asked for, or the rows that changed), and in who calls them. One call would carry these as a flag and as loop memory inside `Batch`.

**The strongest argument against B.** It puts the comparison of requests at the centre, and one mechanism means one bug reaches both. Today the loop's predicate is about 25 lines that read only file identities; a bug in `stale` shows a wrong word on a page. In B a bug in `changed` at the trigger column makes the loop reduce 100 000 rows again. And `changed` rests on a property nobody has stated: resolving a resolved value gives the same value. If that fails for one type of field, its rows read `changed` for ever.
What limits this: the loop reads `changed` only at `at`, where values are files compared by identity, as today. The property can be tested per spec, as the digest is (`ess.spec.testing`). It held for the toy specs; it was not tried on LoKI's.

**What B implies for #191**, by its questions:

1. *What is the batch table?* Both, with a clear cut. The results are a view of the records, and `Batch` keeps nothing of them. The definition is a document: the settings and what was set by hand (edits, rows added, rows removed).
2. *Is the problem in what a record stores?* No. A record stores enough. What is missing is the way from the definition to the record's form: the resolve call.
3. *After closing and reopening?* The same table if the definition is stored, and it is all that needs storing (run: after a restart the edited and the tweaked row read `stale`, and with the edits loaded all rows read `completed`). Where it is stored, and when, is open; the loop has no definition to store but its settings.
4. *What is a tweak?* A property of the row: an edit at a parameter that is not a column.
5. *A spec that changed?* `outdated` on the row's state. The old record is still the row's result and is read. A predicate decides: `remaining` includes it, `arrived` does not.

## 7. Questions

1. Is "one table, two calls" what you meant by the same interface, or did you mean one call (C)?

> Claude: two calls. `reduce` and `step` have different contracts on a refusal, and the loop has memory of its own. What I would make one is everything below the call (the state and the predicates) and everything above it (command, page, settings file).

> Simon:

2. A tweak becomes an edit at a parameter that is not a column: `batch.rows.edit('60339', {'q': {...}})`. The row then follows the other settings, shows its own value as a cell, and keeps it until the edit is reset. `RowState.tweaked` goes.

> Claude: yes. It removes the one kind of result that the records cannot explain, and it makes "what is lost at a restart" one thing, the edits. The cost: a table may then hold a column for a setting, which `frames.rows_of` refuses today, and a stage cannot take such a row, so it is submitted as a plain request.

> Simon:

3. The loop's predicate changes from "the cell at `at` holds a file that no record of the key holds" to "the cell at `at` differs from that of the newest record". A run that leaves a list (marked bad) then reduces the row again without it.

> Claude: yes. It is what makes the loop's predicate a filter on the state a page shows, and the rerun without the bad run is the result a person expects. If you want the old rule, `Batch` would keep the files of every record of a key, as `_held` does today, and `arrived` would compare with those.

> Simon:

4. The resolve call, and the rule that goes with it: resolving a resolved value gives the same value, tested per spec. Is a new call on the workspace acceptable for this?

> Claude: yes, as `client.check(requests)`: the requests as records would hold them, or the `SubmitError` of each, with nothing logged. The same call lets a page show an invalid row before 500 are submitted (D4), which I did not build.

> Simon:

5. Where do the settings of the automatic reduction live? "Layered configuration files" are not built.

> Claude: a `Template` file per batch in the proposal's folder, loaded into the page's form, which the loop reads at each step (run with the form: `page_one.py`). The instrument's default is a template in its package; the proposal's is the saved one. No third format. A scientist's start from the automatic configuration is then a copy of that file and the name of the source.

> Simon:

## 8. Run and inferred

Run, with `.scratch/venv-container/bin/python .scratch/one-interface/<script>`, against a copy of the branch at `9c28338` in `.scratch/one-interface/branch/` (the checkout was edited by another session):

| File | What it shows |
|---|---|
| `one.py` | the spike of B: `Batch`, `RowState`, `remaining`, `arrived`, `Automatic`, on the client's public calls |
| `situations_branch.py`, `situations_one.py` | section 4 on A and on B; lines marked NOTE and DIFFERS |
| `page_branch.py`, `page_one.py` | the web page's server on the loop's batch, on A and on B |
| `differs.py` | the run that leaves a list |
| `ported/` | the branch's `auto_test.py` and `reduction_test.py` on the spike: 53 tests pass; `NO_RESOLVE=1` runs them without the resolve call |
| `scale.py`, `scale.out` | section 5, cost |
| `notes.md` | dead ends, and what was seen on the branch |

Inferred, not run: candidate C; `--follow`; the switch on a page; the Panel view; the tweak form of the web page on the spike; the size of the resolve call; LoKI's specs; a folder workspace; memory.
The spike is given the resolve call from outside (`Checks.template` with the file source), since no client call reaches it.
