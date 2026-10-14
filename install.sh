#!/usr/bin/env bash
# ==============================================================================
# Gemini Web2API One-Click Installer
# ==============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}================================================================${NC}"
echo -e "${BLUE}        Gemini Web2API Bridge - Quick Installer                ${NC}"
echo -e "${BLUE}================================================================${NC}"

# 1. Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERROR] python3 is required but not installed.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo -e "${GREEN}[OK] Detected Python ${PYTHON_VERSION}${NC}"

# 2. Install dependencies
echo -e "${BLUE}[*] Installing required Python dependencies...${NC}"
python3 -m pip install -r requirements.txt

# 3. Create helper environment script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/env_setup.sh"

cat << 'EOF' > "${ENV_FILE}"
# Gemini Web2API Environment Setup
# Source this file before launching your CLI Agent:
#   source env_setup.sh

# Route OpenAI-compatible requests (e.g. Oh My Pi LM Studio / OpenAI provider)
export LM_STUDIO_BASE_URL="http://127.0.0.1:8765/v1"
export OPENAI_BASE_URL="http://127.0.0.1:8765/v1"
export OPENAI_API_KEY="dummy"

# Route Anthropic-compatible requests (e.g. Claude Code / Anthropic provider)
export ANTHROPIC_BASE_URL="http://127.0.0.1:8765"
export ANTHROPIC_API_KEY="dummy"

echo "[Gemini Web2API] Environment loaded. Default model: gemini-4-argon"
EOF

chmod +x "${ENV_FILE}"
echo -e "${GREEN}[OK] Generated environment script: ${ENV_FILE}${NC}"

# 4. Success summary
echo -e "\n${GREEN}================================================================${NC}"
echo -e "${GREEN}  Installation Complete!                                        ${NC}"
echo -e "${GREEN}================================================================${NC}"
echo -e "Next steps:"
echo -e "  1. Start the proxy server:"
echo -e "     ${YELLOW}python3 run_server.py --port 8765${NC}"
echo -e ""
echo -e "  2. Install Tampermonkey script in your browser:"
echo -e "     ${YELLOW}userscript/gemini_bridge.user.js${NC}"
echo -e ""
echo -e "  3. Open Gemini Web (https://gemini.google.com/app) and verify green icon:"
echo -e "     ${GREEN}🟢 Bridge: Connected (Idle)${NC}"
echo -e ""
echo -e "  4. Launch your CLI agent (e.g. Oh My Pi / Claude Code / Cline):"
echo -e "     ${YELLOW}source env_setup.sh${NC}"
echo -e "     ${YELLOW}omp --model lm-studio/gemini-4-argon \"Your prompt here\"${NC}"
echo -e "================================================================\n"
