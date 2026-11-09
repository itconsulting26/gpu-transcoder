#!/usr/bin/env bash
# Bootstrap du MCP d'administration sur le serveur GPU (Ubuntu 20.04, root).
# Usage : placer bootstrap.sh et mcp_admin.py dans le même dossier, puis :
#   chmod +x bootstrap.sh && ./bootstrap.sh
set -euo pipefail

PORT="${MCP_PORT:-8765}"
DIR=/opt/mcp-admin
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Paquets de base"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq software-properties-common curl rsync ca-certificates gnupg >/dev/null

PY=""
echo "==> Tentative Python 3.11 via PPA deadsnakes"
if add-apt-repository -y ppa:deadsnakes/ppa && apt-get update -qq \
   && apt-get install -y -qq python3.11 python3.11-venv; then
  PY=python3.11
else
  echo "    PPA indisponible -> installation de Python 3.11 via uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv python install 3.11
  PY="uv"
fi

echo "==> Venv + dépendances"
mkdir -p "$DIR"
if [ ! -d "$DIR/venv" ]; then
  if [ "$PY" = "uv" ]; then
    uv venv --python 3.11 --seed "$DIR/venv"
  else
    python3.11 -m venv "$DIR/venv"
  fi
fi
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q "mcp>=1.10" uvicorn starlette

echo "==> Token + fichier d'environnement"
if [ ! -f "$DIR/env" ]; then
  TOKEN=$(head -c 64 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 48)
  printf 'MCP_TOKEN=%s\nMCP_PORT=%s\n' "$TOKEN" "$PORT" > "$DIR/env"
  chmod 600 "$DIR/env"
fi
# shellcheck disable=SC1090
source "$DIR/env"

cp "$HERE/mcp_admin.py" "$DIR/mcp_admin.py"
chmod 700 "$DIR/mcp_admin.py"

echo "==> Service systemd"
cat > /etc/systemd/system/mcp-admin.service <<EOF
[Unit]
Description=MCP admin (GPU transcodage)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=$DIR/env
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/mcp_admin.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mcp-admin.service
sleep 2
systemctl --no-pager --lines=5 status mcp-admin.service || true

IP=$(curl -4 -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')
echo
echo "================================================================"
echo " MCP prêt. URL à ajouter comme connecteur dans claude.ai :"
echo
echo "   http://$IP:$MCP_PORT/$MCP_TOKEN/mcp"
echo
echo " Pare-feu : si ufw est actif ->  ufw allow $MCP_PORT/tcp"
echo " Logs     :  journalctl -u mcp-admin -f"
echo "================================================================"