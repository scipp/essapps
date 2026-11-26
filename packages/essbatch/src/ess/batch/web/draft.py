# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The form of the settings of a batch, a draft until Apply: :class:`Draft`.

The draft stays on the server: what a form holds drops a field that is
partly filled (:meth:`~ess.apps.Fields.held`), so it cannot be sent to the
browser and back. A :class:`~.page.Page` holds one, writes it to the batch
at Apply, and builds another when the parameter columns of the batch change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ess.apps.errors import FieldError
from ess.apps.fields import Fields
from ess.apps.listing import Listing

from ..reduction import Batch
from . import forms
from .models import Edit


class Draft:
    """
    The form of the settings of ``batch`` as it is now: a field per
    parameter, but the columns that the source fills, which take no setting
    (:attr:`~ess.batch.Batch.fills`); a field of a parameter column may be
    left unset (:func:`.forms.held`). It starts showing the batch's settings.

    Attributes
    ----------
    fields:
        The form.
    unfit:
        The settings that the form could not show at the last :meth:`reset`,
        such as those kept for another version of the spec, each with why.
    shown:
        What the form held at the last :meth:`reset`.
    """

    def __init__(self, batch: Batch, listing: Listing) -> None:
        self._params = batch.spec.params
        self._names = [n for n in self._params.model_fields if n not in batch.fills]
        self._columns = batch.parameter_columns
        self.fields = self._form(listing)
        self.reset(batch.settings, listing)

    def _form(self, listing: Listing) -> Fields:
        """A form at the fields' defaults."""
        return Fields(self._params, listing, fields=self._names)

    def reset(self, settings: Mapping[str, Any], listing: Listing) -> None:
        """Show ``settings``, the batch's; the form then holds no change."""
        # Settings kept for another version of the spec may not fit the form;
        # the batch keeps them, and the page names them, until Apply.
        self.unfit = self.fields.set_fitting(settings, listing)
        self.shown = self.held()

    def fill(self, params: Mapping[str, Any], listing: Listing) -> None:
        """Show ``params``, all or none; raises as :meth:`Fields.set`."""
        form = self._form(listing)
        form.set(params, listing)
        self.fields = form

    def edit(self, edit: Edit) -> None:
        """Do what a person did in the form; raises as :func:`.forms.apply`."""
        forms.apply(self.fields, edit)

    def refresh(self, listing: Listing) -> None:
        """Offer what ``listing`` lists in the pickers."""
        self.fields.refresh(listing)

    def held(self) -> tuple[dict[str, Any], list[FieldError]]:
        """What the form holds, and what is missing (:func:`.forms.held`)."""
        return forms.held(self.fields, self._columns)

    def unapplied(self) -> str | None:
        """
        What a person does first while the form holds a change, since a
        reduction would use settings other than those shown; else ``None``.
        """
        held = self.held()
        if held == self.shown:
            return None
        return f'{"Fill in" if held[1] else "Apply"} or discard the settings'
