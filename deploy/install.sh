#!/usr/bin/env bash
# One-shot installer for the trade-tape collector on a fresh Ubuntu 24.04 VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/UT-Silver/perp-liquidity-tracker/main/deploy/install.sh | sudo bash
#
# It asks for a GitHub fine-grained token (Contents: Read and write, this repo only)
# so the collector can push hourly results. The token is stored in
# /etc/perp-tape/token (mode 600, owned by the service user) and never printed.
# Re-running the script updates the code and restarts the service.
set -euo pipefail

REPO="UT-Silver/perp-liquidity-tracker"
USER_NAME="perptape"
HOME_DIR="/opt/perp-tape"
SRC="$HOME_DIR/repo"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv ca-certificates >/dev/null
timedatectl set-timezone UTC || true

echo "==> service user"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$USER_NAME"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" /etc/perp-tape

if [ ! -s /etc/perp-tape/token ]; then
  if [ -n "${PERP_TAPE_TOKEN:-}" ]; then
    TOKEN="$PERP_TAPE_TOKEN"          # non-interactive (CI test) path
  else
    echo
    echo "Paste the GitHub fine-grained token (Contents: Read and write on $REPO)."
    echo "Input is hidden. Press Enter when done."
    read -rsp "token: " TOKEN < /dev/tty; echo
  fi
  [ -n "$TOKEN" ] || { echo "no token given"; exit 1; }
  umask 077; printf '%s' "$TOKEN" > /etc/perp-tape/token
  chown "$USER_NAME:$USER_NAME" /etc/perp-tape/token; chmod 600 /etc/perp-tape/token
  unset TOKEN
fi

echo "==> code"
# credential helper reads the token file at push time, so it never lands in .git/config
cat > "$HOME_DIR/git-cred.sh" <<'EOF'
#!/bin/sh
echo "username=x-access-token"
echo "password=$(cat /etc/perp-tape/token)"
EOF
chmod 755 "$HOME_DIR/git-cred.sh"
run_as() { sudo -u "$USER_NAME" -H "$@"; }
if [ ! -d "$SRC/.git" ]; then
  run_as git -c credential.helper="!$HOME_DIR/git-cred.sh" clone -q --depth 50 "https://github.com/$REPO.git" "$SRC"
fi
run_as git -C "$SRC" config credential.helper "!$HOME_DIR/git-cred.sh"
run_as git -C "$SRC" config user.name "tape-bot"
run_as git -C "$SRC" config user.email "tape-bot@users.noreply.github.com"
run_as git -C "$SRC" config pull.rebase true
run_as git -C "$SRC" pull -q

echo "==> python env"
[ -d "$HOME_DIR/venv" ] || run_as python3 -m venv "$HOME_DIR/venv"
run_as "$HOME_DIR/venv/bin/pip" install -q --upgrade pip
run_as "$HOME_DIR/venv/bin/pip" install -q "$SRC"

echo "==> verify push access (dry run)"
if ! run_as git -C "$SRC" push --dry-run -q 2>/tmp/perp-push.err; then
  echo "!! push check failed - is the token's 'Contents' permission set to Read and write?"; cat /tmp/perp-push.err; exit 1
fi

echo "==> systemd service"
cat > /etc/systemd/system/perp-tape.service <<EOF
[Unit]
Description=Perp DEX trade-tape collector (Hyperliquid + Lighter)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$SRC
ExecStart=$HOME_DIR/venv/bin/python -m perp_liquidity.tape --data-dir data --push --flush-seconds 600
Restart=always
RestartSec=10
# light sandboxing
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now perp-tape >/dev/null
systemctl restart perp-tape
sleep 8
systemctl --no-pager --lines=8 status perp-tape || true

cat <<EOF

Done. The collector is running and will push hourly results to $REPO.
  logs:     journalctl -u perp-tape -f
  restart:  systemctl restart perp-tape
  update:   re-run this installer
EOF
