# User stories

For someone who checks what the framework must do for its users, through the API of [README.md](README.md).
Each story has an actor, a goal, and a test that reaches the goal with client code and checks it with assertions.
What a story needs from the system, such as cost, placement, or persistence, is a story in [system-stories.md](system-stories.md) with the same identifier.

The tests are in [`packages/essbatch/tests/stories`](../../packages/essbatch/tests/stories), one file per section.
Each test is named after its story, such as `test_s1_reduce_one_run` for S1.
They use toy specs ([`toys.py`](../../packages/essbatch/tests/stories/toys.py)) whose numbers can be checked by hand: a file made by `measure` holds a list of counts.
The fixtures, such as `client`, `measure` and `upgrade`, are in [`conftest.py`](../../packages/essbatch/tests/stories/conftest.py).
A test marked `xfail` does not pass yet, and its reason names what is missing: a part of the framework, or a fake that the test needs.

## S. Small stories

One concept each. Tests: [`s_small_test.py`](../../packages/essbatch/tests/stories/s_small_test.py).

- **S1. Reduce one run.** Actor: user in a notebook. Goal: reduce one run with one parameter changed from its default, and read the result.
- **S2. Tune one parameter.** Actor: user in a notebook. Goal: change the binning several times, looking at the result after each change.
- **S3. Look at a value inside a reduction.** Actor: user in a notebook. Goal: see an intermediate value, the counts after masking, without a separate workflow.
- **S4. Submit a chain without waiting.** Actor: user in a notebook. Goal: submit two reductions and a third request that combines them, without waiting in between.
- **S5. Sum runs.** Actor: user in a notebook. Goal: reduce three runs of one sample as one measurement, and subtract the sum of two background runs.
- **S7. Reduce each sample with the can measured before it.** Actor: instrument scientist. Goal: every sample subtracts the can measured most recently before it, without naming the can.
- **S8. Trace a result to raw data.** Actor: user. Goal: learn which raw runs and parameter values a two-step result came from.

## A. Getting data in

Tests: [`a_data_in_test.py`](../../packages/essbatch/tests/stories/a_data_in_test.py).

- **A1. Browse a local folder next to a catalogue reference.** Actor: user of a local application. Goal: list the files in a folder, add a reference measurement by PID, and plot all of them.
- **A2. Run number instead of file.** Actor: user at the instrument. Goal: type a run number and get the file.

## B. Manual and interactive reduction

Tests: [`b_interactive_test.py`](../../packages/essbatch/tests/stories/b_interactive_test.py).

- **B1. Tune a SANS reduction and save the result as a template.** Actor: user in a notebook. Goal: change binning and mask several times, looking at the result after each change, then save the final parameters as the beamtime's template.
- **B2. Add a run to a sum, then start over without one.** Actor: user in a notebook. Goal: runs 611 and 612 are summed. 613 finishes and is added. 612 turns out bad, and the sum starts over without it.
- **B4. Explore a 4D volume.** Actor: spectroscopy user in the web UI. Goal: drag through cuts of a Q-E volume, then fit the cut that looks right.

## C. Chaining

Tests: [`c_chaining_test.py`](../../packages/essbatch/tests/stories/c_chaining_test.py).

- **C1. Beam centre feeds a batch of sample reductions.** Actor: user. Goal: use the beam centre as a parameter of every sample reduction. Provenance reaches the beam-centre run.
- **C2. Vanadium from the catalogue.** Actor: user. Goal: use a vanadium result that another proposal published to SciCat.
- **C4. Reflectometry angle series.** Actor: reflectometry user. Goal: reduce four angles against a reference, stitch them, and export the stitched curve to a file.
- **C5. Vanadium and sample tuned together.** Actor: instrument scientist in a notebook. Goal: adjust the vanadium processing and see the effect on a sample reduction at once.

## D. Batch

Tests: [`d_batch_test.py`](../../packages/essbatch/tests/stories/d_batch_test.py).

- **D1. Temperature scan.** Actor: instrument scientist. Goal: reduce a scan with one template, one temperature per run, and plot three chosen temperatures. One corrupt file affects nothing else.
- **D2. Overnight cluster batch.** Actor: NMX user. Goal: submit thirty long runs, close the laptop, and the next day read why some failed and rerun them.
- **D3. Cancel and resubmit.** Actor: user of the shared service. Goal: cancel a running batch of 500 with a wrong shared parameter, fix the parameter, and resubmit without waiting for the cancel.
- **D4. Typo caught before 500 failures.** Actor: user filling in a batch form. Goal: a parameter that the params model rejects is caught before any record exists.
- **D6. Rerun a batch with a new workflow version.** Actor: instrument scientist. Goal: reduce an earlier batch of the proposal again with a new spec version, keeping the first results.
- **D7. Rotation scan over three hundred angles.** Actor: spectroscopy user. Goal: reduce a crystal rotation scan of 300 runs, one run per angle, into one volume, and look at cuts through the volume while the scan continues.

## E. Automatic reduction

Tests: [`e_automatic_test.py`](../../packages/essbatch/tests/stories/e_automatic_test.py). Automatic reduction is a batch of `ess.batch` ([automatic-reduction.md](automatic-reduction.md)).

- **E1. Series grows, reduction follows.** Actor: reflectometry user during a beamtime. Goal: after each angle, the newest row of a sample is its curve with all angles so far.
- **E2. Automatic reduction goes quiet.** Actor: instrument scientist. Goal: after an upgrade removed the spec version of the settings, see why nothing is reduced.
- **E3. Reduction of our own output.** Actor: none, a failure mode. Goal: a published result that SciCat lists as a dataset does not create a row.
- **E4. Template improved during a beamtime.** Actor: instrument scientist. Goal: new runs use the improved settings, earlier results stay, and the results made before the improvement can be found.

## F. Publication and provenance

Tests: [`f_publication_test.py`](../../packages/essbatch/tests/stories/f_publication_test.py).

- **F1. Publish, then trace six months later.** Actor: user, then a colleague. Goal: the SciCat entry alone states what raw files, parameters, and software produced it.
- **F2. Reproduce after two upgrades.** Actor: user. Goal: learn before anything runs that the exact result is not reproducible in the current environment. A rerun is an honestly labelled new record.

## G. Roles and deployment

Tests: [`g_roles_test.py`](../../packages/essbatch/tests/stories/g_roles_test.py).

- **G2. Developer iterates on a workflow.** Actor: workflow developer. Goal: run a workflow defined in a notebook without installing it.
- **G5. Reference across proposals refused.** Actor: external user. Goal: a reference to another proposal's record raises before any record exists.

## I. Values typed in or computed

A value is typed in by one user and computed by another, or computed by an earlier request.
The spec types such a field `X | Referenced[X]`, and the request writes each value inline or gives a reference to an output.
Tests: [`i_forms_test.py`](../../packages/essbatch/tests/stories/i_forms_test.py).

- **I1. Time slices found by a first reduction.** Actor: user in a notebook. Goal: sum a run over the time slices that a first reduction found in its log. A reference given again raises.
- **I2. A beam centre typed in or computed.** Actor: user. Goal: give one multi-run spec a beam centre that a first reduction found, or one typed in.
- **I3. A transmission computed for the summed runs.** Actor: user. Goal: sum two runs of a sample, with one transmission that a first reduction computed for them.
