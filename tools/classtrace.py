"""pytest plugin: per-test set of sqlglot classes used as self/cls."""
import os, sys, json
ROOT = os.environ["MAP_ROOT"]
OUT = os.environ["MAP_OUT"]
SQ = os.path.join(ROOT, "sqlglot") + os.sep
TS = os.path.join(ROOT, "tests") + os.sep
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "classtrace")
DISABLE = mon.DISABLE
_types = set()
_getframe = sys._getframe

def _start(code, offset):
    fn = code.co_filename
    if not (fn.startswith(SQ) or fn.startswith(TS)) or code.co_argcount < 1 or code.co_varnames[0] not in ("self", "cls"):
        return DISABLE
    f = _getframe(1)
    try:
        v = f.f_locals[code.co_varnames[0]]
    except Exception:
        return
    _types.add(v if code.co_varnames[0] == "cls" and isinstance(v, type) else type(v))

mon.register_callback(TOOL, mon.events.PY_START, _start)
mon.set_events(TOOL, mon.events.PY_START)
_result = {}

def _names(types):
    out = set()
    for t in types:
        try:
            mro = t.__mro__
        except Exception:
            continue
        for c in mro:
            m = getattr(c, "__module__", "") or ""
            if m.startswith("sqlglot") or m.startswith("tests"):
                out.add(m + ":" + c.__qualname__)
    return out

import pytest

def _wrap(prefix):
    @pytest.hookimpl(hookwrapper=True)
    def w(item, *a, **k):
        _types.clear()
        yield
        _result[prefix + item.nodeid] = sorted(_names(_types))
        _types.clear()
    return w

pytest_runtest_setup = _wrap("S|")
pytest_runtest_call = _wrap("")
pytest_runtest_teardown = _wrap("T|")

def pytest_sessionfinish(session):
    mon.set_events(TOOL, 0)
    with open(OUT + ".classes.json", "w") as f:
        json.dump(_result, f)
