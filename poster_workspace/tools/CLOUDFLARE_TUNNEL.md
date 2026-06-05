# Step 2 — Expose the local MCP server with a Cloudflare Tunnel (plain-text walkthrough)

You asked me to illustrate this step in plain text. Here it is, end to end. No prior
Cloudflare knowledge assumed.

## What this step accomplishes

Your MCP server (`poster_tools_server.py`) listens only on `127.0.0.1` — your own Mac,
not the internet. But Claude's custom connectors **call your server from Anthropic's
cloud servers, not from your laptop** (confirmed in Anthropic's docs: "Claude connects
to your remote MCP server from Anthropic's cloud infrastructure, rather than from your
local device"). So Anthropic's machines need a public address that forwards to your
local port.

`cloudflared` (Cloudflare's tunnel client) solves exactly this: it dials out from your
Mac to Cloudflare and gets back a public `https://<random>.trycloudflare.com` URL.
Anything hitting that URL is piped down the tunnel to `http://127.0.0.1:8765` on your
machine. No router config, no firewall holes, no static IP, no Cloudflare account.

```
[Anthropic cloud]  -- https -->  [*.trycloudflare.com]  ==tunnel==>  [your Mac :8765]  -->  poster_tools_server.py
                                                                                              |
                                                                                              +--> EasyRouter (gpt-image-2)
```

## Why a "quick tunnel" and the one limitation that matters

A **quick tunnel** is the zero-setup mode: one command, throwaway URL, no login. Perfect
for getting this working. Its documented limits: ~200 concurrent requests, and **it does
NOT support Server-Sent Events (SSE)**.

That SSE limitation would normally break a Streamable-HTTP MCP server, because such
servers stream responses as SSE by default. **I already handled this**: the server is
configured with `MCP_JSON_RESPONSE=1` (plain-JSON responses, no SSE) and
`MCP_STATELESS_HTTP=1` (no long-lived session connection). So it works over a quick
tunnel. You don't have to do anything for this — just don't set those two env vars to `0`.

A quick tunnel's URL **changes every time you restart `cloudflared`**. That's fine for
testing, but it means each restart you must re-paste the new URL into the Claude
connector. The "named tunnel" section at the bottom gives you a permanent URL if you
want to leave this running long-term (recommended once it works).

---

## 2.1 — Install cloudflared (one time)

On macOS with Homebrew:

```bash
brew install cloudflared
```

No Homebrew? Download the binary directly from the official releases
(https://github.com/cloudflare/cloudflared/releases) — pick the `darwin` build matching
your chip (`arm64` for Apple Silicon, `amd64` for Intel), then move it onto your PATH.

Verify:

```bash
cloudflared --version
```

You should see a version string. If "command not found", open a fresh Terminal tab so
the new PATH is picked up.

---

## 2.2 — Start the MCP server (Terminal window #1)

Leave this running. From `~/x-poster-workspace/poster_workspace/tools`:

```bash
cd ~/x-poster-workspace/poster_workspace/tools

# Pick a long, unguessable path. This path is your access control (see SECURITY below).
# Generate one:
export MCP_PATH="/mcp-$(openssl rand -hex 16)"
echo "Your secret MCP path is: $MCP_PATH    <-- write this down"

export MCP_TRANSPORT=streamable-http
export MCP_HOST=127.0.0.1
export MCP_PORT=8765
# Your EasyRouter key is already in tools/.env and is loaded automatically — but if you
# prefer, you can also export it here. The server reads EASYROUTER_API_KEY from the env.

uv run --script poster_tools_server.py
```

You'll see a banner ending with the local URL. Confirm it's alive from another tab:

```bash
curl http://127.0.0.1:8765/healthz
# -> {"status":"ok", ... "easyrouter_key_loaded":true, ...}
```

If `easyrouter_key_loaded` is `false`, the key isn't being read — check `tools/.env`
contains `EASYROUTER_API_KEY=sk-...` (the server loads `.env` from the tools folder).

> Keep the `$MCP_PATH` value handy — you'll append it to the tunnel URL in step 3.

---

## 2.3 — Start the tunnel (Terminal window #2)

Open a **second** Terminal window (leave the server running in the first). Point the
tunnel at the same local port:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

After a second or two, cloudflared prints a box like this:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://random-words-here.trycloudflare.com                                               |
+--------------------------------------------------------------------------------------------+
```

**That `https://....trycloudflare.com` is your public base URL.** Copy it.

Sanity-check the tunnel end to end (from any tab) — the health route is public, so this
should return the same JSON as the local curl did:

```bash
curl https://random-words-here.trycloudflare.com/healthz
```

If that JSON comes back, the public internet can now reach your server. ✅

> Your full connector URL is the tunnel URL **plus your secret path**, e.g.
> `https://random-words-here.trycloudflare.com/mcp-9f3a7c1b...`
> (Do **not** put the secret path on the `/healthz` check above — health is meant to be
> open. The secret path is only for the MCP endpoint itself.)

---

## SECURITY — read this before leaving the tunnel up

A quick-tunnel URL is on the public internet. If someone learns the **full** URL
(tunnel + path), they can call your image tools and spend your EasyRouter credits.
Two layers protect you; use at least the first:

1. **Secret path (primary, always on).** Because Claude's connector UI accepts a full
   URL but has **no field for a custom `Authorization` header**, the practical secret is
   the URL path. The `MCP_PATH=/mcp-<random hex>` you set in 2.2 means the endpoint isn't
   at a guessable location. Requests to any other path (including plain `/mcp`) get a 404.
   Treat the full URL like a password: don't paste it in public.

2. **Bearer token (optional, defense in depth).** The server also supports
   `MCP_AUTH_TOKEN`. If set, every request to the MCP path must carry
   `Authorization: Bearer <token>`. The Claude **connector UI can't send that header**,
   so leave `MCP_AUTH_TOKEN` **unset** for the Cowork connector flow. It's there for the
   case where you drive the server from Claude Code (`claude mcp add --header ...`) or
   front it with your own proxy. For this project: rely on the secret path.

Either way: **rotate your EasyRouter key after testing** and keep an eye on usage. When
you're done for the session, Ctrl-C both Terminal windows — the tunnel dies and the URL
stops working immediately.

---

## OPTIONAL 2.4 — A permanent URL (named tunnel)

A quick tunnel's URL changes on every restart, so a scheduled task that runs daily would
break whenever you restart cloudflared. If you want this to run unattended on a schedule,
switch to a **named tunnel**, which gives a stable hostname on a domain you control in
Cloudflare. This needs a (free) Cloudflare account and a domain added to it.

One-time setup:

```bash
# 1. Log in (opens a browser to authorize cloudflared with your Cloudflare account)
cloudflared tunnel login

# 2. Create a named tunnel (stores a credentials file under ~/.cloudflared/)
cloudflared tunnel create poster-tools

# 3. Route a hostname on your domain to it (replace with a subdomain you own)
cloudflared tunnel route dns poster-tools poster-mcp.yourdomain.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: poster-tools
credentials-file: /Users/jiesong/.cloudflared/<TUNNEL-UUID>.json   # path printed by `create`
ingress:
  - hostname: poster-mcp.yourdomain.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Run it (this is the long-lived command — it replaces the quick-tunnel command in 2.3):

```bash
cloudflared tunnel run poster-tools
```

Now your stable connector URL is `https://poster-mcp.yourdomain.com<your-secret-path>`,
and it survives restarts. You can even install it as a background service with
`cloudflared service install` so it starts on boot — at which point the only thing you
need running for a scheduled job is the Python server itself.

---

## Quick reference

| | Quick tunnel | Named tunnel |
|---|---|---|
| Cloudflare account | not needed | needed (free) |
| Own a domain | no | yes |
| URL stability | changes each restart | permanent |
| Command | `cloudflared tunnel --url http://127.0.0.1:8765` | `cloudflared tunnel run poster-tools` |
| Good for | first-time testing | unattended scheduled runs |

Next: register the URL in Claude as a custom connector — see `CONNECTOR_SETUP.md`.
