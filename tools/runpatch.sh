#!/bin/bash
# usage: runpatch.sh patchfile outfile
set -e
P=$1; OUT=$2
W=$(mktemp -d /tmp/wt.XXXX)
tar -C /home/claude/sqlglot --exclude=.git --exclude=__pycache__ -cf - . | tar -C $W -xf -
cd $W
patch -p1 -s < $P || echo PATCHFAIL >> $OUT
rm -f $OUT.tmp
. /home/claude/venv/bin/activate
SKIP_INTEGRATION=0 FAILREC_OUT=$OUT.tmp PYTHONPATH=$W:/home/claude/tools timeout 900 python -m pytest tests -q -n 2 -p no:cacheprovider -p failrec --timeout 60 >/dev/null 2>&1 || true
sort -u $OUT.tmp > $OUT 2>/dev/null || touch $OUT
rm -rf $W $OUT.tmp
