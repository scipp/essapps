# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A tab to correct the metadata of runs by hand, such as the role of each.

An application names the fields with a pydantic model, so a person chooses
from what the application allows. The corrections go to the
:class:`~ess.apps.batch.CorrectedFiles` that the workspace reads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import panel as pn
from pydantic import BaseModel

from ess.apps import Client, Listing, Selector
from ess.apps.panel import Form, Table
from ess.spec import FileRef, field_types

from .. import CorrectedFiles
from .cells import cell_text, changed_cells, write_choice


class RunSheet:
    """
    A table of the raw files with the fields of ``model``, and an editor.

    ``then`` is called after a correction, such as the ``scan`` of a batch's
    view, so that the rows follow.
    """

    def __init__(
        self,
        files: CorrectedFiles,
        client: Client,
        model: type[BaseModel],
        *,
        then: Callable[[], None] | None = None,
    ) -> None:
        self._files = files
        self._client = client
        self._then = then
        self._listing = Listing(client)
        self._metadata: dict[FileRef, dict[str, Any]] = {}
        self._shown: dict[str, Any] = {}
        self._titles = {
            name: field_types.title(name, info)
            for name, info in model.model_fields.items()
        }
        self.form = Form(model, self._listing)
        self.table = Table[FileRef](
            ['File', *self._titles.values()], self._selected, selectable=True
        )
        apply = pn.widgets.Button(label='Correct selected', color='primary')
        apply.on_click(lambda _: self.correct())
        reset = pn.widgets.Button(label='Undo corrections of selected')
        reset.on_click(lambda _: self.reset())
        relist = pn.widgets.Button(label='List files')
        relist.on_click(lambda _: self.list_files())
        self.write = write_choice(self._titles)
        self._buttons = pn.Row(relist, self.write, apply, reset)
        self.list_files()

    def refresh(self, listing: Listing) -> None:
        """Keep ``listing``; files are listed only when asked."""
        self._listing = listing

    def list_files(self) -> None:
        """List the files and show their metadata as corrected."""
        files = self._client.files
        refs = files.list(Selector())
        self._metadata = dict(zip(refs, files.metadata(refs), strict=True))
        self.table.set(
            {
                ref: {
                    'File': self._listing.file_label(ref),
                    **{
                        title: cell_text(metadata[n], self._listing)
                        if n in metadata
                        else ''
                        for n, title in self._titles.items()
                    },
                }
                for ref, metadata in self._metadata.items()
            }
        )

    def _selected(self) -> None:
        key = self.table.selected
        metadata = {} if key is None else self._metadata[key]
        given = {n: metadata[n] for n in self._titles if n in metadata}
        self.form.set(given, self._listing)
        self._shown = self.form.held()[0]

    def correct(self) -> None:
        """Write the fields changed in the editor to the files selected."""
        changed = changed_cells(self.form.held()[0], self._shown, self.write.value)
        for ref in self.table.selected_keys:
            self._files.correct(ref, **changed)
        self.write.value = ''
        self._changed()

    def reset(self) -> None:
        """Forget the corrections of the files selected."""
        for ref in self.table.selected_keys:
            self._files.reset(ref)
        self._changed()

    def _changed(self) -> None:
        self.list_files()
        if self._then is not None:
            self._then()

    def panel(self) -> pn.Column:
        return pn.Column(
            self.table.widget,
            self.form.panel(),
            self._buttons,
            scroll=True,
            sizing_mode='stretch_both',
        )
