#!/bin/bash
# Rebuild data/map.json.gz from the pinned sqlglot checkout (run once, offline; ~15 min on 2 cores).
# usage: tools/build_map.sh <sqlglot_checkout> <tests.json> <venv_with_requirements_and_coverage>
set -e
SQ=$1; TJ=$2; VENV=$3
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(mktemp -d)
. "$VENV/bin/activate"
cd "$SQ"
export SKIP_INTEGRATION=0 MAP_ROOT=$PWD PYTHONPATH=.:$HERE:$HERE/../app
# 1. per-test line coverage, import-time attribution, data-file opens
COVERAGE_CORE=ctrace MAP_OUT=$OUT/m python -m pytest -q -p no:cacheprovider -p syncpool -p maptrace tests
# 2. classes used (as self/cls) by each test
MAP_OUT=$OUT/m python -m pytest -q -p no:cacheprovider -p syncpool -p classtrace tests
# 3. CPU cost of each test when run in-process
CPU_OUT=$OUT/cpu.json python -m pytest -q -p no:cacheprovider -p syncpool -p cputime tests
python "$HERE/compile_map.py" "$SQ" $OUT/m "$TJ" "$HERE/../data/map.json.gz" $OUT/cpu.json
