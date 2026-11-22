#!/usr/bin/env bash
set -euo pipefail
# M8: Docker Engine + gVisor runsc + uv on Ubuntu 24.04 aarch64 (Oracle A1).
# Run as root: sudo bash vm-bootstrap.sh. Idempotent and non-interactive; safe to run twice.
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || echo "warning: expected aarch64, got $(uname -m)" >&2
. /etc/os-release
[ "$VERSION_ID" = "24.04" ] || echo "warning: expected Ubuntu 24.04, got $VERSION_ID" >&2
export DEBIAN_FRONTEND=noninteractive
arch="$(dpkg --print-architecture)"

# Preseed so installing iptables-persistent never prompts.
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections
apt-get update -q
apt-get install -yq ca-certificates curl gnupg git iptables-persistent
install -m 0755 -d /etc/apt/keyrings

# Docker Engine (official apt repo). Default runtime stays runc; runsc is chosen per
# container via SWF_SANDBOX_RUNTIME, so the egress proxy (Squid) keeps running on runc.
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list
# gVisor (official apt repo)
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$arch signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update -q
apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin runsc
runsc install  # registers the runsc runtime in /etc/docker/daemon.json (default runtime unchanged)
systemctl restart docker

# Test user must be uid 1000 (sandbox --user 1000:1000 writes the bind-mounted workspace; m8 §0.15).
u="$(getent passwd 1000 | cut -d: -f1 || true)"
if [ -z "$u" ]; then useradd -m -u 1000 -s /bin/bash swf; u=swf; fi
home="$(getent passwd "$u" | cut -d: -f6)"
usermod -aG docker "$u"
# Let the admin's ssh key log in as the test user too.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "$u" ]; then
  keys="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.ssh/authorized_keys"
  if [ -f "$keys" ]; then
    install -d -m 700 -o "$u" -g "$u" "$home/.ssh"
    install -m 600 -o "$u" -g "$u" "$keys" "$home/.ssh/authorized_keys"
  fi
fi

# uv, pinned to the dev machine's version.
sudo -u "$u" -H sh -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/0.13.5/install.sh | sh'

# Sandboxes must not reach host services through their bridge gateway (m8 §0.14).
# Safe for the sandbox -> proxy path: both sit on the same task bridge, so that traffic is
# bridged/forwarded and never hits the host's INPUT chain. DNS is Docker's embedded resolver
# at 127.0.0.11 inside each container's own netns, also not host INPUT. The test Postgres
# runs on the default docker0 bridge, which br-+ does not match.
# Caveat: the rule sits at INPUT 1, ahead of Oracle's RELATED,ESTABLISHED ACCEPT, so replies to
# any host -> br-* connection are dropped too. Nothing does that today: the host only talks to
# the proxy and the test Postgres over docker0.
iptables -C INPUT -i br-+ -j DROP 2>/dev/null || iptables -I INPUT 1 -i br-+ -j DROP
ip6tables -C INPUT -i br-+ -j DROP 2>/dev/null || ip6tables -I INPUT 1 -i br-+ -j DROP

# Persist Oracle's base rules plus ours, but none of Docker's runtime state: Docker rebuilds its
# chains on start, and stale per-container DNAT/ACCEPT lines restored at boot would linger.
# (Not `netfilter-persistent save`, which would persist all of it.) Drops every line naming
# docker (DOCKER* chains, docker0 rules) and every br-* line except our INPUT DROP. Table
# headers (*filter, *nat ...), chain policies and COMMIT lines are kept, so the file stays a
# valid iptables-restore input, e.g.:
#   *filter / :INPUT ACCEPT [0:0] / ... / -A INPUT -i br-+ -j DROP / -A INPUT -m state ... / COMMIT
# Rewritten from live state each run, so re-running is idempotent.
persist_rules() {  # $1 = iptables | ip6tables, $2 = target file
  local tmp="$2.swf-tmp"
  "$1-save" | awk '
    $0 == "-A INPUT -i br-+ -j DROP" { print; next }
    tolower($0) ~ /docker/ || /br-/ { next }
    { print }' > "$tmp"
  "$1-restore" --test < "$tmp"
  mv "$tmp" "$2"
}
install -d -m 0755 /etc/iptables
persist_rules iptables /etc/iptables/rules.v4
persist_rules ip6tables /etc/iptables/rules.v6

# Smoke check (sed, not head: head would close the pipe early and trip pipefail).
docker run --rm --runtime=runsc alpine:3 dmesg | sed -n '1,3p'
echo "ok: test user = $u (uid 1000); docker runtimes: $(docker info --format '{{json .Runtimes}}')"
