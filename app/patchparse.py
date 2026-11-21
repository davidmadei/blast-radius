"""Minimal unified-diff parser."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    def __init__(self):
        self.old = None  # old path (None for new file)
        self.new = None  # new path (None for deleted)
        self.hunks = []  # list of (old_start, old_len, new_start, new_len, lines)
        self.binary = False
        self.git_old = None
        self.git_new = None
        self.mode_only = False

    @property
    def path(self):
        return self.new or self.old or self.git_new or self.git_old

    def __repr__(self):
        return f"FilePatch({self.old!r}->{self.new!r}, {len(self.hunks)} hunks)"


def _strip(p):
    p = p.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git (\S+) (\S+)", line)
            if m:
                cur.git_old = _strip(m.group(1))
                cur.git_new = _strip(m.group(2))
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks or cur.old is not None or cur.new is not None:
                cur = FilePatch()
                files.append(cur)
            cur.old = _strip(line[4:])
            cur.new = _strip(lines[i + 1][4:])
            if cur.old is None and cur.git_old and not line[4:].strip().startswith("/dev/null"):
                cur.old = cur.git_old
            i += 2
            continue
        if cur is not None and line.startswith("rename from "):
            cur.git_old = line[len("rename from "):].strip()
        elif cur is not None and line.startswith("rename to "):
            cur.git_new = line[len("rename to "):].strip()
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur.binary = True
        m = HUNK.match(line)
        if m and cur is not None:
            os_, ol, ns, nl = m.groups()
            os_ = int(os_)
            ol = 1 if ol is None else int(ol)
            ns = int(ns)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            ro, rn = ol, nl
            while i < n and (ro > 0 or rn > 0):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                if l == "":
                    l = " "
                c = l[0]
                if c == " ":
                    ro -= 1
                    rn -= 1
                elif c == "-":
                    ro -= 1
                elif c == "+":
                    rn -= 1
                else:
                    break
                body.append(l)
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    for f in files:
        if f.old is None and f.new is None:
            # header-only (mode change, pure rename, binary)
            f.old = f.git_old
            f.new = f.git_new
            if not f.hunks and not f.binary:
                f.mode_only = True
    return files


def changed_old_lines(fp):
    """Return (removed_old_lines, insertions). insertions: {k: [new lines]} for pure
    insertions between old lines k and k+1 (a run of + lines not preceded by -)."""
    removed = set()
    inserts = {}
    for os_, ol, ns, nl, body in fp.hunks:
        o = os_ if ol > 0 else os_ + 1  # with ol==0, os_ is the line before
        i = 0
        n = len(body)
        while i < n:
            c = body[i][0]
            if c == " ":
                o += 1
                i += 1
            elif c == "-":
                while i < n and body[i][0] == "-":
                    removed.add(o)
                    o += 1
                    i += 1
                while i < n and body[i][0] == "+":
                    i += 1
            else:
                run = []
                while i < n and body[i][0] == "+":
                    run.append(body[i][1:])
                    i += 1
                inserts.setdefault(o - 1, []).extend(run)
    return removed, inserts


def apply(old_text, fp):
    """Apply hunks of fp to old_text (strict on context). Returns new text."""
    if old_text is None:
        old_lines = []
    else:
        old_lines = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into old_lines
    for os_, ol, ns, nl, body in fp.hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old_lines[pos:start])
        pos = start
        for l in body:
            c, rest = l[0], l[1:]
            if c == " ":
                if pos >= len(old_lines) or old_lines[pos] != rest:
                    raise ValueError("context mismatch")
                out.append(rest)
                pos += 1
            elif c == "-":
                if pos >= len(old_lines) or old_lines[pos] != rest:
                    raise ValueError("removal mismatch")
                pos += 1
            elif c == "+":
                out.append(rest)
    out.extend(old_lines[pos:])
    return "\n".join(out)
