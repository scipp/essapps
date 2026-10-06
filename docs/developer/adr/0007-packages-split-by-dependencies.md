# ADR 0007: Packages are split by what they depend on and where they run

- Status: accepted
- Deciders: Simon
- Date: 2026-10-06

## Context

The prototype was one package, `essapps` (`ess.apps`), that depended on essreduce for `ess.reduce.spec` and on sciline for one module.
Several programs will be built on it, and they need different things installed:

| Program | Runs | Needs |
|---|---|---|
| notebook, Jupyter app | VISA or a laptop; both count as local | client, in-process backend, workflow packages, a GUI toolkit |
| web frontend (not Python) | in a browser, against a local backend or the service | an HTTP server in front of a backend |
| remote client, CLI | anywhere | client, HTTP client |
| the service | DMSC | backend, HTTP server, durable storage, workers, SciCat, workflow packages |
| workflow package (esssans, ...) | everywhere a backend runs | the spec vocabulary and the binding protocol |

The in-process backend needs nothing the client does not: pydantic and the standard library.
The heavy dependencies are elsewhere:

- **The science stack.** Installing essreduce pulls in scipp, scippnexus, scippneutron, dask, scipy, and graphviz, and importing `ess.reduce.spec` runs `ess/reduce/__init__.py`, which imports them. The spec modules themselves need only pydantic, apart from the conversions to scipp.
- **The service stack**: web framework, database, cluster launcher, SciCat client.
- **GUI toolkits**: ipywidgets, Panel, or a JavaScript build.

Workflow packages write specs and bindings.
If they had to import `ess.apps` for the binding protocol or for `PipelineBinding`, every workflow package would depend on the framework.

The spec vocabulary still changes with the framework's design: it was amended in step with essapps for weeks, on the branch `653-minimal-workflow-spec` of scipp/ess (scipp/ess#690).
Nothing else imports it yet.

esslivedata keeps one module per instrument (`ess.livedata.config.instruments`) with the instrument's params models and the glue to its sciline workflow: facility defaults, adapters, and which settings users may change.
The glue is a product decision, not the workflow package's, so the specs and bindings of offline reduction might never move into the instrument packages.

## Decision

**A package is split off when its dependencies, where it runs, or who must depend on it differ, not for each concept.**
`essspec` runs wherever `essdispatch` runs, but workflow packages must depend on it without depending on the framework, and it moves to scipp/ess later.
`essdispatchinstruments` and the workflow packages depend on `essspec` only.
The service and the GUI packages depend on `essdispatch`, and an instrument app also on `essdispatchinstruments` for its specs.

```text
packages/
  essspec/                  ess.spec                  the spec vocabulary and the binding protocol
  essdispatch/              ess.dispatch              client, in-process backend, drivers
  essdispatchinstruments/   ess.dispatchinstruments   specs and bindings per instrument
  essdispatchservice/       ess.dispatchservice       hosting: storage, workers, SciCat, retention, auth, automatic reduction
  essdispatchjupyter/       ess.dispatchjupyter       ipywidgets forms and apps
frontends/
  <app>/                    a web frontend, against the HTTP API of ess.dispatch
```

`essspec` and `essdispatch` exist now.
Each of the others is created with its first module, so none is an empty scaffold.

```text
essspec  <-  essdispatch  <-  essdispatchservice, essdispatchjupyter
   ^             ^
   |             +----------  frontends (over HTTP)
   +----------  essdispatchinstruments, workflow packages
```

- **`essspec` (`ess.spec`) is everything a workflow author writes against.** The vocabulary (`WorkflowSpec`, data fields, tables, references, parameter models) and the binding protocol (`Binding`, `HeldState`, `HeldStateBinding`, `Function`) need only pydantic. `combine` needs nothing more. `ess.spec.conversions` needs scipp (extra `[scipp]`), and `ess.spec.pipeline` with `PipelineBinding` needs sciline (extra `[sciline]`). It is developed in this repository while it changes, and is not published, so nothing outside this repository depends on a version of it. Once a second consumer, such as esslivedata or essnmx, is to adopt it, it moves to scipp/ess as a package of its own and is published from there.
- **`essdispatch` (`ess.dispatch`) is what notebooks and apps use**: records, the client, the in-process backend and its log, datasets, the drivers (batches, rules, the trigger loop), and fakes for tests. It depends on essspec and pydantic, not on essreduce or sciline. The HTTP layer goes here too, when it is written: `ess.dispatch.http` holds the server and `connect()`, the two halves of one protocol, behind the extras `[server]` and `[remote]`. A local backend serves a web frontend on VISA or a laptop as the service does.
- **The backend finds bindings through the entry-point group `ess.dispatch.workflows`.** Each entry names a function that returns a mapping from spec to binding. `local()` without `bind=`, the command-line tool, and the service read the group, so a package registers its workflows without importing the framework. Listing the specs this way imports the bindings; a client of the service gets the list from the service instead. This is decided, not yet implemented.
- **The command-line tool is a client in `essdispatch`**: `ess.dispatch.cli`, with the script `essdispatch`. It builds its arguments from `SerializedWorkflowSpec`, so it runs any registered spec in its own process, and with `--url` any spec of the service, without importing a workflow package itself. A data field takes a dataset name or a path, a table a JSON or CSV file with one row per line, and a plain value an option typed from the schema. It needs only argparse. Writing outputs to files needs the writers the service needs too ([ADR 0005](0005-the-service-writes-every-output.md), scipp/essapps#23), so these are shared code in `essdispatch` behind an extra `[scipp]`, not part of the tool. A tool with instrument-specific arguments, such as essnmx's reducer, stays in its instrument package.
- **`essdispatchinstruments` holds one subpackage per instrument**, in two halves: `specs.py` needs only essspec, so GUIs and remote clients import it; `bindings.py` needs the instrument's workflow package, behind an extra such as `[loki]`. A spec can move into its instrument package only after essspec has moved to scipp/ess, and only once its binding is a plain `PipelineBinding`, with no facility defaults or adapters. Deployment configuration, such as automatic-reduction rules and data paths, is not here; it belongs to the service's configuration.
- **`essdispatchservice` holds what only the hosted backend needs.** The rules of the trigger loop stay in `essdispatch`; the service runs the loop as a daemon from a configuration file.
- **GUI code has one package per toolkit, and no toolkit-independent GUI package.** A form is built from `SerializedWorkflowSpec`, the JSON Schema of a spec that `ess.spec` provides, so ipywidgets, Panel, and a web frontend share one description of a form. An app keeps its own logic, such as grouping runs by sample and angle, in plain classes apart from its widgets. A package of shared app logic is made once two apps share some. An instrument app moves to its instrument package's `gui` extra, as `essreflectometry[gui]` does today, only after the app's specs have moved there; until then the extra would depend on `essdispatchinstruments`, whose extra depends on the instrument package. That extra is the one way a workflow package depends on the framework; its plain install depends on `essspec` only.
- **Names follow the ess monorepo**: distribution `essX`, import `ess.X`, one level under `ess`. A second distribution cannot install into a regular package such as `ess.dispatch`, and a namespace package `ess.dispatch` could not export `local` and the rest.

```python
from ess.dispatch import local, Template            # notebook or app
from ess.spec import WorkflowSpec, NexusFile        # workflow author
from ess.spec.pipeline import PipelineBinding       # workflow author with a sciline workflow
```

```toml
# pyproject.toml of essdispatchinstruments, or one day of esssans
[project.entry-points."ess.dispatch.workflows"]
loki = "ess.dispatchinstruments.loki.bindings:workflows"
```

```bash
essdispatch specs                                           # the registered specs
essdispatch run loki-iofq/1 --run 4711 --bins 200           # in this process
essdispatch run loki-iofq/1 --run 4711 --url https://reduce.example --proposal p1
```

## Alternatives considered

- **Client and in-process backend as separate packages.** They have the same dependencies, and apps on VISA use `local()`, so the split installs nothing less. The client imports the concrete `Backend`; a protocol for it comes with the remote backend.
- **The spec vocabulary in essreduce.** Every client, remote ones included, would install the science stack. Each change would be a pull request in scipp/ess, made alongside the one here, with a branch pinned in between.
- **The binding protocol and `PipelineBinding` in essreduce.** They are the other half of the contract with workflow packages, and essreduce would depend on a package that still changes.
- **Bindings only passed explicitly, as `bind=` is today.** The command-line tool and the service would each need their own list of workflows, and a new workflow package would need a change to each.
- **The command-line tool in a package of its own.** It needs nothing that `essdispatch` lacks.
- **The HTTP server in the service package.** A web frontend against a local backend needs it too, and so does development.
- **A generic GUI layer under the toolkit packages.** The form description it would hold is the JSON Schema that `SerializedWorkflowSpec` already gives, and the only one a web frontend can read.
- **`ess.apps` as the core's name.** The core is what apps are built on, not an app. Names that mean something else here were avoided: run (a neutron run), job (a cluster job), workflow (sciline), reduce (essreduce), stage and session (terms of the design).

## Consequences

- Workflow packages, remote clients, and GUIs install no scipp through the framework. scipp stays a test dependency of essdispatch.
- A change to the spec, the core, and the instrument specs is one pull request. The ess branch with `ess.reduce.spec` (scipp/ess#690) is replaced by essspec and is to be closed.
- essspec has its own tags (`essspec/*`) and its own ADRs (`packages/essspec/docs/developer/adr`, where scipp/ess keeps them), so moving it to scipp/ess is a variant of the move that scipp/ess's `ADDING_PACKAGES.md` describes: the history filtered to `packages/essspec`, and no tag rename, since its tags already carry the prefix.
- The packages are installed together from the checkout: `pip install -e packages/essspec -e 'packages/essdispatch[test]'`, where the test extra brings `essspec[scipp,sciline]`. Neither is on PyPI.
- The HTTP API is a public interface once a web frontend uses it, with its own versioning.
- The repository keeps its name, scipp/essapps.
