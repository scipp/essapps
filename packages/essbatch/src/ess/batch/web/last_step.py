# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the page of the batch that follows shows of the last step of Follow:
:class:`LastStep`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from ..auto import Step
from ..intake import Intake
from ..state import RowState, not_reduced
from . import forms
from .models import Following


class LastStep:
    """
    The outcome of the last step of Follow, for the page of the batch that
    follows.

    It reports only on the batch that follows now
    (:attr:`~ess.batch.Intake.following`). After Follow moves to another
    batch, it reports nothing on the batch before. Callers hold the lock of
    all pages.

    Parameters
    ----------
    intake:
        The intake that steps.
    interval:
        The seconds between two steps.

    Attributes
    ----------
    failure:
        What the last step raised, ``None`` if it raised nothing.
    """

    def __init__(self, intake: Intake, interval: float) -> None:
        self._intake = intake
        self._interval = interval
        self.failure: str | None = None
        self._last: tuple[str, Step] | None = None

    def record_step(self, label: str, step: Step) -> None:
        """Store ``step``, which did not raise, on the batch of ``label``."""
        self._last = (label, step)
        self.failure = None

    def _step(self, label: str) -> Step | None:
        """The last step that did not raise, if on ``label`` and ``label`` follows."""
        if self._intake.following != label or self._last is None:
            return None
        on, step = self._last
        return step if on == label else None

    def token(self, label: str) -> object:
        """
        A value that changes when what the page of ``label`` shows of Follow
        changes. Compare it, do not read it.
        """
        if self._intake.following != label:
            return None
        step = self._step(label)
        shown = None if step is None else (len(step.incomplete), step.submit_errors)
        return self.failure, shown

    def shown(self, label: str) -> Following:
        """Follow, as the page of the batch ``label`` shows it."""
        on = self._intake.following == label
        step = self._step(label)
        return Following(
            on=on,
            row_source=self._intake.row_source(label),
            interval=self._interval,
            failure=self.failure if on else None,
            incomplete=0 if step is None else len(step.incomplete),
        )

    def unsubmitted(self, label: str, states: Mapping[str, RowState]) -> dict[str, str]:
        """
        Why Follow could not submit each row that its next step tries again,
        by key. Empty unless the batch ``label`` follows.
        """
        if (step := self._step(label)) is None:
            return {}
        return {
            key: f'Follow could not reduce it: {forms.message(step.submit_errors[key])}'
            for key in not_reduced(states)
            if key in step.submit_errors
        }

    def clear_submit_errors(self, label: str) -> None:
        """
        Forget why Follow could not submit rows of the batch ``label``.

        Called when the page applies settings, since the next step tries
        these rows again with the new settings.
        """
        if (step := self._step(label)) is not None:
            self._last = (label, replace(step, submit_errors={}))
