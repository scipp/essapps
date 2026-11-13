"""
Candidate B, watching the automatic reduction on a page: the web page of a
person's batch (``ess.apps.batch.web.server.Server``, unchanged) on the spike's
batch that an ``Automatic`` steps. The loop reads the settings of the page's
form, so the page is where the reduction is configured.

Run: .scratch/venv-container/bin/python .scratch/one-interface/page_one.py
"""

from typing import Any

from common import Proposal, say, stories

from ess.apps.batch import Row
from ess.apps.batch.web.models import Edit, Reduce
from ess.apps.batch.web.server import Server
from one import Automatic, Batch, arrived


def per_run(files: Any) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files]


def shown(server: Server) -> dict[str, str]:
    return {row.key: row.status for row in server.state().rows}


def make(p: Proposal, client: Any) -> tuple[Server, Automatic]:
    batch = Batch(
        client, stories.IOFQ, columns=('sample',), label='iofq', source=per_run,
        persist=True, resolve=p.resolve(stories.IOFQ),
    )  # fmt: skip
    server = Server(batch)  # type: ignore[arg-type]  # the branch's page
    # "follow": the loop runs what arrived, with the settings of the page's form
    auto = Automatic(
        batch, runs=arrived('sample'), settings=lambda: server.settings.params
    )
    return server, auto


def step(server: Server, auto: Automatic, client: Any) -> list[str]:
    with server.lock:  # the page takes one request at a time; so does the loop
        records = auto.step()
        client.wait(records)
        return list(records)


p = Proposal()
client = p.open()
server, auto = make(p, client)
for run in (1, 2):
    p.measure(run, [1.0, 2.0, 3.0, 4.0], role='sample')

# E2-like: the form is incomplete, so nothing runs, and the loop says why
assert step(server, auto, client) == []
say('settings incomplete', auto.refused)
server.edit_settings(Edit(path=['normalization'], op='choose', value='None'))
say('settings completed', 'the loop submits', step(server, auto, client), shown(server))

# a file that fails: the loop does not retry it, the page's primary button does
bad = p.measure(3, [1.0, 2.0, 3.0, 4.0], role='sample')
p.files.corrupt(bad)
ran = step(server, auto, client)
say('a file that fails', shown(server), '| the loop then submits', step(server, auto, client))
p.files.repair(bad)
say('  the page offers', repr(server.state().action.label))
server.reduce(Reduce(which='remaining'))
client.wait(auto.batch.state()['3'].record)
say('  after the button', shown(server), '| the loop then submits', step(server, auto, client))

# E4: a setting changed on the page applies to the runs that arrive after
server.edit_settings(Edit(path=['reduction', 'threshold'], op='set', value=1.5))
p.measure(4, [1.0, 2.0, 3.0, 4.0], role='sample')
say('E4 on the page', 'the loop submits', step(server, auto, client), shown(server))
say('  the page offers', repr(server.state().action.label))

# after a restart the page shows the same, once the form holds the settings again
client = p.restart()
server, auto = make(p, client)
auto.batch.scan()
server.use_settings('4')  # "use the settings of this result"
assert step(server, auto, client) == []
say('after a restart', shown(server), repr(server.state().action.label))
p.close()
