#!/bin/bash
# Install Docker CE inside Ubuntu WSL (purely CLI, no Docker Desktop).
# Idempotent: safe to re-run.
set -e
export DEBIAN_FRONTEND=noninteractive

if command -v docker >/dev/null 2>&1; then
    echo "docker already installed: $(docker --version)"
    exit 0
fi

# Prereqs
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg

# Docker's official GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Repo
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin

# In WSL there's no systemd by default; start dockerd by hand under nohup
if ! pgrep -x dockerd >/dev/null; then
    echo "starting dockerd..."
    nohup dockerd >/var/log/dockerd.log 2>&1 &
    # wait up to 20s for the socket
    for _ in $(seq 1 20); do
        [ -S /var/run/docker.sock ] && break
        sleep 1
    done
fi

docker --version
docker info --format '{{.OperatingSystem}} | {{.ServerVersion}} | storage={{.Driver}}' 2>&1 || {
    echo "FAILED to talk to dockerd. Log tail:"
    tail -40 /var/log/dockerd.log
    exit 1
}
