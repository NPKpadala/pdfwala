#!/bin/bash
# =============================================================================
# PDFWala Enterprise V14.0 — Deploy Script
# =============================================================================
set -euo pipefail

echo "=== PDFWala V14.0 Deploy ==="

# Create host directories (if not using named volumes)
mkdir -p uploads outputs temp
chmod 755 uploads outputs temp

# Pull latest and rebuild
docker compose pull redis nginx || true
docker compose build --no-cache

# Stop old containers gracefully
docker compose down --timeout 30 || true

# Start services
docker compose up -d

echo "Waiting for services to be healthy..."
sleep 10

# Check health
docker compose ps
docker exec pdfwala-app curl -sf http://localhost:5000/health && echo "App healthy" || echo "App not healthy yet"

echo "Deploy complete. Monitor with: docker compose logs -f"
