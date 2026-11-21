#!/bin/bash
# Score the selector on labeled patches of my own.
# Usage: tools/eval_set.sh <patch_dir> <labels_dir> <name> [manifest] [extra docker flags...]
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PATCHES="$1"; LABELS="$2"; NAME="$3"; MANIFEST="${4:-}"; shift 4 || shift $#
DATA="/root/lab/data/$NAME"
python3 "$REPO/tools/make_data.py" "$PATCHES" "$LABELS" "$REPO/sample/tests.json" "$DATA" || exit 1
OUT="/root/lab/out/$NAME.jsonl" bash "$REPO/tools/run_sample.sh" "$DATA" "$@" | tail -8
python3 "$REPO/tools/evaluate.py" "$DATA" "/root/lab/out/$NAME.jsonl" $MANIFEST
