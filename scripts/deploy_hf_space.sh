#!/usr/bin/env bash
# Assemble + push the FastAPI backend to a Hugging Face Space (Docker SDK).
# Usage: HF_TOKEN=hf_xxx HF_USER=harsh29sit SPACE=recon-control-api ./scripts/deploy_hf_space.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HF_TOKEN="${HF_TOKEN:?set HF_TOKEN}"
HF_USER="${HF_USER:?set HF_USER}"
SPACE="${SPACE:-recon-control-api}"
REPO="spaces/$HF_USER/$SPACE"

STAGE="$ROOT/.hf-stage"
rm -rf "$STAGE" && mkdir -p "$STAGE"

# Space payload: backend code + data + Dockerfile + README front-matter
cp -R "$ROOT/backend" "$STAGE/backend"
rm -rf "$STAGE/backend/tests" "$STAGE/backend"/__pycache__ "$STAGE/backend"/.pytest_cache
cp "$ROOT/scripts/hf_space/Dockerfile" "$STAGE/Dockerfile"
cp "$ROOT/scripts/hf_space/README.md" "$STAGE/README.md"
echo ".hf-stage/" >> "$ROOT/.gitignore" 2>/dev/null || true

cd "$STAGE"
git init -q
git config user.email "deploy@local" && git config user.name "deploy"
git add -A && git commit -qm "deploy: recon control api"
git remote add space "https://$HF_USER:$HF_TOKEN@huggingface.co/$REPO"
git push -q --force space main
echo "✓ pushed to https://huggingface.co/$REPO"
echo "  Runtime URL once built: https://$HF_USER-$SPACE.hf.space"
echo "  Now set Space secrets: MONGO_URL, JWT_SECRET, COOKIE_SECURE=true,"
echo "  CORS_ORIGINS=https://recon-control-tower.vercel.app, DB_NAME, CUSTOM_LLM_API_KEY"
