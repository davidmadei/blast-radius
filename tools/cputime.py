import os, json, resource
import pytest
OUT = os.environ["CPU_OUT"]
_r = {}
def _cpu():
    a = resource.getrusage(resource.RUSAGE_SELF); b = resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime
def _wrap(ph):
    @pytest.hookimpl(hookwrapper=True)
    def w(item, *a, **k):
        t = _cpu()
        yield
        _r.setdefault(item.nodeid, {})[ph] = _cpu() - t
    return w
pytest_runtest_setup = _wrap("s")
pytest_runtest_call = _wrap("c")
pytest_runtest_teardown = _wrap("t")
@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    t = _cpu()
    yield
    if collector.nodeid.endswith(".py"):
        _r.setdefault("COLLECT:" + collector.nodeid, {})["c"] = _cpu() - t
def pytest_sessionfinish(session):
    json.dump(_r, open(OUT, "w"))
