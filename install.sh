#!/usr/bin/env bash
# install.sh — symlinks slowdash-agent-tools into the slowdash tree.
# Run once after cloning or pulling the submodule.
# Usage:  bash install.sh   (from any directory)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SLOWDASH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SITE_DIR="$SLOWDASH_DIR/app/site"
LIB_DIR="$SLOWDASH_DIR/lib/slowpy/slowpy"

if [ ! -d "$SITE_DIR" ]; then
    echo "ERROR: cannot find slowdash site directory at: $SITE_DIR"
    echo "Make sure this submodule lives directly under the slowdash repo root."
    exit 1
fi
if [ ! -d "$LIB_DIR" ]; then
    echo "ERROR: cannot find slowpy library at: $LIB_DIR"
    exit 1
fi

echo "Installing slowdash-agent-tools into: $SLOWDASH_DIR"

# Front-end entry page
ln -sf "$SCRIPT_DIR/site/slowagent.html" "$SITE_DIR/slowagent.html"
echo "  Linked slowagent.html"

# Front-end JS/CSS bundle
ln -sf "$SCRIPT_DIR/site/slowagent" "$SITE_DIR/slowagent"
echo "  Linked slowagent/ directory"

# Python library — sit alongside slowpy so `import slowagent` works
# without touching PYTHONPATH or sys.path.
ln -sf "$SCRIPT_DIR/lib/slowagent" "$LIB_DIR/../slowagent"
echo "  Linked lib/slowpy/slowagent/ -> slowdash-agent-tools/lib/slowagent"

echo ""
echo "Done. Restart slowdash and navigate to /slowagent.html"
echo ""
echo "slowagent uses the headless 'claude' CLI for vision/OCR.  If you don't"
echo "have it installed yet, see https://claude.com/claude-code, then run:"
echo "  claude auth login"
