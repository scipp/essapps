"""
Shared setup of the scripts in this folder.

Everything runs on the committed branch ``184-automatic-as-batch``, exported to
``branch/`` (``git archive``), because the checkout is edited by other sessions.
A ``Proposal`` holds files, a history, and a store that outlive a workspace, so
``restart`` is a restart of the process.

Run each script with ``/workspace/essapps/.scratch/venv-container/bin/python``.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / 'branch/packages/essapps'
sys.path.insert(0, str(SNAPSHOT / 'src'))
sys.path.insert(0, str(HERE))


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


table = _load('table_toys', SNAPSHOT / 'tests/batch/toys.py')
"""Toys of a person's batch: sample, transmission, thickness; setting binning."""
stories = _load('story_toys', SNAPSHOT / 'tests/stories/toys.py')
"""Toys of the user stories: IOFQ, IOFQ_V2, STITCH."""

from ess.apps import Client, Template, Workspace  # noqa: E402
from ess.apps.checks import Checks  # noqa: E402
from ess.apps.log import Log  # noqa: E402
from ess.apps.records import read_refs  # noqa: E402
from ess.apps.testing import FakeFiles, FakeStore, read_file  # noqa: E402
from ess.spec import FileRef, function_binding  # noqa: E402

import one  # noqa: E402


def _reduce(params: Any) -> Any:
    if params.binning.bins == 1:
        raise ZeroDivisionError('one bin')
    offset = 0.0 if params.offset is None else params.offset.value
    return table.Outputs(total=read_file(params.sample) / params.thickness + offset)


TABLE_BINDINGS = [function_binding(table.PLAIN, _reduce)]


class Proposal:
    """One proposal in one folder or service: files, a history, and a store."""

    def __init__(self, submitter: str = 'anna') -> None:
        self.directory = Path(tempfile.mkdtemp(prefix='one-interface-'))
        self.files = FakeFiles('p1', self.directory)
        self.log = Log()
        self.store = FakeStore()
        self.submitter = submitter
        self.workspace: Workspace | None = None

    def open(self, story_specs: list[Any] | None = None) -> Client:
        """A client of a new workspace on the same files, history, and store."""
        self.close()
        bound = (
            list(stories.TOYS.values())
            if story_specs is None
            else [stories.TOYS[stories.SpecId.of(s)] for s in story_specs]
        )
        self.workspace = Workspace(
            proposal='p1',
            files=self.files,
            bind=[*bound, *TABLE_BINDINGS],
            log=self.log,
            store=self.store,
        )
        return Client(self.workspace, submitter=self.submitter)

    restart = open

    def measure(self, run: int, content: Any, **fields: Any) -> FileRef:
        return self.files.measure(run, content, **fields)

    def sample(self, run: int, name: str) -> FileRef:
        """A sample run of the table toys, and its transmission run."""
        self.files.measure(run + 1000, 1.0, role='transmission', sample=name)
        return self.files.measure(run, float(run), role='sample', sample=name)

    def resolve(self, spec: Any) -> one.Resolve:
        """MISSING in ess.apps: see ``one.Resolve``."""
        checks = Checks(spec, self.files)

        def resolve(template: Template) -> dict[str, Any]:
            return read_refs(checks.template(template, 'p1')[1])

        return resolve

    def close(self) -> None:
        if self.workspace is not None:
            self.workspace.close()
            self.workspace = None


def texts(states: dict[str, Any]) -> dict[str, str]:
    return {key: state.text for key, state in states.items()}


def say(situation: str, *what: Any) -> None:
    print(f'{situation:<28}', *what)
