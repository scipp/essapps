# Batch and automatic reduction

This sub-design builds on the core of [README.md](README.md).
From the core it needs only that records show their label and member, and a dataset source.
Its stories are S7, C1, D1 to D6, and E1 to E4 in [user-stories.md](user-stories.md).

## Batches

`apply` fills a template for each dataset and returns the requests keyed by member, the value of a metadata field (by default the run number).
It builds plain data and needs no client.

```python
requests = apply(Template(IOFQ, blanks=('run',)), samples, datasets, member_field='temperature')
records = client.submit(requests, label='scan')
```

A lookup fills further blanks per dataset. `LastBefore(selector)` takes the matching dataset with the highest run number below the filled dataset's.
Each dataset fills the one blank the lookup leaves.

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('run', 'can')), samples, datasets, lookup=cans)
```

## Rules

A rule is plain data: a template, a selector, a label, and optionally a lookup or a series.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(role='sample'),
            label='iofq')
```

With `series='sample'`, a request takes every matching dataset with the same `sample` so far, in run order, and `sample` is its member.
Each arrival of an angle then stitches all angles of that sample so far; a stitch is a spec over a list, not an accumulation.

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
```

Templates and rules are frozen dataclasses that serialize to JSON; `dataclasses.replace` changes them.
The core keeps no store of them, and records do not name them: a record's request says everything that determines its result.

## The trigger loop

The trigger loop is the driver for rules. It runs in a driving server or a notebook, never in the backend.

```python
loop = TriggerLoop(client, datasets, rules=[rule])
loop.step()                  # submits what arrived since the last step, returns the records
loop.status(rule).reason     # why the rule submitted nothing, such as an unknown spec version
loop.run()                   # steps forever
```

The loop keeps no memory of its own. A rule has handled a dataset when a record under the rule's label names it, so a restarted or replaced loop does not reduce a dataset again, and a dataset whose file arrives again keeps its identity and is not reduced twice.
A dataset whose record failed counts as handled; running it again is the user's decision (D2, D5).

## Open

- Adding, replacing, and listing rules in a running driving server.
- A can measured after the sample, and other lookups than `LastBefore`.
- A rule that pushes into an accumulator, for a sum that grows with each dataset (D7 as a rule); it needs a session owned by the loop (README open question 2).
