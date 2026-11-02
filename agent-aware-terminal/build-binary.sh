#!/usr/bin/env bash
# Build the native binary: dist/agent-aware-terminal/agent-aware-terminal
# GTK 3 + VTE terminal, native overlay widgets. No WebKit, no HTML,
# no local web server — backend modules run in-process.
set -euo pipefail
cd "$(dirname "$0")"

# The build venv must come from a Python that can import gi
# (a conda/micromamba python3 first in PATH cannot — same trap
# as run.sh). A stale venv built by such a Python is rebuilt.
SYS_PY="/usr/bin/python3"
"$SYS_PY" -c "import gi" >/dev/null 2>&1 || SYS_PY="python3"
if [ -d .venv-build ] && ! .venv-build/bin/python -c "import gi" >/dev/null 2>&1; then
  echo "build-binary.sh: .venv-build has no gi (built by the wrong Python) — recreating"
  rm -rf .venv-build
fi
if [ ! -d .venv-build ]; then
  # --system-site-packages: PyGObject (Gtk/Vte typelibs) is a system
  # package on Ubuntu and is what the native window is built on.
  "$SYS_PY" -m venv --system-site-packages .venv-build
  .venv-build/bin/pip install -q pyinstaller requests pycairo
fi
# Local embedding runtime (optional at runtime: hashing fallback
# covers its absence, but the binary should carry it).
.venv-build/bin/pip install -q onnxruntime numpy tokenizers 2>/dev/null || true
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
  --hidden-import backend.app.embeddings \
  --hidden-import backend.app.vector_store \
  --hidden-import backend.app.content_selector \
  --hidden-import backend.app.screen_perception \
  --collect-all onnxruntime \
  --hidden-import tokenizers \
  --collect-all tokenizers \
  --collect-all gi desktop/native_terminal.py
rm -rf dist/agent-aware-terminal/content dist/agent-aware-terminal/games-base dist/agent-aware-terminal/content-researcher
cp -r games-base dist/agent-aware-terminal/games-base
cp -r content-researcher dist/agent-aware-terminal/content-researcher
echo "Base game packs copied next to the binary (external, from the games submodule)."
echo
echo "Done: dist/agent-aware-terminal/agent-aware-terminal"
echo "Run: MODEL_API_KEY=... ./dist/agent-aware-terminal/agent-aware-terminal"
