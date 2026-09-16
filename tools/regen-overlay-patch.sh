#!/bin/bash
# Regenerates tools/client-overlay.patch from an edited staged tree.
#
# Workflow for changing what the Arkenfall client does: run tools/build-client.sh,
# edit the file under build-client/stage/Arkenfall/, run this, rebuild. The set of
# files the overlay touches is taken from the existing patch, so adding a new one
# means adding its path to FILES below.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=${1:-$REPO/build-client}
SRC=$WORK/src
STAGE=$WORK/stage/Arkenfall
OUT=$REPO/tools/client-overlay.patch

FILES=$(grep '^--- a/' "$OUT" | sed 's|^--- a/||')
[ -n "$FILES" ] || { echo "no files listed in $OUT" >&2; exit 1; }

: > "$OUT"
for f in $FILES; do
	[ -f "$SRC/$f" ] && [ -f "$STAGE/$f" ] || { echo "missing $f" >&2; exit 1; }
	diff -u --label "a/$f" --label "b/$f" "$SRC/$f" "$STAGE/$f" >> "$OUT" || true
done
echo "$OUT: $(grep -c '^--- a/' "$OUT") files, $(wc -l < "$OUT") lines"
