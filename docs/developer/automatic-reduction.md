# Batch and automatic reduction

This sub-design builds on the core of [README.md](README.md).
From the core it needs only that records show their label and member, and `client.files`.
Its stories are S7, C1, D1 to D6, and E1 to E4 in [user-stories.md](user-stories.md).

## Batches

`apply` fills a template for each file and returns the requests keyed by member, the value of a metadata field (by default the run number).
It reads metadata through `client.files` and builds plain data; it submits nothing.
The examples reduce samples with `IOFQ`, whose parameters `sample` and `transmission` are file fields, and find each sample's beam centre with `BEAM_CENTRE`.

```python
template = Template(IOFQ, params={'can': file(run=60393), ...}, blanks=('sample',))
requests = apply(template, samples, client.files, at='sample', member_field='sample_name')
records = client.submit(requests, label='batch', persist=True)
```

Each file fills the template's one blank, `at`. `apply` refuses two files with one member, since one would replace the other.
A batch whose parameters differ per file, such as the transmission run of each sample, is a dict of requests, each with its own params:

```python
requests = {row['name']: template.fill(params={'sample': file(run=row['sample']),
                                               'transmission': file(run=row['transmission'])})
            for row in table}
```

A batch application submits with `persist=`, so its client keeps nothing, and the results are read later from the store ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).

## Rules

The framework knows no technique: which runs belong together, and which requests one new file needs, is for the application to say.
A rule says it with a function:

```python
def reduce_sample(client: Client, sample: FileRef) -> Record:
    fields = client.files.metadata(sample)
    found = client.files.list(Selector(run_type='trans', run_label=fields['run_label']))
    if not found:
        raise LookupError('no transmission run yet')          # try again at the next step
    member = str(fields['run'])
    try:
        centre = client.latest('centre', member=member)        # from a call that stopped half-way
    except KeyError:
        centre = client.submit(BEAM_CENTRE, params={'sample': sample, ...},
                               label='centre', member=member, persist=True)
    return client.submit(IOFQ, params={'sample': sample, 'transmission': found[-1],
                                       'beam_centre': centre.ref('centre'), ...},
                         label='iofq', member=member, persist=True)

rule = Rule('iofq', reduce_sample, selector=Selector(run_type='sans'), label='iofq', at='sample')
```

`run_type` and `run_label` stand in for what ESS files will say about a run, which is not settled ([data](../requirements/data.md)). A selector takes any metadata field, so only the rule's code and the file source name them.

The function returns the record that *settles* the file: one under the rule's label that holds the file at the parameter `at`, or in a row of it if `at` is a list.
The loop checks this and raises `TypeError` otherwise, since it would call the function for that file again at every step.

The function persists what it submits. The loop's client should keep nothing, and a reference must stay readable once the loop's process has restarted.

A function may run again for a file: when a step stopped before the settling record, or when that record was refused. So it submits the settling record last, and reuses what an earlier call submitted, as with `client.latest` above.

Rules are frozen dataclasses. The core keeps no store of them, and records do not name them: a record's request says everything that determines its result.

## The trigger loop

The trigger loop is the controller for rules. It runs in a server of its own or in a notebook, never in the backend.
It finds new files through `client.files`, and calls each rule's function for each of them.

```python
loop = TriggerLoop(client, rules=[rule])
loop.step()                  # calls the rules for what arrived since the last step, returns the settling records
loop.status(rule).reason     # why the rule submitted nothing or skipped a file
loop.run()                   # steps forever
```

The loop keeps nothing that the records do not hold: it reads them from `client.changes`, from the start when it starts, and keeps the files it has seen. A rule has handled a file when a record under the rule's label holds it at the parameter `at`, or in a row of it, so a restarted or replaced loop does not reduce a file again, and a file that arrives again keeps its identity and is not reduced twice.
A file that the record stores at another field, such as an empty-beam run, does not count.
This holds while the proposal's history is kept. A running loop keeps it, since a proposal with an open client is not idle ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

A record can settle more files than the one it was submitted for. A function that stitches all angles of a sample so far settles every new angle of that sample in one call:

```python
def stitch(client: Client, run: FileRef) -> Record:
    sample = client.files.metadata(run)['sample']
    angles = client.files.list(Selector(role='sample', sample=sample))
    angles.sort(key=lambda ref: client.files.metadata(ref)['run'])
    return client.submit(template.fill(params={'runs': angles}),
                         label='reflectivity', member=sample, persist=True)
```

The label belongs to the rule: any record under it counts, whoever submitted it and whether or not it failed.
A record that a notebook submits under a rule's label stops the rule from reducing the files it names, so manual work uses labels of its own.
A failed record counts so that the loop does not retry a failed file, which could fail again at every step; running it again is the user's decision (D2).

A file whose function raises `LookupError` or `ValueError`, or whose request is refused with `SubmitError`, is skipped: the rule goes on with its other new files, and its status names each skipped file and why.
No record names the skipped file, so the next step tries it again, for example once its transmission run has arrived.
Any other exception stops the loop.

## Open

- Adding, replacing, and listing rules in a running trigger loop.
- A rule that pushes into an accumulator, for a sum that grows with each file (D7 as a rule). The loop's client would keep the accumulator, the only thing it keeps, and freeze it with `persist=` once the sum is complete.
