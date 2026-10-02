# Batch and automatic reduction

This sub-design builds on the core of [README.md](README.md).
From the core it needs only that records show their label and member, and `client.datasets`.
Its stories are S7, C1, D1 to D6, and E1 to E4 in [user-stories.md](user-stories.md).

## Batches

`apply` fills a template for each dataset and returns the requests keyed by member, the value of a metadata field (by default the run number).
It reads metadata through `client.datasets` and builds plain data; it submits nothing.

```python
requests = apply(Template(IOFQ, blanks=('run',)), samples, client.datasets, member_field='temperature')
records = client.submit(requests, label='scan')
```

A lookup fills further blanks per dataset. `LastBefore(selector)` takes the matching dataset with the highest run number below the filled dataset's.
Each dataset fills the one blank the lookup leaves.

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('run', 'can')), samples, client.datasets, lookup=cans)
```

## Rules

A rule is plain data: a template, a selector, a label, and optionally a lookup or a series.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(role='sample'),
            label='iofq')
```

With `series='sample'`, a request takes every matching dataset with the same `sample` so far, in run order, and `sample` is its member.
Each arrival of an angle then stitches all angles of that sample so far; a stitch is one request over all angles so far, not an accumulation.

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
```

Templates and rules are frozen dataclasses; `dataclasses.replace` changes them.
The core keeps no store of them, and records do not name them: a record's request says everything that determines its result.

## The trigger loop

The trigger loop is the driver for rules. It runs in a driving server or a notebook, never in the backend.
It finds new datasets through `client.datasets`.

```python
loop = TriggerLoop(client, rules=[rule])
loop.step()                  # submits what arrived since the last step, returns the records
loop.status(rule).reason     # why the rule submitted nothing or skipped a dataset
loop.run()                   # steps forever
```

The loop keeps no memory of its own. A rule has handled a dataset when a record under the rule's label names it, so a restarted or replaced loop does not reduce a dataset again, and a dataset whose file arrives again keeps its identity and is not reduced twice.

The label belongs to the rule: any record under it counts, whoever submitted it and whether or not it failed.
A record that a notebook submits under a rule's label stops the rule from reducing the datasets it names, so manual work uses labels of its own.
A failed record counts so that the loop does not retry a failed dataset, which could fail again at every step; running it again is the user's decision (D2).

A dataset whose request cannot be made, such as a sample with no can measured before it, is skipped: the rule submits the other new datasets, and its status names the skipped one and why.
No record names the skipped dataset, so the next step tries it again, for example once a can whose file arrived late is listed.
A rule whose requests are refused, such as for an unknown spec version, submits nothing.
`apply` raises instead of skipping, since a user who builds a batch wants to see the error.

## Open

- Adding, replacing, and listing rules in a running driving server.
- A can measured after the sample, and other lookups than `LastBefore`.
- A rule that pushes into an accumulator, for a sum that grows with each dataset (D7 as a rule); the loop's client keeps the accumulator.
