# ADR 0007: Distributions are split by who depends on them and when they are released

- Status: accepted
- Deciders: Simon
- Date: 2026-10-26

## Context

A *distribution* is what pip installs, such as `essapps`; a *package* is what Python imports, such as `ess.apps`.
An *extra* is an optional set of dependencies of a distribution, installed as `pip install 'essapps[panel]'`.

The prototype was one distribution, `essapps`, that depended on essreduce for `ess.reduce.spec` and on sciline for one module.
Several programs will be built on it, and they need different things installed:

| Program | Runs | Needs |
|---|---|---|
| notebook, Jupyter app | VISA (ESS's remote desktops) or a laptop; both count as local | client, in-process backend, workflow packages, a GUI toolkit |
| web frontend (not Python) | in a browser, against a local backend or the service | an HTTP server in front of a backend |
| remote client, CLI | anywhere | client, HTTP client |
| the service | DMSC (ESS's data centre) | backend, HTTP server, durable storage, workers, SciCat (ESS's data catalogue), workflow packages |
| workflow package (esssans, ...) | everywhere a backend runs | the spec vocabulary, the binding protocol, and the bindings |

A *workflow package*, such as esssans or essreflectometry, holds the sciline workflows of one technique and its instruments.
The in-process backend needs nothing the client does not: pydantic and the standard library.
The heavy dependencies are elsewhere:

- **The science stack.** Installing essreduce pulls in scipp, scippnexus, scippneutron, dask, scipy, and graphviz. `ess.spec` needs only pydantic; a module that defines specs also imports scipp, for the types of its data values.
- **The service stack**: web framework, database, cluster launcher, SciCat client.
- **GUI toolkits**: ipywidgets, Panel, or a JavaScript build.

Workflow packages write specs and bindings.
If they had to import `ess.apps` for the binding protocol or for the bindings, such as `PipelineBinding`, every workflow package would depend on the framework.
The spec vocabulary still changes whenever the framework's design does, and nothing else imports it.

Each application chooses its own facility defaults, adapters, and which settings users may change.
esslivedata keeps these in one subpackage per instrument (`ess.livedata.config.instruments.loki`, ...), next to the instrument's params models.
Since this glue is the application's choice, not the workflow package's, the specs and bindings of offline reduction might never move into the workflow packages.

Code changes at two different rates.
The client, the backend, the HTTP layer, the GUIs, and the service change together.
A form decides what each field accepts with `ess.spec.places.place` and submits through the client.
The HTTP server and its client are two halves of one protocol.
The service hosts the backend.
An instrument's specs and bindings change when its users or its workflow package need it, independently of the core and of other instruments.

## Decision

**Code gets a distribution of its own when a different set of distributions must depend on it, or when it is released on a different schedule.**
**Code that needs dependencies only some users install is a subpackage behind an extra of the distribution it belongs to.**

```text
packages/
  essspec/          ess.spec               the spec vocabulary, the binding protocol, and the bindings
  essapps/          ess.apps               client, in-process backend, controllers, command-line tool
                    ess.apps.http.server   [server]    HTTP server in front of a backend
                    ess.apps.http.remote   [remote]    connect(), a client of that server
                    ess.apps.panel         [panel]     Panel forms and apps
                    ess.apps.service       [service]   storage, workers, SciCat, retention, auth, automatic reduction
                    ess.apps.writers       [scipp]     writers of outputs to files
  essinstruments/   ess.instruments        specs and bindings per instrument, extras [loki], [dream], ...
frontends/
  <app>/            a web frontend, against the HTTP API of ess.apps
```

The *framework* is `essspec` and `essapps`; the *core* is the plain install of `essapps`, without extras.
`essspec` exists.
`essapps` exists under the name `essdispatch` (`ess.dispatch`), and `ess.apps.panel` as the distribution `essdispatchpanel` (`ess.dispatchpanel`); the renames are decided and not done.
Each of the others is created with its first module, so none is an empty scaffold.

```text
essspec  <-  essapps  <-  frontends (over HTTP)
   ^            ^
   |            +- - - -  essinstruments[<instrument>-app]  (an instrument's app)
   +------------------  essinstruments, workflow packages
```

`essapps` does not depend on `essinstruments`: the backend finds bindings through entry points (below), and an app that needs an instrument's specs depends on both.

- **Names follow the ess monorepo**: distribution `essX`, package `ess.X`, one level under `ess`. A second distribution cannot install into a regular package (one with an `__init__.py`) such as `ess.apps`, and a namespace package (one without, which may span distributions) could not export `local` and the rest. So `ess.apps.panel` exists only as part of the `essapps` distribution.
- **`essspec` (`ess.spec`) is everything a workflow author writes against.** The vocabulary (`WorkflowSpec`, references, the functions that read field types, parameter models), the binding protocol (`Binding`, `StageBinding`, `AccumulatorBinding`, `Driver`), and the bindings `DriverBinding` and `function_binding` need only pydantic. A client imports `ess.spec` for the specs, and the bindings cost it nothing more. `ess.spec.conversions` needs scipp (extra `[scipp]`), and `ess.spec.pipeline` with `PipelineBinding` needs sciline (extra `[sciline]`). It is developed in this repository while it changes, and nothing outside this repository depends on a version of it. Once a second consumer, such as esslivedata or essnmx, is to adopt it, it moves to scipp/ess as a distribution of its own and is published from there.
- **`essapps` (`ess.apps`) is what notebooks and apps use**: records, the client, the in-process backend and its log, datasets, the controllers (batches, rules, the trigger loop), and fakes for tests. The core depends on essspec and pydantic, not on essreduce or sciline. The subpackages behind extras change in step with the core, so they are released with it. Importing one without its extra raises an `ImportError` that names the extra. A local backend serves a web frontend on VISA or a laptop through `ess.apps.http.server`, as the service does. The rules of the trigger loop are in the core; `ess.apps.service` runs the loop as a daemon from a configuration file.
- **`essinstruments` (`ess.instruments`) holds one subpackage per instrument, and is released on its own schedule.** A fix to LoKI's specs ships without whatever changed in the core since its last release, and a core release ships without unreleased instrument changes. It depends on essspec, not on essapps. Each instrument has two modules: `specs.py` with the specs, and `bindings.py` with their bindings, which need the instrument's workflow package, behind an extra such as `[loki]`, so that an install for LoKI does not install the workflow packages of every other instrument. The two are separate because a spec may move into its workflow package without its binding, and a type checker checks the binding against the spec. `ess.instruments.loki` holds the specs and bindings of LoKI's reductions; `ess.loki`, from esssans, holds the workflow they bind to. A spec can move into its workflow package only after essspec has moved to scipp/ess, and only once its binding is a plain `PipelineBinding`, with no facility defaults or adapters. Deployment configuration, such as automatic-reduction rules and data paths, is not here; it belongs to the service's configuration.
- **A Python client that imports spec modules installs the instrument's workflow package.** A local backend, a notebook, and a Panel app on VISA or a laptop run the bindings, so they have it anyway. A notebook against the service wants scipp and plotting for the outputs, so it runs in an ESS environment that has the workflow packages too. A client without workflow packages, such as a web frontend or the command-line tool with `--url`, reads `SerializedWorkflowSpec`: the JSON form of a spec, which names each data type as a string. A spec module imports scipp for the types of data values: a data value is a field of a group, typed by its scipp class, such as `intensity: sc.DataArray` of the group `IofQ`, so that a type checker checks a binding against the spec, and the backend checks by the group's class that an output fits a place.
- **The backend finds bindings through the entry-point group `ess.apps.workflows`.** An entry point is a name that a distribution registers under a group in its `pyproject.toml` (see the example below); `importlib.metadata.entry_points(group=...)` lists the entries of all installed distributions. Each entry names a function that returns a list of bindings, each of which names its spec. `local()` without `bind=`, the command-line tool, and the service read the group, so a distribution registers its workflows without importing the framework. Listing the specs this way imports the bindings; a client of the service gets the list from the service instead. An entry registers whether or not its extra is installed: an entry whose workflow package is missing is skipped, and `essapps specs` lists its specs as unavailable, naming the extra; any other import error fails the load. Two entries that register the same spec name and version fail the load, naming both entries, since picking one would leave records ambiguous about what ran. This is decided and not implemented.
- **The command-line tool is a client in `essapps`**: `ess.apps.cli`, with the script `essapps`. It builds its options from `SerializedWorkflowSpec`, so it runs any registered spec in its own process, and with `--url` any spec of the service without importing a workflow package. Each parameter is a group, a union of groups, or a list of one. A group that holds a file takes each of its fields as an option of its own, named `--<parameter>-<field>`. A file takes a dataset name or a path. A group that holds no file takes a record's output, or, if no field is a data value, its fields as JSON typed from the params schema. A union of groups takes one alternative's fields, with its tag, as JSON typed from the params schema. A file in it is written as a dataset reference, `{"@ref": "dataset", "dataset": "run:loki/60339"}`. Any other field takes a value typed from the params schema. A list parameter that is not a table takes a JSON list of its elements, typed from the params schema. A table takes a JSON or CSV file with one row per line. The tool needs only argparse, and `--url` needs `[remote]`. Writing outputs to files uses `ess.apps.writers`, which the service uses too ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)). A tool with instrument-specific arguments, such as essnmx's reducer, stays in its workflow package.
- **GUI code has one subpackage per toolkit, and no toolkit-independent GUI package.** A Python GUI builds its form from the models through `ess.spec.places.place`. A web frontend builds it from the params schema of `SerializedWorkflowSpec`, which `ess.spec` builds from the same function, so both apply the same rules. An app keeps its own logic, such as grouping runs by sample and angle, in plain classes apart from its widgets. A module of shared app logic is made once two apps share some. An instrument's app goes with its specs: in `essinstruments`, behind an extra such as `[amor-app]` that brings `essapps[panel]`. Once its specs move into the workflow package, the app moves with them, into a `gui` extra such as `essreflectometry[gui]`. Only that extra depends on essapps; the plain install of the workflow package depends on essspec only.

```python
from ess.apps import local, Template                # notebook or app
from ess.apps.panel import App                      # Panel app, needs essapps[panel]
from ess.instruments.loki.specs import IOFQ         # LoKI's specs
from ess.spec import WorkflowSpec                   # workflow author
from ess.spec.pipeline import PipelineBinding       # workflow author with a sciline workflow
```

```toml
# pyproject.toml of essinstruments, or one day of esssans
[project.entry-points."ess.apps.workflows"]
loki = "ess.instruments.loki.bindings:workflows"
```

```bash
pip install 'essapps[panel]' 'essinstruments[loki]'         # a LoKI app on VISA, once published
essapps specs                                                 # the registered specs
essapps run loki-iofq/1 --sample-file run:loki/4711 --reduction '{"bins": 200}'
essapps run loki-iofq/1 --sample-file run:loki/4711 --url https://reduce.example --proposal p1
```

## Alternatives considered

- **The HTTP layer, the GUIs, and the service as distributions of their own**, such as `essdispatchpanel` next to `essdispatch`. Each would pin a narrow range of the core and be released with it, and each name would repeat the core's.
- **Client and in-process backend as separate distributions.** They have the same dependencies, and apps on VISA use `local()`, so the split installs nothing less. The client imports the concrete `Backend`; a protocol for it comes with the remote backend.
- **The instruments' specs and bindings in `essapps`**, as `ess.apps.loki`. A fix to one instrument would ship whatever changed in the core. Since `ess.apps` is a regular package, moving an instrument out later renames every import of it.
- **The instruments' specs in `essspec`, with the bindings behind extras such as `essspec[loki]`.** `essspec[loki]` would depend on esssans, which will depend on `essspec` once it writes bindings. The vocabulary would carry this application's glue to every consumer, such as esslivedata, which has its own, and each instrument fix would be a release of the distribution that all of them pin.
- **The instruments' specs importable without their workflow packages**, as a distribution of their own, for a remote Python client. Such a client runs in an ESS environment, which has the workflow packages, and a client without them reads `SerializedWorkflowSpec`.
- **The spec vocabulary in essreduce.** Every client, remote ones included, would install the science stack. Each change would be a pull request in scipp/ess, made alongside the one here, with a branch pinned in between.
- **The binding protocol and the bindings in essreduce.** They are the other half of the contract with workflow packages, and essreduce would depend on a distribution that still changes.
- **Bindings only passed explicitly, as a list to `bind=`.** The command-line tool and the service would each need their own list of workflows, and a new workflow package would need a change to each.
- **The command-line tool in a distribution of its own.** It needs nothing that `essapps` lacks.
- **The HTTP server in the service.** A web frontend against a local backend needs it too, and so does development.
- **A generic GUI layer under the toolkit subpackages.** The description of a form that it would hold is `ess.spec.places.place`, which is in essspec already, and a web frontend reads its rules from the params schema of `SerializedWorkflowSpec`.
- **`ess.dispatch` as the core's name.** The distribution holds the apps as well as what they are built on, and `essapps` is the repository's name. Names that mean something else here were avoided: run (a neutron run), job (a cluster job), workflow (sciline), reduce (essreduce), stage and session (terms of the design).
- **`essappsinstruments` as the instruments' name.** It says whose glue it is, at the cost of length; `ess.instruments.loki` and `ess.loki` already say which is which.

## Consequences

- A plain install of essspec and essapps installs no scipp; scipp comes with the extras `[scipp]` and with the workflow packages.
- A change to the spec, the core, and the instrument specs is one pull request. A breaking change to essspec updates essapps, essinstruments, and their pins of essspec in that pull request, and each is then released with a tag of its own.
- Moving a spec into its workflow package is one coordinated release: the spec leaves `essinstruments`, and its extra pins the first version of the workflow package that has it, since two entries with the same spec fail the load.
- An instrument's app pins a range of `essapps`, as any app pins the framework it is built on.
- essspec has its own tags (`essspec/*`) and its own ADRs (`packages/essspec/docs/developer/adr`, where scipp/ess keeps them), so moving it to scipp/ess is a variant of the move that scipp/ess's `ADDING_PACKAGES.md` describes: the history filtered to `packages/essspec`, keeping only its own tags, and no tag rename, since they already carry the prefix. essapps and essinstruments are tagged `essapps/*` and `essinstruments/*`.
- None of the distributions is on PyPI. They are installed together from the checkout: `pip install -e packages/essspec -e 'packages/essapps[test]'`, where the test extra of essapps brings `essspec[scipp,sciline]` and its own extras. essspec can be published only once sciline has released `Stage`, which `essspec[sciline]` installs from a branch.
- Testing and type-checking essapps needs all its extras installed.
- The HTTP API is a public interface once a web frontend uses it, with its own versioning.
- The repository keeps its name, scipp/essapps.
