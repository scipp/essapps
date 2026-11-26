# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the page of the batch that follows shows of the steps of Follow:
:class:`LastStep`. :meth:`Pages.tick <.pages.Pages.tick>` steps and tells
it what came of the step.
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
    What Follow said at its last step: what the step raised
    (:attr:`failure`), and the label and the last step that did not raise,
    whose refusals :meth:`applied` empties. What it says of a batch is
    about the batch that follows now (:attr:`~ess.batch.Intake.following`),
    and nothing of another. Its callers hold the lock of all pages.

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

    def stepped(self, label: str, step: Step) -> None:
        """Keep ``step`` on the batch of ``label``, which did not raise."""
        self._last = (label, step)
        self.failure = None

    def _step(self, label: str) -> Step | None:
        """The last step that did not raise, if on ``label``, which follows."""
        if self._intake.following != label or self._last is None:
            return None
        on, step = self._last
        return step if on == label else None

    def token(self, label: str) -> object:
        """
        What the page of the batch of ``label`` shows of the last step, as a
        value to compare with one given before, and not to read: equal while
        what the page shows of Follow stays the same.
        """
        if self._intake.following != label:
            return None
        step = self._step(label)
        shown = None if step is None else (len(step.waiting), step.refused)
        return self.failure, shown

    def shown(self, label: str) -> Following:
        """
        Follow, as the page of the batch of ``label`` shows it: whether the
        batch follows, and while it does, what the last step raised and how
        many rows wait.
        """
        on = self._intake.following == label
        step = self._step(label)
        return Following(
            on=on,
            source=self._intake.source(label),
            interval=self._interval,
            failure=self.failure if on else None,
            waiting=0 if step is None else len(step.waiting),
        )

    def unsubmitted(self, label: str, states: Mapping[str, RowState]) -> dict[str, str]:
        """
        Why Follow could not submit each row of ``states`` that its next step
        would try again, by key; none unless the batch of ``label`` follows.
        """
        if (step := self._step(label)) is None:
            return {}
        return {
            key: f'Follow could not reduce it: {forms.message(step.refused[key])}'
            for key in not_reduced(states)
            if key in step.refused
        }

    def applied(self, label: str) -> None:
        """
        Forget why Follow could not submit rows of the batch of ``label``,
        whose page applied settings: its next step tries them with these.
        """
        if (step := self._step(label)) is not None:
            self._last = (label, replace(step, refused={}))
