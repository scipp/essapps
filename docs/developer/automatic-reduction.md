# Batch and automatic reduction

This sub-design builds on the core of [README.md](README.md).
From the core it needs only that records show their label and member, and `client.files`.
Its stories are S7, C1, D1 to D6, and E1 to E4 in [user-stories.md](user-stories.md).

## Batches

`apply` fills a template for each file and returns the requests keyed by member, the value of a metadata field (by default the run number).
It reads metadata through `client.files` and builds plain data; it submits nothing.
The examples of batches, lookups, and rules reduce transmission runs with `TRANSMISSION` ([README.md](README.md#specs-and-bindings)), whose parameters `run` and `empty_beam` are file models, each a run's file given by reference, and whose output is `transmission: ComputedTransmission`.

```python
template = Template(TRANSMISSION, params={'empty_beam': file(run=60336)}, blanks=('run',))
requests = apply(template, runs, client.files, at='run', member_field='sample')
records = client.submit(requests, label='batch', persist=True)
```

A batch application submits with `persist=`, so its client keeps nothing, and the results are read later from the store ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).

A lookup fills further blanks per file. `LastBefore(selector)` takes the matching file with the highest run number below the filled file's.
Each file fills the one blank the lookup leaves, `at`, a parameter of a file model: the file reference is its value, and `apply` fills no other parameters.
A lookup names the blank it fills the same way, such as `Lookup({'empty_beam': LastBefore(...)})`.
So each file that a rule or a lookup finds needs a parameter of its own in the spec, such as `run` and `empty_beam`.
A batch whose parameters differ per file, such as the thickness of each sample, is a list of requests, each with its own params.

```python
empty_beams = Lookup({'empty_beam': LastBefore(Selector(role='empty-beam'))})
requests = apply(Template(TRANSMISSION, blanks=('run', 'empty_beam')), runs, client.files, at='run',
                 lookup=empty_beams)
```

## Rules

A rule is plain data: a name, a template, a selector, a label, the blank `at`, and optionally a `member_field`, a `lookup`, a `series`, and `persist`, the outputs it persists, every output by default.
A record that the template references, such as a beam centre, must be persisted, since the trigger loop's client reads it.

```python
rule = Rule('auto-transmission', Template(TRANSMISSION, blanks=('run', 'empty_beam')),
            selector=Selector(role='transmission'), label='transmission', at='run', lookup=empty_beams)
```

With `series='sample'`, a request takes every matching file with the same `sample` so far, in run order, and `sample` is its member.
The template's blank `at='runs'` is a table of a file model, its rows the files, `[file, ...]`, and the lookup is not used.
Each arrival of an angle submits one plain request that stitches all angles of that sample so far.

```python
reference = file(run=500)                # the reference run, which divides each angle
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', at='runs', label='reflectivity')
```

A rule whose template leaves no single blank for a file, or whose `at` is not that blank, is refused when it is made.
`TriggerLoop` refuses a rule whose files could not fill their blanks: each blank it fills, `at` and the lookup's, is a parameter of a file model, a list one if and only if the rule has `series`, and each selector names the model's metadata, so that every file it lists fits the model.
`apply` has no spec at hand, so the backend refuses its requests instead.

Templates and rules are frozen dataclasses; `dataclasses.replace` changes them.
The core keeps no store of them, and records do not name them: a record's request says everything that determines its result.

## The trigger loop

The trigger loop is the controller for rules. It runs in a server of its own or in a notebook, never in the backend.
It finds new files through `client.files`, and submits with `persist=` the outputs each rule names, so its client keeps nothing.

```python
loop = TriggerLoop(client, rules=[rule])
loop.step()                  # submits what arrived since the last step, returns the records
loop.status(rule).reason     # why the rule submitted nothing or skipped a file
loop.run()                   # steps forever
```

The loop keeps no memory of its own. A rule has handled a file when a record under the rule's label holds it at the blank `at`, or for a series in a row of that blank table, `runs[i]`, so a restarted or replaced loop does not reduce a file again, and a file that arrives again keeps its identity and is not reduced twice.
A file that the record holds at another place, such as an empty-beam run that a lookup fills, does not count.
This holds while the proposal's history is kept. A running loop keeps it, since a proposal with an open client is not idle (README.md, [How long records and values are kept](README.md#how-long-records-and-values-are-kept)).

The label belongs to the rule: any record under it counts, whoever submitted it and whether or not it failed.
A record that a notebook submits under a rule's label stops the rule from reducing the files it names, so manual work uses labels of its own.
A failed record counts so that the loop does not retry a failed file, which could fail again at every step; running it again is the user's decision (D2).

A file whose request cannot be made, such as a transmission run with no empty-beam run measured before it, is skipped: the rule submits the other new files, and its status names the skipped one and why.
No record names the skipped file, so the next step tries it again, for example once an empty-beam run whose file arrived late is listed.
A rule whose requests are refused, such as for an unknown spec version, submits nothing.
`apply` raises instead of skipping, since a user who builds a batch wants to see the error.

## Open

- Adding, replacing, and listing rules in a running trigger loop.
- A file that a lookup finds measured after the filled one, such as an empty-beam run measured after the transmission run, and other lookups than `LastBefore`.
- A rule that pushes into an accumulator, for a sum that grows with each file (D7 as a rule). The loop's client would keep the accumulator, the only thing it keeps, and freeze it with `persist=` once the sum is complete.
