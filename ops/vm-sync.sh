#!/usr/bin/env bash
set -euo pipefail
# Copy a branch to the VM with git bundle + scp (no GitHub before M9). Ships committed history only,
# so untracked and ignored files (including secrets) never leave this machine.
# Usage: bash ops/vm-sync.sh user@host [branch]
target="${1:?usage: vm-sync.sh user@host [branch]}"
branch="${2:-$(git rev-parse --abbrev-ref HEAD)}"
if [ "$branch" = HEAD ]; then
  echo "detached HEAD: pass the branch to sync, e.g. bash ops/vm-sync.sh $target feature/m8" >&2
  exit 1
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git bundle create "$tmp/swf.bundle" "$branch"
scp -o BatchMode=yes "$tmp/swf.bundle" "$target:swf.bundle"
ssh -o BatchMode=yes "$target" bash -s -- "$branch" <<'REMOTE'
set -euo pipefail
branch="$1"
git bundle verify ~/swf.bundle >/dev/null
[ -d ~/swf/.git ] || git clone --branch "$branch" ~/swf.bundle ~/swf
cd ~/swf
git fetch ~/swf.bundle "$branch"
git checkout -f -B "$branch" FETCH_HEAD  # the VM copy is a mirror: local edits are discarded
git log --oneline -1
REMOTE
