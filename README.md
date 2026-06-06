# OAuth-fronted MCP server wrapping a shared-key API (Tavily)

A working, deployed reference for the AgentCon HK 2026 thesis: **agents as
delegates**. Wrap an upstream API whose only auth is a *shared* key, and put a
real per-user OAuth 2.1 front door on it with Amazon Cognito — no new service
accounts, no shared identity leaking to clients.

> Companion to the talk *"Empower Team-Wide Vibe Coding with LLM Gateway and
> Security-First MCPs"* and the write-up linked at the bottom.

## The problem this solves

Tavily's own remote MCP server authenticates by putting a single shared API key
in the URL query string:

    https://mcp.tavily.com/mcp/?tavilyApiKey=<SHARED_KEY>

Every engineer shares one identity. No per-user scoping, no per-user audit, and
the key is exposed to every client and sits in URL/proxy logs. The same shape
shows up for any SaaS or internal/legacy API whose only auth is an API key
(shared by nature).

## The architecture

    MCP client
      │  Authorization: Bearer <Cognito access token>   (per-user / per-client)
      ▼
    API Gateway HTTP API  ──  Cognito JWT authorizer (validates sig/iss/aud at the edge)
      ▼
    Lambda (MCP server)   ──  re-checks the `tavily-mcp/search` scope
      │                       logs the caller identity (audit)
      │  shared key fetched server-side, never returned to the client
      ▼
    Tavily Search API

- **Cognito** = OAuth 2.1 authorization server. Two app clients:
  - M2M (`client_credentials`) for backend agents + the curl e2e test.
  - Public (`authorization_code` + PKCE) for the per-user delegation story.
- **API Gateway + JWT authorizer** = authentication at the edge. Invalid/expired/
  forged token → `401` before any of your code runs.
- **Lambda** = authorization + audit. Re-checks the `tavily-mcp/search` scope,
  logs the caller, then calls the upstream with the shared key. The Lambda is the
  **final** enforcement point — anyone who can read the secret or invoke the
  function bypassing the authorizer gets full upstream access.

## Where the shared key lives (pick one)

This reference uses **AWS Secrets Manager**, but for a single static key you have
cheaper options with the same IAM-scoped security boundary:

| Option | Cost | Trade-off |
|---|---|---|
| Encrypted Lambda env var | free | Readable via `lambda:GetFunctionConfiguration`; rotating means redeploy; can leak into IaC state/CI logs. Demo-only. |
| **SSM Parameter Store `SecureString`** | effectively free | KMS-encrypted, IAM-scoped. No built-in rotation / cross-account. **Sweet spot for a static key.** |
| AWS Secrets Manager | ~$0.40/secret/mo | Built-in rotation, resource policies, cross-account. Worth it if the secret rotates or is security-governed. |

Whichever you pick, grant read to **only** the Lambda's execution role.

## Deploy

```bash
sam build
sam deploy --guided   # first time: pick a stack name + region

# seed the shared key (Secrets Manager variant)
aws secretsmanager put-secret-value \
  --secret-id <project>/tavily-api-key \
  --secret-string '{"TAVILY_API_KEY":"tvly-..."}'
```

Get a free Tavily API key at <https://tavily.com> (1,000 credits/month, no card).

## Verify

Both scripts read endpoints and client IDs **live from the CloudFormation stack
outputs** — nothing is hardcoded. Set `AWS_PROFILE` / `AWS_REGION` (and `STACK`
if you changed the name) in your environment.

M2M (fully automated):

```bash
bash scripts/e2e_m2m.sh
```

Per-user (auth code + PKCE — needs a browser for the login step):

```bash
python3 scripts/user_pkce.py            # prints the authorize URL
# log in, copy the ?code=... from the redirect
python3 scripts/user_pkce.py <code>     # exchanges code, calls MCP as that user
```

To create a test user:

```bash
aws cognito-idp admin-create-user --user-pool-id <pool-id> \
  --username you@example.com --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id <pool-id> \
  --username you@example.com --password '<password>' --permanent
```

## What you should see

- `client_credentials` token carries `scope: tavily-mcp/search`, `token_use: access`.
- `tools/list` returns the `tavily_search` tool.
- `tools/call` returns a real Tavily answer + sources — through the wrapper.
- No token / bad token → **HTTP 401** at the edge (before Lambda runs).
- A CloudWatch audit line attributes each call to the caller identity.

## Teardown

```bash
sam delete --stack-name <stack-name> --region <region>
```

## Further reading

- Talk slides: <https://the-quantum-nargle.github.io/agentcon-2026-hk-slides/>
- Write-up: the architecture, cost breakdown, and the "agents as delegates"
  argument in full.

## License

MIT
