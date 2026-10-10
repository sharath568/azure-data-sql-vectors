#!/usr/bin/env bash
# Deploy sqlvec's Azure setup (Azure SQL free offer database) and write .env.azure.
#
# Usage:
#   BUDGET_EMAIL=you@example.com ./scripts/up.sh
#
# Optional environment variables:
#   RG               resource group name            (default: sqlvec-rg)
#   LOCATION         region                         (default: eastus2)
#   CLIENT_IP        IP allowed through firewall    (default: auto-detect)
#   MONTHLY_BUDGET   budget amount                  (default: 5)
#   AUTOPAUSE_DELAY  minutes idle before pausing    (default: 60; 15 burns fewer vCore-seconds)
#   DEPLOY_OPENAI    true/false                     (default: false)
#   OPENAI_LOCATION  region for Azure OpenAI        (default: LOCATION)
#
# The free offer database pauses itself when the monthly allowance is gone,
# so it can't run up a bill. Azure OpenAI (if deployed) is pay per token.
# Delete everything with scripts/down.sh when you're done.
set -euo pipefail

RG="${RG:-sqlvec-rg}"
LOCATION="${LOCATION:-eastus2}"
MONTHLY_BUDGET="${MONTHLY_BUDGET:-5}"
AUTOPAUSE_DELAY="${AUTOPAUSE_DELAY:-60}"
DEPLOY_OPENAI="${DEPLOY_OPENAI:-false}"
OPENAI_LOCATION="${OPENAI_LOCATION:-$LOCATION}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ENV="$ROOT/.env.azure"

command -v az >/dev/null || { echo "Azure CLI (az) is required: https://aka.ms/azcli" >&2; exit 1; }
: "${BUDGET_EMAIL:?set BUDGET_EMAIL to the address that should get budget alerts}"

if [[ -z "${CLIENT_IP:-}" ]]; then
  CLIENT_IP="$(curl -fsS https://api.ipify.org || true)"
  [[ -n "$CLIENT_IP" ]] || { echo "could not detect your IP; set CLIENT_IP" >&2; exit 1; }
fi

# Letters + digits + a fixed suffix that satisfies Azure SQL's complexity rules.
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)Aa1!}"

echo "Resource group : $RG ($LOCATION)"
echo "Database       : GP_S_Gen5_2 serverless, free offer (auto-pause when used up)"
echo "Auto-pause     : after $AUTOPAUSE_DELAY idle minutes"
echo "Client IP      : $CLIENT_IP"
echo "Azure OpenAI   : $DEPLOY_OPENAI"
echo "Budget         : $MONTHLY_BUDGET per month, alerts to $BUDGET_EMAIL"

az group create --name "$RG" --location "$LOCATION" --tags project=sqlvec --output none

OUTPUTS="$(az deployment group create \
  --resource-group "$RG" \
  --name sqlvec \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters \
      clientIp="$CLIENT_IP" \
      adminPassword="$ADMIN_PASSWORD" \
      budgetEmail="$BUDGET_EMAIL" \
      monthlyBudget="$MONTHLY_BUDGET" \
      autoPauseDelayMinutes="$AUTOPAUSE_DELAY" \
      deployOpenAi="$DEPLOY_OPENAI" \
      openAiLocation="$OPENAI_LOCATION" \
  --query properties.outputs --output json)"

out() { echo "$OUTPUTS" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])"; }

FQDN="$(out serverFqdn)"
DB="$(out databaseName)"
CONN="Server=tcp:${FQDN},1433;Database=${DB};UID=sqlvecadmin;PWD=${ADMIN_PASSWORD};Encrypt=yes;TrustServerCertificate=no"

{
  echo "# Written by scripts/up.sh. Contains secrets; do not commit."
  echo "SQLVEC_CONN=$CONN"
  echo "SQLVEC_EMBED_DIMS=1536"
} > "$OUT_ENV"

if [[ "$DEPLOY_OPENAI" == "true" ]]; then
  AOAI_NAME="$(out openAiName)"
  AOAI_ENDPOINT="$(out openAiEndpoint)"
  AOAI_KEY="$(az cognitiveservices account keys list -g "$RG" -n "$AOAI_NAME" --query key1 -o tsv)"
  {
    echo "SQLVEC_EMBED_PROVIDER=azure"
    echo "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT"
    echo "AZURE_OPENAI_API_KEY=$AOAI_KEY"
    echo "AZURE_OPENAI_EMBED_DEPLOYMENT=$(out embeddingDeploymentName)"
  } >> "$OUT_ENV"
fi
chmod 600 "$OUT_ENV"

cat <<MSG

Done. Connection details are in .env.azure (mode 600, git-ignored).

Next:
  cp .env.azure .env
  sqlvec doctor
  sqlvec init
  sqlvec ingest examples/docs
  sqlvec search "how do I keep the database free?"

The first connection after a pause takes a little while (auto-resume).
When you are finished for good:  ./scripts/down.sh
MSG
