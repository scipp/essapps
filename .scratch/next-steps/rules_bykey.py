"""Run a rules_*.py script with "handled" decided per row: the row's files at
`at` against those of the newest record under the label with the row's key."""
import runpy
import sys

import rules_common as rc
from ess.apps.batch import ready
from ess.spec.refs import iter_refs


def unhandled_by_key(batch, at):
    newest = {
        r.member: r for r in batch.client.records(label=batch.label) if r.member
    }
    states = batch.state()

    def new(key):
        cell = {ref for _, ref in iter_refs({at: batch.rows[key].values.get(at)})}
        held = rc.files_at(newest[key], at) if key in newest else set()
        return bool(cell - held)

    return [k for k in ready(states) if new(k)]


rc.unhandled = unhandled_by_key
runpy.run_path(sys.argv[1], run_name='__main__')
