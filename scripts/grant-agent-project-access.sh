#!/usr/bin/env sh
set -eu

foundry_user_role_id='53ca6127-db72-4b80-b1b0-d745d6d5456d'
project_id="${AZURE_AI_PROJECT_ID:-}"

principal_ids="$({
    env | awk -F= '/^AGENT_.*_INSTANCE_IDENTITY_PRINCIPAL_ID=/ && length($2) > 0 { print $2 }'
} | sort -u)"
principal_count="$(printf '%s\n' "$principal_ids" | awk 'NF { count++ } END { print count + 0 }')"

if [ "$principal_count" -ne 1 ]; then
    echo "Expected one deployed agent identity, but found $principal_count." >&2
    exit 1
fi
if [ -z "$project_id" ]; then
    echo 'AZURE_AI_PROJECT_ID is unavailable. Run this script through the azd postdeploy hook.' >&2
    exit 1
fi

existing_count="$(az role assignment list \
    --role "$foundry_user_role_id" \
    --scope "$project_id" \
    --query "[?principalId=='$principal_ids'] | length(@)" \
    --output tsv \
    --only-show-errors)"

if [ "$existing_count" -gt 0 ]; then
    echo 'Agent identity already has Foundry User at the project scope.'
    exit 0
fi

az role assignment create \
    --assignee-object-id "$principal_ids" \
    --assignee-principal-type ServicePrincipal \
    --role "$foundry_user_role_id" \
    --scope "$project_id" \
    --output none \
    --only-show-errors

echo 'Assigned Foundry User to the agent identity at the project scope.'