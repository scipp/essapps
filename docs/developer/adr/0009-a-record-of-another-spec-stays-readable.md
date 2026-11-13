# ADR 0009: A record names its spec by name, version, and digest, and a record of another spec stays readable

- Status: accepted, not implemented. On `main`, an output of a record whose spec has no binding raises `NotReadable`, and a stored group is read into the bound outputs class if no required field is missing.
- Deciders: Simon
- Date: 2026-11-13

## Context

A workspace made with `open_folder` keeps its history and its persisted outputs in a folder, and a later process reads them, possibly with another version of the technique package installed.
An outputs class can change under the same spec version:

```python
# iofq/v1 wrote Centre(x=0.5, y=0.25); the package now has, still under v1:
class Centre(BaseModel):
    x_mm: float = 0.0   # renamed from x
    y: float

client.output(old_record, 'centre')   # Centre(x_mm=0.0, y=0.25), no error
```

A version bump instead makes the old outputs unreadable, and a batch shows their rows as not reduced.

Each deployment has its own workspaces, so a proposal has several histories ([deployments.md](../deployments.md#one-proposal-several-histories)).
What must reach another workspace, or outlive the history, is published.

## Decision

Callers write `SpecId(name, version)` or pass the spec.
A request made with a spec whose digest differs from that of the spec bound under the same name and version is refused at submission.

The *digest* of a spec covers the name, type, and requiredness of every field of its params and outputs models, including the fields of groups inside them.
Titles, descriptions, and defaults are not part of it.
A record stores its params with defaults filled in, so a changed default does not change what an old record means.
The digest cannot see a change of what a workflow computes.
The version remains the author's statement of any change a consumer can observe: the params, the outputs, or what the workflow computes (`WorkflowSpec`).

Every event that names a spec, a submission or the opening of an accumulator, carries the digest of each spec it names that the log does not hold yet under that name and version, or holds with another digest.
The digest of a record's spec is the last one logged for its name and version before the record.
A submission, abridged:

```json
{"kind": "submitted", "specs": {"iofq/v1": "9f2c..."}, "records": [{"id": "r2", "request": {"spec": {"name": "iofq", "version": 1}, "params": {"bins": 4}}}]}
```

A record is *of the bound spec* if the workspace has a binding with the same name, version, and digest.
Otherwise it is *of another spec*: the version was bumped, the models changed, or no binding is given.
A record of another spec stays in the history, and is readable without the classes that made it:

| Of a record of another spec | What a client gets |
|---|---|
| its request | name and version, and the params as the stored dict, with files and references |
| an output | the stored scipp data: a `DataArray`, or a `DataGroup` by field name |
| `describe` and `select=` | work on that data |
| a new request that references its output | refused at submission with `SubmitError`, since the output's type cannot be checked |
| a restart while it is pending | it fails with a reason that says its spec is not bound, and so does a pending record that reads an output of it |

A technique package's tests compare each spec's digest with one kept in the package, so a change of a model without a new version fails in the author's CI (`ess.spec.testing`).

## Alternatives considered

- **The digest as the spec's identity**, `SpecId(name, digest)`. Callers, templates, and saved settings would name a digest. A person writes and reads the version, and the digest only has to detect a change.
- **Refusing a folder whose spec changed.** A person could open the folder only with the old package installed, and could not compare results across the change.
- **Keeping each spec's JSON schema in the log**, so that old params show with their titles and descriptions. Not needed while the stored names and values are readable.
- **A spec bound only for reading old outputs** into their classes. Not needed while plain scipp data is enough to show and compare an old result.

## Consequences

- A result of another spec cannot feed a new request. A person who needs it as an input reduces again, or keeps the old spec installed.
- An edit to a title or a description changes no digest, so no record becomes one of another spec.
- What the digest covers is part of the log's format ([ADR 0004](0004-history-is-append-only-lists.md#decision)).

## Open

- A batch should show a row whose record is of another spec as no longer current, while its result stays readable, so that results of two versions of a spec can be compared. How, and how that relates to "stale", is scipp/essapps#191.
