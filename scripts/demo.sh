#!/usr/bin/env bash
# End-to-end demo: install, run all three scenarios, verify audit, print metrics.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Installing dependencies"
uv sync --all-extras

echo "==> Running scenarios"
uv run asdlc demo --all

echo "==> Verifying audit chain"
uv run asdlc audit-verify

echo "==> Reliability metrics"
uv run asdlc metrics

echo
echo "Start the dashboard with: make serve"
echo "Then open:              http://localhost:8000"
