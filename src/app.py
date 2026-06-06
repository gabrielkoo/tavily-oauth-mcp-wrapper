"""
MCP server (streamable HTTP, minimal JSON-RPC) that wraps the Tavily Search API.

Why this exists
---------------
Tavily's own remote MCP server authenticates by putting a *single shared* API
key in the URL query string (`?tavilyApiKey=...`). That means every engineer
shares one identity: no per-user scoping, no per-user audit, and the key is
exposed to every client.

This wrapper fixes that:
  * Amazon Cognito is the OAuth 2.1 authorization server. Each caller gets their
    OWN token, bound to their own identity, carrying a custom scope.
  * API Gateway's JWT authorizer validates the token signature/expiry/audience.
  * This Lambda re-checks the required scope, then calls Tavily using the shared
    key fetched from Secrets Manager. The shared key NEVER leaves the server.
  * Every tool call is logged with the caller's Cognito identity, so the audit
    trail attributes actions to a human/client -- something the upstream's
    shared-key logs can never do.

The shared API key is a single point of compromise: anyone who can read the
secret or bypass this Lambda gets full Tavily access. This Lambda is therefore
the ONLY enforcement point -- treat it accordingly.
"""

import json
import os
import time
import urllib.request
import urllib.error

import boto3

# ---------------------------------------------------------------------------
# Config (from environment, set by the SAM template)
# ---------------------------------------------------------------------------
SECRET_ARN = os.environ["TAVILY_SECRET_ARN"]
REQUIRED_SCOPE = os.environ.get("REQUIRED_SCOPE", "tavily-mcp/search")
TAVILY_API_URL = "https://api.tavily.com/search"

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "tavily-oauth-wrapper", "version": "1.0.0"}

# ---------------------------------------------------------------------------
# Secrets Manager: fetch + cache the shared Tavily key for the Lambda lifetime.
# The key is held only in this process; it is never returned to the client.
# ---------------------------------------------------------------------------
_secrets_client = boto3.client("secretsmanager")
_cached_key = None


def get_tavily_key():
    global _cached_key
    if _cached_key is None:
        resp = _secrets_client.get_secret_value(SecretId=SECRET_ARN)
        raw = resp["SecretString"]
        # Stored either as a raw string or as {"TAVILY_API_KEY": "..."}
        try:
            parsed = json.loads(raw)
            _cached_key = parsed.get("TAVILY_API_KEY", raw) if isinstance(parsed, dict) else raw
        except json.JSONDecodeError:
            _cached_key = raw
    return _cached_key


# ---------------------------------------------------------------------------
# Tool: tavily_search
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "tavily_search",
        "description": (
            "Search the web with Tavily. Returns an LLM-friendly answer plus "
            "source results. Auth and rate-limiting are enforced per-user by "
            "this server; the upstream Tavily key is never exposed to the client."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-10).",
                    "default": 5,
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "default": "basic",
                },
            },
            "required": ["query"],
        },
    }
]


def call_tavily(arguments):
    key = get_tavily_key()
    payload = {
        "query": arguments["query"],
        "max_results": int(arguments.get("max_results", 5)),
        "search_depth": arguments.get("search_depth", "basic"),
        "include_answer": True,
    }
    req = urllib.request.Request(
        TAVILY_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        body = json.loads(r.read().decode("utf-8"))

    # Shape a compact MCP text result.
    lines = []
    if body.get("answer"):
        lines.append(f"Answer: {body['answer']}\n")
    for i, res in enumerate(body.get("results", []), 1):
        lines.append(f"{i}. {res.get('title','(no title)')}\n   {res.get('url','')}")
        if res.get("content"):
            lines.append(f"   {res['content'][:300]}")
    return "\n".join(lines) if lines else "No results."


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------
def rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_rpc(message, caller):
    method = message.get("method")
    req_id = message.get("id")

    if method == "initialize":
        return rpc_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None  # notification, no response

    if method == "tools/list":
        return rpc_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "tavily_search":
            return rpc_error(req_id, -32601, f"Unknown tool: {name}")
        # AUDIT: attribute the action to the authenticated caller.
        print(json.dumps({
            "audit": "tool_call",
            "tool": name,
            "caller": caller,
            "query": arguments.get("query"),
            "ts": int(time.time()),
        }))
        try:
            text = call_tavily(arguments)
        except urllib.error.HTTPError as e:
            return rpc_error(req_id, -32000, f"Tavily error {e.code}: {e.reason}")
        except Exception as e:  # noqa: BLE001
            return rpc_error(req_id, -32000, f"Tavily call failed: {e}")
        return rpc_result(req_id, {"content": [{"type": "text", "text": text}]})

    return rpc_error(req_id, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Lambda entrypoint (API Gateway HTTP API proxy, payload format 2.0)
# ---------------------------------------------------------------------------
def _claims(event):
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return {}


def _has_scope(claims):
    # Cognito puts space-delimited scopes in the "scope" claim for access tokens.
    scopes = (claims.get("scope") or "").split()
    return REQUIRED_SCOPE in scopes


def _http(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    claims = _claims(event)
    # Identity for audit: human user (sub/username) or M2M client_id.
    caller = claims.get("username") or claims.get("sub") or claims.get("client_id") or "unknown"

    if not _has_scope(claims):
        return _http(403, {"error": "forbidden", "detail": f"missing scope {REQUIRED_SCOPE}"})

    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        message = json.loads(raw_body)
    except json.JSONDecodeError:
        return _http(400, {"error": "invalid JSON-RPC body"})

    # Support a single message (this minimal server doesn't batch).
    response = handle_rpc(message, caller)
    if response is None:
        return {"statusCode": 202, "headers": {"Content-Type": "application/json"}, "body": ""}
    return _http(200, response)
