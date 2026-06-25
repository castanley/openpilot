#!/usr/bin/env bash
# [MyPilot DIAG] Bring up SSH as early as possible — before the AGNOS update screen and before the
# openpilot manager — so the device is reachable for log inspection at ANY boot stage, even if the
# manager hangs. Every step is non-fatal; this can never block or break boot.

KEYS="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIADjTRuhrJePGo64QLpU5yRalnRx0gDo2bMXTIFiBwbs
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEHASukec8SFiEZBPptaF1zpMjeEpemVAXxCl7xQwrW1
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILjiEh2TKbmhpNx+mwN9WXB62mP1m73qY8u9eus4jqmO"

# comma-native path: AGNOS's sshd authorizes via the GithubSshKeys param.
GITHUB_SSH_KEYS="$KEYS" python3 -c "import os; from openpilot.common.params import Params; p=Params(); p.put_bool('SshEnabled', True); p.put('GithubSshKeys', os.environ['GITHUB_SSH_KEYS']); p.put('GithubUsername', 'castanley')" 2>/dev/null || true

# direct fallback: drop authorized_keys for the comma user and make sure sshd is up.
mkdir -p /home/comma/.ssh 2>/dev/null || true
printf '%s\n' "$KEYS" > /home/comma/.ssh/authorized_keys 2>/dev/null || true
chmod 700 /home/comma/.ssh 2>/dev/null || true
chmod 600 /home/comma/.ssh/authorized_keys 2>/dev/null || true
chown -R comma:comma /home/comma/.ssh 2>/dev/null || true
sudo systemctl start ssh 2>/dev/null || true
sudo systemctl start sshd 2>/dev/null || true

echo "[mypilot] SSH enable hook ran at $(date)"
exit 0
