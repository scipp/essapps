# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates and the trigger loop: batch and automatic reduction over records.

A template is an immutable, versioned partial request. A batch is the records
made from one template under one label, keyed per member. Automatic reduction
is the trigger loop instantiating a template for each new dataset a rule
matches; its records are a batch labelled by the template it is bound to.

The architecture (D14) goes further than this module: one label field in place
of ``slot`` and ``batch``, rules as stored data with a lower bound and an
active state, one ``apply`` operation behind the batch form and the loop, and
a loop that asks the record store which datasets still need firing on instead
of remembering them. This module keeps the callable rule and the seen-set until
that lands.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .backend import SubmitError
from .client import Client
from .records import RunRecord, RunRequest
from .spec import SpecId, WorkflowSpec, data_ref_fields
from .testing import Dataset


class Template(BaseModel, frozen=True):
    """A partial request; ``blanks`` are the fields a use must supply."""

    name: str
    version: int = 1
    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)
    blanks: tuple[str, ...] = ()
    derived_from: str | None = None

    @classmethod
    def from_request(
        cls,
        name: str,
        request: RunRequest,
        spec: WorkflowSpec,
        blank: Iterable[str] = (),
    ) -> Template:
        """Save a request as a template with its data-reference fields blank."""
        blanks = tuple(sorted(set(data_ref_fields(spec.params)) | set(blank)))
        params = {k: v for k, v in request.params.items() if k not in blanks}
        return cls(name=name, spec=request.spec, params=params, blanks=blanks)

    def revise(self, **changes: Any) -> Template:
        """A new version by copy; the old one stays."""
        return self.model_copy(
            update={
                'version': self.version + 1,
                'params': self.params | changes,
                'derived_from': f'{self.name}/v{self.version}',
            }
        )

    def fill(self, **values: Any) -> dict[str, Any]:
        missing = set(self.blanks) - values.keys()
        if missing:
            raise ValueError(f'template {self.name} needs {sorted(missing)}')
        return self.params | values


def batch(
    client: Client,
    template: Template,
    members: Mapping[str, Mapping[str, Any]],
    *,
    batch_id: str,
) -> dict[str, RunRecord]:
    """One request per member, keyed by a meaningful member key; validated whole."""
    group = {
        key: client.request(
            template.spec, template.fill(**fills), batch=batch_id, member_key=key
        )
        for key, fills in members.items()
    }
    return client.submit_group(group)


class DatasetSource(Protocol):
    def new_datasets(self, proposal: str) -> Iterable[Dataset]: ...


Rule = Callable[[Dataset], Mapping[str, Any] | None]
"""Maps a dataset to the template fills it should trigger, or None to ignore it."""


class TriggerStatus(BaseModel):
    last_fire: datetime | None = None
    last_refusal: datetime | None = None
    last_errors: tuple[str, ...] = ()
    fired: int = 0


class TriggerLoop:
    """
    On a new dataset matching ``rule``, instantiate ``template`` and submit.

    Bound to one template version. Datasets are deduplicated by PID, so repeated
    delivery is a no-op; a refusal at submission is as visible as a failed run.
    """

    def __init__(
        self,
        client: Client,
        template: Template,
        source: DatasetSource,
        rule: Rule,
        *,
        dataset_field: str,
    ) -> None:
        self.client = client
        self.template = template
        self.source = source
        self.rule = rule
        self.dataset_field = dataset_field
        self.status = TriggerStatus()
        self._seen: set[str] = set()

    def run_once(self) -> list[RunRecord]:
        fired = []
        for dataset in self.source.new_datasets(self.client.proposal):
            if dataset.pid in self._seen:
                continue
            self._seen.add(dataset.pid)
            fills = self.rule(dataset)
            if fills is None:
                continue
            ref = self.client.dataset(dataset.pid, dataset.path)
            params = self.template.fill(**{self.dataset_field: ref, **fills})
            request = self.client.request(
                self.template.spec,
                params,
                batch=f'{self.template.name}/v{self.template.version}',
            )
            try:
                fired.append(self.client.submit(request))
            except SubmitError as e:
                self.status.last_refusal = datetime.now(UTC)
                self.status.last_errors = tuple(str(e).splitlines()[1:])
                continue
            self.status.last_fire = datetime.now(UTC)
            self.status.fired += 1
        return fired
