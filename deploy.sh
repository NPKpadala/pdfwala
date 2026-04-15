#!/bin/bash
# ============================================================================
# PDFWala V10.0 - Production Deployment Script
# ============================================================================

set -e  # Exit on any error

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║              PDFWala V10.0 - Production Deploy                   ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ----------------------------------------------------------------------------
# 1. PRE-FLIGHT CHECKS
# ----------------------------------------------------------------------------
echo -e "${BLUE}[1/6]${NC} Running pre-flight checks..."

# Check if .env exists
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠${NC}  .env file not found. Creating from .env.example..."
    cp .env.example .env
    echo -e "${RED}❗${NC} Please edit .env file with your actual values!"
    echo -e "${RED}❗${NC} Especially: SECRET_KEY, SIGNED_URL_SECRET, API_KEY"
    exit 1
fi

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}✗${NC} Docker not installed. Please install Docker first."
    exit 1
fi

# Check Docker Compose
if ! docker compose version &> /dev/null; then
    echo -e "${RED}✗${NC} Docker Compose not available."
    exit 1
fi

echo -e "${GREEN}✓${NC} Pre-flight checks passed."

# ----------------------------------------------------------------------------
# 2. CREATE DIRECTORIES
# ----------------------------------------------------------------------------
echo -e "${BLUE}[2/6]${NC} Creating data directories..."

mkdir -p uploads outputs temp
chmod 755 uploads outputs temp

echo -e "${GREEN}✓${NC} Directories created."

# ----------------------------------------------------------------------------
# 3. PULL LATEST CODE (if in git repo)
# ----------------------------------------------------------------------------
echo -e "${BLUE}[3/6]${NC} Checking for updates..."

if [ -d .git ]; then
    echo "Pulling latest code from git..."
    git pull origin main 2>/dev/null || echo -e "${YELLOW}⚠${NC}  Could not pull (no remote or no changes)"
else
    echo -e "${YELLOW}⚠${NC}  Not a git repository. Skipping pull."
fi

echo -e "${GREEN}✓${NC} Code ready."

# ----------------------------------------------------------------------------
# 4. STOP RUNNING CONTAINERS
# ----------------------------------------------------------------------------
echo -e "${BLUE}[4/6]${NC} Stopping existing containers..."

docker compose down 2>/dev/null || true

echo -e "${GREEN}✓${NC} Containers stopped."

# ----------------------------------------------------------------------------
# 5. BUILD AND START
# ----------------------------------------------------------------------------
echo -e "${BLUE}[5/6]${NC} Building and starting containers..."

docker compose build --no-cache
docker compose up -d

echo -e "${GREEN}✓${NC} Containers started."

# ----------------------------------------------------------------------------
# 6. HEALTH CHECK
# ----------------------------------------------------------------------------
echo -e "${BLUE}[6/6]${NC} Running health checks..."

# Wait for services to start
echo "Waiting for services to be ready..."
sleep 5

# Check each service
SERVICES=("app" "worker-fast" "worker-office" "worker-slow" "redis")
ALL_HEALTHY=true

for SERVICE in "${SERVICES[@]}"; do
    STATUS=$(docker compose ps --format json 2>/dev/null | grep "\"Name\":\"pdfwala-${SERVICE}\"" | grep -o '"Health":"[^"]*"' | cut -d'"' -f4)
    if [ "$STATUS" = "healthy" ] || [ "$SERVICE" = "worker-fast" -o "$SERVICE" = "worker-office" -o "$SERVICE" = "worker-slow" ]; then
        echo -e "${GREEN}✓${NC} $SERVICE is running"
    else
        echo -e "${RED}✗${NC} $SERVICE may have issues"
        ALL_HEALTHY=false
    fi
done

# Test API
echo ""
echo "Testing API health endpoint..."
HEALTH_RESPONSE=$(curl -s http://localhost:5000/health 2>/dev/null || echo "failed")

if echo "$HEALTH_RESPONSE" | grep -q '"success":true'; then
    echo -e "${GREEN}✓${NC} API is healthy"
    echo "Response: $HEALTH_RESPONSE" | head -c 200
    echo ""
else
    echo -e "${RED}✗${NC} API health check failed"
    echo "Response: $HEALTH_RESPONSE"
    ALL_HEALTHY=false
fi

# ----------------------------------------------------------------------------
# SUMMARY
# ----------------------------------------------------------------------------
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
if [ "$ALL_HEALTHY" = true ]; then
    echo -e "║              ${GREEN}✓ DEPLOYMENT SUCCESSFUL!${NC}                              ║"
else
    echo -e "║              ${YELLOW}⚠ DEPLOYMENT COMPLETED WITH WARNINGS${NC}                   ║"
fi
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║                                                                  ║"
echo "║  📍 API Endpoint:    http://localhost:5000                       ║"
echo "║  ❤️  Health Check:    curl http://localhost:5000/health           ║"
echo "║  📋 View Logs:       docker compose logs -f [service]            ║"
echo "║  🛑 Stop Services:   docker compose down                         ║"
echo "║  🔄 Restart:         ./deploy.sh                                 ║"
echo "║                                                                  ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Show logs hint
echo -e "${BLUE}Tip:${NC} Run '${YELLOW}docker compose logs -f app${NC}' to watch logs"
echo ""
