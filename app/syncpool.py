"""Replace ProcessPoolExecutor with an in-process executor.

sqlglot's optimizer/executor tests farm work out to spawned processes. Spawning
interpreters is expensive (and invisible to a tracer), so we run the work inline,
pickling arguments and results on the way through like a real pool would."""
import concurrent.futures
import concurrent.futures.process
import pickle


# Hooks for callers: TASK_HOOK(nodeid, k) is a context manager around task k of the
# current test (used by the map builder); SKIP(nodeid, k) -> True skips task k (the
# selector runs only the tasks a patch can reach). CURRENT is the running test id.
CURRENT = {"nodeid": None, "k": 0}
TASK_HOOK = None
SKIP = None


class SyncPool(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._initializer = initializer
        self._initargs = initargs
        self._inited = False

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            if not self._inited:
                self._inited = True
                if self._initializer is not None:
                    self._initializer(*self._initargs)
            idx = CURRENT["k"]
            CURRENT["k"] = idx + 1
            nid = CURRENT["nodeid"]
            if SKIP is not None and SKIP(nid, idx):
                f.set_result(None)
                return f
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            if TASK_HOOK is not None:
                with TASK_HOOK(nid, idx):
                    r = fn(*a, **k)
            else:
                r = fn(*a, **k)
            r = pickle.loads(pickle.dumps(r))
            f.set_result(r)
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def pytest_runtest_setup(item):
    CURRENT["nodeid"] = item.nodeid
    CURRENT["k"] = 0


def install():
    concurrent.futures.ProcessPoolExecutor = SyncPool
    concurrent.futures.process.ProcessPoolExecutor = SyncPool


install()
