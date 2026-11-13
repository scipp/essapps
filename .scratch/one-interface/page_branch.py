"""
Candidate A, watching the automatic reduction on a page: the web page of a
person's batch (``ess.apps.batch.web.server.Server``) on the batch that an
``Automatic`` steps, in one process, on the branch as committed.

Run: .scratch/venv-container/bin/python .scratch/one-interface/page_branch.py
"""

from typing import Any

from common import Proposal, say, stories

from ess.apps.batch import Batch, Row
from ess.apps.batch.auto import Automatic
from ess.apps.batch.web.models import Reduce
from ess.apps.batch.web.server import Server


def per_run(files: Any) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files]


def shown(server: Server) -> dict[str, str]:
    return {row.key: row.status for row in server.state().rows}


p = Proposal()
client = p.open()
settings: dict[str, Any] = {'normalization': None}
batch = Batch(
    client, stories.IOFQ, columns=('sample',), label='iofq', source=per_run, persist=True
)
auto = Automatic(batch, at='sample', settings=lambda: settings)
server = Server(batch)  # the page of a person's batch, on the loop's batch
version = server.state().version

for run in (1, 2):
    p.measure(run, [1.0, 2.0, 3.0, 4.0], role='sample')
with server.lock:  # the loop takes the page's lock, since Batch has none
    client.wait(auto.step())
state = server.poll(version)
assert state is not None  # the page sees the loop's records at its next poll
say('page after a step', shown(server))

# NOTE: the page has settings of its own, the form's. They are incomplete
# (normalization has no default), so the page shows the results as outdated.
say('settings of the page', server.settings.params, '| of the loop', settings)
server.use_settings('1')  # the page's "use the settings of this result"
# NOTE: still outdated. The form writes every default out, and the branch
# compares requests as given (#191), so the primary button offers to reduce
# every row of the automatic batch again.
say("  with the result's settings", shown(server), repr(server.state().action.label))
assert server.state().action.remaining == 2

# A page with a Batch of its own (same label) never shows a row as outdated:
# that Batch submitted none of the rows.
other = Batch(
    client, stories.IOFQ, columns=('sample',), label='iofq', source=per_run, persist=True
)
other.scan()
page = Server(other)
page.use_settings('1')
say('page with its own Batch', shown(page), repr(page.state().action.label))
p.close()
