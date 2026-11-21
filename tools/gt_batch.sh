#!/bin/bash
# Label every patch in a directory with a full pytest run, several containers at
# a time, each pinned to its own pair of cores (so the tests' process pools see
# two CPUs, as a small CI machine would).
# Usage: tools/gt_batch.sh <patch_dir> <out_name under /root/lab/out> [parallel=3] [first core=0]
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PATCHES="$1"; NAME="$2"; PAR="${3:-3}"; BASE="${4:-0}"
mkdir -p "/root/lab/out/$NAME"
export REPO PATCHES NAME BASE
export BR_GT_TIMEOUT="${BR_GT_TIMEOUT:-300}" BR_GT_SESSION="${BR_GT_SESSION:-2400}"
ls "$PATCHES"/*.patch | xargs -P "$PAR" -n 3 --process-slot-var=SLOT bash -c '
  A=$((BASE + SLOT * 2)); B=$((A + 1))
  docker run --rm --cpuset-cpus "$A,$B" -e BR_GT_TIMEOUT -e BR_GT_SESSION \
    -v /root/lab/out:/out -v "$REPO/tools:/tools:ro" \
    -v "$REPO/sample/tests.json:/data/tests.json:ro" -v "$PATCHES:/patches:ro" br-lab \
    python /tools/gt_run.py /data/tests.json "/out/$NAME" $(for p in "$@"; do echo /patches/$(basename $p); done)
' _
