#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Record the Agent Forge demo with asciinema
# Outputs: assets/demo.cast (asciinema recording)
#          assets/demo.svg  (SVG animation for README)
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ASSETS_DIR="$ROOT_DIR/assets"
CAST_FILE="$ASSETS_DIR/demo.cast"
SVG_FILE="$ASSETS_DIR/demo.svg"

# ── Preflight checks ───────────────────────────────────────
if ! command -v asciinema &>/dev/null; then
    echo "❌ asciinema not found. Install with: pip install asciinema"
    exit 1
fi

mkdir -p "$ASSETS_DIR"

# ── Record ──────────────────────────────────────────────────
echo "🎥 Recording demo..."
echo "   Output: $CAST_FILE"
echo ""

asciinema rec "$CAST_FILE" \
    --command "bash $SCRIPT_DIR/demo.sh" \
    --title "Agent Forge — Autonomous Coding Agent" \
    --cols 90 \
    --rows 30 \
    --overwrite

echo ""
echo "✅ Recording saved: $CAST_FILE"

# ── Convert to SVG (optional) ──────────────────────────────
if command -v svg-term &>/dev/null; then
    echo "🖼  Converting to SVG..."
    svg-term \
        --in "$CAST_FILE" \
        --out "$SVG_FILE" \
        --window \
        --no-cursor \
        --width 90 \
        --height 30 \
        --padding 18 \
        --term iterm2
    echo "✅ SVG saved: $SVG_FILE"
else
    echo ""
    echo "ℹ️  svg-term not found — skipping SVG conversion."
    echo "   Install with: npm install -g svg-term-cli"
    echo "   Then re-run this script."
    echo ""
    echo "   Alternative: upload $CAST_FILE to https://asciinema.org"
    echo "   and use the embed link in the README."
fi

echo ""
echo "🎬 Done! Next steps:"
echo "   1. Review the recording: asciinema play $CAST_FILE"
echo "   2. Upload (optional):   asciinema upload $CAST_FILE"
echo "   3. Embed in README (see implementation plan)"
