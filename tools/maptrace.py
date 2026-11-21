"""pytest plugin: per-test line coverage + file opens, for building the map."""
import os, sys, json, atexit
import coverage

ROOT = os.environ["MAP_ROOT"]
OUT = os.environ["MAP_OUT"]
_cur = {"ctx": "", "collect": ""}
_opens = {}

def _hook(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(p)
            except Exception:
                return
            ap = os.path.abspath(p)
            if ap.startswith(ROOT) and not ap.endswith((".py", ".pyc")):
                key = _cur["ctx"] or ("COLLECT:" + _cur["collect"])
                _opens.setdefault(key, set()).add(os.path.relpath(ap, ROOT))

sys.addaudithook(_hook)
cov = coverage.Coverage(data_file=OUT + ".cov", source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")], config_file=False)
cov.start()

import importlib._bootstrap_external as _be
_orig_exec = _be._LoaderBasics.exec_module
_stack = []

def _exec_module(self, module):
    name = module.__name__
    if not (name.startswith("sqlglot") or name.startswith("tests")):
        return _orig_exec(self, module)
    prev = _cur["ctx"]
    _stack.append(prev)
    _cur["ctx"] = "I|" + name
    cov.switch_context("I|" + name)
    try:
        return _orig_exec(self, module)
    finally:
        prev = _stack.pop()
        _cur["ctx"] = prev
        cov.switch_context(prev)

_be._LoaderBasics.exec_module = _exec_module

def _dump():
    with open(OUT + ".opens.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
atexit.register(_dump)

import pytest
import contextlib
try:
    import syncpool as _sp
except ImportError:
    _sp = None

TASK_TESTS = {"tests/test_executor.py::TestExecutor::test_execute_tpcds",
              "tests/test_executor.py::TestExecutor::test_execute_tpch"}


@contextlib.contextmanager
def _task_ctx(nid, k):
    if nid not in TASK_TESTS:
        yield
        return
    prev = _cur["ctx"]
    _cur["ctx"] = "P|%s|%d" % (nid, k)
    cov.switch_context(_cur["ctx"])
    try:
        yield
    finally:
        _cur["ctx"] = prev
        cov.switch_context(prev)


if _sp is not None:
    _sp.TASK_HOOK = _task_ctx

@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    old = _cur["collect"]
    _cur["collect"] = getattr(collector, "nodeid", "")
    yield
    _cur["collect"] = old

def _wrap(prefix):
    @pytest.hookimpl(hookwrapper=True)
    def w(item, *a, **k):
        _cur["ctx"] = prefix + item.nodeid
        cov.switch_context(prefix + item.nodeid)
        yield
        _cur["ctx"] = ""
        cov.switch_context("")
    return w

pytest_runtest_setup = _wrap("S|")
pytest_runtest_call = _wrap("")
pytest_runtest_teardown = _wrap("T|")

def pytest_sessionfinish(session):
    cov.stop()
    cov.save()
    _dump()
