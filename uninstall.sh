#!/usr/bin/env bash
# ==============================================================================
# Gemini Web2API Uninstaller
# ==============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}Uninstalling Gemini Web2API generated files...${NC}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "${SCRIPT_DIR}/env_setup.sh"

echo -e "${GREEN}[OK] Cleaned up environment files.${NC}"
echo -e "To completely remove dependencies, you may run: pip uninstall -y -r requirements.txt"
