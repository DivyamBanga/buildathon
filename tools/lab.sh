#!/bin/bash
# Run a command in the lab image with the repo mounted read-only at /repo.
# Usage: tools/lab.sh [docker-run-flags --] command...
REPO="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p /root/lab/out
FLAGS=()
if [[ " $* " == *" -- "* ]]; then
  while [[ "$1" != "--" ]]; do FLAGS+=("$1"); shift; done
  shift
fi
exec docker run --rm "${FLAGS[@]}" -v /root/lab/out:/out -v "$REPO:/repo:ro" \
  -v "$REPO/tools:/tools:ro" -v "$REPO/sample:/data:ro" br-lab "$@"
