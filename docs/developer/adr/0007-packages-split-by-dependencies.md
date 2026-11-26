# ADR 0007: Distributions are split by who depends on them and when they are released

- Status: accepted
- Deciders: Simon
- Date: 2026-10-26

## Context

A *distribution* is what pip installs, such as `essapps`; a *package* is what Python imports, such as `ess.apps`.
An *extra* is an optional set of dependencies of a distribution, installed as `pip install 'essapps[panel]'`.

Several programs are built on the framework, and they need different things installed:

| Program | Runs | Needs |
|---|---|---|
| notebook, Jupyter app | VISA or a laptop | client, workspace, workflow packages, a GUI toolkit |
| web frontend | a browser | an HTTP server in front of a workspace |
| the service | DMSC | workspaces, storage, workers, SciCat, workflow packages |
| workflow package (esssans, ...) | everywhere a workspace runs | the spec vocabulary, the binding protocol, the bindings |

The workspace and the client need only pydantic and the standard library.
The heavy dependencies are the science stack (essreduce brings scipp, scippnexus, scippneutron, dask, scipy), the service stack, and GUI toolkits.
If workflow packages had to import `ess.apps` for the binding protocol, every workflow package would depend on the framework.

Code changes at two rates.
The client, the workspace, the HTTP layer, the GUIs, and the service change together.
An instrument's specs and bindings change when its users or its workflow package need it, independently of the core and of other instruments.

## Decision

**Code gets a distribution of its own when a different set of distributions must depend on it, or when it is released on a different schedule. Code that needs dependencies only some users install is a subpackage behind an extra of the distribution it belongs to.**

- `essspec` (`ess.spec`) holds everything a workflow author writes against: the spec vocabulary, the binding protocol, and the bindings. It needs only pydantic, with sciline and scipp behind extras.
- `essapps` (`ess.apps`) holds what notebooks and apps use: the client, the workspace, files, and fakes for tests. Its subpackages behind extras, such as `ess.apps.panel`, are released with it.
- `essinstruments` (`ess.instruments`) holds the specs and bindings of each instrument, released on its own schedule, with each instrument's workflow package behind an extra such as `[loki]`. Specs and bindings are separate modules, so that a spec may move into its workflow package without its binding, and a type checker checks the binding against the spec.
- An application that science users run is a distribution of its own ([ADR 0014](0014-applications-for-science-users-are-distributions-of-their-own.md)).
- Names follow the ess monorepo: distribution `essX`, package `ess.X`. A second distribution cannot install into a regular package such as `ess.apps`, and a namespace package could not export `local`, so a subpackage such as `ess.apps.panel` is part of its parent's distribution.
- `essapps` finds bindings through the entry-point group `ess.apps.workflows`, so a distribution registers its workflows without importing the framework. An entry whose extra is not installed is skipped. Two entries for one spec name and version fail the load, naming both, since picking one would leave records ambiguous about what ran.
- GUI code has one subpackage per toolkit, and no toolkit-independent GUI package. An instrument's app goes with its specs: in `essinstruments` behind an extra such as `[loki-app]`, and with them into a `gui` extra of the workflow package.

## Alternatives considered

- **The HTTP layer, the GUIs, and the service as distributions of their own.** Each would pin a narrow range of the core and be released with it.
- **Client and workspace as separate distributions.** They have the same dependencies, so the split installs nothing less.
- **The instruments' specs and bindings in `essapps`.** A fix to one instrument would ship whatever changed in the core, and moving an instrument out later renames every import.
- **The instruments' specs in `essspec`, with bindings behind extras.** `essspec[loki]` would depend on esssans, which will depend on `essspec`, and every consumer would carry this application's glue.
- **The instruments' specs importable without their workflow packages.** A Python client runs in an ESS environment that has them, and a web frontend reads the serialized spec.
- **The spec vocabulary in essreduce.** Every client would install the science stack, and each change would be a pull request in two repositories.
- **The binding protocol and the bindings in essreduce.** essreduce would depend on a distribution that still changes.
- **Bindings only passed explicitly, as a list to `bind=`.** Every command and service would need its own list, and a new workflow package a change to each.
- **The HTTP server in the service.** A web frontend against a local workspace needs it too.
- **A generic GUI layer under the toolkit subpackages.** Its rules are the request types of `ess.spec.request_types`, which essspec holds already.
- **`ess.dispatch` as the core's name.** `essapps` is the repository's name, and other names (run, job, workflow, reduce, stage, session) already mean something here.
- **`essappsinstruments` as the instruments' name.** Longer, and `ess.instruments.loki` and `ess.loki` already say which is which.

## Consequences

- A plain install of essspec and essapps installs no scipp.
- A change to the spec, the core, and the instrument specs is one pull request. Each distribution is released with a tag of its own, such as `essspec/*`.
- Moving a spec into its workflow package is one coordinated release, since two entries with the same spec fail the load.
- essspec has its own ADRs, so it can move to scipp/ess once a second consumer adopts it.
- Testing and type-checking essapps needs all its extras installed.
