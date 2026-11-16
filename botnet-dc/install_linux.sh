#!/bin/bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/DevAgent"

if command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" != "0" ]; then
    echo "Este instalador precisa de root (autostart de maquina, so admin remove)."
    echo "Rode: sudo ./install_linux.sh"
    exit 1
  fi
  TARGET_USER="${SUDO_USER:-$(whoami)}"
  TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
  cat > /etc/systemd/system/devagent.service << EOF
[Unit]
Description=DevAgent - Gerenciamento remoto de dispositivos
After=network-online.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
ExecStart=$BIN
Environment=DISPLAY=:0
Environment=XAUTHORITY=$TARGET_HOME/.Xauthority
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now devagent.service
  echo "Agente instalado como servico de sistema (roda como $TARGET_USER) e iniciado."
  echo "Somente root desabilita: systemctl disable --now devagent.service"
  echo "Sem icone de bandeja; logs: journalctl -u devagent -f"
else
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/devagent.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DevAgent
Exec=$BIN
X-GNOME-Autostart-enabled=true
EOF
  echo "ATENCAO: sem systemd — autostart por sessao grafica (removivel pelo usuario)."
  echo "        Para autostart protegido, instale em uma maquina com systemd e rode com sudo."
  nohup "$BIN" >/dev/null 2>&1 &
fi
