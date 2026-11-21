"""Blast-radius selector: static coverage map -> candidate tests -> run the cheap ones
on the patched tree -> report failing + statically kept tests, packed onto workers."""
import json
import os
import sys
import time

T0 = time.time()
APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

import patchparse  # noqa: E402
import analyze  # noqa: E402
import runner  # noqa: E402
import tree as treemod  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/opt/sq")
MAP = os.environ.get("BR_MAP", os.path.join(APP, "..", "data", "map.json.gz"))
TMP = os.environ.get("BR_TMP", "/tmp")
BUDGET = float(os.environ.get("BR_BUDGET", "150"))  # wall seconds we allow ourselves
HARD = float(os.environ.get("BR_HARD", "160"))      # every child is done by then (limit 180 s)
PAR = int(os.environ.get("BR_PAR", "2"))
NORUN = os.environ.get("BR_NORUN") == "1"

WORKER_MS = 2000
RUN_OVERHEAD_MS = 450      # CPU of one child: pytest session + sqlglot import
FILE_OVERHEAD_MS = 40      # importing / collecting one test module
CPU_FACTOR = 1.15          # child CPU per recorded test second
P_FAIL = 0.5
DIFF_MAX_MODS = int(os.environ.get("BR_DIFF_MODS") or "100000")
DIFF_MIN_MS = int(os.environ.get("BR_DIFF_MS") or "3000")
CPU_W = float(os.environ.get("BR_W") or "1.15")  # board CPU seconds per estimated test CPU second


def log(*a):
    print("[br %.1fs]" % (time.time() - T0), *a, file=sys.stderr, flush=True)


def lpt(items, k, dur):
    loads = [0] * k
    ws = [[] for _ in range(k)]
    for i in sorted(items, key=lambda x: -dur[x]):
        j = loads.index(min(loads))
        ws[j].append(i)
        loads[j] += dur[i]
    return ws, loads


def best_pack(items, dur):
    if not items:
        return 0, []
    best = None
    for k in range(1, min(8, len(items)) + 1):
        ws, loads = lpt(items, k, dur)
        c = max(loads) + WORKER_MS * k
        if best is None or c < best[0]:
            best = (c, ws)
    return best


CPU = []      # estimated child CPU ms per test
GROUP = []    # class group per test
GSETUP = {}   # class setup CPU ms


OVERRIDE = {}
GEN_MEMORY_I = None
_SPECIFIC = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/", "sqlglot/tokenizers/")


BROAD_NOTES = ("classlevel", "modlevel", "modassign", "modimport", "attr:", "dictkeys", "newmethods",
               "removed-names", "impfunc", "cachefn")
TEST_MODULES = []
LINE_ONLY = ("ast-same:", "lambda:", "data:", "defline", "IMPORTRISK")


def set_overrides(changed_mods, paths, j=None, m=None, num=None):
    """Per-commit CPU estimates for tests the child runs only partly."""
    OVERRIDE.clear()
    if GEN_MEMORY_I is None:
        return
    py = [p for p in paths if p.endswith(".py")]
    if py and all(p.startswith(_SPECIFIC) and not p.endswith(("/dialect.py", "/__init__.py")) for p in py):
        OVERRIDE[GEN_MEMORY_I] = 1200
    # pool-based executor tests: only the tasks the change can reach need to run, when
    # every rule that fired was a line/function rule and the test's main body is clear
    j["tasks"] = {}
    if m is None or not m.vtasks:
        return
    if any(not n.startswith(LINE_ONLY) for n in j["notes"]):
        return
    if any(not p.startswith(("sqlglot/", "tests/")) for p in paths):
        return
    mask = j["mask"]
    for nid, vt in m.vtasks.items():
        i = num.get(nid)
        if i is None or not (mask >> i & 1):
            continue
        mainb = vt.get("main")
        if mainb is None or mask >> mainb & 1:
            continue
        ks = [k for k, b in vt.items() if k != "main" and mask >> b & 1]
        ntask = len(vt) - 1
        j["tasks"][nid] = ks
        OVERRIDE[i] = int(CPU[i] * (len(ks) + 0.5) / max(ntask, 1)) + 300


def cpu_of(i):
    return OVERRIDE.get(i, CPU[i])


def run_cost(items, dur, fileof):
    if not items:
        return 0
    files = set(fileof[i] for i in items)
    groups = set(GROUP[i] for i in items)
    return (RUN_OVERHEAD_MS + FILE_OVERHEAD_MS * len(files)
            + sum(GSETUP.get(g, 0) for g in groups) + sum(cpu_of(i) for i in items))


def plan(cands, dur, fileof, p_fail=P_FAIL):
    """-> (static_set, run_set, est_cost_ms)"""
    T = sorted(cands, key=lambda i: -dur[i])
    best = None
    c0 = CPU_W * run_cost(T, dur, fileof) + (p_fail * (WORKER_MS + 50) if T else 0)
    best = (c0, [], T)
    if T:
        cs, _ = best_pack(T, dur)
        if cs < best[0]:
            best = (cs, list(T), [])
    for m in range(1, min(len(T), 12) + 1):
        S = T[:m]
        rest = T[m:]
        for k in range(1, min(8, m) + 1):
            ws, loads = lpt(S, k, dur)
            mk = max(loads)
            absorbed = []
            left = []
            for i in rest:
                j = loads.index(min(loads))
                if loads[j] + dur[i] <= mk:
                    loads[j] += dur[i]
                    absorbed.append(i)
                else:
                    left.append(i)
            c = mk + WORKER_MS * k + CPU_W * run_cost(left, dur, fileof)
            if c < best[0]:
                best = (c, S + absorbed, left)
    return best[1], best[2], best[0]


def main():
    tj = json.load(open(os.path.join(DATA, "tests.json")))
    tests = tj["tests"]
    ids = [t["id"] for t in tests]
    dur = [int(round(t["seconds"] * 1000)) for t in tests]
    fileof = [t["file"] for t in tests]
    num = {t: i for i, t in enumerate(ids)}
    N = len(ids)
    try:
        m = analyze.Map(MAP, ids)
    except Exception as e:  # no map: be safe
        log("map load failed", e)
        m = None
    log("map loaded")
    for i in range(N):
        c = m.cpu[i] if m is not None else None
        CPU.append(c if c is not None else int(dur[i] * CPU_FACTOR) + 5)
        GROUP.append(ids[i].rsplit("::", 1)[0])
    if m is not None:
        GSETUP.update(m.gsetup)
    global GEN_MEMORY_I
    GEN_MEMORY_I = num.get(runner.GEN_MEMORY)
    TEST_MODULES.extend(sorted(set(analyze.module_name(f) for f in fileof)))
    commits = sorted(f[:-6] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))
    if os.environ.get("BR_ONLY"):
        commits = [c for c in commits if c in os.environ["BR_ONLY"].split(",")]
    jobs = {}
    results = {}
    for c in commits:
        try:
            text = open(os.path.join(DATA, "commits", c + ".patch"), encoding="utf-8", errors="replace").read()
            fps = patchparse.parse(text)
            if m is None:
                raise RuntimeError("no map")
            mask, notes = analyze.candidates(m, SRC, fps)
            jobs[c] = {"fps": fps, "mask": mask, "notes": notes,
                       "nonclass": analyze.LAST.get("nonclass", mask),
                       "assigns": analyze.LAST.get("assigns", [])}
        except Exception as e:
            log(c, "analysis failed:", repr(e))
            results[c] = list(range(N))
    # compile check of patched python files happens in tree build below
    order = []
    digest_mods = set()
    for c, j in jobs.items():
        j["diffmods"] = attrdiff_modules(m, j)
        if j["diffmods"]:
            cc = [i for i in range(N) if j["mask"] >> i & 1]
            # narrowing must be able to pay for importing the modules and running the diff
            if len(j["diffmods"]) > DIFF_MAX_MODS or best_pack(cc, dur)[0] < DIFF_MIN_MS:
                j["diffmods"] = None
        digest_mods |= set(j["diffmods"] or ())
    pristine = None
    if digest_mods and not NORUN:
        pristine = run_pristine_digests(sorted(digest_mods))
    for c, j in jobs.items():
        if j["diffmods"] and pristine is not None:
            j["kind"] = "diff"
            order.append(c)
            log(c, "attribute diff over", len(j["diffmods"]), "modules")
            continue
        replan(c, j, m, N, dur, fileof, num, results, order)
    jobs_ctx.update(m=m, N=N, dur=dur, fileof=fileof, num=num, pristine=pristine)

    # build trees + check that patched python compiles
    # (children that only import sqlglot need none of pytest/pandas)
    if any(jobs[c].get("run") or jobs[c].get("kind") == "diff" for c in order):
        ensure_preload()
    order.sort(key=lambda c: (jobs[c].get("kind") != "diff", -sum(dur[i] for i in jobs[c].get("run", []))))
    running = {}
    slots = list(range(PAR))
    pending = list(order)
    while pending or running:
        while pending and slots and (time.time() - T0) < BUDGET:
            c = pending.pop(0)
            j = jobs[c]
            if j.get("kind") != "diff" and j.get("run"):
                est_wall = 2.0 + 1.5 * run_cost(j["run"], dur, fileof) / 1000
                if time.time() - T0 + est_wall > HARD:
                    log(c, "no time left to run: static")
                    results[c] = sorted(set(j["static"]) | set(j["run"]))
                    continue
            slot = slots.pop()
            tdir = os.path.join(TMP, "w%d" % slot)
            try:
                changes = treemod.build(SRC, tdir, j["fps"])
                bad = []
                for rel, txt in changes.items():
                    if txt is not None and rel.endswith(".py"):
                        try:
                            compile(txt, rel, "exec")
                        except SyntaxError:
                            bad.append(rel)
                if bad:
                    log(c, "syntax error in", bad)
                    mask = j["mask"] | m.broken_usage([analyze.module_name(b) for b in bad])
                    results[c] = [i for i in range(N) if mask >> i & 1]
                    slots.append(slot)
                    continue
            except Exception as e:
                log(c, "tree build failed:", repr(e))
                results[c] = expand_broken(m, j, N)
                slots.append(slot)
                continue
            if j.get("kind") == "diff":
                resf = os.path.join(TMP, "r%d.json" % slot)
                try:
                    os.unlink(resf)
                except OSError:
                    pass
                pid = runner.spawn_digest(tdir, j["diffmods"], resf)
                running[pid] = (c, slot, resf, time.time() + 20, time.time())
                continue
            nodeids = [ids[i] for i in j["run"]]
            tmo = {ids[i]: 10.0 + 4.0 * (CPU[i] + GSETUP.get(GROUP[i], 0)) / 1000 for i in j["run"]}
            resf = os.path.join(TMP, "r%d.json" % slot)
            try:
                os.unlink(resf)
            except OSError:
                pass
            tf = {nid: set(ks) for nid, ks in j.get("tasks", {}).items() if num[nid] in j["run"]}
            if tf:
                log(c, "task filter", {k.split("::")[-1]: len(v) for k, v in tf.items()})
            pid = runner.spawn(tdir, nodeids, tmo, resf, j["check"], j["changed_mods"], tf)
            limit = time.time() + (30 + 3 * run_cost(j["run"], dur, fileof) / 1000 if j["run"] else 8)
            running[pid] = (c, slot, resf, min(limit, T0 + HARD), time.time())
        if not running:
            if pending:  # out of budget: keep everything statically
                for c in pending:
                    results[c] = jobs[c]["static"] + jobs[c]["run"]
                    log(c, "budget exhausted: static")
                pending = []
            break
        pid, status = wait_any(running)
        c, slot, resf, limit, st = running.pop(pid)
        slots.append(slot)
        j = jobs[c]
        if j.get("kind") == "diff":
            j["kind"] = "run"
            nm = None
            if status == "ok":
                try:
                    nm = narrowed_by_diff(m, j, jobs_ctx["pristine"], json.load(open(resf)))
                except Exception as e:
                    log(c, "diff failed", repr(e))
                    nm = None
            if nm is not None:
                log(c, "attribute diff: %d changed %s -> %d candidates (was %d)" % (
                    len(j["diff"]), [a for _, a in j["diff"]][:6], bin(nm).count("1"), bin(j["mask"]).count("1")))
                j["mask"] = nm
            front = []
            replan(c, j, m, N, dur, fileof, num, results, front)
            pending[:0] = front
            continue
        res = None
        if status == "ok":
            try:
                res = json.load(open(resf))
            except Exception:
                res = None
        if res is None:
            log(c, "child failed (%s): keeping all" % status)
            results[c] = j["static"] + j["run"]
            continue
        if res.get("import_failed"):
            log(c, "import failed:", res["import_failed"])
            mask = j["mask"] | m.broken_usage(res["import_failed"])
            results[c] = sorted(set(i for i in range(N) if mask >> i & 1) | set(j["static"]) | set(j["run"]))
            continue
        failed = set()
        for nid in res["failed"]:
            if nid in num:
                failed.add(num[nid])
        passed = set(num[n] for n in res.get("passed", []) if n in num)
        coll = res.get("collect") or []
        out = set(j["static"]) | failed
        # anything we asked to run that neither passed nor failed: keep it
        for i in j["run"]:
            if i not in passed and i not in failed:
                out.add(i)
        if coll:
            log(c, "collection errors", coll[:3])
            mods = [analyze.module_name(fp.path) for fp in j["fps"] if (fp.path or "").endswith(".py")]
            mask = m.broken_usage(mods) if mods else 0
            out |= set(i for i in range(N) if mask >> i & 1)
            for f in coll:
                f = f.split("::")[0]
                out |= set(i for i in range(N) if fileof[i] == f)
        results[c] = sorted(out)
        log(c, "ran %d in %.1fs cpu %.2fs (est %.2fs): %d failed" % (len(j["run"]), time.time() - st, CHILD_CPU.get(pid, -1), run_cost(j["run"], dur, fileof) / 1000, len(failed)))
    for c in commits:
        sel = results.get(c)
        if sel is None:
            sel = list(range(N))
        _, ws = best_pack(sorted(set(sel)), dur)
        print(json.dumps({"commit": c, "workers": [sorted(w) for w in ws if w]}))
    sys.stdout.flush()
    log("done")


jobs_ctx = {}


def replan(c, j, m, N, dur, fileof, num, results, order):
    cands = [i for i in range(N) if j["mask"] >> i & 1]  # real tests only
    j["cands"] = cands
    paths = [fp.path or "" for fp in j["fps"]]
    j["changed_mods"] = [analyze.module_name(p) for p in paths if p.endswith(".py") and p.startswith("sqlglot/")]
    set_overrides(j["changed_mods"], paths, j, m, num)
    code = [p for p in paths if p.endswith(".py") and not any(
        n == "ast-same:" + p for n in j["notes"])]
    if not code:
        pf = 0.95   # only data files changed: whoever reads them almost surely fails
    elif all(p.startswith("tests/") for p in code):
        pf = 0.9
    else:
        pf = P_FAIL
    static, run, est = plan(cands, dur, fileof, pf)
    if NORUN:
        static, run = cands, []
    j["static"], j["run"] = static, run
    log(c, "cands=%d static=%d run=%d est=%.1fs notes=%s" % (len(cands), len(static), len(run), est / 1000, j["notes"][:4]))
    j["check"] = []
    if "IMPORTRISK" in j["notes"]:
        for fp in j["fps"]:
            p = fp.new or fp.git_new
            if p and p.endswith(".py") and (p.startswith("sqlglot/") or p.startswith("tests/")):
                j["check"].append(analyze.module_name(p))
        # class/module-level changes in sqlglot can break code that test modules run
        # at import: import every test module too
        if any(n.startswith(BROAD_NOTES) for n in j["notes"]) and any(
                (fp.path or "").startswith("sqlglot/") for fp in j["fps"]):
            # (lazily imported dialects run sqlglot code in their class bodies too)
            j["check"] += [x for x in sorted(m.modpath) if x.startswith("sqlglot.") and
                           not x.endswith("__main__")] + TEST_MODULES
    if run or j["check"]:
        order.append(c)
    else:
        results[c] = static



def attrdiff_modules(m, j):
    """Modules whose class attributes to compare before/after the patch, or None when
    the commit's class-body assignments cannot be narrowed this way."""
    if not j.get("assigns") or m is None:
        return None
    paths = [fp.path or "" for fp in j["fps"]]
    py = [p for p in paths if p.endswith(".py")]
    if not py or not all(p.startswith("sqlglot/") for p in py):
        return None
    if any(fp.new is None or fp.old is None for fp in j["fps"] if (fp.path or "").endswith(".py")):
        return None
    mods = set(analyze.module_name(p) for p in py)
    clo = m.importer_closure(mods)
    out = set(x for x in clo if x.startswith("sqlglot"))
    return sorted(out | mods)


_PRELOADED = []


def ensure_preload():
    """Heavy imports happen in the parent only when some child will be forked."""
    if not _PRELOADED and not NORUN:
        _PRELOADED.append(1)
        runner.preload()


def run_pristine_digests(mods):
    tdir = os.path.join(TMP, "w0")
    try:
        treemod.build(SRC, tdir, [])
        resf = os.path.join(TMP, "pristine.json")
        pid = runner.spawn_digest(tdir, mods, resf)
        deadline = time.time() + 30
        while True:
            r, st = os.waitpid(pid, os.WNOHANG)
            if r:
                break
            if time.time() > deadline:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
                return None
            time.sleep(0.01)
        res = json.load(open(resf))
        log("pristine digests for", len(mods), "modules")
        return res
    except Exception as e:
        log("pristine digests failed", repr(e))
        return None


def narrowed_by_diff(m, j, pristine, patched):
    """Candidate mask from the class attributes that actually changed."""
    mask = j["nonclass"]
    for mod in j["diffmods"]:
        a, b = pristine.get(mod, 0), patched.get(mod, 0)
        if (a is None) != (b is None):
            return None  # the patch changes whether it imports at all
    changed = []
    dm = set(j["diffmods"])
    keys = set(k for k in list(pristine) + list(patched) if ":" in k and k.split(":", 1)[0] in dm)
    for k in keys:
        a = pristine.get(k) or {}
        b = patched.get(k) or {}
        if k not in pristine or k not in patched:
            changed.append((k, None))
            continue
        for attr in set(a) | set(b):
            if a.get(attr) != b.get(attr):
                changed.append((k, attr))
    for k, attr in changed:
        u = m.classes.get(k, 0)
        if attr is None:
            mask |= u
            continue
        refs = analyze.textref_tests(m, SRC, attr)
        mask |= u if refs is None else (u & refs)
    j["diff"] = changed
    return mask


def expand_broken(m, j, N):
    """A patched python file does not compile/apply: everything that touches it."""
    mask = j["mask"]
    for fp in j["fps"]:
        p = fp.old or fp.new
        if not p:
            continue
        if p.endswith(".py") and m is not None:
            u = m.module_union(p)
            mod = analyze.module_name(p) + ":"
            for k, v in m.classes.items():
                if k.startswith(mod):
                    u |= v
            if not u or p.startswith("sqlglot/__init__") or p.endswith("__init__.py"):
                return list(range(N))
            mask |= u
        elif m is None:
            return list(range(N))
    return [i for i in range(N) if mask >> i & 1]


CHILD_CPU = {}


def wait_any(running):
    while True:
        try:
            pid, st, ru = os.wait4(-1, os.WNOHANG)
        except ChildProcessError:
            pid = 0
        if pid and pid in running:
            CHILD_CPU[pid] = ru.ru_utime + ru.ru_stime
            return pid, ("ok" if os.WIFEXITED(st) else "signal")
        now = time.time()
        for p, (c, slot, resf, limit, stt) in list(running.items()):
            if now > limit:
                try:
                    os.kill(p, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(p, 0)
                except OSError:
                    pass
                return p, "timeout"
        time.sleep(0.01)


if __name__ == "__main__":
    for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(k, "1")
    main()
