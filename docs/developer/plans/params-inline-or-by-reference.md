# A request gives each parameter inline or by reference

**Status: implemented** on the branch `69-params-and-inputs` (issue #69, PR #71).

It rewrites [ADR 0001](../../../packages/essspec/docs/developer/adr/0001-minimal-workflow-spec.md) (essspec) in place, since that ADR decides what a spec consists of, and the README sections from Specs and bindings to Stages and accumulators.
[binding-jobs.md](binding-jobs.md) describes how a binding is called; with this note, a binding gets one model instance.
Terms of binding-jobs.md used here: a *stage* is a template that the backend keeps for a client and calls with values for its blanks; an *accumulator* is a template whose blanks are tables, filled by *pushes*; a *driver* is the object, written by the workflow package, that an accumulator pushes into; a *pin* takes the state of an accumulator after the pushes so far.

## In short

A spec declares two models:

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | everything the workflow takes: settings, files, data | each value given inline or by reference | the values, and the data the references name |
| outputs | results | (none) | the data the workflow returns |

A request gives each value in one of two forms: *inline* or *by reference*.
An *inline value* is the value itself in the request; the record holds its JSON form.
A *reference* names data that exist elsewhere: a dataset, an output of a record, or an output of an accumulator.
Whether a parameter is given inline or by reference is the request's choice, per value.
The parameter's type decides which of the two it accepts:

| A parameter's type | Example | Inline | By reference |
|---|---|---|---|
| a file: `Path` or a `NewType` of it | `SampleFile` | no | to a dataset |
| has a JSON form | `Reduction`, `BeamCentre`, `TimeRange` | yes | to an output that fits |
| anything else | `Transmission = NewType('Transmission', sc.DataArray)` | no | to an output that fits |

`None` is a value of an optional parameter of any type.
The backend reads each reference and builds the params instance from the values and the data, so every field of it holds the type it declares.
The binding gets that instance: `compute(params) -> outputs`.

```python
client.submit(IOFQ, params={'sample': dataset(run=60339),
                            'beam_centre': BeamCentre(x=0.012, y=-0.003)})   # inline
client.submit(IOFQ, params={'sample': dataset(run=60339),
                            'beam_centre': finder.ref('centre')})            # a reference
```

## Terms

| Term | Meaning |
|---|---|
| parameter | A field of the params model. Its *name* is the field's name. |
| JSON form | A type has a JSON form when it is not a file, holds no file, and pydantic makes a JSON schema of it with the params model's config. |
| inline value | The value itself in the request. The record holds its JSON form. A request gives each value in one of two forms: inline, or by reference. |
| round-trips | A value round-trips when its JSON form reads back as the value. |
| reference | Names data that exist elsewhere: a dataset, an output of a record, or an output of an accumulator. |
| group | A pydantic model with a JSON form, such as `Reduction` or the edges models of `ess.spec.parameters`. The usual type of a parameter given inline. |
| data value | A value of a type without a JSON form: a file, or an object such as a scipp array. Given by reference only. |
| table, index, column, row | A *table* is a set of list parameters of equal length, pushed together. Its *index* is the parameter that names it, such as the run; its other *columns* are attached to the index, such as a correction of each run. Each position is a *row*. As in a pandas DataFrame, the index values of a table are distinct. |
| blank | A parameter, or a table (named by its index), that a template leaves open. |
| fits | A reference fits a parameter when the type of the data it names can be the parameter's value (see Validation). |
| request phase, call phase | The *request phase* is what the client and the backend do with a request before any data is read: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |

## Why

**The form of a value belongs to the request, not to the spec.**
The same value is typed in by one user and computed by another:

- A beam centre is computed by a first reduction and feeds the sample reductions ([users.md](../../requirements/users.md), [sans.md](../../requirements/sans.md)); esslivedata takes it typed in, as `BeamCenterXY` in LoKI's params.
- "A technique that needs this runs a first reduction to find the values" ([README](../../requirements/README.md#non-goals)): time slices found from a log feed the `time_range` table of a slicing accumulator.
- A sample thickness "comes with the runs' data or from a table a person fills" ([sans.md](../../requirements/sans.md)).

A review of 112 values that users or other workflows supply to the LoKI, Amor, ESTIA, FREIA, DREAM, BIFROST, and ODIN workflows found 7 that are sometimes typed in and sometimes computed (beam centre, I0, angle scale factor, FREIA surface normal, proton charge, lattice, disk choppers), and 6 choices that must match those of an earlier reduction (Amor's wavelength bins, pixel limits, and supermirror constants, which the Amor GUI copies from the reference run's row today).
A spec that fixes the form per field puts each of these on one side, and one of the two uses then needs a workaround: the client reads the value and sends it again, which loses the record of where it came from, or a spec per form.

**The two phases still hold, per value.**
In the request phase a value given by reference is a reference; in the call phase it is the data.
The params model types the call phase only.
The request holds each value inline or as a reference, and the framework tells the two apart by their JSON form (see References), without the spec.

**The bindings already take one mapping.**
`make_stage`, `make_accumulator`, `Driver.push`, and `PipelineBinding` take values by name; a binding does not see which form a value arrived in.

## The example

The spec side, which needs no workflow package:

```python
class Reduction(BaseModel):                       # a group
    bins: int = 2

class BeamCentre(BaseModel):                      # a group: inline, or a finder's output
    x: float
    y: float

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)
IofQ = NewType('IofQ', sc.DataArray)

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: SampleFile
    can: CanFile | None = None
    beam_centre: BeamCentre | None = None

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, ..., params=IofQParams, outputs=IofQOutputs)
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))

class CentreParams(BaseModel):
    sample: SampleFile

class CentreOutputs(BaseModel):
    centre: BeamCentre

FIND_CENTRE = WorkflowSpec(name='sans-beam-centre', version=1, ...,
                           params=CentreParams, outputs=CentreOutputs)
```

The workflow computes a beam centre as a scipp vector; the bindings of both specs convert between it and `BeamCentre` with a provider, as they convert bin edges.

## The design

### Rules for the model

Checked when the spec is made:

- The names of params and outputs are distinct.
- Each parameter's type is a type, possibly optional, or a list of one, as on the branch; not a dict or a union of two types.
- Which forms a parameter takes follows from its type, with `| None` removed and, for a list parameter, its element type taken, tested in this order:

  ```
  file:      its NewType chain reaches Path              -> a reference to a dataset
  JSON form: pydantic makes a JSON schema of it, with the
             params model's config, and it holds no file -> an inline value, or a reference to an output that fits
  otherwise                                              -> a reference to an output that fits
  ```

  A group holds no file: a file is given by reference to a dataset only, so that the dataset source checks access and the record holds the dataset's identity.
- A default is a value, never a reference: of a type with a JSON form, a value that round-trips and whose JSON form is neither an `@ref` object nor a list holding one; of any other type, `None` or `[]`.
- The params model has no field validators, serializers, computed fields, or aliases, and no `default_factory` that takes the other values. A list parameter carries only length constraints, such as `Field(min_length=2)`. The params model may have model validators, but the framework never calls them.

No parameter sees another: each parameter is validated alone, in every phase, since a stage and a driver never get a whole params instance. A check of one value belongs to the value's type, as `EdgesModel` checks its edges, and a check of one row to the element type.
A check across parameters or rows is the binding's or the driver's, and fails when the request runs; in a sciline workflow, a missing parameter fails when the pipeline is built. The params model may state such a check as a model validator, next to the fields it reads. A binding may run it, `IofQParams.model_validate(dict(params))`, where it has the whole instance. `tabulate` does not carry it over, since its fields become lists.

Two parameters may share a type. A `PipelineBinding` sets each parameter under its type, so it refuses two with one type unless `keys=` names a key for one; a spec with two axes of a Q map may give each a `NewType`, or map them with `keys=`.

### References

A reference stands for the value of a parameter, or for one row of a list parameter: never for a whole list, and never for part of a group.
Its JSON form is an object with the key `"@ref"`, which names its kind:

```json
{"@ref": "dataset", "dataset": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "centre"}
{"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
{"@ref": "accumulator", "accumulator": "a3", "output": "iofq"}
```

The framework looks for references at two places only: the top of each value, and the top of each element of a value that is a JSON array.
At these places it refuses an `@ref` object that the spec does not allow as a reference: an element of a parameter that is not a list, such as a `tuple` of groups, whose JSON is an array; or the JSON form of an inline value, such as a group with extra fields that holds the key `@ref`.
Deeper inside a value, `@ref` is plain data.
So the framework finds every reference of a request, a record, or the log without the spec, as a remote client that holds only the serialized spec must, and an inline value never reads as a reference.
`@ref` is not a JSON Schema keyword, so an example in a schema that holds a reference stays plain data.

### Requests and records

A request is a spec and its params by name, each an inline value or a reference, or a list of these for a list parameter:

```python
record = client.submit(IOFQ, params={'reduction': Reduction(bins=4),
                                     'sample': dataset(run=60339),
                                     'beam_centre': finder.ref('centre')})
```

`params` is a dict by name, or an instance of the params model if every value is given inline, as for esslivedata's specs; a spec with a file takes a dict.
It may be left out when every parameter has a default.
A file is always given by reference, `dataset(path=...)` for a file named by its path: a string where a file is expected is refused.
`record.request.params` holds the values in their JSON form, with defaults filled in; `Request.upstream()`, `accumulators()`, and `datasets()` list its references.
`record.ref(output, key=None)` names an output, or one element of it: by index for a list output, `key='2'`, and by key for a dict output. A key that the output lacks fails in the call phase.
A malformed reference is refused with a `SubmitError`, as any other request that cannot run.

### Templates, stages, and accumulators

A template is a spec, params, and blanks:

```python
tune = client.stage(Template(IOFQ, params={'sample': dataset(run=60339)}, blanks=('reduction',)))
client.compute(tune, params={'reduction': Reduction(bins=50)})

batch = Template(IOFQ, params={'can': dataset(run=60340)}, blanks=('sample',))
requests = apply(batch, samples, client.datasets)            # each dataset fills 'sample'

acc = client.accumulator(Template(IOFQ_MULTI, params={'reduction': Reduction(bins=100)},
                                  blanks=('sample', 'can')))
acc.push(sample=[dataset(run=611), dataset(run=612)])
```

### Multi-run specs

`tabulate` is as before, with one model: each field named becomes a `list`; an index loses `| None`, and a required index gets at least one row; an attached column keeps its type as written.

```python
SANS_MULTI = SANS.tabulate(name='sans-multi', version=1,
                           tables={'sample': ('sample_geometry',), 'can': ()})
SLICES = SANS.tabulate(name='sans-slices', version=1, tables=('time_range',))
```

A push gives the index and its columns by name, as lists of equal length; each row is given inline or by reference, as its type allows:

```python
acc.push(sample=[dataset(run=613)], sample_geometry=[SampleGeometry(thickness=0.2)])
acc.push(time_range=[TimeRange(start=0, stop=60)])
acc.push(time_range=[slicer.ref('slices', key='0'), slicer.ref('slices', key='1')])
```

The other rules of the tables are as before: a push names one table, through its index; a column left out is `None` in each row if its element type is optional.

**Distinct index values.**
The index values of a table are distinct, compared in two ways:

- *As the record holds them*, in the request phase: an inline value by its JSON form, a dataset by the identity the dataset source gives it, an output by record, output, and key. This refuses a push, a request, or a template early, as on the branch.
- *By the values read*, in the call phase, for an index whose type has a JSON form: by the JSON form of each value read. The backend keeps, for each blank table of an accumulator, the JSON forms of the index values read so far, and compares each push's rows with them, inline values and references alike, before the driver gets the rows; a table that a request, a template, or a stage call gives whole is compared within itself, when it is read.

A repeat found in the call phase fails the push, which stops the accumulator, as any push whose data fail does.
So `TimeRange(start=0, stop=60)` and a reference to an output whose value is that range are one index value, and a slice is never summed twice.
Accumulators and stages do not survive a restart of the backend, so the values read need not be rebuilt from the log.

### Validation

The request phase checks what can be checked without data.
The call phase checks each value read by its field once, when it is read.
The *table rules* are: the columns of a table have equal length, the number of rows meets the index's length constraints (a required index has a row), and the index values are distinct.

| What | Request phase | Call phase |
|---|---|---|
| a plain request | names and required parameters; each inline value by its field, or its element type for a row, from its JSON form; each reference readable and fitting its field; the table rules | each value read by its field or element type; the index values read distinct |
| a template | the same, for the values it fixes, and the table rules on fixed tables | each fixed value read, once, when the stage or the driver is made; the index values of fixed tables read distinct |
| a stage call | the same, for the values given; the table rules on given tables | each given value read; the index values of given tables read distinct |
| a push | each row by its column's element type, as for a request; the table rules on the table with the push | each value read by its column's element type; the index values read distinct from those read before, before the driver gets them |
| a state, at a pin or a read | the number of rows of each table, by its length constraints | (none) |

An inline value must round-trip, since the log holds its JSON form: a number that is not finite does not.
In both phases a value is validated by a model of its field alone, made with the params model's config, or for a row by the element type with its metadata (`ess.spec.field_types.validated`): an inline value from its JSON (`model_validate_json`, so that a strict enum takes its string), a value read from its object.
The params instance is then made from the validated values with `model_construct`, which validates nothing again.

**Fit.** A reference fits a parameter when the type of the data it names fits the parameter's type: `| None` is removed on both sides; a reference with a key names one element, of the output's element type; a list parameter takes each reference by its element type.
Then the data's type fits when, with its `NewType`s removed one by one, it reaches the parameter's type or a subclass of it; or when the parameter's type, with its `NewType`s removed, reaches the data's type or a superclass of it.
A generic alias, such as a sciline scope `SampleSurfaceNormal[SampleRun]`, fits only itself; a spec gives such a parameter a type of its own and maps it with `keys=`.
A dataset reference fits a file parameter only.
So a `BeamCentre` output fits a `BeamCentre` parameter, a `list[TimeRange]` output with a key fits a `TimeRange` row, and an `IofQ` does not fit a `Transmission` parameter.

### The binding protocol

```python
class Binding(Protocol[P, O]):
    spec: WorkflowSpec[P, O]
    def compute(self, params: P) -> O: ...

class StageBinding(Binding[P, O], Protocol):
    def make_stage(self, fixed: Mapping[str, Any], blanks: Collection[str]
                   ) -> Callable[[Mapping[str, Any]], O]: ...

class AccumulatorBinding(Binding[P, O], Protocol):
    def make_accumulator(self, fixed: Mapping[str, Any], blanks: Collection[str]) -> Driver[O]: ...
```

`Driver` is unchanged.
`make_stage`, `make_accumulator`, a stage call, and a push give values by name, each as its type: a group as the model, a file as a local path, any other data value as the object.
A function binding is `def iofq(params: IofQParams) -> IofQOutputs`.
`PipelineBinding(IOFQ, make_pipeline, keys=...)` is unchanged.

### The serialized spec

`SerializedWorkflowSpec` holds `params_schema`, `outputs_schema`, and the tables.
The framework makes `params_schema` from a model of the same fields, each with its title, description, default, and length constraints, and with its type replaced by the forms it takes: `anyOf` its JSON schema and a reference for a type with a JSON form, a reference otherwise.
A reference's schema is the `@ref` object, with the name of the data type it must fit.
The schema refuses a name that is not a parameter (`additionalProperties: false`), and an attached column that the backend fills with `None` when it is left out is not required.
A form shows a group's fields, with a choice to give it by reference; a picker per data parameter shows references.

### Three cases the design must carry

**Time slices found by a first reduction.**

```python
slicer = client.submit(FIND_SLICES, params={'log': dataset(run=611)})   # outputs slices: list[TimeRange]
n = len(client.output(slicer, 'slices'))                                # once the slicer completes
acc = client.accumulator(Template(SLICES, params={'sample': dataset(run=611)}, blanks=('time_range',)))
acc.push(time_range=[slicer.ref('slices', key=str(i)) for i in range(n)])
acc.push(time_range=[TimeRange(start=0, stop=60)])                      # a repeat of slice 0: the push fails
```

The client reads how many slices there are, since a reference names one row; the record of the state holds the references, so it says where each slice came from.

**A beam centre typed in, and computed, through `tabulate`.**

```python
finder = client.submit(FIND_CENTRE, params={'sample': dataset(run=600)})
computed = client.accumulator(Template(IOFQ_MULTI, params={'beam_centre': finder.ref('centre')},
                                       blanks=('sample', 'can')))
typed = client.accumulator(Template(IOFQ_MULTI, params={'beam_centre': BeamCentre(x=0.012, y=-0.003)},
                                    blanks=('sample', 'can')))
```

Both drivers get a `BeamCentre`, and pushes of the same runs give the same result when the finder found that centre.

**A row computed per run.** A per-run column given by reference, `transmission=[t611.ref('transmission'), None]`, next to rows given inline, in one push.

## What changes, relative to the branch

- **essspec.**
  - `WorkflowSpec[P, O]` with `params`, `outputs`, and `tables`; `inputs`, `NoInputs`, `side`, and `split` go. The model rules above replace the params and inputs rules, and the rule that each parameter has a type of its own.
  - References get the `@ref` JSON form, with the Ref models serializing it in the log as everywhere; `as_ref` reads it at a value or a list element.
  - `Binding`, `StageBinding`, `AccumulatorBinding`, `DriverBinding`, `PipelineBinding`, and `function_binding` take `P` and `O`; `compute(params)`.
  - `check_one_row(single, multi, params)`.
  - `Quantity` is a group like any other; its docstring stops sending a computed value to the inputs.
- **essdispatch.**
  - `Request`, `Template`, records, and the log entries (`Opened`, `Pushed`) hold `params` only. The merges of the two sides and the check that a name is given on its side go. A log written by the branch does not read back.
  - `Checks` checks each value by its form: an inline value by its field or element type from its JSON form, a reference by readability and fit; a file takes a dataset reference only. Model validators are never run, on requests or on states; a state is checked by its tables' length constraints.
  - The call phase validates each value by its field and makes the params instance from them; one helper serves plain requests, stages, and drivers. Distinct index values read are checked before the driver gets a push.
  - `Record.ref(output, key=None)`.
  - `submit`, `Template`, a stage call, and a fill take `params=`.
- **ADR 0001.** Rewritten in place: "Params and inputs" becomes "Params inline or by reference", opening with the two phases per value.
- **ADR 0007.** "Input" becomes "parameter" in the command-line tool and in the rule that a spec module may import scipp; data types still bring scipp to the spec side.
- **binding-jobs.md, README, role notebooks (sources in `.scratch/role-notebooks`), `toy_sans.py`, three-ways-to-run-a-spec.html.** One model; `Transmission` and `BeamCentre` given either way.
- **Tests.** Every request written with `params=`; tests of side errors and of params model validators go; the stories gain the three cases above.

## Order of the work

1. References: the `@ref` form, `as_ref` by kind, `Record.ref(output, key)`. No other change.
2. One model: `inputs` merged into `params` throughout, with each type taking the forms it took on the branch (a group inline, a data value by reference). Source first, then the tests, which subagents rewrite file by file.
3. Forms per value: any type with a JSON form takes a reference too; validation per parameter in every phase; distinct index values read; the three cases as stories.
4. The serialized spec.
5. Docs.

## Decisions

- **The model's name is `params`.** sciline sets files and data on a pipeline as parameters, essreduce's `parameter_registry` holds `FilenameParameter`s, and esslivedata's specs have `params`, which then is the special case with every value given inline.
- **Distinct index values, also by the values read.** Without it, an inline value and a reference that name the same time slice, or references to two records that found the same slices, are two index values, and the slice is summed twice without an error. It costs a set of JSON forms per blank table of an accumulator, and a push that repeats one stops the accumulator. The alternative compares as the record holds the values only, and states that limitation.
- **A model validator of the params model is the binding's to run.** None of esslivedata's 14 params models has one; its 16 groups with validators each check their own fields. A check across parameters, such as a transmission mode that requires a transmission run, fails in the call phase, also for each request of a batch.
- **Groups for values both typed and computed, for now.** A beam centre is a group, converted by a provider in each binding. Of the 7 values both typed and computed, 5 are scalars or vectors with a unit that ess workflows already use as sciline keys (beam centre, I0, MValue, CriticalEdge, Alpha). A JSON form for these small scipp values, opt in, `Annotated[BeamCenter, SmallScipp]`, would let such a parameter take an inline value with no group and no converter; a probe needed about 40 lines. Under this plan that is a type with a JSON form like any other, so it can come later without a change to the design.

## Left for later

- **A small-scipp JSON form**, as in the decision on groups for values both typed and computed.
- **A file or a computed result for one parameter**, such as a direct-beam function from a file prepared by an instrument scientist or from a record. sciline does this with one key and a loader provider (`XFilename -> X`). A spec types such a parameter as the object, `direct_beam: DirectBeam | None`, so that a dataset reference loaded by a loader that the binding declares can come later without a new spec version.
- **One spec for one run and for many.** The tables could be declared on the single-run spec itself, with no second spec.
- **Filling params per dataset in `apply`**, such as each sample's thickness in a batch. Until then, such a batch is a list of requests.
- **Corrections held by the dataset source**, so that every reduction of a run sees them. Until then, a correction is a column next to its run.
- **Streams**, for esslivedata. A context stream, such as an ROI or a motor log, changes while a job runs, so it is more like the pushes of a table than a reference, which names data that do not change. Its default is data, which a default here never is.
- **Choices that must match an earlier reduction's**, such as Amor's wavelength bins and pixel limits: references to outputs of the reference reduction carry them, if that workflow outputs them; essreflectometry reading them from the reduced reference is simpler for its users.
- **Spec identity by content.** A spec's version is the author's promise. A shared group, or the spec a tabulated spec is made from, can change under a spec whose version stays; a fingerprint of the schemas, held by records, would detect that.
- **Fit on a remote client.** The serialized spec names each data type; checking fit needs its `NewType` chain and base classes too.

## Alternatives considered

- **Two models, params inline and inputs by reference.** The spec fixes the form per field; each value used both ways needs a client that reads and resends it, or a spec per form, and moving a field between the models breaks every client and needs a new version.
- **Inputs that take a value inline (`value(...)`).** Keeps two models, but the inputs then hold inline values, which removes the reason for two.
- **A constant spec per typed value**, `params=BeamCentreXY → outputs: BeamCentre`, referenced by the request. Needs no framework change, but a submission per typed value, and no route from a computed value into a setting.
- **Every value tagged**, `{"value": ...}` and `{"ref": ...}`. Unambiguous without a reserved key, but every inline value in requests, the log, and esslivedata's params gets a wrapper.
- **"Literal" or "by value" for a value given inline.** "Literal" clashes with `typing.Literal`, which a parameter's type may use, such as `Literal['linear', 'log']`, and with a literal in Python source, which `Reduction(bins=4)` is not. "By value" would be a second name for the same form; "inline" names it once and pairs with "by reference".
- **`$ref` as the key.** It is the JSON Schema keyword for a reference within a schema; a reference held in a schema, as a default, would be read as one.
- **A reference to a whole list** filling a list parameter. The table rules, such as equal lengths, could then be checked only in the call phase.
- **The framework running model validators of the params model in the call phase of a plain request.** A stage and a driver never make a whole params instance, so such a check would hold for some requests of a spec and not for others.
- **One model, data fields typed as data, validation switched by phase.** Every validator runs on references in the request phase.
- **`DataField` annotations on data fields.** Field types tell the framework the same, and the author writes no metadata for the framework.
- **Multi-run models written by hand**, next to the single-run model. Repeats the single-run model, and the one-row symmetry holds only by convention.
