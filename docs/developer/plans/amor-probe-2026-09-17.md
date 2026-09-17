# Amor bound to the framework: what the contract could not express

Amor reflectometry was bound as a probe of the callable contract, not as an app.
Two specs in `packages/essapps/src/ess/apps/amor.py`: `amor-reflectivity`, one
sample run against a supermirror reference, a `WarmPipeline` over
`amor.AmorWorkflow()`; and `amor-combine`, a plain callable that fits scale
factors over a set of curves and averages them onto one Q grid. Tests in
`packages/essapps/tests/amor_test.py` drive both through a `Client` over the
tutorial files. Nothing outside those two files and the `amor` extra in
`packages/essapps/pyproject.toml` was touched.

## What worked without friction

Collection outputs are real. `CombineOutputs.scaled: dict[str, Array(CURVE)]`
is stored and served per key by `runner._store_output` and `DataStore.path_for`,
and `client.output(record, 'scaled', key='608')` reads one element without
touching the rest. A collection parameter, `CombineParams.curves`, resolves
element by element through `binding.resolve`, which walks dicts, and
`backend._dispatch` treats each element as an ordinary reference: scheduling,
failure propagation, and provenance all work across a collection with no special
case (`test_an_element_of_a_collection_output_feeds_the_next_combine`,
`test_curves_of_four_rotations_stitch_into_one`). A literal collection output,
`scale_factors: dict[str, float]`, stays inline on the record and one element of
it inlines into a scalar parameter of a later run through
`backend._resolve`/`_inline`, which is what lets the tutorial's
`scale_to_overlap` round trip be expressed as three requests
(`test_a_fitted_scale_factor_feeds_back_into_the_member_that_produced_it`).

The binding's freedom over stage inputs (D8) paid off exactly as the sketch
claims. Making `sample_run` a stage input rather than a held parameter puts the
reduced 120 MB supermirror reference at the stage's frontier, so a series of
four rotations reduces it once; held instead, it was reduced four times and the
test suite took 26 s instead of 17 s. The choice is invisible to the spec and
correctness did not depend on it.

## Findings

**The warm stage holds one frontier, which decides whether a series of members
is affordable (D8, `warm.py`, `amor.py:reflectivity_workflow`).** `WarmPipeline`
keeps a single `sciline.Stage` keyed by the held parameters; any change to a
held parameter discards it. For Amor the expensive shared part is the reduced
reference, and it survives a change of sample run only if `sample_run` is
declared a stage input. That works, but it makes stage inputs carry two
unrelated meanings: "the slider a notebook moves" (`q_num_bins`, `scale_factor`)
and "the parameter that varies across members of a series, whose frontier must
not be discarded". The second is not a slider and is not cheap to recompute
downstream of. Nothing in the sketch says the two coincide, and here they only
coincide by luck: had anything expensive depended on both the reference and the
sample run, no choice of stage inputs would have kept it. The sketch should
either say that a stage input is also how a session shares work across members
of a series, or D8 should let a binding name more than one frontier.

**A non-additive combine gets none of D15's consistency check (D15,
`backend.py:_check_contributions`, `amor.py:COMBINE`).** `_check_contributions`
runs only for requests carrying `contributions`, so an opaque combine over a
collection parameter is checked by `_check_ref` alone: proposal, spec known,
output exists, format matches. Nothing compares the parameters the producing
members ran with. `amor-combine` will happily stitch a curve reduced with
`z_index_limits=(80, 370)` onto one reduced with different limits, or against a
different reference run, and produce a plausible curve. This is the same mistake
the sketch says the declared path must prevent ("without that second check a
combine over members contributed under different masks concatenates their events
without complaint") -- the reasoning applies verbatim to the opaque path, which
gets no check because it has no declaration to check against. The cheapest fix
is to make the check a property of a collection-valued data parameter rather than
of `contributions`: a spec could mark a collection parameter as "members of one
series", and the backend would then require the referenced records to share a
spec and to agree on everything but their own data references.

**A collection's keys are the submitter's invention and mean nothing to the
framework (D13, `spec.py`, `records.py:output_keys`).** The sketch says "keys are
declared on the spec where the author can, such as bank names, and free
otherwise", but the vocabulary has no way to declare them: a collection is
`dict[str, Array(...)]` and that is all. In the test, `{'608':
curves[608].ref('reflectivity')}` pairs a key with a reference and nothing checks
that the key names the run the reference came from; a transposed dict is
accepted and the stitched curve is wrong in a way no validation can see. The
framework already owns an identifier for this -- `member_key` on a request, used
for batch tables -- but it does not reach a collection parameter. Either a
collection parameter should be able to say its keys come from the producing
records (a run number, a member key), or the sketch should drop the claim that
keys are ever declared. Relatedly, `backend._check_ref` never checks
`ref.key` for a reference into a pending output; only `_resolve` checks it at
dispatch, against a completed producer. So D8's runnability claim, "for a
collection element, to a key the producer declares", is not met for a group
submission, which is the case it was written for.

**Chaining is a format check, so a declared `ArraySpec` is decorative
(D13, `backend.py:_check_ref`).** `_check_ref` compares `produced.format` with
`consumer.format` and stops. `CombineParams.curves` declares
`ArraySpec(dims=('Q',), unit='dimensionless', coords={'Q': '1/Å'})`, and any
scipp output of any spec satisfies it -- a wavelength spectrum, a beam-centre
vector, a binned event list. For reflectometry this matters more than for LoKI:
`combine_curves` requires dense histograms with a Q bin-edge coordinate, and
`ReflectivityOverQ` is binned events, so `amor.py:reflectivity_curve` histograms
the member's output and `binned=False` on the `ArraySpec` is the only statement
that the combine needs the dense form -- a statement nothing reads. Two smaller
consequences: unit strings have no canonical spelling (this spec writes `1/Å`
because that is what scipp prints; `loki.py` writes `1/angstrom` for the same
unit), so even a string comparison would need normalising; and the runner's
`_check_array` checks dims and coordinate names but not units or `binned`, so an
output that violates its own declaration is stored. Making the backend compare
`ArraySpec` on a chain, and the runner check `binned` and units, would cost
little and is what the sketch already promises.

**Every scalar parameter costs a `NewType` and a provider (D8, `warm.py`,
`amor.py`).** `WarmPipeline.keys` maps a parameter name to a sciline key, and
`resolve` says whether a data reference arrives as a path or an object, but there
is no place for a conversion, so each of nine parameters needs a `NewType` and a
one-line provider whose only job is to turn JSON into a scipp object:
`float` -> `sc.scalar(..., unit='deg')`, `PixelRange` -> a tuple of scalars,
`WavelengthEdges` -> `sc.geomspace`. That is 60 lines of `amor.py`, more than
either spec, and it puts nine nodes into the graph that exist only because the
request is JSON. `loki.py` pays the same tax (`q_edges`, `wavelength_edges`,
`beam_center_vector`). A `convert=` mapping next to `resolve=`, from parameter
name to a callable applied before the value is set, would remove all of it and
would keep the conversion where the binding already decides the key.

**The parameter vocabulary has ranges only in physical units (D13,
`ess.reduce.spec.parameters`, `amor.py`).** Amor needs a detector pixel-index
range (`YIndexLimits`, `ZIndexLimits`), an angular range (`BeamDivergenceLimits`)
and a Q interval (the critical edge), and the vocabulary offers `TOARange` and
`WavelengthRange` only. `amor.py` defines `PixelRange`, `AngleRange` and
`QRange` locally, which means three models a UI cannot recognise and three
places where the ordering validator is written again. `RangeModel` needs the
same unit-subclass treatment `EdgesModel` already has, plus a dimensionless
integer range.

**Bin edges are sometimes a count over a range the data decides (D13,
`ess.reduce.spec.parameters.QEdges`, `amor.py:q_bins`).** `QEdges` did not fit
the member spec. `ess.amor.utils.qgrid` derives the accessible Q range from the
run's geometry, and the ranges of two sample rotations differ -- that difference
is precisely why the curves have to be stitched. A request that set start and
stop would put every angle on one grid and destroy the thing the combine exists
for. The parameter is therefore `q_num_bins: int` over a derived range, and the
binding replaces the package's `qgrid` provider to inject it. `QEdges` fits the
combine spec, where the grid really is the submitter's choice, and it is used
there. The vocabulary should carry an edges model whose range is optional,
meaning "derived unless given"; otherwise every instrument whose binning follows
the geometry invents its own integer parameter.

**An output the framework cannot serialize has nowhere to declare a serializer
(D8, D13, `datastore.py:Serializers`, `runner.py:main`).** The tutorial's actual
deliverable is an ORSO `.ort` file, and `orso.OrsoIofQDataset` is neither a scipp
object nor bytes. The sketch says such an output "must come with its own
serializer, declared with the output", but the output model has no field for one
and `Serializers` is constructed inside `DataStore.__init__` and again inside
`runner.main`, with no hook a binding can reach -- a throwaway runner would fail
with `No serializer for OrsoDataset; declare one`. The probe therefore has no
ORSO output. (Read from the code, not run.) This is the one place where the
contract's stated intent has no implementation at all; either the spec's output
field carries a serializer entry point, or the binding's factory does.

**Reflectometry's additive half is still not expressible, for the reason
`loki.py` already recorded (D8, D15).** Angle stitching is genuinely
non-additive and stays an opaque workflow, as the sketch says. But
reflectometry also concatenates events of several runs measured at the same
angle, which is additive and is exactly D15's shape -- contribute at
`ReducibleData[SampleRun]`, combine by concatenation, finalize into R(Q).
`essreflectometry` expresses it as `with_filenames`, which rebuilds the graph
from a list of filenames, the same construction that defeated the LoKI pixel
masks. So a list-valued `NexusFile` parameter still cannot be bound, and the way
out is D15's declared contribution rather than a collection parameter. This
probe did not build it; it is the obvious next thing to try, and it would be the
first real test of `AggregatePipeline` against a package that is not LoKI.

**The feedback round trip is expressible but unnamed (D14, D15).** Fitting scale
factors over all members and then re-reducing each member with its factor is a
cycle: members -> combine -> members -> combine. It works as three requests
(the test does it), because a literal collection element can fill a scalar
parameter. But D14's trigger loop only ever runs members then a combine, and
there is no vocabulary for "this combine's output is a parameter of a rerun of
its own inputs". A rule that automated the tutorial's `scale_to_overlap` would
have to be written as two rules with a hand-made label convention. Worth one
paragraph in D14 saying whether such a cycle is in scope or explicitly deferred.

**Small practical note.** The Amor tutorial files raise scippnexus
transformation warnings that the notebook silences; with
`filterwarnings = ["error"]` in `pyproject.toml` the test file needs three
`pytest.mark.filterwarnings` entries. Not a contract issue, but any workflow
package whose tutorial data is imperfect will hit it.

## Status

```
4 passed in 17.41s          # packages/essapps/tests/amor_test.py
145 passed in 29.00s        # packages/essapps/tests, -n 4
ruff check packages/essapps   -> All checks passed!
ruff format packages/essapps  -> 36 files already formatted
```
