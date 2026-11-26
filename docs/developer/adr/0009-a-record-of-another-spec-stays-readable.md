# ADR 0009: A record names its spec by name, version, and digest, and a record of another spec stays readable

- Status: accepted
- Deciders: Simon
- Date: 2026-11-13

## Context

A workspace made with `open_folder` stores its history and its persisted outputs in a folder, and a later process reads them, possibly with another version of the technique package installed.
Without this decision, an outputs class can change under the same spec version:

```python
# iofq/v1 wrote Centre(x=0.5, y=0.25); the package now has, still under v1:
class Centre(BaseModel):
    x_mm: float = 0.0   # renamed from x
    y: float

client.output(old_record, 'centre')   # Centre(x_mm=0.0, y=0.25), no error
```

Without this decision, a version bump makes the old outputs unreadable, and a batch shows their rows as not reduced.

## Decision

A record names its spec by name, version, and *digest*, a hash of the name, type, and requiredness of every field of the spec's models.
Titles, descriptions, and defaults are not part of the digest, and callers still write `SpecId(name, version)`.
A record is *of the bound spec* if the workspace has a binding with the same three, and otherwise *of an unbound spec*.
A record of an unbound spec stays in history, and its outputs are read as stored scipp data, without the classes that made them.
A new request may not reference such an output, since its type cannot be checked.
A technique package's tests compare each spec's digest with one stored in the package (`ess.spec.testing.check_digests`), so a change of a model without a new version fails in the author's CI.

## Alternatives considered

- **The digest as the spec's identity**, `SpecId(name, digest)`. A person writes and reads the version, and the digest only has to detect a change.
- **Refusing a folder whose spec changed.** A person could open the folder only with the old package installed, and could not compare results across the change.
- **Storing each spec's JSON schema in the log**, for titles and descriptions of old params. Not needed while the stored names and values are readable.
- **A spec bound only for reading old outputs** into their classes. Not needed while plain scipp data is enough to show and compare an old result.

## Consequences

- A result of an unbound spec cannot feed a new request. A person who needs it as an input reduces again, or keeps the old spec installed.
- An edit to a title or a description changes no digest.
- What the digest covers is part of the log's format.
- A batch shows a row whose result is of an unbound spec as outdated, and the result stays readable, so that the results of two versions of a spec can be compared.
