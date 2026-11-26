# ADR 0014: An application for science users is a distribution of its own

- Status: accepted
- Deciders: Simon
- Date: 2026-11-25

## Context

[ADR 0007](0007-packages-split-by-dependencies.md) gives code a distribution of its own when a different set of distributions depends on it, or when it is released on a different schedule.
By that rule the batch application stayed in `essapps`, as `ess.apps.batch`: it changes together with the core, and needs no dependency that an extra of `essapps` could not bring.

The two parts have different readers:

| Part | Who uses it | How |
|---|---|---|
| `ess.apps`: client, workspace, records, files, writers, Panel forms | developers: workflow authors, application developers | `import ess.apps` |
| the batch application: the command and the web page | science users | `essapps-batch ...`, a browser |

The core is an interface that developers build on, so it should change rarely and with care.
The batch application changes with what its users ask for.
In one distribution, the name `essapps` stood for both, and a user-facing change could not be told apart from a change to the interface.

ADR 0007 also planned a second command, `essapps`, which runs one spec with one option per parameter (`essapps run loki-iofq/1 --sample run:loki/4711 --reduction '{"bins": 200}'`).
The batch command already runs a single reduction as a batch of one row:

```sh
essbatch template loki-iofq -o iofq.yaml    # fill it in
essbatch reduce iofq.yaml -l run4711        # one row, keyed by the label
essbatch export -l run4711 results
```

That command would also have needed most of what the batch command has: a workspace folder to keep records that a later command references (`--centre r17.centre`), `specs`, a template of a spec, writers, and the files table.

## Decision

**An application that science users run is a distribution of its own, which depends on the framework.**
The framework, `essapps`, holds what every application needs.
The batch application is the distribution `essbatch`, with the package `ess.batch` and the command `essbatch`.

```text
packages/
  essspec/      ess.spec      the spec vocabulary, the binding protocol, and the bindings
  essapps/      ess.apps      client, workspace, folder workspace, files, writers, SciCat, Panel forms
  essbatch/     ess.batch     batches: the command essbatch, and the page ess.batch.web [web]
  essinstruments/             specs and bindings per instrument; [loki-app] brings essbatch[web]
```

- **The command-line tool is `essbatch`.** A single reduction is a batch of one row. The command with one option per parameter that ADR 0007 planned is not built.
- **The modules that only batch uses so far stay in `essapps`**: the folder workspace (`ess.apps.folder`), the writers of outputs (`ess.apps.writers`), and publishing to SciCat (`ess.apps.scicat`). None of them is about batches, and a second application, the Panel app or the service, needs them as well.
- **`ess.apps` never imports `ess.batch`.** `tests/boundaries_test.py` of essbatch checks this.
- **The user stories (`docs/developer/user-stories.md`) are tested in essbatch.** They exercise the whole system, and section E runs batches. The tests of essapps import nothing of essbatch.

## Alternatives considered

- **Batch stays in `essapps`**, as ADR 0007's rule implies. The name `essapps` stays one for developers and users at once, and the users' command is named after the framework.
- **`ess.batch` as a second package of the distribution `essapps`.** It gives the package its name without a second distribution, but breaks ADR 0007's naming rule (distribution `essX`, package `ess.X`), and the framework would still be released with each change to the application.
- **The core renamed instead**, for example to `ess.workspace`. "Framework for ESS data-reduction applications" describes the core well once the applications are out of it; the name was vague only while it held the main application too.
- **A command `essapps` for single reductions next to `essbatch`.** It would repeat the folder, the template, the writers and the files table of the batch command, with parameters as command-line options, which are awkward for nested groups.

## Consequences

- `essbatch` depends on `essapps` without a version pin while neither is published. Once they are, a release of `essbatch` that needs a change to the core pins the release of `essapps` that has it. One pull request can still change both.
- `essbatch` is tagged `essbatch/*`.
- An instrument's batch page depends on `essbatch[web]`: `essinstruments[loki-app]` brings it.
- The Panel app (`ess.apps.panel`) stays in `essapps` until it has users. Then this ADR applies to it as well.
