"""Build step: sqlglot at the pinned commit into <dst> (no git binary in the image).

Tries GitHub's archive of the commit, then a shallow fetch of exactly that commit
with dulwich (pure-python git, from PyPI)."""
import io
import os
import shutil
import sys
import tarfile
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
dst = sys.argv[1]


def via_archive():
    url = f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz"
    data = urllib.request.urlopen(url, timeout=120).read()
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        prefix = tf.getnames()[0].split("/")[0]
        for m in tf.getmembers():
            if not m.name.startswith(prefix + "/"):
                continue
            m.name = m.name[len(prefix) + 1:]
            if m.name:
                tf.extract(m, dst, filter="data")


def via_dulwich():
    from dulwich.client import get_transport_and_path
    from dulwich.index import build_index_from_tree
    from dulwich.repo import Repo
    os.makedirs(dst, exist_ok=True)
    repo = Repo.init(dst)
    client, path = get_transport_and_path("https://github.com/tobymao/sqlglot.git")
    sha = SHA.encode()
    client.fetch(path, repo, determine_wants=lambda refs, depth=None: [sha], depth=1)
    build_index_from_tree(repo.path, repo.index_path(), repo.object_store, repo[sha].tree)
    repo.close()
    shutil.rmtree(os.path.join(dst, ".git"), ignore_errors=True)


for attempt, fn in enumerate([via_archive, via_dulwich, via_archive, via_dulwich]):
    try:
        shutil.rmtree(dst, ignore_errors=True)
        fn()
        if os.path.isfile(os.path.join(dst, "sqlglot", "__init__.py")) and os.path.isdir(os.path.join(dst, "tests", "fixtures")):
            break
    except Exception as e:  # network hiccup: try the other way
        print("fetch via", fn.__name__, "failed:", repr(e), file=sys.stderr)
else:
    sys.exit("could not fetch sqlglot " + SHA)
n = sum(len(f) for _, _, f in os.walk(dst))
print("sqlglot", SHA, "->", dst, n, "files", file=sys.stderr)
