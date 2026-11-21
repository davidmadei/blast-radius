"""Dynamic part: run selected tests of the patched tree in forked children."""
import os
import signal
import sys
import json


GEN_MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"
BASE_MODULES = {"sqlglot.dialects.dialect", "sqlglot.generator", "sqlglot.parser", "sqlglot.tokens",
                "sqlglot.tokenizer_core", "sqlglot.jsonpath", "sqlglot.dialects"}


class _DialectFilter:
    """test_generator_memory loops over every dialect; when the patch only touches
    dialect-specific modules, run it for the dialects built from those modules."""

    def __init__(self, changed):
        self.changed = set(changed)

    def _relevant(self, mod):
        from sqlglot.dialects.dialect import Dialect
        names = list(mod.Dialects)
        per = {}
        for name in names:
            d = Dialect.get_or_raise(name)
            classes = list(type(d).__mro__)
            for attr in ("tokenizer_class", "generator_class", "parser_class", "jsonpath_tokenizer_class"):
                c = getattr(d, attr, None)
                if isinstance(c, type):
                    classes += list(c.__mro__)
            per[name] = {c.__module__ for c in classes}
        allmods = set().union(*per.values()) if per else set()
        for c in self.changed:
            if c in BASE_MODULES or c not in allmods:
                return None
        return [n for n in names if per[n] & self.changed]

    def pytest_runtest_call(self, item):
        if item.nodeid != GEN_MEMORY:
            return
        mod = sys.modules.get(item.module.__name__) if hasattr(item, "module") else None
        if mod is None or not hasattr(mod, "Dialects"):
            return
        try:
            rel = self._relevant(mod)
        except Exception:
            rel = None
        if rel is not None:
            mod.Dialects = rel


class _Rec:
    """pytest plugin collecting failing test node ids (subtests included)."""

    def __init__(self, timeouts, path):
        self.timeouts = timeouts
        self.path = path
        self.failed = set()
        self.collect_failed = set()
        self.passed = set()

    def _flush(self):
        with open(self.path, "w") as f:
            json.dump({"failed": sorted(self.failed), "collect": sorted(self.collect_failed),
                       "passed": sorted(self.passed - self.failed)}, f)

    def pytest_collection_modifyitems(self, session, config, items):
        import pytest
        for it in items:
            t = self.timeouts.get(it.nodeid)
            if t is not None:
                it.add_marker(pytest.mark.timeout(t))

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_failed.add(report.nodeid)

    def pytest_sessionfinish(self, session):
        self._flush()


def preload():
    """Import, in the parent, everything heavy that tests use but that a patch cannot
    change, so forked children start warm."""
    import pytest  # noqa
    import _pytest.python, _pytest.unittest, _pytest.main, _pytest.runner  # noqa
    import unittest, unittest.mock, doctest, decimal, datetime, json, re, typing, dataclasses  # noqa
    import pytest_timeout  # noqa
    try:
        import xdist  # noqa
    except Exception:
        pass
    for mod in ("duckdb", "pandas", "numpy", "dateutil", "pytz", "setuptools",
                "setuptools.command.build_ext", "zoneinfo", "sqlite3", "csv", "gzip"):
        try:
            __import__(mod)
        except Exception:
            pass


def _digest(v, depth=0, seen=None):
    import enum
    import hashlib
    import types
    if seen is None:
        seen = set()
    if depth > 8:
        return "deep"
    if v is None or isinstance(v, (bool, int, float, str, bytes)):
        return "%s:%r" % (type(v).__name__, v)
    if isinstance(v, enum.Enum):
        return "E:%s.%s" % (type(v).__qualname__, v.name)
    if isinstance(v, type):
        return "T:%s.%s" % (v.__module__, v.__qualname__)
    if id(v) in seen:
        return "cycle"
    seen = seen | {id(v)}
    if isinstance(v, types.FunctionType):
        c = v.__code__
        parts = [v.__module__ or "", v.__qualname__, _code_digest(c)]
        for cell in v.__closure__ or ():
            try:
                parts.append(_digest(cell.cell_contents, depth + 1, seen))
            except ValueError:
                parts.append("empty")
        for d in v.__defaults__ or ():
            parts.append(_digest(d, depth + 1, seen))
        return "F:" + hashlib.md5("|".join(parts).encode()).hexdigest()
    if isinstance(v, (types.BuiltinFunctionType, types.MethodType)):
        return "B:%s" % getattr(v, "__qualname__", type(v).__name__)
    if isinstance(v, dict):
        items = sorted(_digest(k, depth + 1, seen) + "=" + _digest(x, depth + 1, seen) for k, x in v.items())
        return "D:" + hashlib.md5("|".join(items).encode()).hexdigest()
    if isinstance(v, (list, tuple)):
        return "L%s:" % type(v).__name__ + hashlib.md5("|".join(_digest(x, depth + 1, seen) for x in v).encode()).hexdigest()
    if isinstance(v, (set, frozenset)):
        return "S:" + hashlib.md5("|".join(sorted(_digest(x, depth + 1, seen) for x in v)).encode()).hexdigest()
    if isinstance(v, (staticmethod, classmethod)):
        return "M:" + _digest(v.__func__, depth + 1, seen)
    if isinstance(v, property):
        return "P:" + "|".join(_digest(f, depth + 1, seen) for f in (v.fget, v.fset, v.fdel))
    d = getattr(v, "__dict__", None)
    if isinstance(d, dict):
        return "O:%s:" % type(v).__qualname__ + _digest(d, depth + 1, seen)
    try:
        import pickle
        return "X:%s:" % type(v).__qualname__ + hashlib.md5(pickle.dumps(v)).hexdigest()
    except Exception:
        return "U:%s" % type(v).__qualname__


def _code_digest(c):
    import hashlib
    consts = []
    for k in c.co_consts:
        if hasattr(k, "co_code"):
            consts.append(_code_digest(k))
        else:
            consts.append(repr(k))
    return hashlib.md5((c.co_code.hex() + "|" + "|".join(consts) + "|" + ",".join(c.co_names)).encode()).hexdigest()


def class_digests(modnames):
    """{"mod:qual": {attr: digest}} for the non-method attributes of every class each
    module defines (nested classes included). A module that fails to import -> None."""
    import importlib
    import types
    out = {}

    def walk(mn, cls, q):
        attrs = {}
        for k, v in list(vars(cls).items()):
            if k.startswith("__") and k.endswith("__"):
                continue
            if isinstance(v, types.FunctionType):
                continue
            if isinstance(v, type) and v.__module__ == mn and v.__qualname__ == q + "." + k:
                walk(mn, v, v.__qualname__)
                continue
            try:
                attrs[k] = _digest(v)
            except Exception:
                attrs[k] = "error"
        out[mn + ":" + q] = attrs

    for mn in modnames:
        try:
            mod = importlib.import_module(mn)
        except BaseException:
            out[mn] = None
            continue
        for name, obj in list(vars(mod).items()):
            if isinstance(obj, type) and obj.__module__ == mn and obj.__qualname__ == name:
                walk(mn, obj, name)
    return out


def digest_child(tree, modnames, resfile):
    _redirect(tree)
    os.chdir(tree)
    sys.path.insert(0, tree)
    res = class_digests(modnames)
    with open(resfile, "w") as f:
        json.dump(res, f)


def spawn_digest(tree, modnames, resfile):
    pid = os.fork()
    if pid == 0:
        try:
            digest_child(tree, modnames, resfile)
        except BaseException:
            pass
        finally:
            os._exit(0)
    return pid


def _redirect(tree):
    dbg = os.environ.get("BR_DEBUG")
    if dbg:
        fd = os.open(os.path.join(dbg, os.path.basename(tree) + "_" + str(os.getpid()) + ".log"),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    else:
        fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = open(1, "w", closefd=False)
    sys.stderr = open(2, "w", closefd=False)


def _cap_duckdb_threads():
    """duckdb starts one thread per host core per connection; keep the pid count low."""
    try:
        import duckdb
        orig = duckdb.connect

        def connect(*a, **k):
            cfg = dict(k.pop("config", None) or {})
            cfg.setdefault("threads", 1)
            try:
                return orig(*a, config=cfg, **k)
            except TypeError:
                return orig(*a, **k)

        duckdb.connect = connect
    except Exception:
        pass


def child(tree, nodeids, timeouts, resfile, check_mods=(), changed_mods=(), task_filter=None):
    _redirect(tree)
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    import syncpool  # noqa: F401  (patches ProcessPoolExecutor before tests import it)
    import importlib
    bad = []
    for mod in check_mods:
        try:
            importlib.import_module(mod)
        except BaseException as e:  # noqa
            bad.append(mod)
    _cap_duckdb_threads()
    if bad or not nodeids:
        with open(resfile, "w") as f:
            json.dump({"failed": [], "collect": [], "passed": [], "import_failed": bad}, f)
        return
    import pytest
    rec = _Rec(timeouts, resfile)
    args = ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly",
            "--assert=plain", "-o", "addopts=", "--rootdir", tree,
            "-W", "ignore", "--no-header", "--tb=no", "-rN"] + nodeids
    if os.environ.get("BR_DEBUG"):
        args = [a for a in args if a not in ("--tb=no", "-rN")] + ["--tb=short", "-rf"]
    try:
        import syncpool
        if task_filter:
            syncpool.SKIP = lambda nid, k: nid in task_filter and k not in task_filter[nid]
        pytest.main(args, plugins=[rec, _DialectFilter(changed_mods), syncpool])
    except BaseException:
        pass
    try:
        rec._flush()
    except BaseException:
        pass


def spawn(tree, nodeids, timeouts, resfile, check_mods=(), changed_mods=(), task_filter=None):
    pid = os.fork()
    if pid == 0:
        try:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            child(tree, nodeids, timeouts, resfile, check_mods, changed_mods, task_filter)
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except BaseException:
                pass
            os._exit(0)
    return pid
