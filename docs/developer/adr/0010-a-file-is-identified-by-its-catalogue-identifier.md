# ADR 0010: A file is identified by its catalogue identifier, or else by its path

- Status: accepted, not implemented. On `main`, `FolderFiles` gives every file the identity `path:<absolute path>`.
- Deciders: Simon
- Date: 2026-11-13

## Context

A person names a file as `file(run=60339)`, and the file source resolves the name to the file's *identity*, which the record keeps and provenance lists.
With paths as identities:

- A moved folder changes every identity, so a trigger loop reduces its runs again.
- The same run seen through two file sources has two identities.

The catalogue is SciCat.
A derived dataset in the catalogue lists its inputs by *catalogue identifier*.
A raw file carries `entry/entry_identifier_uuid`, and the catalogue lists it as `20.500.12269/<uuid>` ([data.md](../../requirements/data.md)).

## Decision

| File | Identity |
|---|---|
| a file the catalogue lists | `pid:20.500.12269/<uuid>` |
| any other file, inside the workspace's folder | `path:masks/main.xml`, relative to the folder |
| any other file, outside it | `path:/data/other/run-1.nxs` |

The catalogue identifier is written in full.
For a raw file, a file source without access to the catalogue forms it from the identifier the file carries.
An *other file* is one the catalogue does not list, such as a mask, a test file, or a file of another facility.
A workspace without a folder, made with `local`, gives absolute paths.
A person still writes `file(run=60339)`.

## Alternatives considered

- **Paths throughout.** A moved folder and a second file source change the identity of a raw file, and provenance would not name inputs as the catalogue does.
- **A hash of the content.** A file would be read whole to be identified, and an hour of DREAM at its highest rate is about 2 TB ([data.md](../../requirements/data.md)).

## Consequences

- A moved folder, and a second file source, give a raw file the same identity.
- Provenance lists raw files as the catalogue names them.
- A file source reads the identifier from a raw file, or gets it from the catalogue.
- Identities are in the log, so changing them changes the log's format ([ADR 0004](0004-history-is-append-only-lists.md#decision)).

## Open

- Whether every raw file in user operation carries the identifier, and whether a file the catalogue lists can still be unreadable. To be asked of the teams that run the file writer and the catalogue.
