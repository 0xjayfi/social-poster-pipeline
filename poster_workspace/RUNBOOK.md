# Poster pipeline — runbook (remote MCP over Cloudflare Tunnel)

The single source of truth for running this. Supersedes `HOW_TO_RUN.md` (the old
human-in-the-loop script approach) and the stdio parts of `tools/MCP_REGISTRATION.md`.

## The architecture, in one picture

```
  Cowork (Opus 4.8 agent)
        |  enables the "poster-tools" connector
        v
  Anthropic cloud  --- public HTTPS --->  *.trycloudflare.com  (Cloudflare Tunnel)
                                                  |
                                                  v
                                       your Mac : 127.0.0.1:8765
                                       poster_tools_server.py  (Streamable HTTP)
                                                  |
                                                  v
                                       EasyRouter  /v1  (gpt-image-2)
```

Why this shape:
- Cowork's sandbox can't reach `easyrouter.io` (egress allowlist). The tools run on your
  Mac instead, where it's reachable.
- Claude calls custom connectors **from Anthropic's cloud, not your laptop** — so the
  server needs a public URL. The Cloudflare Tunnel provides exactly that.
- The desktop app only wires in **remote** MCP servers (over HTTP), not local stdio ones.
  So the server speaks Streamable HTTP, configured for plain-JSON responses so it works
  over a quick tunnel (which can't do SSE).

## What's already done (by the rebuild)

- `tools/poster_tools_server.py` converted from stdio to **Streamable HTTP**, verified
  end-to-end in-sandbox (initialize handshake + all four tools listed + a live
  `render_font_specimen` call writing a valid PNG, in stateless + JSON-response mode).
- Quick-tunnel SSE limitation handled (`MCP_JSON_RESPONSE=1`, `MCP_STATELESS_HTTP=1`).
- Secret-path access control + optional bearer token.
- `inputs/` populated: `brand_font.ttf`, `brief.md`, `reference_poster.png`.

## What YOU do, in order

### 0. One-time: install prerequisites on your Mac
```bash
which uv         || curl -LsSf https://astral.sh/uv/install.sh | sh
which cloudflared || brew install cloudflared
```
Confirm `tools/.env` has your `EASYROUTER_API_KEY=sk-...`.

### 1. Start the server — Terminal #1 (leave running)
```bash
cd ~/x-poster-workspace/poster_workspace/tools
export MCP_PATH="/mcp-$(openssl rand -hex 16)"     # your secret path — copy it
echo "SECRET PATH: $MCP_PATH"
export MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT=8765
uv run --script poster_tools_server.py
```
Check: `curl http://127.0.0.1:8765/healthz` → `easyrouter_key_loaded:true`.

### 2. Start the tunnel — Terminal #2 (leave running)
```bash
cloudflared tunnel --url http://127.0.0.1:8765
```
Copy the printed `https://....trycloudflare.com`. Your **full connector URL** is that
plus your secret path. Verify: `curl https://<tunnel>/healthz` returns the OK JSON.
→ Details & the permanent named-tunnel option: `tools/CLOUDFLARE_TUNNEL.md`.

### 3. Register the connector in Claude
Settings → Connectors → "+" → Add custom connector → paste the **full URL (incl. secret
path)** → leave OAuth blank → Add. Enable it in this project via the chat "+" → Connectors.
→ Details & troubleshooting: `tools/CONNECTOR_SETUP.md`.

### 4. Gate check — confirm EasyRouter is reachable through the tunnel
In a Cowork chat here:
> Using poster-tools, call `check_easyrouter` with probe_endpoints true and show the report.

Want: `GET /models 200`, `gpt-image-2 present: True`. That single line proves the whole
chain works. Note whether `/images/edits` exists (decides with-layout vs degraded).

### 5. Run the pipeline once (manual)
Paste the fenced operating prompt from `tools/scheduled_task_prompt.md` into the chat.
The agent runs Steps 0–6 autonomously: picks mode, renders the specimen, drafts a prompt,
generates, self-critiques against the rubric, iterates (cap 6), and finalizes.
Review `outputs/final_poster.png` and `outputs/run_log.md`.

### 6. (Later) Make it unattended
You chose manual-only for now. When ready: stand up the **named tunnel** (permanent URL)
+ run server/cloudflared as background services, then create a Cowork scheduled task with
model `claude-opus-4-8` and the same prompt. See the bottom of
`tools/scheduled_task_prompt.md`. Note: `brief.md`'s KPIs are hardcoded to one week —
update the brief each period or add a data-pull step before scheduling a recurring run.

## The four tools

| Tool | Network | Purpose |
|---|---|---|
| `check_easyrouter(probe_endpoints=true)` | all endpoints | First-run gate. Proves reachability, lists models, probes `/images/edits`, `/images/generations`, and (legacy) chat. Never hard-fails on a missing endpoint. |
| `render_font_specimen(font_path, output_path, size=1024)` | none | Pillow specimen sheet. Tested, works. |
| `generate_poster(prompt, reference_images, output_path, size)` | /v1/images/edits → falls back to /v1/images/generations | **With-layout mode.** Use when you HAVE a layout reference. Sends layout + specimen as image refs. |
| `generate_poster_from_brief_with_specimen(prompt, font_specimen_path, output_path, size)` | /v1/images/edits | **Brief + font, no layout.** Specimen sent as a style-only image; model invents the composition. Best choice when you have a specimen but no layout poster. |
| `generate_poster_from_brief(prompt, font_specimen_path, output_path, size)` | /v1/images/generations | **Brief only.** Text-to-image; specimen NOT sent (font described in words). Use when exact typography isn't critical. `FROM_BRIEF_ROUTE=chat` switches to the legacy chat route, which is **unsupported for gpt-image-2 on EasyRouter**. |

**Which generate tool?** Have a layout reference → `generate_poster`. Brief + font specimen, no
layout → `generate_poster_from_brief_with_specimen`. Brief only → `generate_poster_from_brief`.
Why: only `/images/edits` accepts an image input for `gpt-image-2` on EasyRouter;
`/images/generations` is text-only; chat image output is not supported for `gpt-image-2`
(confirmed against OpenAI's docs — that path exists only for mainline models via the
Responses API, not the image model).

## Server configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | `stdio` or `sse` also accepted; HTTP is the one for tunneling. |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8765` | Local bind. Tunnel points here. |
| `MCP_PATH` | `/mcp` | **Set this to a long random path** — it's the access secret over the tunnel. |
| `MCP_JSON_RESPONSE` | `1` | Keep `1` for quick tunnels (no SSE). `0` re-enables SSE streaming. |
| `MCP_STATELESS_HTTP` | `0` | Normal session mode (default). `1` = stateless; not needed for the quick tunnel. |
| `MCP_ALLOWED_HOSTS` | `*` | Hosts allowed in the Host header. Default `*` = any (the secret path is the real control). The server **also** normalizes the Host header internally, so this rarely needs changing. |
| `MCP_AUTH_TOKEN` | unset | Optional bearer token. **Leave unset for the Cowork connector** (UI can't send the header). |
| `FROM_BRIEF_ROUTE` | `images` | Route for `generate_poster_from_brief`. `images` = `/images/generations` (works). `chat` = legacy `/chat/completions` (**unsupported for gpt-image-2**). |
| `EASYROUTER_API_KEY` | from `tools/.env` | Required for the image tools. Loaded from `tools/.env` automatically. |
| `IMAGE_MODEL` | `gpt-image-2` | Override if `check_easyrouter` shows a different id on your account. |

## When something breaks

- **Connector won't connect / tools missing** → are both Terminals up? Does
  `curl https://<tunnel>/healthz` work? Is the secret path appended to the URL?
- **Worked yesterday, dead today** → quick-tunnel URL changes on restart. Re-paste, or
  use a named tunnel.
- **`check_easyrouter` models line is not 200** → server or tunnel down, or key missing.
- **`gpt-image-2 present: False`** → use the reported image-like id; set `IMAGE_MODEL`.
- **Server log shows `Invalid Host header` + `421 Misdirected Request`** → the Host-header
  normalization isn't active. Make sure you restarted the server after the fix (see "Why it
  works" below). This is the failure that looks like an OAuth/"sign-in service" error in
  Claude.
- **`easyrouter_key_loaded:false` in `/healthz`** → the server didn't read `tools/.env`.
  Confirm the file exists with `EASYROUTER_API_KEY=sk-...` and restart the server.

---

## Why it works — the three changes that made the connection succeed

The server started life speaking **stdio** (local only). Three changes took it to "connects
over a Cloudflare tunnel." Recorded here because the failures along the way were misleading.

**1. Transport: stdio → Streamable HTTP.** Stdio can't be tunneled. The server now serves
over `127.0.0.1:8765` (Streamable HTTP), which `cloudflared` exposes publicly. This is what
makes a remote connector possible at all. Claude calls custom connectors from Anthropic's
cloud — not from your laptop — so a public URL is mandatory even though everything runs
locally.

**2. Quick-tunnel compatibility: JSON responses, not SSE.** A Cloudflare *quick* tunnel does
not support Server-Sent Events, but Streamable HTTP streams responses as SSE by default.
Setting `MCP_JSON_RESPONSE=1` makes responses plain JSON so they survive the tunnel. (This
was the real quick-tunnel requirement — not statelessness, which is why `MCP_STATELESS_HTTP`
now defaults to `0`.)

**3. The actual fix — Host-header normalization (defeats HTTP 421).** This was the cause of
the "Couldn't register with sign-in service / add an OAuth Client ID" error.

   - When Claude calls through the tunnel, the request arrives with
     `Host: <name>.trycloudflare.com`.
   - The MCP SDK has built-in DNS-rebinding protection that validates the Host header against
     the bound address (`127.0.0.1:8765`) and rejects anything else with **HTTP 421
     "Misdirected Request"** — *before* the MCP handshake runs.
   - Claude sees the 421, assumes the server wants OAuth, probes for `/.well-known/oauth-*`
     endpoints (which don't exist on an authless server), and surfaces the sign-in error.
     **The OAuth message was a symptom; the 421 was the disease.**
   - Rewriting the Host header to `127.0.0.1` alone did NOT fix it — the SDK wants the
     **port too**. Even a tunnel-side `--http-host-header 127.0.0.1` still 421'd.
   - Fix: a tiny ASGI wrapper (outermost layer) overwrites the inbound Host header with
     exactly `127.0.0.1:8765` on every request, so the SDK always sees the one host it trusts.
     Verified: compile passed, local handshake returned HTTP 200, and re-adding the connector
     registered all tools. There's also `MCP_ALLOWED_HOSTS` and an SDK `transport_security`
     relaxation as backups, but the ASGI rewrite is what guarantees no 421 regardless of SDK
     version.

**Supporting changes:** the server now auto-loads `EASYROUTER_API_KEY` from `tools/.env`
(it didn't before — an early `/healthz` showed `easyrouter_key_loaded:false`), and a secret
URL path (`MCP_PATH=/mcp-<random>`) is the access control since Claude's connector UI has no
field for an auth header.

**One-line summary:** *stdio → Streamable HTTP, made tunnel-safe with JSON responses, and the
Host header normalized to the bound address so the SDK's 421 host-check can't reject tunneled
requests.*

> Note: any code change (e.g. adding a tool) requires **restarting the server process** — the
> running process holds the old code. Restarting the server keeps the same tunnel URL, so the
> connector stays valid; only restarting `cloudflared` changes the URL.
