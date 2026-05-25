#!/usr/bin/env bash
set -e

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
error() { echo -e "${RED}[error]${NC} $*"; exit 1; }

# ── Detect OS — Windows (Git Bash/MSYS) uses Scripts/, Unix uses bin/ ─────────
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    VENV_ACTIVATE=".venv/Scripts/activate"
    ACTIVATE_HINT="source .venv/Scripts/activate"
else
    VENV_ACTIVATE=".venv/bin/activate"
    ACTIVATE_HINT="source .venv/bin/activate"
fi

# ── Prereq checks ─────────────────────────────────────────────────────────────
command -v python3 &>/dev/null || command -v python &>/dev/null || error "Python not found. Install Python 3.11+."
command -v node   &>/dev/null || error "node not found. Install Node.js 18+."
command -v npm    &>/dev/null || error "npm not found."

# Use whichever python command exists
PYTHON=$(command -v python3 2>/dev/null || command -v python)
PYTHON_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PYTHON_VER detected"

# ── Backend ───────────────────────────────────────────────────────────────────
info "Setting up backend..."
cd backend

if [ ! -d ".venv" ]; then
    "$PYTHON" -m venv .venv
    info "Virtual environment created at backend/.venv"
fi

# shellcheck disable=SC1090
source "$VENV_ACTIVATE"

pip install --quiet --upgrade pip 2>/dev/null || true   # non-fatal on Windows
pip install --quiet litellm
pip install --quiet -r requirements.txt
info "Backend dependencies installed."

# ── .env ──────────────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cat > .env <<'EOF'
# Required — get a free key at console.groq.com
GROQ_API_KEY=

# Required — get a free key at openrouter.ai/keys
OPENROUTER_API_KEY=
EOF
    warn ".env created — fill in your API keys before running the backend."
else
    info ".env already exists, skipping."
fi

deactivate
cd ..

# ── Frontend ──────────────────────────────────────────────────────────────────
info "Setting up frontend..."
cd frontend
npm install --silent
info "Frontend dependencies installed."
cd ..

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Setup complete.${NC}"
echo ""
echo "  Start backend:   cd backend && $ACTIVATE_HINT && uvicorn main:app --reload"
echo "  Start frontend:  cd frontend && npm run dev"
echo ""
warn "Make sure backend/.env has your GROQ_API_KEY and OPENROUTER_API_KEY set."
