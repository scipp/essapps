# Batch and automatic reduction

This sub-design builds on the core of [README.md](README.md).
From the core it needs only that records show their label and member, and `client.datasets`.
Its stories are S7, C1, D1 to D6, and E1 to E4 in [user-stories.md](user-stories.md).

## Batches

`apply` fills a template for each dataset and returns the requests keyed by member, the value of a metadata field (by default the run number).
It reads metadata through `client.datasets` and builds plain data; it submits nothing.

```python
requests = apply(Template(IOFQ, blanks=('sample',)), samples, client.datasets, at=('sample', 'file'),
                 fields={'transmission': None}, member_field='temperature')
records = client.submit(requests, label='scan', persist=True)
```

A batch application submits with `persist=`, so its client keeps nothing, and the results are read later from the store ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).

A lookup fills further blanks per dataset. `LastBefore(selector)` takes the matching dataset with the highest run number below the filled dataset's.
Each dataset fills the one blank the lookup leaves, at the place `at`: the dataset reference is the value there, and `apply` fills no other parameters.
Every parameter is a group, so `at` names the blank and the fields down to a file, such as `('sample', 'file')` for the file of the group `sample: Sample`.
`fields` gives the group's other fields, the same for each dataset, such as `{'transmission': None}` for a `Sample` reduced without a transmission correction; a field left out takes its default, and a field without a default makes the backend refuse the requests, naming the field.
A lookup names its place the same way, such as `('can', 'file')` for the file of the group `can: Can`, one place per blank.
Its `fields` gives the other fields of each blank it fills, by blank, as `apply` takes them, such as `Lookup({('can', 'file'): LastBefore(...)}, fields={'can': {...}})`; a field left out takes its default.
So each dataset that a rule or a lookup finds needs a parameter of its own in the spec, such as `sample` and `can`.
A batch whose parameters differ per dataset, such as the thickness of each sample, is a list of requests, each with its own params.

```python
cans = Lookup({('can', 'file'): LastBefore(Selector(role='can'))})
requests = apply(Template(IOFQ, blanks=('sample', 'can')), samples, client.datasets, at=('sample', 'file'),
                 fields={'transmission': None}, lookup=cans)
```

## Rules

A rule is plain data: a name, a template, a selector, a label, the place `at`, and optionally the blank's other `fields`, a `member_field`, a `lookup`, a `series`, and `persist`, the outputs it persists, every output by default.
A record that the template references, such as a beam centre, must be persisted, since the trigger loop's client reads it.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('sample',)), selector=Selector(role='sample'),
            label='iofq', at=('sample', 'file'), fields={'transmission': None})
```

With `series='sample'`, a request takes every matching dataset with the same `sample` so far, in run order, and `sample` is its member.
The template's blank `runs` is a table with a row for each dataset, and the lookup is not used.
`at` names the blank and the fields within a row down to the file: `at=('runs', 'file')` makes the rows `[{'file': dataset}, ...]`, and `fields` gives the other fields of each row, the same for every row, or they take their defaults.
Each arrival of an angle submits one plain request that stitches all angles of that sample so far.

```python
reference = dataset(run=500)                # the reference run, which divides each angle
rule = Rule('reflectivity', Template(STITCH, params={'reference': {'file': reference}}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', at=('runs', 'file'), label='reflectivity')
```

A rule whose template leaves no single blank for a dataset, or whose `at` is not a field of that blank, is refused when it is made.

Templates and rules are frozen dataclasses; `dataclasses.replace` changes them.
The core keeps no store of them, and records do not name them: a record's request says everything that determines its result.

## The trigger loop

The trigger loop is the controller for rules. It runs in a server of its own or in a notebook, never in the backend.
It finds new datasets through `client.datasets`, and submits with `persist=` the outputs each rule names, so its client keeps nothing.

```python
loop = TriggerLoop(client, rules=[rule])
loop.step()                  # submits what arrived since the last step, returns the records
loop.status(rule).reason     # why the rule submitted nothing or skipped a dataset
loop.run()                   # steps forever
```

The loop keeps no memory of its own. A rule has handled a dataset when a record under the rule's label holds it at the place `at`, or for a series at that place in a row of the blank table, `runs[i].file`, so a restarted or replaced loop does not reduce a dataset again, and a dataset whose file arrives again keeps its identity and is not reduced twice.
A dataset that the record holds at another place, such as a can that a lookup fills, does not count.
This holds while the proposal's history is kept. A running loop keeps it, since a proposal with an open client is not idle (README.md, [How long records and values are kept](README.md#how-long-records-and-values-are-kept)).

The label belongs to the rule: any record under it counts, whoever submitted it and whether or not it failed.
A record that a notebook submits under a rule's label stops the rule from reducing the datasets it names, so manual work uses labels of its own.
A failed record counts so that the loop does not retry a failed dataset, which could fail again at every step; running it again is the user's decision (D2).

A dataset whose request cannot be made, such as a sample with no can measured before it, is skipped: the rule submits the other new datasets, and its status names the skipped one and why.
No record names the skipped dataset, so the next step tries it again, for example once a can whose file arrived late is listed.
A rule whose requests are refused, such as for an unknown spec version, submits nothing.
`apply` raises instead of skipping, since a user who builds a batch wants to see the error.

## Open

- Adding, replacing, and listing rules in a running trigger loop.
- A can measured after the sample, and other lookups than `LastBefore`.
- A rule that pushes into an accumulator, for a sum that grows with each dataset (D7 as a rule). The loop's client would keep the accumulator, the only thing it keeps, and freeze it with `persist=` once the sum is complete.
