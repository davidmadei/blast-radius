"""Compile raw coverage/class/open traces of the pinned sqlglot into data/map.json.gz.

usage: compile_map.py <repo_root> <raw_prefix> <tests.json> <out>
"""
import gzip, json, os, sys
import coverage

root, raw, tests_json, out = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
num = {t: i for i, t in enumerate(ids)}
groups = {}
for t in ids:
    groups.setdefault(t.rsplit("::", 1)[0], []).append(num[t])


# virtual tests: the pool tasks of the big executor tests, plus a "main" pseudo-task
# for everything those tests run outside their tasks
vt = []
vpos = {}
def vbit(nid, k):
    key = (nid, k)
    if key not in vpos:
        vpos[key] = len(ids) + len(vt)
        vt.append([nid, k])
    return 1 << vpos[key]


TASK_TESTS = {"tests/test_executor.py::TestExecutor::test_execute_tpcds",
              "tests/test_executor.py::TestExecutor::test_execute_tpch"}


def targets(ctx):
    """context label -> bitmask of test numbers (None for import-time)."""
    if ctx == "" or ctx.startswith("I|"):
        return None
    if ctx.startswith("P|"):
        _, nid, k = ctx.split("|")
        i = num.get(nid)
        return 0 if i is None else (1 << i) | vbit(nid, int(k))
    if ctx[:2] in ("S|", "T|"):
        nid = ctx[2:]
        g = groups.get(nid.rsplit("::", 1)[0])
        if g is None:
            return 0
        m = 0
        for i in g:
            m |= 1 << i
            if ids[i] in TASK_TESTS:
                m |= vbit(ids[i], "main")
        return m
    i = num.get(ctx)
    if i is None:
        return 0
    m = 1 << i
    if ctx in TASK_TESTS:
        m |= vbit(ctx, "main")
    return m


cache = {}
def tmask(ctx):
    if ctx not in cache:
        cache[ctx] = targets(ctx)
    return cache[ctx]


sets = {}
def sid(mask):
    if mask not in sets:
        sets[mask] = len(sets)
    return sets[mask]


modsets = {}
def msid(t):
    if t not in modsets:
        modsets[t] = len(modsets)
    return modsets[t]


d = coverage.CoverageData(raw + ".cov")
d.read()
files = {}
for f in d.measured_files():
    rel = os.path.relpath(f, root)
    if rel.startswith(".."):
        continue
    by = d.contexts_by_lineno(f)
    lines = {}
    imp = {}
    for ln, ctxs in by.items():
        m = 0
        mods = set()
        for c in ctxs:
            t = tmask(c)
            if t is None:
                mods.add(c[2:] if c.startswith("I|") else "")
            else:
                m |= t
        if m:
            lines[ln] = sid(m)
        if mods:
            imp[ln] = msid(tuple(sorted(mods)))
    files[rel] = {"l": {str(k): v for k, v in sorted(lines.items())},
                  "imp": {str(k): v for k, v in sorted(imp.items())}}

classes = {}
cl = json.load(open(raw + ".classes.json"))
for ctx, names in cl.items():
    t = tmask(ctx)
    if not t:
        continue
    for n in names:
        classes[n] = classes.get(n, 0) | t
classes = {k: sid(v) for k, v in classes.items()}

opens = {}
op = json.load(open(raw + ".opens.json"))
for ctx, paths in op.items():
    if ctx.startswith("COLLECT:"):
        nid = ctx[len("COLLECT:"):]
        m = 0
        for t, i in num.items():
            if t.startswith(nid):
                m |= 1 << i
    else:
        m = tmask(ctx) or 0
    for p in paths:
        if os.path.exists(os.path.join(root, p)) and os.path.isfile(os.path.join(root, p)):
            opens[p] = opens.get(p, 0) | m
opens = {k: sid(v) for k, v in opens.items() if v}

setlist = [None] * len(sets)
for m, i in sets.items():
    setlist[i] = format(m, "x")
cpu = [None] * len(ids)
gsetup = {}
if len(sys.argv) > 5:
    ct = json.load(open(sys.argv[5]))
    for k, v in ct.items():
        if k in num:
            cpu[num[k]] = int(round(1000 * (v.get("c", 0) + v.get("t", 0))))
            g = k.rsplit("::", 1)[0]
            gsetup[g] = max(gsetup.get(g, 0), int(round(1000 * v.get("s", 0))))
# static import graph of the checkout (sqlglot/ and tests/)
import ast as _ast
def _modname(rel):
    p = rel[:-3].split("/")
    if p[-1] == "__init__":
        p = p[:-1]
    return ".".join(p)
allmods = {}
for base in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(root, base)):
        for f in fn:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(dp, f), root)
                allmods[_modname(rel)] = rel
imports = {}
for mod, rel in allmods.items():
    try:
        tree = _ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
    except Exception:
        continue
    pkg = mod if rel.endswith("__init__.py") else mod.rsplit(".", 1)[0]
    outs = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                outs.add(a.name)
        elif isinstance(node, _ast.ImportFrom):
            if node.level:
                b = pkg.split(".")
                b = b[: len(b) - (node.level - 1)] if node.level > 1 else b
                basemod = ".".join(b + ([node.module] if node.module else []))
            else:
                basemod = node.module or ""
            outs.add(basemod)
            for a in node.names:
                outs.add(basemod + "." + a.name)
    # keep only modules of the checkout, and their parent packages
    res = set()
    for o in outs:
        parts = o.split(".")
        for k in range(1, len(parts) + 1):
            c = ".".join(parts[:k])
            if c in allmods and c != mod:
                res.add(c)
    imports[mod] = sorted(res)

# function definitions by name (sqlglot/ only)
defs = {}
for mod, rel in allmods.items():
    if not rel.startswith("sqlglot/"):
        continue
    try:
        tree = _ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
    except Exception:
        continue
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            st = min([node.lineno] + [d.lineno for d in node.decorator_list])
            defs.setdefault(node.name, []).append([rel, st, node.end_lineno])

mlist = [None] * len(modsets)
for t, i in modsets.items():
    mlist[i] = list(t)
doc = {"vtasks": vt, "defs": defs, "imports": imports, "modsets": mlist, "ids": ids, "sets": setlist, "files": files, "classes": classes, "opens": opens,
       "cpu": cpu, "gsetup": gsetup}
with gzip.open(out, "wt") as f:
    json.dump(doc, f, separators=(",", ":"))
print("sets", len(sets), "files", len(files), "classes", len(classes), "opens", len(opens), file=sys.stderr)
