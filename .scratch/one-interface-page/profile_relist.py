"""Server.state() right after the listing is made again, as each poll with a new record does."""
import cProfile, pstats, sys, time
from harness import Setup
N = int(sys.argv[1])
s = Setup(); s.fill(N); srv = s.server
t0 = time.perf_counter(); srv.state(); print('state, warm listing', f'{(time.perf_counter()-t0)*1e3:.0f} ms')
srv._relist()
pr = cProfile.Profile(); pr.enable()
t0 = time.perf_counter(); srv.state(); t = time.perf_counter()-t0
pr.disable(); print('state after _relist', f'{t*1e3:.0f} ms')
t0 = time.perf_counter(); srv.state(); print('state again', f'{(time.perf_counter()-t0)*1e3:.0f} ms')
pstats.Stats(pr).sort_stats('cumulative').print_stats(18)
s.stop()
