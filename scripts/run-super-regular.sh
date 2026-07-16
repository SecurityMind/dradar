#!/usr/bin/env bash
set -euo pipefail

# ds0-only launcher for the fixed-cell "super account regular test" batch.
# The client runtime is intentionally separate from the DRadar server venv so
# server deployments cannot silently replace the parallel-capable CLI.

concurrency="${1:-24}"
runtime="${DRADAR_CLI_RUNTIME:-/home/aloha/dradar-cli-runtime/current}"
python="$runtime/.venv/bin/python"
server_venv="${DRADAR_SERVER_VENV:-/home/aloha/dradar/.venv}"
pier_bin="${DRADAR_PIER_BIN:-/home/aloha/dradar-super-pier/current/bin/pier}"
pier_python="$(dirname "$pier_bin")/python"
egress_upstream="${PIER_EGRESS_UPSTREAM_PROXY:-http://host.docker.internal:7897}"

if ! [[ "$concurrency" =~ ^[0-9]+$ ]] || (( concurrency < 1 || concurrency > 90 )); then
  echo "concurrency must be an integer from 1 to 90" >&2
  exit 2
fi
if [[ ! -x "$python" ]]; then
  echo "pinned DRadar CLI runtime is missing: $python" >&2
  exit 1
fi
if [[ ! -x "$pier_bin" ]]; then
  echo "pinned Pier runtime is missing: $pier_bin" >&2
  exit 1
fi
if [[ ! -x "$pier_python" ]]; then
  echo "pinned Pier Python runtime is missing: $pier_python" >&2
  exit 1
fi

run_path="$runtime/.venv/bin:$(dirname "$pier_bin"):$server_venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH="$run_path"
export PIER_EGRESS_UPSTREAM_PROXY="$egress_upstream"
export DRADAR_BATCH_FAIL_FAST=1

# Fail closed on the exact capabilities this production batch relies on.
help="$($python -m dradar.cli go --help)"
grep -q -- "--parallel" <<<"$help" || {
  echo "refusing to start: pinned CLI has no --parallel support" >&2
  exit 1
}
if [[ "$(readlink -f "$(command -v pier)")" != "$(readlink -f "$pier_bin")" ]]; then
  echo "refusing to start: PATH resolved the wrong Pier binary" >&2
  exit 1
fi

"$pier_python" - <<'PY'
import inspect

from pier.agents.installed import codex
from pier.environments import agent_setup

if tuple(codex._MODEL_CAPACITY_RETRY_DELAYS) != (30, 60, 120):
    raise SystemExit("refusing to start: Pier lacks the capacity-resume policy")
proxy_source = inspect.getsource(agent_setup)
if "PIER_EGRESS_UPSTREAM_PROXY" not in proxy_source:
    raise SystemExit("refusing to start: Pier lacks the upstream-proxy policy")
PY

$python - <<'PY'
import shutil
import socksio  # noqa: F401 - required when ds0 uses a SOCKS proxy
from dradar.api_client import ApiClient
from dradar.runner import CODEX_SUBMISSION_PROMPT

required = ("checkout", "runner_heartbeat", "runner_close")
missing = [name for name in required if not hasattr(ApiClient, name)]
if missing:
    raise SystemExit(f"refusing to start: CLI missing session APIs: {missing}")
for marker in ("bash /tests/pre_artifacts.sh", "/logs/artifacts/model.patch"):
    if marker not in CODEX_SUBMISSION_PROMPT:
        raise SystemExit(f"refusing to start: artifact prompt missing {marker}")
if not shutil.which("pier"):
    raise SystemExit("refusing to start: pier is not on the explicit batch PATH")
PY

if systemctl --user list-units --type=service --state=running --no-legend \
    "dradar-regular24-*" | grep -q .; then
  echo "refusing to start: another regular-test batch is still running" >&2
  exit 1
fi

batch="dradar-regular24-$(date +%Y%m%d-%H%M%S)"
for i in $(seq -w 1 "$concurrency"); do
  systemd-run --user \
    --setenv=PATH="$run_path" \
    --setenv=PIER_EGRESS_UPSTREAM_PROXY="$egress_upstream" \
    --setenv=DRADAR_BATCH_FAIL_FAST=1 \
    --unit="$batch-$i" \
    --description="DRadar super regular test $batch worker $i/$concurrency" \
    "$python" -u -m dradar.cli go -y --parallel >/dev/null
done

echo "$batch"
