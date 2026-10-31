#!/usr/bin/env bash
# Optional: install the built binary into ~/.local/opt and register a
# desktop entry (app menu / launcher icon) for the current user.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="dist/agent-aware-terminal"
DST="$HOME/.local/opt/agent-aware-terminal"
[ -d "$SRC" ] || { echo "First run: ./build-binary.sh"; exit 1; }

mkdir -p "$DST" "$HOME/.local/share/applications" "$HOME/.local/share/icons"
rm -rf "$DST"
cp -r "$SRC" "$DST"
ffmpeg -y -loglevel error -i docs/assets/world.webp \
  -vf scale=256:256 "$HOME/.local/share/icons/agent-aware-terminal.png" \
  2>/dev/null || true

cat > "$HOME/.local/share/applications/agent-aware-terminal.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Agent-Aware Terminal
Comment=A terminal that understands its AI agent (Muse · Muse Spark · Muse Image)
Exec=$DST/agent-aware-terminal
Icon=$HOME/.local/share/icons/agent-aware-terminal.png
Terminal=false
Categories=Development;Utility;TerminalEmulator;
Keywords=terminal;ai;agent;muse;
EOF
echo "Installed: $DST/agent-aware-terminal"
echo "The icon appears in the applications menu (Agent-Aware Terminal)."
