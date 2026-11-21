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
# A memory limit by default (LAB_MEMORY to change it): an unlimited container can take the whole machine down.
exec docker run --rm --memory "${LAB_MEMORY:-4g}" --memory-swap "${LAB_MEMORY:-4g}" "${FLAGS[@]}" \
  -v /root/lab/out:/out -v "$REPO:/repo:ro" \
  -v "$REPO/tools:/tools:ro" -v "$REPO/sample:/data:ro" br-lab "$@"
