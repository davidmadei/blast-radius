"""Build a patched view of the pristine checkout from symlinks + rewritten files."""
import os
import shutil

from patchparse import apply


def build(src_root, dst, fps):
    """Returns list of (relpath, newtext or None) applied. Raises on patch failure."""
    if os.path.lexists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    changes = {}
    for fp in fps:
        old = fp.old or fp.git_old
        new = fp.new or fp.git_new
        if fp.binary:
            raise ValueError("binary patch")
        if fp.mode_only:
            if old and new and old != new:
                text = open(os.path.join(src_root, old), encoding="utf-8").read()
                changes[old] = None
                changes[new] = text
            continue
        if fp.old is None:
            base = None
        else:
            p = os.path.join(src_root, fp.old)
            base = changes.get(fp.old) if fp.old in changes else (
                open(p, encoding="utf-8", newline="").read() if os.path.isfile(p) else None)
            if base is None and fp.hunks and fp.hunks[0][1] > 0:
                raise ValueError("missing file " + fp.old)
        text = apply(base, fp) if fp.hunks else (base or "")
        if fp.old and fp.old != fp.new:
            changes[fp.old] = None
        if fp.new:
            changes[fp.new] = text
    # directories that need to be real
    real_dirs = set([""])
    for rel in changes:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            real_dirs.add("/".join(parts[:i]))
    for d in sorted(real_dirs, key=len):
        sd = os.path.join(src_root, d)
        dd = os.path.join(dst, d)
        os.makedirs(dd, exist_ok=True)
        if os.path.isdir(sd):
            for name in os.listdir(sd):
                rel = (d + "/" + name) if d else name
                if rel in real_dirs or rel in changes or name == "__pycache__":
                    continue
                os.symlink(os.path.join(sd, name), os.path.join(dd, name))
    for rel, text in changes.items():
        if text is None:
            continue
        p = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return changes
