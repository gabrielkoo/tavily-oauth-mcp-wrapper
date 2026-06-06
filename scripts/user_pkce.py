#!/usr/bin/env python3
"""
Per-user (Authorization Code + PKCE) flow helper.

This is the heart of the "agents as delegates" story: a HUMAN logs in, and the
resulting token is bound to THEIR identity -- not a shared service account.

All config is read live from the deployed CloudFormation stack -- nothing is
hardcoded. Set STACK / AWS_REGION (and AWS_PROFILE) in your environment.

Usage:
    python3 scripts/user_pkce.py
        Prints the authorize URL. Open it, log in, and Cognito redirects to the
        callback with ?code=...

    python3 scripts/user_pkce.py <code>
        Exchanges the code for an access token, decodes the scope/username
        claims, and calls the MCP server as that user.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request

STACK = os.environ.get("STACK", "tavily-mcp-oauth")
REGION = os.environ.get("AWS_REGION", "us-east-1")
CALLBACK = os.environ.get("CALLBACK_URL", "http://localhost:8765/callback")
VERIFIER_FILE = "/tmp/pkce_verifier.txt"


def _stack_output(key):
    out = subprocess.check_output([
        "aws", "cloudformation", "describe-stacks", "--stack-name", STACK,
        "--region", REGION,
        "--query", f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue",
        "--output", "text",
    ], text=True).strip()
    if not out:
        sys.exit(f"Stack output {key} not found — is '{STACK}' deployed in {REGION}?")
    return out


# Resolve endpoints + client id from the live stack
AUTHORIZE = _stack_output("AuthorizeEndpoint")
TOKEN = _stack_output("TokenEndpoint")
USER_CLIENT_ID = _stack_output("UserClientId")
MCP_URL = _stack_output("McpEndpoint")


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def step1():
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    with open(VERIFIER_FILE, "w") as f:
        f.write(verifier)
    params = {
        "client_id": USER_CLIENT_ID,
        "response_type": "code",
        "scope": "openid tavily-mcp/search",
        "redirect_uri": CALLBACK,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    print("Open this URL, log in, then copy the ?code=... from the redirect:\n")
    print(f"{AUTHORIZE}?{urllib.parse.urlencode(params)}\n")
    print(f"Then run: python3 {sys.argv[0]} <code>")


def step2(code):
    verifier = open(VERIFIER_FILE).read().strip()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": USER_CLIENT_ID,
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        TOKEN, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    tok = json.loads(urllib.request.urlopen(req).read())["access_token"]
    claims = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
    print("token_use:", claims.get("token_use"),
          "| username:", claims.get("username"),
          "| scope:", claims.get("scope"))

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "tavily_search",
                   "arguments": {"query": "latest AWS Lambda features", "max_results": 3}},
    }).encode()
    req = urllib.request.Request(
        MCP_URL, data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    print(resp["result"]["content"][0]["text"][:600])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        step2(sys.argv[1])
    else:
        step1()
