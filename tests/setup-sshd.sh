#!/bin/bash
# Set up sshd inside this WSL Ubuntu so we have a real SSH target for testing.
# Idempotent.
set -e

# Run as root.
if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root: wsl -u root -- bash $0" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# 1. Install openssh-server if missing.
if ! command -v sshd >/dev/null 2>&1; then
    echo "installing openssh-server..."
    apt-get update -qq
    apt-get install -y -qq openssh-server
fi

# 2. Generate host keys if missing (apt should do it; be defensive).
ssh-keygen -A 2>/dev/null

# 3. Configure sshd for password-less testing.
mkdir -p /run/sshd
cat > /etc/ssh/sshd_config.d/99-netcore-lldb-test.conf <<'EOF'
# Test config for netcore-lldb e2e suite. Restrictive: localhost only, keys only.
ListenAddress 127.0.0.1
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
ChallengeResponseAuthentication no
UsePAM no
EOF

# 4. Start sshd in the foreground if it's not already running.
if ! pgrep -x sshd >/dev/null; then
    echo "starting sshd..."
    /usr/sbin/sshd
fi

# 5. Generate a key for the 'cad' user (the WSL default account) and authorize it.
CAD_HOME=/home/cad
if [ -d "$CAD_HOME" ]; then
    sudo -u cad mkdir -p "$CAD_HOME/.ssh"
    sudo -u cad chmod 700 "$CAD_HOME/.ssh"
    if [ ! -f "$CAD_HOME/.ssh/netcore-lldb-test_ed25519" ]; then
        sudo -u cad ssh-keygen -t ed25519 -N '' -f "$CAD_HOME/.ssh/netcore-lldb-test_ed25519" >/dev/null
    fi
    cat "$CAD_HOME/.ssh/netcore-lldb-test_ed25519.pub" >> "$CAD_HOME/.ssh/authorized_keys"
    sort -u "$CAD_HOME/.ssh/authorized_keys" -o "$CAD_HOME/.ssh/authorized_keys"
    chown cad:cad "$CAD_HOME/.ssh/authorized_keys"
    chmod 600 "$CAD_HOME/.ssh/authorized_keys"

    # Set up ~/.ssh/config so `ssh 127.0.0.1` finds the right key without
    # any flags (matches what our client emits — no -i option).
    SSH_CONFIG="$CAD_HOME/.ssh/config"
    if ! grep -q "netcore-lldb-test" "$SSH_CONFIG" 2>/dev/null; then
        cat >> "$SSH_CONFIG" <<EOF

# --- netcore-lldb-test ---
Host 127.0.0.1 localhost
    User cad
    IdentityFile $CAD_HOME/.ssh/netcore-lldb-test_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
# --- end netcore-lldb-test ---
EOF
        chown cad:cad "$SSH_CONFIG"
        chmod 600 "$SSH_CONFIG"
    fi
fi

# 7. Also let root reach cad@127.0.0.1 without specifying -i (the test
#    harness runs as root because docker needs root in WSL, but the SSH
#    target is the cad user).
ROOT_SSH=/root/.ssh
mkdir -p "$ROOT_SSH"
chmod 700 "$ROOT_SSH"
if ! grep -q "netcore-lldb-test" "$ROOT_SSH/config" 2>/dev/null; then
    cat >> "$ROOT_SSH/config" <<EOF

# --- netcore-lldb-test ---
Host 127.0.0.1 localhost
    User cad
    IdentityFile /home/cad/.ssh/netcore-lldb-test_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
# --- end netcore-lldb-test ---
EOF
    chmod 600 "$ROOT_SSH/config"
fi

echo
echo "=== root smoke test ==="
ssh -o BatchMode=yes -o ConnectTimeout=5 127.0.0.1 'echo "as root reaches: $(whoami)@$(hostname)"'

# 6. Smoke-test the SSH path.
echo
echo "=== smoke test ==="
sudo -u cad ssh \
    -i "$CAD_HOME/.ssh/netcore-lldb-test_ed25519" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    cad@127.0.0.1 'echo "ssh ok from $(whoami)@$(hostname)"'
