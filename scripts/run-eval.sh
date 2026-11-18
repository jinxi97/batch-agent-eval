#!/usr/bin/env bash
# Run a Harbor eval with terminus-2 + Kimi-K3.1-Pro on a Modal Shared Endpoint.
#
# Usage:
#   scripts/run-eval.sh                                   # default: one SWE-bench Verified task
#   scripts/run-eval.sh -t swe-bench/astropy__astropy-12907
#   scripts/run-eval.sh -d swe-bench/swe-bench-verified -l 20 -n 8
#   scripts/run-eval.sh -t swe-bench/django__django-11099 --job-name my-run
#
# Any arguments are passed straight to `harbor run` and can override the config.
# Environment variables are read from .env (see .env.example).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="${HARBOR_CONFIG:-configs/kimi-k3.1-pro-modal.yaml}"
DEFAULT_TASK="swe-bench/django__django-11099"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${MODAL_PROXY_KEY:?Set MODAL_PROXY_KEY (wk-<id>.ws-<secret>) in .env — see .env.example}"
: "${MODAL_ENDPOINT_URL:?Set MODAL_ENDPOINT_URL in .env — see .env.example}"

BASE_URL="${MODAL_ENDPOINT_URL%/}"
BASE_URL="${BASE_URL%/v1}/v1"

# Only add the default task if the caller didn't choose tasks/datasets themselves.
args=("$@")
has_selection=false
for a in "${args[@]+"${args[@]}"}"; do
  case "$a" in
    -t|--task|-d|--dataset|-p|--path|-t=*|--task=*|-d=*|--dataset=*|-p=*|--path=*) has_selection=true ;;
  esac
done
if [[ "$has_selection" == false ]]; then
  args=(-t "$DEFAULT_TASK" "${args[@]+"${args[@]}"}")
fi

exec uv run harbor run \
  -c "$CONFIG" \
  --base-url "$BASE_URL" \
  "${args[@]}"
