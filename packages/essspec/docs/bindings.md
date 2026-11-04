# Writing a binding

A *binding* is what the backend calls to compute the requests of a spec ([params.md](params.md)).
A workflow package writes one binding per spec, from a function, a sciline pipeline, or a driver:

```python
from ess.spec import DriverBinding, function_binding
from ess.spec.pipeline import PipelineBinding    # needs the extra [sciline]

export = function_binding(EXPORT, export_csv)
single = PipelineBinding(IOFQ, make_pipeline, keys={'sample': (Filename[SampleRun],)})
multi = DriverBinding(IOFQ_MULTI, IofQDriver)
```

Each binding serves some of a client's actions.
Making a stage or an accumulator over a binding that does not serve it is refused:

| Binding | Plain request | Stage | Accumulator |
|---|---|---|---|
| `function_binding` | `compute` | refused | refused |
| `PipelineBinding` | `compute` | `make_stage` | refused |
| `DriverBinding` | `compute` | refused | `make_accumulator` |

[roles/workflow-authors.ipynb](../../../docs/developer/roles/workflow-authors.ipynb) walks through a spec, its bindings, and a driver on a running example, [roles/toy_sans.py](../../../docs/developer/roles/toy_sans.py).
[accumulators.md](../../../docs/developer/accumulators.md) describes stages and accumulators from the client's side.

**In short:**

- A binding gets values, never a reference or a file name: each group as its model, each file as its local path, each table as the list of its rows, every default filled in.
- A binding is pure: it modifies no value it gets, returns no output that shares memory with one, and gives the same outputs for the same values. The backend may call it from several threads at once.
- Defaults belong to the spec. A pipeline holds no value at a key that a parameter's key leads to, since the parameter would then have no effect.
- In a `PipelineBinding` each parameter sets the sciline key of its class. A parameter whose class names no role, such as a file or an `int`, gives its key with `keys=`. A parameter typed `X | None` sets no key.
- A driver gives the outputs of the plain request over the rows pushed so far, however the rows were grouped into pushes and whatever the order of pushes into different tables.
- `ess.spec.testing` checks these promises in a package's tests.

## What a binding gets

A plain request calls `compute(params)` with an instance of the spec's params model.
A stage or an accumulator is made from some values only, so `make_stage` and `make_accumulator` get the values by name, and the names of the *blanks*, the parameters left for later calls or pushes:

```python
binding.make_stage(
    fixed={'reduction': Reduction(bins=2), 'beam_centre': centre,
           'sample': Path('run-611.nxs'), 'can': Path('run-614.nxs')},
    blanks=('transmission',),
)
```

Every value is of its field's type, whether the request gave it inline or by reference:

| Field | The binding gets |
|---|---|
| a group, `Reduction` | an instance of `Reduction` |
| `Referenced[T]` | the `T` that the referenced record computed |
| a file, `SampleRunFile` | the file's local path, a `Path` |
| a table, `list[SampleRunFile]` | the list of its rows, each a `Path` |

The backend reads the data of each reference, and resolves each file, before it calls the binding.
`compute` gets every parameter, with the defaults of the model filled in, and so do `make_pipeline` and `make_driver` for the fixed values.
The params model's model validators have not run; a binding may run them ([params.md, When values are validated](params.md#when-values-are-validated)).

A binding returns an instance of the outputs model.
An output that the spec declares optional, `X | None`, is left out by being `None`.

## A function

`function_binding` serves plain requests only.
It fits drafts in notebooks, and workflows that hold nothing worth keeping between calls:

```python
def iofq(params: IofQParams) -> IofQOutputs:
    ...  # params.reduction.bins; params.sample is a local path
    return IofQOutputs(iofq=IofQ(intensity=result))

draft = function_binding(IOFQ, iofq)
```

It serves no stage, since a stage over a function would hold nothing.

## A pipeline

`PipelineBinding(spec, make_pipeline, keys=...)` serves plain requests and stages over a sciline pipeline.
`make_pipeline` makes the pipeline from the fixed values, by name, with defaults filled in, so that the graph may depend on them.
The binding sets each value at its key and computes each output at its key.

### Parameters and keys

Each parameter sets the key of its type, without `Annotated` metadata:

| Parameter | Key it sets |
|---|---|
| `reduction: Reduction` | `Reduction` |
| `transmission: Referenced[ComputedTransmission]` | `ComputedTransmission` |
| `runs: list[Run]` | `list[Run]`, to the whole list |
| `beam_centre: Annotated[GivenCentre \| FindCentre, ...]` | `GivenCentre` or `FindCentre`, the member the value is (see Unions) |
| `centre: BeamCentre \| Referenced[BeamCentre]` | `BeamCentre`: the members are of one class |
| `sample: SampleRunFile` | none of its own: needs `keys=` |
| `bins: int` | none of its own: needs `keys=` |
| `direct_beam: RunFile \| None` | none: needs `keys={'direct_beam': ()}` (see `None`) |

Each output is computed at the key of its type without `| None`, such as `IofQ` for `iofq: IofQ`.

A parameter whose type, or element type for a list, is neither a group nor a union of groups needs `keys=`, since its class names no role.
The binding refuses it without one: no provider would read a key such as `int` or `Path`, and the value would have no effect.
A file sets the workflow's own key of the file's path, so that a provider takes a plain path, as esssans' providers take `Filename[SampleRun]`:

```python
class IofQParams(BaseModel):
    sample: SampleRunFile                         # Annotated[Path, File(role='sample')]
    can: BackgroundRunFile

PipelineBinding(IOFQ, make_pipeline,
                keys={'sample': (Filename[SampleRun],), 'can': (Filename[BackgroundRun],)})
```

### `keys=`

`keys=` gives a tuple of keys for a parameter or an output, by name:

| Value | For |
|---|---|
| `(K,)` | a parameter or an output |
| `(K1, K2, ...)` | a union of groups: a key for each member, in the order of the union |
| `()` | a parameter that sets no key |

It names keys of the workflow's own, such as the key at which the graph computes an output:

```python
PipelineBinding(IOFQ, make_pipeline, keys={'iofq': (BackgroundSubtractedIofQ,)})
```

`binding.keys` gives the keys of every parameter and output, with those of `keys=` and those from the types.

No two fields, parameters or outputs, share a key, over all members of a union.
An output computed at a parameter's key would be the value that the binding was given, and an output must not share memory with one.
Two parameters of one class each get a key with `keys=`, or a class of their own, `class Can(Run): ...`.
An output that reports the beam centre given or found has a key of its own, such as `UsedCentre`, which a provider computes from `GivenCentre`.

### Groups and adapters

A group is one key, so a provider takes it apart.
For a value in another shape, `make_pipeline` inserts an adapter provider, such as one that makes the output group `IofQ` from the workflow's array:

```python
def iofq(intensity: BackgroundSubtractedIofQ) -> IofQ:
    return IofQ(intensity=intensity)

def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sans_workflow()
    pipeline.insert(iofq)
    return pipeline
```

Files that vary independently are separate parameters, not fields of one group.
The spec names them by role, `sample` and `can`, not after the workflow's run types.
A stage fixes or blanks whole parameters, and `sciline.Stage` computes again on each call everything that depends on the key of a blank.
A blank group of sample, can, and direct beam would compute the can's branch again on every call, though only the sample changes.
A group of files fits files that always change together; a provider in `make_pipeline` takes it apart into the workflow's keys.

### Unions

A union of groups has a key for each member, the group as declared, in the order of the union.
A value sets the key of the member nearest to its class: the first class of `type(value).__mro__` that is a member.
Here one graph serves both alternatives, since `find_centre` leads from one alternative's key to the other's:

```python
class IofQParams(BaseModel):
    sample: Sample
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] = GivenCentre()

def find_centre(settings: FindCentre, data: SampleData) -> GivenCentre: ...

binding = PipelineBinding(IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq]))
binding.keys['beam_centre']  # (GivenCentre, FindCentre)
# a GivenCentre: pipeline[GivenCentre] = value, which replaces find_centre
# a FindCentre:  pipeline[FindCentre] = value, and find_centre computes GivenCentre
```

Where the members need different providers for one key, `make_pipeline` branches on the fixed value.
That always works, since a union is never a blank.

### `None`

A parameter typed `X | None` sets no key, and `make_pipeline` says what `None` means.
Each value of a union sets one key, but `None` has no key to set.
Setting `X` to `None` would replace a provider of `X`.
Setting nothing would leave the meaning of `None` to whatever the pipeline holds or computes at `X`.
So `keys=` gives such a parameter no key, `()`:

```python
def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = sciline.Pipeline(providers)
    if fixed['direct_beam'] is None:
        pipeline[sans.DirectBeam] = None          # esssans' providers take None
    else:
        pipeline[sans.DirectBeamFilename] = fixed['direct_beam']
    return pipeline

PipelineBinding(SPEC, make_pipeline, keys={'direct_beam': ()})
```

The same holds for a union typed `U | None`.

An optional output, `X | None`, means that the graph may lack it, not that a provider returns `None`.
It is computed if the pipeline has a value or a provider for its key, and is `None` otherwise.

### Defaults and values held in a pipeline

Defaults belong to the spec.
`make_pipeline` holds no value at a key that a parameter's key leads to: the value replaces the provider that would read the parameter, and the request's value has no effect.
The binding cannot check this, since the provider is gone from the graph.
`make_pipeline` replaces such a value with a provider, `pipeline.insert(...)`, such as an adapter from the spec's group to the workflow's key.

A value held at the key of a parameter itself is harmless, since the binding sets the request's value over it (but see Edge cases for unions).

### Purity and copying

The author of the pipeline promises that it is pure: it modifies no value it is given, and returns the same results for the same values.

`make_pipeline` may return a pipeline that it holds, since the binding copies the pipeline before it sets values in it.
`make_pipeline` itself sets no value in a pipeline that it holds: a value set for one request would stay for the next.

### Stages

A stage cuts the pipeline of the fixed values at the keys of the blanks.
It computes what does not depend on the blanks on its first call, and holds it for later calls.
A plain request is a stage with no blanks, called once.

`make_pipeline` gets no blank, so a field that changes the graph cannot be a blank.
A parameter that sets no key cannot be a blank, nor can a union of groups, since its key may depend on its value.
`make_stage` raises `ValueError` for either.

## A driver

A *driver* is the object behind an accumulator: tables are pushed into it, and it gives the outputs over the rows so far.
The workflow package writes it, for one spec, and it holds everything an accumulator keeps.
The framework never decides how rows combine, not even by keeping them; only a driver accumulates.
A driver is a sciline driver in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245):

```python
class Driver(Protocol):
    def push(self, table: str, rows: Sequence[Any]) -> None: ...
    def outputs(self) -> Outputs: ...
```

`DriverBinding(spec, make_driver)` serves plain requests and accumulators over a spec with tables.
`make_driver` makes a driver from the fixed values that are not tables, by name.
An accumulator gets a driver of its own.
The binding pushes the rows of each table in the fixed values, such as a can run fixed by the client's template, one push per table, and skips a table without rows.
Each push of the client then calls `push` once per table, with its rows, and each state read calls `outputs`.
A plain request is an accumulator with no blanks, read once: it too computes with a driver from `make_driver`.

### What a driver promises

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a value it is given. A driver that adds into the first contribution copies it first, so that adding never changes a value that another record returned.
- An output that the spec declares optional may be `None`.
- A driver may refuse a state, such as one whose runs it cannot combine yet, by raising in `outputs()`. Later pushes may make the state valid.

A push that raises stops the accumulator, since the driver may hold part of its rows.

### What the caller promises

- It calls `push` and `outputs` from one thread at a time.
- It pushes only once it is done with the outputs of the state before, so `outputs()` may return arrays that share memory with what the driver holds.
- A push has at least one row. A push may carry several rows, so that the driver can work on them together, such as by loading their files in parallel.

### Example

A driver for a toy SANS reduction sums each run's numerator and denominator per table, and divides once:

```python
class IofQDriver:
    def __init__(self, fixed):                    # the fixed values that are not tables
        self._bins = fixed['reduction'].bins
        self._sums = {}                           # per table: numerator and denominator

    def push(self, table, rows):
        for row in rows:                          # the local path of a sample or a can run
            parts = numerator_and_denominator(row, self._bins)
            old = self._sums.get(table)
            self._sums[table] = parts if old is None else [a + b for a, b in zip(old, parts)]

    def outputs(self):                            # a state read has a sample row and a can row
        sample_num, sample_den = self._sums['sample']
        can_num, can_den = self._sums['can']
        return IofQOutputs(iofq=IofQ(intensity=sample_num / sample_den - can_num / can_den))

multi = DriverBinding(IOFQ_MULTI, IofQDriver)
```

`a + b` makes a new sum, so no value the driver was given is modified.
Summing numerators and denominators apart, and dividing once, is what makes a sum of runs correct; a sum of normalized curves would not be.

A workflow that needs every row at once, such as a stitch of runs, writes a driver that keeps its rows and computes over all of them at each read.

### Reusing the single-run pipeline

A driver may compute each row with the pipeline of the single-run binding.
`single.pipeline(fixed)` gives the graph that a stage of `single` cuts, and `single.keys[table]` the key at which a row goes in:

```python
class IofQDriver:
    def __init__(self, fixed):
        workflow = single.pipeline(fixed)
        self._contribute = {
            table: sciline.Stage(workflow, inputs=single.keys[table],
                                 outputs=(Numerator[run], Denominator[run]))
            for table, run in (('sample', SampleRun), ('can', BackgroundRun))
        }
        ...
```

[roles/toy_sans.py](../../../docs/developer/roles/toy_sans.py) has the whole driver.
A driver passes the fixed values that it is made with to `pipeline`, so a table of the multi-run spec cannot shape the graph, as a blank cannot.
A table whose single-run parameter is a union of several classes has a key per member, so a driver cannot cut the graph at one key for its rows.
A table of `X | Referenced[X]` has one key.

### Calling a driver by hand

Code that calls a driver itself keeps the order of the backend, and is done with the outputs of a state before it pushes:

```python
driver = binding.make_accumulator(fixed, blanks=('runs',))
for run in runs:                                  # each the local path of a run
    driver.push('runs', [run])
    counts = driver.outputs().volume.counts       # may be the next push's array
    show(counts['q', 0])                          # done with it before the next push
```

A driver needs no spec, backend, or client, so a package uses and tests it as plain code.

## Threading

The backend may make and call bindings from several threads at once:

| Call | From several threads at once |
|---|---|
| `compute` | yes |
| `make_stage`, and the callable it returns | yes |
| `make_accumulator`, and `make_driver` of a `DriverBinding` | yes |
| `push` and `outputs` of one driver | no: one thread at a time |

A callable of a stage may return the same object in several calls, so records made through one stage share what does not depend on the blanks.
Since no caller modifies a value, sharing is safe.

## Testing the symmetries

`ess.spec.testing` checks the symmetries that a workflow package keeps ([accumulators.md, Symmetries](../../../docs/developer/accumulators.md#symmetries)).
Each check computes the same outputs in two ways and compares them, so it needs no known result:

```python
from ess.spec.testing import check_arrival_and_order, check_caching, check_one_row

settings = {'reduction': Reduction(bins=2), 'beam_centre': BeamCentre(),
            'transmission': TransmissionFraction(value=0.8)}
check_one_row(single, multi, IofQParams(**settings, sample=run_101, can=run_110))
check_caching(single, {'transmission': settings['transmission'], 'sample': run_101, 'can': run_110},
              [{'reduction': Reduction(bins=n)} for n in (2, 1, 2)])
check_arrival_and_order(IofQDriver, settings, {'sample': [run_101, run_102], 'can': [run_110]})
```

| Check | Symmetry | Compares |
|---|---|---|
| `check_one_row` | one-row | the multi-run binding with one row per table, and the single-run binding |
| `check_caching` | caching | each call through one stage, and the plain request with the same values |
| `check_arrival_and_order` | arrival and order | a driver under several push schedules, and a driver with one push per table |

Values are those that the binding gets: data read, files as local paths.
Outputs are compared up to `rtol`, since adding in another order rounds differently.
`check_one_row` and `check_caching` also check that no output shares memory with a value given, for values backed by numpy.

Repeat a call after others in `check_caching`, as bins 2, 1, and 2 again above, to find a stage whose calls change what it holds.
`check_arrival_and_order` compares the driver with itself, so it does not prove the outputs right.
Only `check_one_row`, against the single-run binding, compares with an independent reference.

## Edge cases

**Subclasses in a union.** An instance of a subclass of a member sets that member's key, such as an output of a subclass of `T` at `Referenced[T]`.

**A union of one class.** A union whose members are of one class is that class to the binding, with one key, and not a union, so it may be a blank:

```python
centre: BeamCentre | Referenced[BeamCentre]       # keys: (BeamCentre,)
beam_centre: (
    Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    | Referenced[ComputedCentre]
)                                                 # keys: (GivenCentre, FindCentre, ComputedCentre)
```

**One key for all members.** A union's own `keys=` may give its members one key, `(K, K)`: the value of either member is then set at `K`.
The union is still not a blank, since `make_pipeline` may branch on its value.

**A union in a list.** A union in a list, such as a table, sets no key of its own: the list's key holds it, and a provider tells its rows apart.

**A value held at a key of a union.** A union whose members have keys of their own is one where a held value does harm: one member sets the key, the others do not.
If the pipeline of the beam-centre example held a default `GivenCentre`, the tag `'find'` would set `FindCentre`, but `iofq` would read the held default, and `find_centre` would never run.
The binding cannot tell which key leads to which, since the held value has replaced the provider that would show it.
So `pipeline` refuses a value held at any key of such a union.

**Outputs at a shared type.** An output that a provider returns as `X | None` needs `keys=`, since the binding computes the key `X`.
An output typed `sc.DataArray` is computed at the key `sc.DataArray`, so two such outputs need `keys=`.
