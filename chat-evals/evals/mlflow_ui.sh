#!/bin/bash
# Add a .pth file to the venv that swaps sqlite3 for pysqlite3.
# .pth files with "import" lines run at Python startup for ALL processes.
# Created before mlflow starts, removed on exit so Chainlit is unaffected.
SITE_DIR=$(python -c "import site; print(site.getsitepackages()[0])")
PTH_FILE="$SITE_DIR/pysqlite3_swap.pth"

cat > "$PTH_FILE" <<'EOF'
import pysqlite3; import sys; sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
EOF

echo "Installed pysqlite3 swap at $PTH_FILE"

cleanup() {
    rm -f "$PTH_FILE"
    echo "Removed pysqlite3 swap"
}
trap cleanup EXIT INT TERM

mlflow ui --port 8000
