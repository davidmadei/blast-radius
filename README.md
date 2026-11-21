# Blast radius: which sqlglot tests does this patch break?

For every patch in `/data/commits`, the selector prints the tests to run and how to
split them across up to 8 workers. It runs in three stages.

## 1. Coverage map (built once from the pinned commit, committed as `data/map.json.gz`)

`tools/build_map.sh` runs sqlglot's test suite at `eb11aec` three times. Each run uses a
plugin from `tools/`:

* `maptrace.py`: line coverage per test (setup and teardown lines go to the whole
  test class). Lines that run while a module is being imported are attributed to
  that module, not to whichever test happened to trigger the import. The plugin
  also records which data files each test opens.
* `classtrace.py`: the sqlglot classes each test uses as `self`/`cls`. This includes
  Dialects, Parsers, Generators, Tokenizers and every Expression subclass.
* `cputime.py`: the in-process CPU cost of each test and of each class's setup.

`syncpool.py` swaps the optimizer and executor tests' `ProcessPoolExecutor` for an
in-process one. Arguments and results are still pickled, as a real pool would do.
Without the swap, their work is invisible to tracing and costly to spawn.

`compile_map.py` packs all of this into a gzipped JSON file of about 470 KB. The file
also holds a static import graph of `sqlglot/` and `tests/`.

## 2. From a patch to candidate tests (`app/analyze.py`)

The rules for each changed line of a Python file (old-file coordinates; for pure
insertions, the scope is decided by indentation):

| Where the change is | Candidates |
| --- | --- |
| Python file whose AST is unchanged (comments, formatting) | none |
| inside a function or method | tests that executed any line of that function |
| function code that runs at import (decorators, metaclasses, builders) | users of each module whose import ran it |
| inside a multi-line lambda in a class body | tests that executed the lambda |
| class-body `NAME = ...` | users of the class, narrowed to tests that executed a line mentioning `NAME` (only when every such line is test-time code) |
| other class-body code | users of the class |
| module level | users of the module (lines executed, or classes used); for a package `__init__`, users of every importer |
| a module stops defining a name | test files and modules that mention the name, and their importers' users |
| new methods in a class | users of the class that ran a method of that name elsewhere, or (for `*_sql`) used that expression class |
| changed `exp.X: ...` entries of a dict literal | users of the class (or module) that also used `exp.X` |
| syntax error, failed import, deleted or renamed module | users of every module that imports it, transitively |
| data file (fixtures) | tests that opened it |
| file the map never saw (build files, new fixtures) | tests whose source mentions its name |

Class-body assignments that cannot be narrowed statically, such as dialect settings
that the `_Dialect` metaclass digests into tries at import, go through one more step.
The selector imports the module once unpatched and once patched, and compares a
stable digest of every class attribute. The test is then narrowed to the attributes
that really changed, derived ones included: users of the class that executed a line
mentioning that attribute.

## 3. Run the cheap ones, keep the expensive ones (`app/selector.py`, `app/runner.py`)

For each commit, a planner splits the candidates in two:

* tests kept without running them, packed onto workers;
* tests run now against the patched tree, so only failures need to be reported.

The planner minimises worker cost plus the CPU of running. For example,
`test_execute_tpcds` is kept rather than run, and small tests then fill the other
workers for free.

The patched tree is a symlink farm over a pristine checkout in `/opt/sq`, holding
only the rewritten files. Bytecode for it is precompiled at build time through
`PYTHONPYCACHEPREFIX`.

Tests run in children forked from a warm parent that has already imported pytest,
duckdb and pandas. Two children run at a time, each with a per-test timeout.

The safety nets:

* If a child crashes or times out, everything it was asked to run is kept.
* If a test file fails to collect, all its tests are kept.
* If a module fails to import, its importers' users are kept.

Two expensive tests are run only in part, when the change allows it:

* `test_execute_tpcds` / `test_execute_tpch` run only the pool tasks (queries) whose
  recorded coverage reaches the change. This applies when every rule that fired was
  a line rule and the change does not reach the tests' main bodies.
* `test_generator_memory` runs only for the dialects built from the changed
  dialect-specific modules.
