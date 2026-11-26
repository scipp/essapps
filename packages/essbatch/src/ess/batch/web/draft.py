# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The settings form of a batch, a draft until Apply: :class:`Draft`.

The draft is held on the server, not in the browser:
:meth:`~ess.apps.Fields.params_and_missing` drops a partly filled field, so
the browser could not send the form back as it was. A :class:`~.page.Page`
holds one draft and creates a new one when the parameter columns of the
batch change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ess.apps.errors import FieldError
from ess.apps.fields import Fields
from ess.apps.listing import Listing

from ..reduction import Batch
from . import forms
from .models import Edit


class Draft:
    """
    The settings form of ``batch``, showing the batch's settings.

    It has a field per parameter except the source columns
    (:attr:`~ess.batch.Batch.source_columns`), which have no setting. The
    field of a parameter column may be left unset
    (:func:`.forms.params_and_missing`).

    Attributes
    ----------
    fields:
        The form.
    unfit:
        The settings that the form could not show at the last :meth:`reset`,
        such as those stored for another version of the spec, each with the
        reason.
    shown:
        What the form held at the last :meth:`reset`.
    """

    def __init__(self, batch: Batch, listing: Listing) -> None:
        self._params = batch.spec.params
        self._names = [
            n for n in self._params.model_fields if n not in batch.source_columns
        ]
        self._columns = batch.parameter_columns
        self.fields = self._form(listing)
        self.reset(batch.settings, listing)

    def _form(self, listing: Listing) -> Fields:
        """A form at the fields' defaults."""
        return Fields(self._params, listing, fields=self._names)

    def reset(self, settings: Mapping[str, Any], listing: Listing) -> None:
        """Show the batch's ``settings``. The form then holds no change."""
        # Settings stored for another version of the spec may not fit the form.
        # The batch retains them, and the page lists them, until Apply.
        self.unfit = self.fields.set_fitting(settings, listing)
        self.shown = self.params_and_missing()

    def fill(self, params: Mapping[str, Any], listing: Listing) -> None:
        """Show ``params``, all or none. Raises as :meth:`Fields.set`."""
        form = self._form(listing)
        form.set(params, listing)
        self.fields = form

    def edit(self, edit: Edit) -> None:
        """Apply a person's edit of the form. Raises as :func:`.forms.apply`."""
        forms.apply(self.fields, edit)

    def refresh(self, listing: Listing) -> None:
        """Offer what ``listing`` lists in the pickers."""
        self.fields.refresh(listing)

    def params_and_missing(self) -> tuple[dict[str, Any], list[FieldError]]:
        """
        What the form holds, and what is missing
        (:func:`.forms.params_and_missing`).
        """
        return forms.params_and_missing(self.fields, self._columns)

    def at_applied(self, errors: Iterable[FieldError]) -> list[FieldError]:
        """
        Those of ``errors`` that refer to no field, or to a field whose value
        is still that of the last :meth:`reset`, so that the form shows the
        invalid value.
        """
        given, shown = self.params_and_missing()[0], self.shown[0]
        return [
            e
            for e in errors
            if not e.loc or given.get(str(e.loc[0])) == shown.get(str(e.loc[0]))
        ]

    def unapplied(self) -> str | None:
        """
        What a person must do first while the form holds a change, such as
        ``'Apply or discard the settings'``; else ``None``.
        """
        held = self.params_and_missing()
        if held == self.shown:
            return None
        return f'{"Fill in" if held[1] else "Apply"} or discard the settings'
