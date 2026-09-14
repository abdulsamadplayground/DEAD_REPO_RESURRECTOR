#!/usr/bin/env bash
#
# deploy_dashboard.sh — build the React dashboard and publish it to the
# CloudFront-fronted S3 bucket provisioned by template.yaml, wired to the
# stack's live API endpoints.
#
# Prereqs: the SAM stack (STACK_NAME) is already deployed so its outputs exist
# (DashboardSiteBucketName, DashboardDistributionId, DashboardApiUrl,
# LiveApiUrl). Run `sam deploy` first (or use `make deploy-all` semantics).
#
# What it does:
#   1. Reads stack outputs (bucket, distribution id, /state + /live URLs).
#   2. Builds the frontend (Vite).
#   3. Rewrites the built index.html so the dashboard-api / dashboard-live meta
#      tags point at the real API Gateway URLs (no rebuild-per-env needed).
#   4. Syncs dist/ to S3 (long-cache for hashed assets, no-cache for index.html).
#   5. Invalidates the CloudFront distribution.
#   6. Prints the live dashboard URL.
#
# Usage:
#   scripts/deploy_dashboard.sh [STACK_NAME] [AWS_REGION]
# Defaults: STACK_NAME=dead-repo-resurrector  AWS_REGION=us-east-1

set -euo pipefail

STACK_NAME="${1:-${STACK_NAME:-dead-repo-resurrector}}"
AWS_REGION="${2:-${AWS_REGION:-us-east-1}}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
DIST_DIR="$FRONTEND_DIR/dist"

echo "==> Stack: $STACK_NAME  Region: $AWS_REGION"

get_output() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

BUCKET="$(get_output DashboardSiteBucketName)"
DIST_ID="$(get_output DashboardDistributionId)"
STATE_URL="$(get_output DashboardApiUrl)"
LIVE_URL="$(get_output LiveApiUrl)"

if [[ -z "$BUCKET" || "$BUCKET" == "None" ]]; then
  echo "!! Could not read DashboardSiteBucketName from stack '$STACK_NAME'." >&2
  echo "   Deploy the SAM stack first (sam deploy)." >&2
  exit 1
fi

echo "==> Bucket:        $BUCKET"
echo "==> Distribution:  $DIST_ID"
echo "==> /state URL:    $STATE_URL"
echo "==> /live  URL:    $LIVE_URL"

# 1. Build the frontend.
echo "==> Building frontend…"
( cd "$FRONTEND_DIR" && npm run build )

# 2. Inject the real API URLs into the built index.html meta tags.
#    Uses a temp file so it works identically on macOS/BSD and GNU sed.
echo "==> Injecting live API URLs into index.html…"
INDEX="$DIST_DIR/index.html"
python3 - "$INDEX" "$STATE_URL" "$LIVE_URL" <<'PY'
import sys, re
index_path, state_url, live_url = sys.argv[1], sys.argv[2], sys.argv[3]
html = open(index_path, encoding="utf-8").read()
html = re.sub(
    r'(<meta name="dashboard-api" content=")[^"]*(")',
    lambda m: m.group(1) + state_url + m.group(2),
    html,
)
html = re.sub(
    r'(<meta name="dashboard-live" content=")[^"]*(")',
    lambda m: m.group(1) + live_url + m.group(2),
    html,
)
open(index_path, "w", encoding="utf-8").write(html)
print("    done")
PY

# 3. Sync hashed assets with a long cache, then index.html with no-cache so a
#    redeploy is picked up immediately.
echo "==> Syncing to s3://$BUCKET …"
aws s3 sync "$DIST_DIR" "s3://$BUCKET" \
  --region "$AWS_REGION" \
  --delete \
  --exclude "index.html" \
  --cache-control "public,max-age=31536000,immutable"

aws s3 cp "$INDEX" "s3://$BUCKET/index.html" \
  --region "$AWS_REGION" \
  --cache-control "no-cache" \
  --content-type "text/html"

# 4. Invalidate CloudFront so the new index.html is served right away.
if [[ -n "$DIST_ID" && "$DIST_ID" != "None" ]]; then
  echo "==> Invalidating CloudFront $DIST_ID …"
  aws cloudfront create-invalidation \
    --distribution-id "$DIST_ID" \
    --paths "/*" \
    --query "Invalidation.Id" --output text
fi

SITE_URL="$(get_output DashboardSiteUrl)"
echo ""
echo "==> Done. Dashboard: $SITE_URL"
