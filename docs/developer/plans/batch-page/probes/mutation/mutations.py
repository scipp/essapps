# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Mutations of ess.apps.batch, as (id, file under src/ess/apps/batch, search string,
replacement, what it breaks). ``mutate.py`` applies them one at a time.
"""

S = 'state.py'
A = 'auto.py'
R = 'reduction.py'
T = 'table.py'
D = 'definition.py'
W = 'web/server.py'

MUTATIONS = [
    # state.py
    (
        'S1',
        S,
        "return str(self.status) if self.tweaked else 'outdated'",
        "return 'outdated'",
        'RowState.text: a tweak not compared reads outdated too',
    ),
    (
        'S2',
        S,
        "        if name not in columns\n",
        "        if name not in columns and False\n",
        'problems: a value at a name that is not a column is no problem',
    ),
    (
        'S3',
        S,
        "if column not in row.values and model.model_fields[column].is_required()",
        "if column not in row.values",
        'problems: an empty cell with a default is a problem',
    ),
    (
        'S4',
        S,
        "status.get(ref.record) in _RERUN",
        "status.get(ref.record) is Status.FAILED",
        'problems: a cell reading a cancelled record is no problem',
    ),
    (
        'S5',
        S,
        "return {r.member: r for r in records if r.member is not None}",
        "return {r.member: r for r in reversed(list(records)) if r.member is not None}",
        'results: the oldest record is the result',
    ),
    (
        'S6',
        S,
        "if n not in now or n not in was or now[n] != was[n]]",
        "if now.get(n) != was.get(n)]",
        'differs: a value lacking on one side equals None',
    ),
    (
        'S7',
        S,
        "outdated = bool(names & set(columns) if tweak else names)",
        "outdated = bool(names)",
        'row_states: a tweak is outdated at any change',
    ),
    (
        'S8',
        S,
        (
            "        if row.key in spec_changed:\n"
            "            outdated: bool | None = True\n        elif names is None:"
        ),
        "        if names is None:",
        'row_states: a result of another spec is not outdated by itself',
    ),
    (
        'S9',
        S,
        "if not s.problems and s.status is not Status.PENDING",
        "if not s.problems",
        'ready: a running row is ready',
    ),
    (
        'S10',
        S,
        "if not s.problems and s.status is not Status.PENDING",
        "if s.status is not Status.PENDING",
        'ready: a row with problems is ready',
    ),
    (
        'S11',
        S,
        "or states[k].status in _RERUN ",
        "",
        'remaining: failed/cancelled rows are not left',
    ),
    (
        'S12',
        S,
        " or states[k].outdated\n",
        "\n",
        'remaining: outdated rows are not left',
    ),
    (
        'S13',
        S,
        "        and changed[k].isdisjoint(columns)\n",
        "",
        'earlier_settings: a row changed at a column counts as earlier settings',
    ),
    (
        'S14',
        S,
        "        and not states[k].spec_changed\n",
        "",
        'earlier_settings: a result of another spec counts',
    ),
    (
        'S15',
        S,
        "        and not states[k].tweaked\n",
        "",
        'earlier_settings: a tweak counts',
    ),
    (
        'S16',
        S,
        "if states[k].status is Status.COMPLETED\n",
        "if True\n",
        'earlier_settings: a failed result counts',
    ),
    (
        'S17',
        S,
        "and changed[k]\n        and changed[k].isdisjoint",
        "and changed[k].isdisjoint",
        'earlier_settings: an unchanged result counts',
    ),
    (
        'S18',
        S,
        "if states[k].record is None or not changed[k].isdisjoint(at)",
        "if not changed[k].isdisjoint(at)",
        'arrived: a row with no result does not arrive',
    ),
    (
        'S19',
        S,
        "if states[k].record is None or not changed[k].isdisjoint(at)",
        "if states[k].record is None or changed[k]",
        'arrived: any change (settings too) arrives',
    ),
    (
        'S20',
        S,
        "        for k in ready(states)\n        if states[k].record is None or not",
        "        for k in states\n        if states[k].record is None or not",
        'arrived: rows not ready (running, incomplete) arrive',
    ),
    (
        'S21',
        S,
        "return [k for k in remaining(states) if k not in earlier]",
        "return list(remaining(states))",
        'left_to_reduce: equals remaining',
    ),
    (
        'S22',
        S,
        "    if not_compared := sorted(states.keys() - changed.keys()):",
        "    if False:",
        '_compared: rows not compared are not refused',
    ),
    # auto.py
    (
        'A2',
        A,
        "return {}, refused | dict.fromkeys(keys, str(error))",
        "return {}, refused",
        '_reduce: a refusal naming no row records no reason',
    ),
    (
        'A3',
        A,
        "refused[key] = str(SubmitError(errors))",
        "refused[key] = str(error)",
        '_reduce: each row gets the whole error, not its own reasons',
    ),
    (
        'A4',
        A,
        "self.refused = dict.fromkeys(ready(batch.results()), str(error))",
        "self.refused = {}",
        'reduce: refused settings give no reasons',
    ),
    (
        'A5',
        A,
        "self.refused = dict.fromkeys(ready(batch.results()), str(error))",
        "pass",
        'reduce: refused settings keep the reasons of the step before',
    ),
    (
        'A6',
        A,
        "records, self.refused = self._reduce(arrived(state, self.at))",
        "records, _ = self._reduce(arrived(state, self.at))",
        'reduce: refused never updated',
    ),
    (
        'A8',
        A,
        "        while keys:\n",
        "        if keys:\n",
        '_reduce: no retry after dropping the rows named',
    ),
    (
        'A9',
        A,
        "self._reduce(arrived(state, self.at))",
        "self._reduce(arrived(state, set(self.batch.columns)))",
        'reduce: at ignored, all columns',
    ),
    (
        'A10',
        A,
        "        if unknown := sorted(set(at) - set(batch.columns)):",
        "        if False:",
        '__init__: at with unknown columns accepted',
    ),
    # reduction.py
    (
        'R1',
        R,
        (
            "asked[row.key] = {\n"
            "                    c: cells[c] for c in state.differs(cells, was,"
            " self.columns)\n"
            "                }"
        ),
        "asked[row.key] = {}",
        '_compare: cells of a row with a result are never compared',
    ),
    (
        'R2',
        R,
        (
            "            if not required <= row.values.keys():\n"
            "                continue  # an empty cell is a problem, and nothing to"
            " resolve\n"
        ),
        "",
        '_compare: rows with empty required cells are resolved too',
    ),
    (
        'R3',
        R,
        "names = [*self.settings_fields, *cells_now[key]]",
        "names = [*cells_now[key]]",
        '_compare: settings are not compared',
    ),
    (
        'R4',
        R,
        "    hash(value)\n    return (type(value), value)",
        "    hash(value)\n    return value",
        '_typed: cache key without the type',
    ),
    (
        'R5',
        R,
        "return (name, _typed(value))",
        "return ('', _typed(value))",
        '_kept: cache key without the column',
    ),
    ('R6', R, "        self._resolved.clear()\n", "", 'scan: cache not dropped'),
    (
        'R7',
        R,
        "                    self._resolved[at] = result.params[name]",
        "                    pass",
        '_resolve_cells: nothing cached',
    ),
    (
        'R8',
        R,
        "new = [i for i in changes.records if i not in self._seen]",
        "new = list(changes.records)",
        '_poll: records seen before read again',
    ),
    (
        'R9',
        R,
        "records = [r for r in self._known(new) if r.label == self.label]",
        "records = self._known(new)",
        '_poll: records of other labels taken',
    ),
    (
        'R10',
        R,
        "            if any(e.loc for e in error.errors):\n                raise",
        "            raise",
        '_submit: a failed stage is not replaced',
    ),
    (
        'R11',
        R,
        "            if any(e.loc for e in error.errors):\n                raise\n",
        "            pass\n",
        '_submit: the stage is replaced for a row refusal too',
    ),
    (
        'R12',
        R,
        "(*states[k].problems, *_running(states[k]))",
        "(*states[k].problems,)",
        'reduce: a running row is submitted again',
    ),
    (
        'R13',
        R,
        "        if refused:\n            raise SubmitError(refused)\n",
        "",
        'reduce: no check before submit (not all-or-nothing on problems/running)',
    ),
    (
        'R14',
        R,
        "e if e.loc and e.loc[0] in self.columns else replace(e, entry=None)",
        "e",
        '_once: a settings reason is named per row',
    ),
    ('R15', R, "        self._tweaked[key] = record.id\n", "", 'tweak: not remembered'),
    (
        'R16',
        R,
        (
            "        if running := _running(self.results([key])[key]):\n"
            "            raise SubmitError(running)\n"
        ),
        "",
        'tweak: a running row can be tweaked',
    ),
    (
        'R17',
        R,
        "if self._stage is None or self._stage[0] != template:",
        "if self._stage is None:",
        '_open: stage kept when the settings change',
    ),
    (
        'R18',
        R,
        "r.spec != spec or r.id in self._unbound",
        "r.spec != spec",
        '_spec_changed: unbound records not of another spec',
    ),
    (
        'R19',
        R,
        "chosen = [r for k, r in self._found.items() if keys is None or k in keys]",
        "chosen = list(self._found.values())",
        'cancel: keys ignored',
    ),
    (
        'R20',
        R,
        "record = None if row.spec_changed else row.record",
        "record = row.record",
        'tweak_params: params of another spec used',
    ),
    (
        'R21',
        R,
        "{k for k, r in found.items() if self._tweaked.get(k) == r.id}",
        "{k for k in found if k in self._tweaked}",
        '_states: a tweak stays tweaked after a newer record',
    ),
    (
        'R22',
        R,
        "            return [r for i in ids for r in self._known([i])]",
        "            raise",
        '_known: one unknown record breaks all',
    ),
    (
        'R24',
        R,
        "self.client.cancel([r for r in chosen if status[r.id] is Status.PENDING])",
        "self.client.cancel(chosen)",
        'cancel: finished records cancelled too',
    ),
    (
        'R25',
        R,
        "    def rows(self, rows: RowTable) -> None:\n",
        "    def rows(self, rows: RowTable) -> None:\n        self._rows = rows\n",
        'rows setter: assigned before the definitions keep it',
    ),
    (
        'R26',
        R,
        "        settings = self._own(settings)\n        if self._definitions",
        (
            "        settings = self._own(settings)\n"
            "        self._settings = settings\n        if self._definitions"
        ),
        'settings setter: assigned before the definitions keep it',
    ),
    # table.py
    (
        'T1',
        T,
        "                removed.discard(row.key)\n",
        "",
        'put: a removed row is not brought back',
    ),
    (
        'T2',
        T,
        "                edits.pop(row.key, None)  # as :meth:`add`\n",
        "",
        'put: a row added takes edits left from a row gone',
    ),
    (
        'T3',
        T,
        "if (c in row.values, row.values.get(c)) != (c in shown, shown.get(c))",
        "if True",
        'put: every column becomes an edit (stops following the scan)',
    ),
    (
        'T4',
        T,
        "removed = self.removed | {key} if in_scan else self.removed",
        "removed = self.removed | {key}",
        'remove: a row added is marked removed',
    ),
    (
        'T5',
        T,
        (
            "        edits = {k: v for k, v in self.edits.items() if k != key}\n"
            "        added = tuple("
        ),
        "        edits = dict(self.edits)\n        added = tuple(",
        'remove: edits of a removed row kept',
    ),
    (
        'T6',
        T,
        "return replace(self, edits={**edits, key: kept} if kept else edits)",
        "return replace(self, edits={**edits, key: kept})",
        'reset of a cell: empty edits entry left',
    ),
    (
        'T7',
        T,
        "            removed=self.removed - {key},",
        "            removed=self.removed,",
        'reset: a removed row stays removed',
    ),
    (
        'T8',
        T,
        "values={column: v for column, v in cells.items() if v is not EMPTY}",
        "values=cells",
        '_with_edits: EMPTY shows as a value',
    ),
    (
        'T9',
        T,
        "hidden = self.removed | {row.key for row in self.added}",
        "hidden = self.removed",
        '_rows: a scanned row of an added key is not hidden',
    ),
    (
        'T10',
        T,
        "return replace(self, scan=_unique(rows))",
        "return replace(self, scan=tuple(rows))",
        'rescan: a key twice accepted',
    ),
    (
        'T11',
        T,
        "if k != row.key}",
        "}",
        'add: a row added takes edits left from a row gone',
    ),
    (
        'T12',
        T,
        "if edits is None and not any(v is EMPTY for v in row.values.values()):",
        "if edits is None:",
        '_with_edits: EMPTY given by a scan shows',
    ),
    # definition.py
    (
        'D1',
        D,
        "        cleared[key] = [c for c, v in cells.items() if v is EMPTY]\n",
        "",
        'edits_data: cleared cells not saved',
    ),
    (
        'D2',
        D,
        (
            "            edits[key] = {**edits.get(key, {}),"
            " **dict.fromkeys(columns, EMPTY)}"
        ),
        "            pass",
        'edits_of: cleared cells not loaded',
    ),
    (
        'D3',
        D,
        (
            "        for left in path.parent.glob(f'.{path.name}.*'):\n"
            "            left.unlink(missing_ok=True)"
        ),
        "        for left in []:\n            pass",
        'load: left temporary files not removed',
    ),
    (
        'D4',
        D,
        "        content = to_json(data, indent=1)\n",
        (
            "        content = to_json(data, indent=1)\n"
            "        path.write_bytes(content)\n        return\n"
        ),
        'save: written in place, not atomic',
    ),
    (
        'D5',
        D,
        "quote(name, safe='').replace('.', '%2E')",
        "quote(name, safe='')",
        'path: dots not escaped',
    ),
    (
        'D6',
        D,
        "if len(written.encode()) + 10 > _NAME_MAX:",
        "if len(written.encode()) > _NAME_MAX:",
        'path: room for the temporary suffix not reserved',
    ),
    # web/server.py
    (
        'W1',
        W,
        "        if refused:\n            return 'incomplete'\n",
        "",
        '_status: a row Follow refused does not read incomplete',
    ),
    (
        'W2',
        W,
        (
            "        if state.status in _ENDED and not state.problems:\n"
            "            return str(state.status)\n"
        ),
        "",
        '_status: failed/cancelled overridden by outdated',
    ),
    (
        'W3',
        W,
        "        if earlier:\n            return 'done'\n",
        "",
        '_status: earlier-settings row reads outdated',
    ),
    (
        'W4',
        W,
        "if state.status in _ENDED and not state.problems:",
        "if state.status in _ENDED:",
        '_status: failed row with problems reads failed',
    ),
    (
        'W5',
        W,
        "        elif kinds == {'failed'}:\n            label = f'Retry {n} failed'\n",
        "",
        "_primary: no 'Retry n failed'",
    ),
    (
        'W6',
        W,
        (
            "        elif kinds <= {'failed', 'cancelled'}:\n"
            "            label = f'Retry {n}'\n"
        ),
        "",
        "_primary: mixed failed+cancelled reads 'Reduce n remaining'",
    ),
    (
        'W7',
        W,
        (
            "        elif kinds == {'outdated'}:\n"
            "            label = f'Reduce {n} outdated'\n"
        ),
        "",
        "_primary: no 'Reduce n outdated'",
    ),
    (
        'W8',
        W,
        "again=len(ready(states)),",
        "again=n,",
        '_primary: again counts left rows',
    ),
    (
        'W9',
        W,
        "earlier=earlier,\n            running",
        "earlier=0,\n            running",
        '_primary: earlier count 0',
    ),
    (
        'W10',
        W,
        "running=sum(s.status is Status.PENDING for s in states.values()),",
        "running=0,",
        '_primary: running count 0',
    ),
    (
        'W10b',
        W,
        "progress=_progress(states),",
        "progress=None,",
        '_primary: no progress',
    ),
    (
        'W11',
        W,
        "if changes.records or self.batch.rows != self._rows:",
        "if changes.records:",
        'poll: rows changed by another caller do not bump version',
    ),
    (
        'W12',
        W,
        "if after == self.version and start == self.start:",
        "if after == self.version:",
        'poll: restart ignored',
    ),
    (
        'W13',
        W,
        "if changes.records or self.batch.rows != self._rows:",
        "if self.batch.rows != self._rows:",
        'poll: new records do not bump version',
    ),
    (
        'W14',
        W,
        (
            "            self._relist()\n            self.version += 1\n"
            "        self._rows = self.batch.rows\n        if after"
        ),
        (
            "            self.version += 1\n        self._rows = self.batch.rows\n"
            "        if after"
        ),
        'poll: listing not refreshed',
    ),
    (
        'W15',
        W,
        "        return records or runs\n",
        "        return runs\n",
        '_caught_up: new records do not count as change',
    ),
    (
        'W16',
        W,
        "        return records or runs\n",
        "        return records\n",
        '_caught_up: moved runs do not count',
    ),
    (
        'W17',
        W,
        "            or self.waiting != waiting\n",
        "",
        '_step: refusals do not count',
    ),
    (
        'W18',
        W,
        "            or self.step_failure != failure\n",
        "",
        '_step: failure does not count',
    ),
    (
        'W19',
        W,
        "            or self.batch.rows != rows\n",
        "",
        '_step: rows do not count',
    ),
    (
        'W20',
        W,
        "        return (\n            moved\n",
        "        return True or (\n            moved\n",
        '_step: every step changes the version',
    ),
    (
        'W21',
        W,
        "            self.step_failure = None\n",
        "",
        '_step: failure sticks after success',
    ),
    (
        'W22',
        W,
        "                self.version += 1\n            self._rows = self.batch.rows\n",
        "                self.version += 1\n",
        'step: _rows not updated',
    ),
    (
        'W23',
        W,
        "if self.loop is not None and self.following\n",
        "if self.loop is not None\n",
        'state: refusals by Follow shown when not following',
    ),
    (
        'W24',
        S,
        "        arrived(states, at)\n        if compared(states)",
        "        list(states)\n        if compared(states)",
        'refused_again: all rows, not arrived',
    ),
    (
        'W25',
        S,
        "        else [k for k, s in states.items() if s.record is None]",
        "        else list(states)",
        'refused_again: done rows marked refused while settings refused',
    ),
    (
        'W26',
        W,
        "                server.version += 1\n",
        "",
        '_action: version not bumped',
    ),
    (
        'W27',
        W,
        "                server._rows = server.batch.rows\n",
        "",
        '_action: _rows not updated',
    ),
    (
        'W28',
        W,
        (
            "                server._settings_shown = None  # the form shows the"
            " batch's again\n"
        ),
        "",
        '_action: form not reset after a change not kept',
    ),
    (
        'W29',
        W,
        "        if settings == self._settings_shown:\n            return\n",
        "",
        '_show_settings: form always reset to the batch',
    ),
    (
        'W30',
        W,
        "        if not missing:\n            self.batch.settings = given",
        "        if True:\n            self.batch.settings = given",
        '_write_settings: written with something missing',
    ),
    (
        'W31',
        W,
        (
            "            self._settings_shown = self.batch.settings\n"
            "            self._unfit = []"
        ),
        "            self._unfit = []",
        '_write_settings: shown not recorded',
    ),
    (
        'W31b',
        W,
        (
            "            self._settings_shown = self.batch.settings\n"
            "            self._unfit = []\n"
        ),
        "            self._settings_shown = self.batch.settings\n",
        '_write_settings: unfit kept',
    ),
    (
        'W32',
        W,
        "if not (e.entry in keys and e.loc and str(e.loc[0]) == column)",
        "if not (e.entry in keys)",
        '_forget: all reasons of the row forgotten',
    ),
    (
        'W33',
        W,
        "        self._forget(edit.keys, edit.column)\n",
        "",
        'edit: refusal not forgotten',
    ),
    (
        'W34',
        W,
        "'earlier': lambda s: earlier_settings(s, columns),",
        "'earlier': lambda s: left_to_reduce(s, columns),",
        "reduce: 'earlier' reduces left_to_reduce",
    ),
    (
        'W35',
        W,
        "'remaining': lambda s: left_to_reduce(s, columns),",
        "'remaining': ready,",
        "reduce: 'remaining' reduces every ready row",
    ),
    (
        'W35b',
        W,
        "'all': ready,",
        "'all': lambda s: left_to_reduce(s, columns),",
        "reduce: 'all' reduces only left_to_reduce",
    ),
    (
        'W36',
        W,
        "        self.refused = []\n        self._relist()",
        "        self._relist()",
        'reduce: refusals kept after success',
    ),
    (
        'W37',
        W,
        "if e.entry or (path and tuple(e.loc[:1]) != tuple(path[:1]))",
        "if e.entry",
        '_forget_settings: any setting edit forgets all setting refusals',
    ),
    (
        'W39',
        W,
        "if self.loop is not None and self.following:\n            # Follow",
        "if self.loop is not None:\n            # Follow",
        'submit_tweak: notice when not following',
    ),
    (
        'W41',
        W,
        "result = 'outdated' if status == 'outdated' else 'current'",
        "result = 'current'",
        "_row: result always 'current'",
    ),
    (
        'W43',
        W,
        "            if self.following:\n                self.step()",
        "            self.step()",
        'tick: steps when not following',
    ),
    (
        'W44',
        W,
        "        self._step()\n        return 'Following.'",
        "        return 'Following.'",
        'follow: no step at once',
    ),
    (
        'W46',
        W,
        "s.record.created >= since",
        "s.record.created > since",
        '_progress: off by one',
    ),
    (
        'W52',
        W,
        "            self._relist()\n            self._new_runs()\n",
        "            self._relist()\n",
        '_scanned: runs version not bumped by a scan or a correction',
    ),
    (
        'W53',
        W,
        "        self._runs = self._runs_seen()\n        self.runs_version += 1\n",
        "        self._runs = self._runs_seen()\n",
        '_new_runs: runs version never bumped',
    ),
    (
        'W54',
        W,
        (
            "        if row.spec_changed:\n            raise Problem(\n"
            "                f'{name} was reduced by another"
        ),
        (
            "        if False:\n            raise Problem(\n"
            "                f'{name} was reduced by another"
        ),
        'use_settings: settings of another spec taken',
    ),
]
