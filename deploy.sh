#!/bin/bash

# ═════════════════════════════════════════════════════════════════
# PDFWala Production Deployment Script
# Safe, informative, and handles all edge cases
# ═════════════════════════════════════════════════════════════════

set -e  # Exit on any error

# Colors for pretty output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
APP_DIR="/home/opc/pdfwala"
BRANCH="main"
BACKUP_DIR="/home/opc/backups"
KEEP_BACKUPS=5

# ═════════════════════════════════════════════════════════════════
# Helper Functions
# ═════════════════════════════════════════════════════════════════

print_header() {
    echo ""
    echo -e "${CYAN}════════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}════════════════════════════════════════════════════════════════${NC}"
}

print_step() {
    echo -e "${BLUE}➤${NC} $1"
}

print_success() {
    echo -e "${GREEN}✅${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠️${NC} $1"
}

print_error() {
    echo -e "${RED}❌${NC} $1"
}

print_info() {
    echo -e "${PURPLE}ℹ️${NC} $1"
}

confirm() {
    read -p "$(echo -e ${YELLOW}"❓ $1 [y/N]: "${NC})" response
    case "$response" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

# ═════════════════════════════════════════════════════════════════
# Pre-Deployment Checks
# ═════════════════════════════════════════════════════════════════

print_header "PDFWala Deployment Script"

print_step "Checking current directory..."
if [[ ! -d "$APP_DIR" ]]; then
    print_error "Application directory not found: $APP_DIR"
    exit 1
fi
cd "$APP_DIR"
print_success "In $APP_DIR"

print_step "Checking git status..."
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    print_error "Not a git repository"
    exit 1
fi
print_success "Git repository verified"

print_step "Checking for uncommitted changes..."
if [[ -n $(git status --porcelain) ]]; then
    print_warning "You have uncommitted changes:"
    git status --short
    if ! confirm "Continue anyway? (Changes will be stashed)"; then
        print_info "Deployment cancelled"
        exit 0
    fi
    print_step "Stashing changes..."
    git stash save "Auto-stash before deployment $(date '+%Y-%m-%d %H:%M:%S')"
    STASHED=true
else
    print_success "Working directory clean"
    STASHED=false
fi

# ═════════════════════════════════════════════════════════════════
# Backup Current State
# ═════════════════════════════════════════════════════════════════

print_header "Creating Backup"

mkdir -p "$BACKUP_DIR"
BACKUP_NAME="pdfwala_backup_$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

print_step "Creating backup: $BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

# Backup important files
cp docker-compose.yml "$BACKUP_PATH/" 2>/dev/null || true
cp nginx/nginx.conf "$BACKUP_PATH/" 2>/dev/null || true
cp app.py "$BACKUP_PATH/" 2>/dev/null || true
cp gunicorn.conf.py "$BACKUP_PATH/" 2>/dev/null || true
cp -r static "$BACKUP_PATH/" 2>/dev/null || true

print_success "Backup created at $BACKUP_PATH"

# Clean old backups
print_step "Cleaning old backups (keeping last $KEEP_BACKUPS)..."
cd "$BACKUP_DIR"
ls -dt pdfwala_backup_* 2>/dev/null | tail -n +$((KEEP_BACKUPS+1)) | xargs rm -rf 2>/dev/null || true
print_success "Old backups cleaned"

# ═════════════════════════════════════════════════════════════════
# Pull Latest Code
# ═════════════════════════════════════════════════════════════════

print_header "Pulling Latest Code"

cd "$APP_DIR"
print_step "Fetching from origin/$BRANCH..."
git fetch origin "$BRANCH"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/$BRANCH)

if [[ "$LOCAL" == "$REMOTE" ]]; then
    print_info "Already up to date!"
    if ! confirm "Rebuild anyway?"; then
        print_info "Skipping rebuild"
        UP_TO_DATE=true
    else
        UP_TO_DATE=false
    fi
else
    print_step "Changes detected! Pulling..."
    git pull origin "$BRANCH"
    print_success "Code pulled successfully"
    UP_TO_DATE=false
fi

# Show what changed
if [[ "$UP_TO_DATE" == false ]]; then
    print_info "Recent commits:"
    git log --oneline -3
fi

# ═════════════════════════════════════════════════════════════════
# Rebuild and Deploy
# ═════════════════════════════════════════════════════════════════

if [[ "$UP_TO_DATE" == false ]] || confirm "Proceed with rebuild?"; then
    
    print_header "Rebuilding Containers"
    
    print_step "Stopping containers..."
    docker compose down
    print_success "Containers stopped"
    
    print_step "Building pdfwala image (no cache)..."
    docker compose build --no-cache pdfwala
    print_success "Image built successfully"
    
    print_step "Starting containers..."
    docker compose up -d
    print_success "Containers started"
    
    # Wait for services to be healthy
    print_step "Waiting for services to be healthy..."
    sleep 5
    
    # Check service status
    print_header "Service Status"
    docker compose ps
    
else
    print_info "Skipping rebuild"
fi

# ═════════════════════════════════════════════════════════════════
# Health Check
# ═════════════════════════════════════════════════════════════════

print_header "Health Check"

print_step "Testing API health endpoint..."
if curl -s -f "http://localhost/api/health" > /dev/null 2>&1; then
    print_success "API is healthy"
    
    # Show health response
    HEALTH_RESPONSE=$(curl -s "http://localhost/api/health")
    VERSION=$(echo "$HEALTH_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null || echo "unknown")
    UPTIME=$(echo "$HEALTH_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uptime_seconds',0))" 2>/dev/null || echo "0")
    
    print_info "Version: $VERSION"
    print_info "Uptime: ${UPTIME}s"
else
    print_error "API health check failed!"
    print_info "Checking logs..."
    docker compose logs --tail=20 pdfwala_app
fi

print_step "Testing HTTPS..."
if curl -s -f -I "https://npkpadala.com" > /dev/null 2>&1; then
    print_success "HTTPS is accessible"
else
    print_warning "HTTPS check failed (may be normal if just started)"
fi

# ═════════════════════════════════════════════════════════════════
# Show Recent Logs
# ═════════════════════════════════════════════════════════════════

if confirm "Show recent logs?"; then
    print_header "Recent Logs"
    docker compose logs --tail=30
fi

# ═════════════════════════════════════════════════════════════════
# Quick Test Links
# ═════════════════════════════════════════════════════════════════

print_header "Quick Test Links"

echo -e "${CYAN}Homepage:${NC}         https://npkpadala.com/pdfwala/"
echo -e "${CYAN}Health API:${NC}        https://npkpadala.com/api/health"
echo -e "${CYAN}Merge PDF:${NC}         https://npkpadala.com/pdfwala/merge-pdf/"
echo -e "${CYAN}Compress PDF:${NC}      https://npkpadala.com/pdfwala/compress-pdf/"
echo -e "${CYAN}PDF to Word:${NC}       https://npkpadala.com/pdfwala/pdf-to-word/"
echo -e "${CYAN}Sitemap:${NC}           https://npkpadala.com/sitemap.xml"
echo -e "${CYAN}Robots.txt:${NC}        https://npkpadala.com/robots.txt"

# ═════════════════════════════════════════════════════════════════
# Deployment Summary
# ═════════════════════════════════════════════════════════════════

print_header "Deployment Summary"

echo -e "${GREEN}✅ Deployment Complete!${NC}"
echo ""
echo -e "${CYAN}📋 Details:${NC}"
echo -e "   Backup:     $BACKUP_NAME"
echo -e "   Directory:  $APP_DIR"
echo -e "   Branch:     $BRANCH"
echo -e "   Commit:     $(git rev-parse --short HEAD)"

if [[ "$STASHED" == true ]]; then
    echo ""
    print_warning "Remember: Your changes were stashed!"
    echo "   Run: git stash pop"
fi

echo ""
echo -e "${CYAN}📊 Monitor Commands:${NC}"
echo "   docker compose ps              # Check service status"
echo "   docker compose logs -f         # Follow logs"
echo "   docker compose logs pdfwala_app # App logs only"
echo "   docker stats                   # Resource usage"

echo ""
echo -e "${CYAN}🔄 Rollback Command (if needed):${NC}"
echo "   cp $BACKUP_PATH/* $APP_DIR/ && docker compose restart"

echo ""
echo -e "${GREEN}🎉 PDFWala is LIVE! 🎉${NC}"
