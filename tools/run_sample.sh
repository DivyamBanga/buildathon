#!/bin/bash
# Build the submission image, run it on a data set under the scorer's limits,
# measure the container's CPU time from its cgroup, and score the output.
# Usage: tools/run_sample.sh [data_dir=sample] [extra docker run flags...]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${1:-$REPO/sample}"; shift || true
IMAGE=blast-radius-entry
OUT="${OUT:-/root/lab/out/output.jsonl}"
cd "$REPO"
if [ -z "${NOBUILD:-}" ]; then
  docker build -q --platform linux/amd64 --tag $IMAGE . > /dev/null || exit 1
fi
ID=$(docker create --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$DATA/commits:/data/commits:ro" \
  --volume "$DATA/tests.json:/data/tests.json:ro" "$@" $IMAGE)
STAT="/sys/fs/cgroup/system.slice/docker-$ID.scope/cpu.stat"
START=$(date +%s.%N)
docker start -a "$ID" > "$OUT" 2> "$OUT.err" &
PID=$!
CPU=0
while kill -0 $PID 2>/dev/null; do
  V=$(awk '/^usage_usec/ {print $2}' "$STAT" 2>/dev/null)
  [ -n "$V" ] && CPU=$V
  sleep 0.05
done
wait $PID; RC=$?
END=$(date +%s.%N)
docker rm -f "$ID" > /dev/null
tail -n "${ERRLINES:-6}" "$OUT.err"
CPUS=$(awk "BEGIN {printf \"%.2f\", $CPU/1000000}")
echo "exit $RC  wall $(awk "BEGIN {printf \"%.1f\", $END-$START}") s  cpu ~$CPUS s"
if [ -f "$DATA/answers.json" ]; then
  python3 score.py "$DATA" "$DATA/answers.json" "$OUT" --cpu-seconds "$CPUS"
fi
