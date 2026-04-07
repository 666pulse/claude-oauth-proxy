# 中文

[中文介绍](readmd_zh.md)

# claude-oauth-proxy

Use your Claude Max/Pro subscription's OAuth token to call the Anthropic Messages API directly.

Claude Code authenticates via OAuth tokens (`sk-ant-oat01-*`), but these tokens **don't work** if you just pass them as a regular API key. There are 3 undocumented requirements that must be satisfied. This proxy handles all of them automatically.

## The Problem

You have a Claude Max subscription. Claude Code works fine. You want to use the same subscription quota for your own apps via the Messages API. You grab the OAuth token and try:

```bash
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: sk-ant-oat01-YOUR-TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-20250514","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'
```

**Result: 401 Unauthorized.** Switching to `Authorization: Bearer` gets you past 401, but then you hit 400 errors. The token is valid — Claude Code uses it successfully — but the API rejects your direct calls.

## The 3 Hidden Requirements

After reverse-engineering Claude Code's authentication flow, I found that OAuth tokens require **all 3** of these to be present:

| # | Requirement | Header / Field | Why it exists |
|---|------------|----------------|---------------|
| 1 | **Beta headers** | `anthropic-beta: claude-code-20250219,oauth-2025-04-20,...` | OAuth support is behind a beta flag; the `claude-code` beta enables the CLI-specific auth path |
| 2 | **Identity system prompt** | Body must contain `"You are Claude Code, Anthropic's official CLI for Claude."` | Server-side validation checks that the request originates from Claude Code |
| 3 | **Browser access header** | `anthropic-dangerous-direct-browser-access: true` | Required for OAuth tokens (vs. API keys) to bypass CORS-like restrictions |

**Miss any one → 401 or 400.**

## How I Found This

The journey from "why does my token not work?" to "here are the 3 exact requirements":

1. **`x-api-key` → 401** — OAuth tokens aren't API keys. They need `Authorization: Bearer`.
2. **Bearer token → 401** — Still rejected. Missing the OAuth beta flag.
3. **Added `anthropic-beta: oauth-2025-04-20` → 401** — Need `claude-code-20250219` beta too.
4. **Added both betas → 400** — Authentication passed! But the request body is now rejected.
5. **Injected system prompt → 400** — Still failing. The system prompt must be the **first** entry.
6. **System prompt as first entry → 400** — Used `http.client` for the request; Python's HTTP library buffers SSE streams, breaking streaming responses.
7. **Switched to raw TCP sockets → works for non-streaming** — But streaming still hangs.
8. **Added `anthropic-dangerous-direct-browser-access: true` → streaming works!** — This was the final piece.
9. **Cleaned up and parameterized → `proxy.py`** — Production-ready proxy.

Key insight: you can't discover these requirements from the public API docs. They're enforced server-side specifically for OAuth tokens.

## Quick Start

### 1. Get your OAuth token

Claude Code stores it at `~/.claude/oauthToken` after you authenticate. If you use a tool that stores it elsewhere (like a JSON cache), the proxy supports that too.

### 2. Run the proxy

```bash
git clone https://github.com/lonkyzhang/claude-oauth-proxy.git
cd claude-oauth-proxy
python3 proxy.py
```

That's it. No `pip install`, no dependencies — pure Python stdlib.

### 3. Use the proxy

Point your API calls to `http://127.0.0.1:8080` instead of `https://api.anthropic.com`:

```bash
curl -X POST http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

```bash
curl -X POST http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "max_tokens": 256,
    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    "messages": [{"role": "user", "content": "今天有ai相关的重要新闻么"}]
  }'
```

With the OpenAI-compatible SDKs:

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:8080",
    api_key="unused",  # proxy handles auth
)

message = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=256,
    messages=[{"role": "user", "content": "Hello!"}],
)
print(message.content[0].text)
```

## Options

```
python3 proxy.py [OPTIONS]

  --port, -p PORT          Listen port (default: 8080, env: ANTHROPIC_PROXY_PORT)
  --token-file, -t PATH    OAuth token file (default: ~/.claude/oauthToken, env: ANTHROPIC_OAUTH_TOKEN_FILE)
  --log-file, -l PATH      Optional log file (always logs to stdout)
  --host HOST              Bind host (default: 127.0.0.1)
```

Token file formats supported:
- **Plain text** — file contains just the token string
- **JSON** — `{"claudeAiOauth":{"accessToken":"sk-ant-..."}}` or `{"accessToken":"sk-ant-..."}`

## How It Works

```
Your App                    Proxy (this)                   Anthropic API
   │                           │                               │
   │  POST /v1/messages        │                               │
   │  (no auth needed)         │                               │
   │ ─────────────────────────>│                               │
   │                           │  1. Load OAuth token          │
   │                           │  2. Inject system prompt      │
   │                           │  3. Add beta + browser hdrs   │
   │                           │                               │
   │                           │  POST /v1/messages            │
   │                           │  (all 3 requirements met)     │
   │                           │ ─────────────────────────────>│
   │                           │                               │
   │                           │  SSE stream / JSON response   │
   │                           │<──────────────────────────────│
   │                           │                               │
   │  Raw TCP pipe-through     │                               │
   │<──────────────────────────│                               │
```

The proxy uses **raw TCP sockets** instead of Python's `http.client` or `urllib`. This is intentional — HTTP libraries buffer responses, which breaks SSE streaming. Raw sockets pipe bytes through as they arrive.

## Remote Server Usage (Optional)

If you want to run apps on a remote server but authenticate through your local Claude Max subscription:

```bash
# On your local machine (where Claude Code is authenticated):
python3 proxy.py --port 8080

# From your remote server, create an SSH reverse tunnel:
ssh -R 8080:127.0.0.1:8080 user@your-server

# On the remote server, use the proxy:
curl http://127.0.0.1:8080/v1/messages ...
```

This keeps your OAuth token on your local machine — the remote server never sees it.

## ⚠️ Claude Code 2.x Update (2026-04)

**Starting from Claude Code 2.x, `~/.claude/oauthToken` no longer exists.**

Claude Code 2.x changed its authentication mechanism:
- ❌ **Old (1.x)**: Token stored at `~/.claude/oauthToken` (plaintext file)
- ✅ **New (2.x)**: OAuth session stored internally, no accessible token file

### Getting a Long-Lived Token (Claude Code 2.x)

Use the `setup-token` command to generate a 1-year OAuth token:

```bash
# May require proxy in some regions
HTTPS_PROXY=http://127.0.0.1:7891 claude setup-token
```

This creates a token like:
```
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
```

**Save this token** and use it with the proxy:

1. **Option A**: Put it in a file
   ```bash
   echo "sk-ant-oat01-YOUR-TOKEN-HERE" > ~/.claude/oauthToken
   python3 proxy.py  # Will read from default location
   ```

2. **Option B**: Use environment variable
   ```bash
   export ANTHROPIC_OAUTH_TOKEN="sk-ant-oat01-YOUR-TOKEN-HERE"
   python3 proxy.py --token-file /dev/stdin <<< "$ANTHROPIC_OAUTH_TOKEN"
   ```

### Regional Restrictions & Proxy Setup

Some regions require a proxy to authenticate with Claude:

**Example: VPS in Hong Kong accessing Claude via Singapore proxy**

1. **Setup mihomo (Clash Meta) with Singapore node**
   ```bash
   # Install mihomo
   curl -L https://github.com/MetaCubeX/mihomo/releases/download/v1.18.0/mihomo-linux-amd64 -o /usr/local/bin/mihomo
   chmod +x /usr/local/bin/mihomo

   # Start with your subscription config
   mihomo -d /path/to/config
   ```

2. **Switch to Singapore node via API**
   ```bash
   # Get current proxies
   curl http://127.0.0.1:9090/proxies

   # Switch to Singapore
   curl -X PUT http://127.0.0.1:9090/proxies/%E2%99%BB%EF%B8%8F%20%E6%89%8B%E5%8A%A8%E9%80%89%E6%8B%A9%E8%8A%82%E7%82%B9 \
     -H 'Content-Type: application/json' \
     -d '{"name":"pro-新加坡 01"}'

   # Verify (should show "country": "SG")
   curl -x http://127.0.0.1:7891 https://ipinfo.io/json
   ```

3. **Persist proxy for Claude CLI**
   ```bash
   echo 'export HTTPS_PROXY=http://127.0.0.1:7891' >> ~/.bashrc
   echo 'export HTTP_PROXY=http://127.0.0.1:7891' >> ~/.bashrc
   source ~/.bashrc
   ```

4. **Login and get token**
   ```bash
   # OAuth login (one-time)
   claude login

   # Generate long-lived token
   claude setup-token
   ```

**Common Issues:**
- ❌ **403 Forbidden** → Wrong region, use proxy
- ❌ **OAuth error** → Code expired (60s limit), retry immediately
- ❌ **Invalid API key** → Long-lived token is NOT a standard API key, use with this proxy

## FAQ

**Q: Is this against Anthropic's terms of service?**
A: This uses your own subscription quota through your own authenticated token. You're not bypassing any rate limits or accessing anything you haven't paid for. That said, this relies on undocumented behavior that Anthropic could change at any time.

**Q: Why not just use an API key?**
A: API keys are billed per-token. With Claude Max ($100/month or $200/month), you get a usage quota included in your subscription. This proxy lets you use that quota programmatically.

**Q: Will this break when Anthropic updates their API?**
A: Possibly. The beta headers and authentication requirements are undocumented and could change. If it breaks, check the issues for updates.

**Q: Why raw TCP instead of `requests` or `httpx`?**
A: Python HTTP libraries buffer the response body, which breaks Server-Sent Events (SSE) streaming. With raw sockets, bytes are piped through as they arrive from Anthropic's servers, giving you real-time streaming.

**Q: Can multiple apps share the same proxy?**
A: Yes. The proxy is stateless and handles concurrent connections via threading.

## License

MIT
