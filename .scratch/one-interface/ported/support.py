"""What the ported tests need besides ``one``: a ``Batch`` given the missing
resolve call, made from the fake file source of the test that runs."""

import os
from typing import Any

import one
from ess.apps import SubmitError, Template
from ess.apps.checks import Checks
from ess.apps.records import read_refs

FILES: Any = None  # set by the fixture ``files`` of conftest.py


def resolver(spec: Any, files: Any, proposal: str = 'p1') -> one.Resolve:
    checks = Checks(spec, files)

    def resolve(template: Template) -> dict[str, Any]:
        if os.environ.get('NO_RESOLVE'):  # as if ess.apps had no such call
            raise SubmitError([])
        return read_refs(checks.template(template, proposal)[1])

    return resolve


def Batch(client: Any, spec: Any, **options: Any) -> one.Batch:  # noqa: N802
    return one.Batch(client, spec, resolve=resolver(spec, FILES), **options)
