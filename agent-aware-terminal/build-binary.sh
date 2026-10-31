#!/usr/bin/env bash
# Build the native binary: dist/agent-aware-terminal/agent-aware-terminal
# GTK 3 + VTE terminal, native overlay widgets. No WebKit, no HTML,
# no local web server — backend modules run in-process.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv-build ]; then
  # --system-site-packages: PyGObject (Gtk/Vte typelibs) is a system
  # package on Ubuntu and is what the native window is built on.
  python3 -m venv --system-site-packages .venv-build
  .venv-build/bin/pip install -q pyinstaller requests
fi
.venv-build/bin/pyinstaller --noconfirm --onedir \
  --name agent-aware-terminal --paths . \
  --add-data "demo_project:demo_project" \
  --add-data "desktop/fonts:desktop/fonts" \
  --hidden-import gi --hidden-import gi.repository.Gtk \
  --hidden-import gi.repository.Gdk --hidden-import gi.repository.Vte \
  --hidden-import gi.repository.GdkPixbuf --hidden-import gi.repository.Pango \
  --hidden-import cairo --hidden-import gi._gi_cairo \
  --hidden-import gi.repository.GLib --hidden-import gi.repository.Gio \
  --hidden-import backend.app.muse_adapter \
  --hidden-import backend.app.session_observer \
  --hidden-import backend.app.project_indexer \
  --hidden-import backend.app.content \
  --hidden-import backend.app.meta_client \
  --hidden-import backend.app.liquid_client \
  --hidden-import backend.app.content_engine \
  --collect-all gi desktop/native_terminal.py
rm -rf dist/agent-aware-terminal/content
cp -r content dist/agent-aware-terminal/content
echo "Content packs copied next to the binary (external, hot-swappable)."
echo
echo "Done: dist/agent-aware-terminal/agent-aware-terminal"
echo "Run: MODEL_API_KEY=... ./dist/agent-aware-terminal/agent-aware-terminal"
