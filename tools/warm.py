"""Build step: precompile bytecode for the paths the selector imports from at run time."""
import compileall, os, sys
sys.path.insert(0, "/app/app")
import tree
for slot in range(int(sys.argv[1])):
    d = f"/tmp/w{slot}"
    tree.build("/opt/sq", d, [])
    for sub in ("sqlglot", "tests"):
        compileall.compile_dir(os.path.join(d, sub), quiet=1, workers=1)
