"""Server.state() once, as is, after _relist(), at N rows. Run: python state_as_is.py N"""

import sys
import time

from harness import Setup

N = int(sys.argv[1])
s = Setup()
s.fill(N)
s.server._relist()
t0 = time.perf_counter()
s.server.state()
print(f'N={N} Server.state() as is, after _relist: {time.perf_counter() - t0:.1f} s', flush=True)
s.stop()
