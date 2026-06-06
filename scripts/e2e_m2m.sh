#!/usr/bin/env bash
# End-to-end test: machine-to-machine (client_credentials) flow.
# Proves the OAuth-fronted MCP wrapper works without exposing the upstream key.
#
# Reads everything from the deployed CloudFormation stack — no hardcoded IDs.
# Usage:
#   AWS_PROFILE=your-profile AWS_REGION=us-east-1 bash scripts/e2e_m2m.sh
set -euo pipefail

STACK="${STACK:-tavily-mcp-oauth}"
REGION="${AWS_REGION:-us-east-1}"

echo "Reading stack outputs from '$STACK' ($REGION)…"
get_out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

TOKEN_URL=$(get_out TokenEndpoint)
MCP_URL=$(get_out McpEndpoint)
M2M_ID=$(get_out M2MClientId)
POOL_ID=$(get_out UserPoolId)

# M2M client secret is fetched live (never stored in the repo)
M2M_SECRET=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" --client-id "$M2M_ID" --region "$REGION" \
  --query 'UserPoolClient.ClientSecret' --output text)

echo "1) Fetch a client_credentials access token from Cognito"
TOKEN=$(curl -s -X POST "$TOKEN_URL" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "${M2M_ID}:${M2M_SECRET}" \
  -d "grant_type=client_credentials&scope=tavily-mcp/search" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "   token acquired (${#TOKEN} chars)"

echo "2) tools/list"
curl -s -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool

echo "3) tools/call -> real Tavily search"
curl -s -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"tavily_search","arguments":{"query":"what is Model Context Protocol","max_results":3}}}' \
  | python3 -m json.tool

echo "4) Negative test: no token must be rejected at the edge (expect 401)"
curl -s -o /dev/null -w "   HTTP %{http_code}\n" -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/list"}'
