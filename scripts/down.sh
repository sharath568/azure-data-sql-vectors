#!/usr/bin/env bash
# Delete the whole sqlvec resource group: server, database, Azure OpenAI and
# budget. There is no undo.
set -euo pipefail
RG="${RG:-sqlvec-rg}"
command -v az >/dev/null || { echo "Azure CLI (az) is required" >&2; exit 1; }
echo "Deleting resource group $RG (runs in the background)..."
az group delete --name "$RG" --yes --no-wait
echo "Requested. Check progress with: az group show --name $RG"
