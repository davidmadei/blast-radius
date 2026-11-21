"""Static part: from a patch to a candidate set of tests (bitmask over test numbers).

Inputs are a coverage map of the pinned sqlglot (which tests execute which lines,
which modules' import executes which lines, which classes each test uses, which
data files each test opens) and the patch. Rules, per changed python line:
  * inside a function: every test that executed any line of that (outermost) function
  * executed while importing module M (class bodies, decorators, metaclasses,
    builders called at class creation): every test that uses M
  * class-body assignment NAME = ...: tests that use the class, narrowed to tests
    that executed a line mentioning NAME when every such line is test-time code
  * module level: every test that uses the module
Python files whose AST did not change (comments, formatting) select nothing.
Data files select the tests that opened them."""
import ast
import gzip
import json
import os
import re

from patchparse import changed_old_lines, apply


class Map:
    def __init__(self, path, ids):
        with gzip.open(path, "rt") as f:
            doc = json.load(f)
        mids = doc["ids"]
        self.n = len(ids)
        vt = doc.get("vtasks", [])
        # virtual bits (pool tasks of big tests) live above the real test numbers
        self.vtasks = {}
        for j, (nid, k) in enumerate(vt):
            self.vtasks.setdefault(nid, {})[k] = len(ids) + j
        if mids == ids:
            remap = None
        else:
            pos = {t: i for i, t in enumerate(ids)}
            remap = [pos.get(t) for t in mids] + [len(ids) + j for j in range(len(vt))]
        sets = []
        for h in doc["sets"]:
            m = int(h, 16)
            if remap is not None:
                m2 = 0
                i = 0
                while m:
                    if m & 1 and remap[i] is not None:
                        m2 |= 1 << remap[i]
                    m >>= 1
                    i += 1
                m = m2
            sets.append(m)
        self.sets = sets
        modsets = doc.get("modsets", [])
        self.files = {}
        for f, v in doc["files"].items():
            imp = v.get("imp", {})
            if isinstance(imp, list):
                imp = {str(k): None for k in imp}
            self.files[f] = ({int(k): sets[s] for k, s in v["l"].items()},
                             {int(k): (modsets[s] if s is not None else [""]) for k, s in imp.items()})
        self.classes = {k: sets[s] for k, s in doc["classes"].items()}
        self.opens = {k: sets[s] for k, s in doc["opens"].items()}
        mcpu = doc.get("cpu") or [None] * len(mids)
        pos = {t: i for i, t in enumerate(ids)}
        self.cpu = [None] * len(ids)
        for t, c in zip(mids, mcpu):
            if t in pos:
                self.cpu[pos[t]] = c
        self.gsetup = doc.get("gsetup", {})
        self.all = (1 << self.n) - 1
        self.ids = ids
        self.modpath = {}
        for f in self.files:
            if f.endswith(".py"):
                self.modpath[module_name(f)] = f
        self.by_file = {}
        for i, t in enumerate(ids):
            f = t.split("::", 1)[0]
            self.by_file[f] = self.by_file.get(f, 0) | (1 << i)
        self._musage = {}
        self.defs = doc.get("defs", {})
        self.imports = doc.get("imports", {})
        self.rimports = {}
        for a, bs in self.imports.items():
            for b in bs:
                self.rimports.setdefault(b, set()).add(a)

    def exp_usage(self, name, lower=False):
        """Tests that used expression class `name` (or a subclass)."""
        u = 0
        for k, v in self.classes.items():
            if not k.startswith("sqlglot.expressions"):
                continue
            q = k.split(":", 1)[1]
            if q == name or (lower and q.lower() == name):
                u |= v
        return u

    def def_union(self, name):
        u = 0
        for path, a, b in self.defs.get(name, []):
            u |= self.lines_union(path, a, b)
        return u

    def importer_closure(self, mods):
        seen = set(mods)
        todo = list(mods)
        while todo:
            x = todo.pop()
            for y in self.rimports.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        return seen

    def broken_usage(self, mods):
        """Tests that fail when modules mods cannot be imported."""
        clo = self.importer_closure(mods)
        if "sqlglot" in clo or "tests" in clo or "tests.helpers" in clo:
            return self.all
        u = 0
        for x in clo:
            u |= self.module_usage(x)
            if x.startswith("tests."):
                u |= self.by_file.get(x.replace(".", "/") + ".py", 0)
        return u

    def lines_union(self, path, a, b):
        lm = self.files.get(path)
        if not lm:
            return 0
        m = 0
        for ln, s in lm[0].items():
            if a <= ln <= b:
                m |= s
        return m

    def module_union(self, path):
        lm = self.files.get(path)
        if not lm:
            return 0
        m = 0
        for s in lm[0].values():
            m |= s
        return m

    def module_usage(self, modname):
        """Tests that use module modname: executed any of its lines at test time, or
        used one of its classes. Unknown/empty module -> all tests."""
        if modname in self._musage:
            return self._musage[modname]
        if modname == "":
            u = self.all
        else:
            path = self.modpath.get(modname)
            u = self.module_union(path) if path else 0
            pref = modname + ":"
            for k, v in self.classes.items():
                if k.startswith(pref):
                    u |= v
            if modname.startswith("tests."):
                f = modname.replace(".", "/") + ".py"
                u |= self.by_file.get(f, 0)
                if modname in ("tests", "tests.helpers") or not u:
                    u = self.all
            if not u:
                u = 0
        self._musage[modname] = u
        return u

    def imp_mods(self, path, line):
        lm = self.files.get(path)
        if not lm:
            return []
        return lm[1].get(line, [])


def module_name(path):
    p = path[:-3] if path.endswith(".py") else path
    parts = p.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _fills_process_cache(fn, module_globals):
    """Does fn memoise into process-wide state (module globals, class registries,
    globals(), functools caches)? Its body then runs once per process, so coverage
    only credits the first test that got there."""
    for d in fn.decorator_list:
        txt = ast.dump(d)
        if "lru_cache" in txt or "'cache'" in txt or "cached_property" in txt:
            return True
    for node in ast.walk(fn):
        if isinstance(node, ast.Global):
            return True
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Subscript):
                base = t.value
                if isinstance(base, ast.Name) and base.id in module_globals:
                    return True
                if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == "cls":
                    return True
                if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "globals":
                    return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
            b = node.func.value
            if isinstance(b, ast.Name) and b.id in module_globals:
                return True
    return False


def _strip_doc(body):
    """Docstrings never change behaviour here (nothing in sqlglot or its tests reads them)."""
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _node_eq(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for f in a._fields:
            x, y = getattr(a, f, None), getattr(b, f, None)
            if f == "body":
                x, y = _strip_doc(x), _strip_doc(y)
            if not _node_eq(x, y):
                return False
        return True
    if isinstance(a, ast.AST):
        for f in a._fields:
            if not _node_eq(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(_node_eq(x, y) for x, y in zip(a, b))
    return a == b


def _body_eq(ab, bb, lines):
    """Compare statement lists, only looking closely at statements on changed lines."""
    if len(ab) != len(bb):
        return False
    for x, y in zip(ab, bb):
        s = getattr(x, "lineno", 0)
        e = getattr(x, "end_lineno", s)
        if hasattr(x, "decorator_list") and x.decorator_list:
            s = min([s] + [d.lineno for d in x.decorator_list])
        if not any(s - 1 <= ln <= e + 1 for ln in lines):
            continue
        if isinstance(x, ast.ClassDef) and isinstance(y, ast.ClassDef):
            if not (x.name == y.name and _node_eq(x.bases, y.bases) and _node_eq(x.keywords, y.keywords)
                    and _node_eq(x.decorator_list, y.decorator_list)):
                return False
            if not _body_eq(_strip_doc(x.body), _strip_doc(y.body), lines):
                return False
        elif not _node_eq(x, y):
            return False
    return True


def top_names(tree):
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            tg = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tg:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    out.add(sub.name)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        out.add((a.asname or a.name).split(".")[0])
    return out


def removed_name_users(m, root, path, names):
    """A module stops defining names: test files and modules that import them from it
    (from M import X / M.X / relative imports), and those modules' importers' users."""
    mod = module_name(path)
    pkg = mod if path.endswith("__init__.py") else mod.rsplit(".", 1)[0]
    last = mod.rsplit(".", 1)[-1]
    imp_rx = re.compile(r"from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n]*)")
    mask = 0
    brk = []
    for f in list(m.files) + list(m.by_file):
        if f == path or not f.endswith(".py"):
            continue
        txt = _SRC_INDEX.get(f)
        if txt is None:
            try:
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    txt = fh.read()
            except OSError:
                txt = ""
            _SRC_INDEX[f] = txt
        hit = False
        for name in names:
            if name not in txt:
                continue
            if re.search(r"\b%s\.%s\b" % (re.escape(mod), re.escape(name)), txt):
                hit = True
            for im in imp_rx.finditer(txt):
                src_mod, what = im.group(1), im.group(2)
                fpkg = module_name(f) if f.endswith("__init__.py") else module_name(f).rsplit(".", 1)[0]
                if src_mod == mod or (src_mod == "." and fpkg == pkg and False) or (
                        src_mod == "." + last and fpkg == pkg):
                    if re.search(r"\b%s\b" % re.escape(name), what):
                        hit = True
            if hit:
                break
        if hit:
            if f.startswith("tests/"):
                mask |= m.by_file.get(f, 0)
            brk.append(module_name(f))
    if brk:
        mask |= m.broken_usage(sorted(set(brk)))
    return mask


def _singleton(n):
    """None, or an Enum member written Class.MEMBER: `is` and `==` agree on these."""
    if isinstance(n, ast.Constant):
        return n.value is None
    return (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id[:1].isupper()
            and not n.value.id.isupper() and n.attr.isupper() and len(n.attr) > 1)


class _IsEq(ast.NodeTransformer):
    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) \
                and _singleton(node.comparators[0]):
            node.ops = [ast.Eq() if isinstance(node.ops[0], (ast.Is, ast.Eq)) else ast.NotEq()]
        return node


def ast_same(src, new, lines):
    try:
        a, b = ast.parse(src), ast.parse(new)
        if _body_eq(_strip_doc(a.body), _strip_doc(b.body), lines):
            return True
        sl = src.splitlines()
        if not any(1 <= ln <= len(sl) and (" is " in sl[ln - 1] or "==" in sl[ln - 1] or "!=" in sl[ln - 1])
                   for ln in lines):
            return False
        a, b = _IsEq().visit(a), _IsEq().visit(b)
        return _body_eq(_strip_doc(a.body), _strip_doc(b.body), lines)
    except SyntaxError:
        return False


class Scopes:
    """Function/class/statement structure of one python source."""

    def __init__(self, src):
        self.tree = ast.parse(src)
        self.funcs = []  # (start, end, qual) outermost functions
        self.lambdas = []  # (start, end) lambdas outside functions
        self.classes = []  # (start, end, qual)
        self.cls_assign = []  # (start, end, classqual, [names])
        self.assign_nodes = {}
        self.cachefns = set()
        self.globals = top_names(self.tree)
        self._walk(self.tree.body, [], False)
        # module functions called from a memoising function run once per process too
        top = {n.name: n for n in self.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        nodes = {}
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                st = min([n.lineno] + [d.lineno for d in n.decorator_list])
                nodes[(st, n.end_lineno)] = n
        for key in list(self.cachefns):
            fn = nodes.get(key)
            if fn is None:
                continue
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in top:
                    t = top[c.func.id]
                    self.cachefns.add((min([t.lineno] + [d.lineno for d in t.decorator_list]), t.end_lineno))

    def _walk(self, body, quals, in_func):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                q = quals + [node.name]
                if isinstance(node, ast.ClassDef):
                    if not in_func:
                        self.classes.append((start, node.end_lineno, ".".join(q)))
                    self._walk(node.body, q, in_func)
                else:
                    if not in_func:
                        self.funcs.append((start, node.end_lineno, ".".join(q)))
                        if _fills_process_cache(node, self.globals):
                            self.cachefns.add((start, node.end_lineno))
                    self._walk(node.body, q + ["<locals>"], True)
            elif not in_func:
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    tg = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = [t.id for t in tg if isinstance(t, ast.Name)]
                    if len(names) == len(tg):
                        self.cls_assign.append((node.lineno, node.end_lineno, ".".join(quals), names))
                        self.assign_nodes[(".".join(quals), tuple(names))] = node
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Lambda):
                        self.lambdas.append((sub.lineno, sub.end_lineno))

    def locate(self, line):
        for s, e, q in self.funcs:
            if s <= line <= e:
                return ("func", s, e, q)
        cls = [(s, e, q) for s, e, q in self.classes if s <= line <= e]
        if cls:
            return ("class", cls)
        return ("module",)

    def module_stmt_at(self, line):
        for node in self.tree.body:
            if node.lineno <= line <= getattr(node, "end_lineno", node.lineno):
                return node
        return None

    def assign_at(self, line):
        for s, e, q, names in self.cls_assign:
            if s <= line <= e:
                return s, e, q, names
        return None

    def lambda_at(self, line):
        best = None
        for s, e in self.lambdas:
            if s <= line <= e and (best is None or e - s < best[1] - best[0]):
                best = (s, e)
        return best


_SRC_INDEX = {}


def name_refs(root, name, files):
    """(path, line) of every line in the map's python files that mentions identifier name."""
    rx = re.compile(r"\b%s\b" % re.escape(name))
    out = []
    for path in files:
        if not path.endswith(".py"):
            continue
        txt = _SRC_INDEX.get(path)
        if txt is None:
            try:
                with open(os.path.join(root, path), encoding="utf-8") as f:
                    txt = f.read()
            except OSError:
                txt = ""
            _SRC_INDEX[path] = txt
        if name not in txt:
            continue
        for i, l in enumerate(txt.split("\n"), 1):
            if rx.search(l):
                out.append((path, i))
    return out


def name_users(m, root, names, own_path, own_range):
    """Tests that read any of names: executed a line mentioning it at test time, or use
    a module whose import executed such a line."""
    mask = 0
    for name in names:
        for path, ln in name_refs(root, name, list(m.files)):
            if path == own_path and own_range[0] <= ln <= own_range[1]:
                continue
            lm = m.files.get(path)
            if lm is None:
                continue
            if ln in lm[0]:
                mask |= lm[0][ln]
            for mod in lm[1].get(ln, []):
                mask |= m.module_usage(mod)
    return mask


def rx_name(name):
    return re.compile(r"\b%s\b" % re.escape(name))


def narrow_by_name(m, root, names, own_path, own_range):
    """Tests that executed, at test time, a line mentioning one of names. None if a
    mention is import-time code (class creation etc.) and narrowing is unsafe."""
    mask = 0
    for name in names:
        if len(name) < 4:
            return None
        for path, ln in name_refs(root, name, list(m.files)):
            if path == own_path and own_range[0] <= ln <= own_range[1]:
                continue
            lm = m.files.get(path)
            if lm is None:
                continue
            lines, imp = lm
            if ln in imp:
                # executed while importing: definitions in other classes are fine,
                # anything else (metaclass/class creation reading it) is not.
                txt = _SRC_INDEX[path].split("\n")[ln - 1]
                dm = re.match(r"\s*%s\s*(:[^=]*)?=(?!=)" % re.escape(name), txt)
                if dm and not rx_name(name).search(txt[dm.end():]):
                    continue
                return None
            if ln in lines:
                mask |= lines[ln]
    return mask


def py_candidates(m, root, path, src, fp, notes):
    """Candidate tests for one changed python file. src: old source (None if new file)."""
    if src is None:
        return 0
    new_src = None
    try:
        new = apply(src, fp)
        new_src = new
        rm, ins = changed_old_lines(fp)
        if ast_same(src, new, set(rm) | set(ins) | set(k + 1 for k in ins)):
            notes.append("ast-same:" + path)
            return 0
    except Exception:
        pass
    try:
        sc = Scopes(src)
    except SyntaxError:
        notes.append("unparsable:" + path)
        return m.module_usage(module_name(path))
    removed, inserts = changed_old_lines(fp)
    points = set(removed)
    olines = src.split("\n")
    nlines = len(olines)
    modname = module_name(path)
    mask = 0
    try:
        new_tree = Scopes(new_src) if new_src is not None else None
    except SyntaxError:
        new_tree = None
    if new_tree is not None:
        gone = top_names(sc.tree) - top_names(new_tree.tree)
        gone = {g for g in gone if len(g) >= 3 and g not in ("annotations", "t", "exp")}
        if gone:
            mask |= removed_name_users(m, root, path, sorted(gone))
            notes.append("removed-names:%s:%s" % (path, ",".join(sorted(gone))[:80]))
            notes.append("IMPORTRISK")
    for k, new in inserts.items():
        pts = insertion_points(sc, olines, k, new)
        cls_pts = [p for p in pts if p < 0 or sc.locate(p)[0] != "func"]
        names = inserted_defs(new) if cls_pts and len(pts) == len(cls_pts) else None
        if names:
            # new methods in a class: they matter where that name is looked up on it
            ln0 = abs(cls_pts[0])
            cls = [(s_, e_, q) for s_, e_, q in sc.classes if s_ <= ln0 <= e_]
            if cls and k + 1 <= cls[-1][1] and k >= cls[-1][0]:
                usage = 0
                for s_, e_, q in cls:
                    usage |= m.classes.get(modname + ":" + q, 0)
                ok = True
                u = 0
                for nm in names:
                    hit = False
                    if nm.endswith("_sql") and not nm.startswith("_"):
                        u |= m.exp_usage(nm[:-4], lower=True)
                        hit = True
                    if nm in m.defs:
                        u |= m.def_union(nm)
                        hit = True
                    if not hit:
                        ok = False
                if ok:
                    mask |= usage & u
                    notes.append("newmethods:%s:%s" % (path, ",".join(names)))
                    notes.append("IMPORTRISK")
                    continue
        points |= pts
    seen = set()
    for ln in sorted(points):
        beside = ln < 0
        ln = max(1, min(abs(ln), nlines))
        loc = sc.locate(ln)
        if beside and loc[0] == "func":
            cls = [(s, e, q) for s, e, q in sc.classes if s <= ln <= e]
            loc = ("class", cls) if cls else ("module",)
        if loc[0] == "func":
            # function code that runs while some module is imported (decorators,
            # metaclasses, builders called from class bodies) shapes that module
            isdef = _is_def_line(olines, loc, ln)
            if isdef:
                notes.append("defline")
                notes.append("IMPORTRISK")
            for mod in m.imp_mods(path, ln):
                if not isdef:
                    mask |= m.module_usage(mod)
                    notes.append("impfunc:" + mod)
                    notes.append("IMPORTRISK")
            key = loc
            if key not in seen:
                seen.add(key)
                mask |= m.lines_union(path, loc[1], loc[2])
                if (loc[1], loc[2]) in sc.cachefns:
                    mask |= m.broken_usage([modname])
                    notes.append("cachefn:%s:%s" % (path, loc[3]))
            continue
        lam = sc.lambda_at(ln)
        asg = sc.assign_at(ln)
        if loc[0] == "class":
            cls = loc[1]
            usage = 0
            for s, e, q in cls:
                usage |= m.classes.get(modname + ":" + q, 0)
            if lam is not None and lam[0] < ln <= lam[1] and not beside:
                # a line inside a multi-line lambda body in the class body runs only
                # when the lambda is called
                mask |= m.lines_union(path, lam[0], lam[1])
                notes.append("lambda:%s:%d" % (path, ln))
                continue
            if asg is not None:
                s, e, q, names = asg
                keys = changed_dict_exp_keys(sc.assign_nodes.get((q, tuple(names))),
                                             new_sc_assign(new_tree, q, names))
                if keys:
                    eu = 0
                    for x in keys:
                        eu |= m.exp_usage(x)
                    mask |= usage & eu
                    notes.append("dictkeys:%s:%s.%s:%s" % (path, q, ",".join(names), ",".join(sorted(keys))))
                    continue
                nb = narrow_by_name(m, root, names, path, (s, e))
                if nb is not None:
                    mask |= usage & nb
                    notes.append("attr:%s:%s.%s" % (path, q, ",".join(names)))
                    notes.append("IMPORTRISK")
                    continue
            contrib = usage
            for s, e, q in cls:
                contrib |= m.lines_union(path, s, e)
            if asg is not None:
                # may be narrowed later by comparing class attributes before/after
                _CTX["class_mask"] |= contrib
                _CTX["assigns"].append((modname, asg[2], tuple(asg[3])))
            else:
                mask |= contrib
            notes.append("classlevel:%s:%s" % (path, cls[-1][2]))
            notes.append("IMPORTRISK")
        else:
            if asg is not None:
                s_, e_, q, names = asg
                keys = changed_dict_exp_keys(sc.assign_nodes.get((q, tuple(names))),
                                             new_sc_assign(new_tree, q, names))
                if keys:
                    eu = 0
                    for x in keys:
                        eu |= m.exp_usage(x)
                    mask |= m.broken_usage([modname]) & eu
                    notes.append("dictkeys:%s:%s:%s" % (path, ",".join(names), ",".join(sorted(keys))))
                    notes.append("IMPORTRISK")
                    continue
            stmt = sc.module_stmt_at(ln)
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                # names that disappear are handled above; anything else shows up as an
                # import error, which the run step checks
                mask |= m.module_usage(modname)
                notes.append("modimport:%s:%d" % (path, ln))
            elif asg is not None and asg[2] == "" and not path.endswith("__init__.py"):
                # module-level NAME = ...: whoever reads NAME, at test time or while
                # importing some module
                mask |= m.module_usage(modname) | name_users(m, root, asg[3], path, (asg[0], asg[1]))
                notes.append("modassign:%s:%s" % (path, ",".join(asg[3])))
            elif path.endswith("__init__.py"):
                # package-level state is read through cached lazy loaders: everyone
                # importing the package may depend on it
                mask |= m.broken_usage([modname])
                notes.append("modlevel:%s:%d" % (path, ln))
            else:
                mask |= m.module_usage(modname)
                notes.append("modlevel:%s:%d" % (path, ln))
            notes.append("IMPORTRISK")
    return mask


def _exp_key(k):
    """exp.X -> 'X' (also sqlglot.exp.X / expressions.X), else None."""
    if isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id in ("exp", "expressions"):
        return k.attr
    return None


def changed_dict_exp_keys(old, new):
    """Assignments old/new whose value is a dict literal: the exp.X keys of the entries
    that differ, or None if anything else differs."""
    if old is None or new is None or type(old) is not type(new):
        return None
    if isinstance(old, ast.AugAssign):
        return None
    tg_o = old.targets if isinstance(old, ast.Assign) else [old.target]
    tg_n = new.targets if isinstance(new, ast.Assign) else [new.target]
    if not _node_eq(tg_o, tg_n):
        return None
    if isinstance(old, ast.AnnAssign) and not _node_eq(old.annotation, new.annotation):
        return None
    vo, vn = old.value, new.value
    if not (isinstance(vo, ast.Dict) and isinstance(vn, ast.Dict)):
        return None

    def entries(d):
        out = {}
        for k, v in zip(d.keys, d.values):
            if k is None:
                key = "**" + ast.dump(v)
            else:
                key = ast.dump(k)
            out[key] = (k, ast.dump(v))
        return out

    eo, en = entries(vo), entries(vn)
    names = set()
    for key in set(eo) | set(en):
        a, b = eo.get(key), en.get(key)
        if a is not None and b is not None and a[1] == b[1]:
            continue
        knode = (a or b)[0]
        x = _exp_key(knode) if knode is not None else None
        if x is None:
            return None
        names.add(x)
    return names if names else None


def inserted_defs(new_lines):
    """Names of functions in an inserted block that holds only defs (and comments)."""
    import textwrap
    txt = textwrap.dedent("\n".join(new_lines))
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        return None
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        else:
            return None
    return names or None


def new_sc_assign(new_sc, q, names):
    if new_sc is None:
        return None
    return new_sc.assign_nodes.get((q, tuple(names)))


def _is_def_line(olines, loc, ln):
    """Is ln part of the def statement/decorators of the function at loc (these run at
    definition time for every function, so they say nothing about the importer)?"""
    s = loc[1]
    i = s
    while i <= ln:
        t = olines[i - 1].strip()
        if t.startswith("def ") or t.startswith("async def "):
            j = i
            while j <= len(olines) and not olines[j - 1].rstrip().endswith(":"):
                j += 1
            return ln <= j
        i += 1
    return True


def _indent(s):
    return len(s) - len(s.lstrip(" \t"))


def insertion_points(sc, olines, k, new):
    """Old lines whose scope a pure insertion after old line k belongs to."""
    code = [l for l in new if l.strip() and not l.strip().startswith("#")]
    if not code:
        return {max(k, 1)}
    ind = min(_indent(l) for l in code)
    n = len(olines)
    prev = k
    while prev >= 1 and not olines[prev - 1].strip():
        prev -= 1
    nxt = k + 1
    while nxt <= n and not olines[nxt - 1].strip():
        nxt += 1
    pts = set()
    for ln in (prev, nxt):
        if not 1 <= ln <= n:
            continue
        loc = sc.locate(ln)
        if loc[0] == "func" and ind > _indent(olines[loc[1] - 1]) and _indent(olines[ln - 1]) >= ind:
            pts.add(ln)
    if pts:
        return pts
    for ln in (prev, nxt):
        if not 1 <= ln <= n:
            continue
        loc = sc.locate(ln)
        if loc[0] == "func":
            if ind > _indent(olines[loc[1] - 1]):
                pts.add(ln)
            else:
                # sits beside the function: class or module level at that position
                pts.add(-loc[1])
        else:
            pts.add(ln)
    return pts


def mention_tests(m, root, path):
    """Tests in test files whose source mentions the file's name (for files the map
    never saw opened: build files, new fixtures, ...)."""
    base = os.path.basename(path)
    keys = {base}
    if path.startswith("tests/fixtures/"):
        keys.add(path[len("tests/fixtures/"):])
        stem = base.rsplit(".", 1)[0]
        if len(stem) >= 4:
            keys.add('"%s"' % stem)
    top = path.split("/")[0]
    if top not in ("sqlglot", "tests") and "/" in path:
        keys.add(top)
    mask = 0
    for tf, tm in m.by_file.items():
        txt = _SRC_INDEX.get(tf)
        if txt is None:
            try:
                with open(os.path.join(root, tf), encoding="utf-8") as f:
                    txt = f.read()
            except OSError:
                txt = ""
            _SRC_INDEX[tf] = txt
        if any(k in txt for k in keys):
            mask |= tm
    return mask


_CTX = {"class_mask": 0, "assigns": []}


def candidates(m, root, fps):
    """-> (mask, notes). After the call, LAST holds the mask without the class-body
    assignment fallbacks ("nonclass") and those assignments ("assigns")."""
    _CTX["class_mask"] = 0
    _CTX["assigns"] = []
    mask = 0
    notes = []
    for fp in fps:
        path = fp.path
        if path is None:
            continue
        olds = [p for p in (fp.old, fp.git_old) if p]
        old = olds[0] if olds else None
        top = (old or path).split("/")[0]
        base = os.path.basename(path)
        if base == "conftest.py" or base in ("pytest.ini", "tox.ini") or (
                base in ("setup.cfg", "pyproject.toml") and "pytest" in "".join(
                    l for h in fp.hunks for l in h[4] if l[:1] in "+-")):
            # pytest configuration: can change how every test below it runs
            d = os.path.dirname(path)
            sub = 0
            for f, tm in m.by_file.items():
                if not d or f.startswith(d + "/"):
                    sub |= tm
            mask |= sub
            notes.append("pytest-config:" + path)
            continue
        if (path.endswith(".py") or (old and old.endswith(".py"))) and top not in ("sqlglot", "tests"):
            g = mention_tests(m, root, old or path)
            mask |= g
            notes.append("outside-py:%s:%d" % (old or path, bin(g).count("1")))
            continue
        if path.endswith(".py") or (old and old.endswith(".py")):
            src = None
            if old and os.path.isfile(os.path.join(root, old)):
                with open(os.path.join(root, old), encoding="utf-8") as f:
                    src = f.read()
            if src is None:
                notes.append("newpy:" + path)
            mask |= py_candidates(m, root, old or path, src, fp, notes)
            if old and (fp.new is None or (fp.old and fp.new and fp.old != fp.new)
                        or (fp.git_old and fp.git_new and fp.git_old != fp.git_new)):
                mask |= m.broken_usage([module_name(old)])
                notes.append("delete_or_rename:" + str(old))
            elif src is None:
                notes.append("IMPORTRISK")
        else:
            for p in set(filter(None, [fp.old, fp.new, fp.git_old, fp.git_new])):
                if p in m.opens:
                    mask |= m.opens[p]
                    notes.append("data:" + p)
                else:
                    g = mention_tests(m, root, p)
                    mask |= g
                    notes.append("unmapped:%s:%d" % (p, bin(g).count("1")))
    LAST["nonclass"] = mask
    LAST["assigns"] = list(_CTX["assigns"])
    return mask | _CTX["class_mask"], notes


LAST = {}


def textref_tests(m, root, name):
    """Tests that executed, at test time, a line mentioning name; None if there is none."""
    mask = 0
    found = False
    for path, ln in name_refs(root, name, list(m.files)):
        lm = m.files.get(path)
        if lm is None:
            continue
        if ln in lm[0]:
            mask |= lm[0][ln]
            found = True
    return mask if found else None
