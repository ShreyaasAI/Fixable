#!/usr/bin/env bash
set -euo pipefail
# M8 DoD on the VM: the non-live suite with sandboxes under gVisor. Log in .m8-vm/.
# Run from ~/swf as the uid-1000 user: bash ops/vm-test.sh [pytest args...]
cd "$(dirname "$0")/.."
mkdir -p .m8-vm
log=".m8-vm/pytest-$(date -u +%Y%m%dT%H%M%SZ).log"
{ uname -m; docker info --format '{{json .Runtimes}}'; runsc --version | sed -n '1p'; git log --oneline -1; } | tee "$log"
rc=0
SWF_SANDBOX_RUNTIME=runsc PYTHONDONTWRITEBYTECODE=1 ~/.local/bin/uv run pytest -o addopts="" -q -rs -m "not live" "$@" 2>&1 \
  | tee -a "$log" || rc=$?
echo "leftovers:" | tee -a "$log"
docker ps -a --filter label=swf.managed=1 --format '{{.Names}}' | tee -a "$log"
echo "log: $log (pytest exit $rc)"
exit "$rc"
