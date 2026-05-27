#!/usr/bin/env bash
set -e

# Glyph — fast incremental codebase knowledge graph indexer
# Installs to ~/.glyph/ with a symlink to ~/.local/bin/glyph

INSTALL_DIR="${HOME}/.glyph"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_DIR="${HOME}/.local/bin"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║     𐂷  Glyph — codebase indexer            ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# Create directories
mkdir -p "${INSTALL_DIR}" "${BIN_DIR}"

# Copy source
cp "$(dirname "$0")/glyph.py" "${INSTALL_DIR}/glyph.py"
echo "  ✓ Copied glyph.py → ${INSTALL_DIR}/"

# Create virtual environment
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    echo "  ✓ Created virtual environment"
fi

# Install dependencies
"${VENV_DIR}/bin/pip" install -q tree-sitter tree-sitter-typescript tree-sitter-python tree-sitter-go tree-sitter-bash
echo "  ✓ Installed tree-sitter + language grammars (TS, Python, Go, Bash)"

# Create executable wrapper
WRAPPER="${BIN_DIR}/glyph"
cat > "${WRAPPER}" << 'EOF'
#!/usr/bin/env bash
exec "${HOME}/.glyph/venv/bin/python3" "${HOME}/.glyph/glyph.py" "$@"
EOF
chmod +x "${WRAPPER}"

# Ensure ~/.local/bin is in PATH
if ! echo "${PATH}" | grep -q "${BIN_DIR}"; then
    echo ""
    echo "  ⚠  Add this to your shell config (~/.bashrc or ~/.zshrc):"
    echo "     export PATH=\"\${HOME}/.local/bin:\${PATH}\""
fi

echo ""
echo "  ✅ Glyph installed!"
echo ""
echo "  Quick start:"
echo "    glyph scan myproject /path/to/project"
echo "    glyph find myproject sendEmail"
echo "    glyph godnodes myproject"
echo "    glyph map myproject          # Generates PROJECT_MAP.md"
echo ""
