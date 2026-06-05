# Step 3 — Register the tunneled server as a custom connector in Claude

This connects the public tunnel URL (from Step 2) to Claude so the `poster-tools` tools
appear inside your Cowork project.

## Before you start, have these ready

- Your **full connector URL** = tunnel base URL + secret path, e.g.
  `https://random-words-here.trycloudflare.com/mcp-9f3a7c1b2d4e6f80`
- The server (Terminal #1) and the tunnel (Terminal #2) both **running**.
- Confirmed `curl https://<tunnel>/healthz` returns the OK JSON.

## Key facts about how Claude connects (so nothing surprises you)

- Claude reaches your server **from Anthropic's cloud**, not from your Mac. That's why
  the public tunnel is required even though Cowork runs locally.
- The connector is **authless** from Claude's side — the secret in your URL path is what
  protects it. Claude's connector UI has fields for the URL and (optionally) OAuth
  client id/secret only; there is **no field for a custom `Authorization` header**. So
  leave `MCP_AUTH_TOKEN` unset and rely on the secret path. (Anthropic's docs confirm
  authless remote servers are supported.)
- Remote MCP connectors are added in **Settings → Connectors**. (The desktop app will
  *not* pick up a remote server from `claude_desktop_config.json` — that file is only for
  local stdio servers, which aren't available in Cowork anyway.)

## Add the connector (Pro / Max individual plan)

1. Open **Settings → Connectors** (in the Claude desktop app, or at
   claude.ai → Customize → Connectors — same account, same list).
2. Click **"+"**, then **"Add custom connector."**
3. Paste your **full connector URL** (tunnel base **+ secret path**) into the URL field.
4. Leave **Advanced settings** (OAuth Client ID / Secret) **empty** — this server is
   authless; the path is the secret.
5. Click **Add**.

Claude will attempt an MCP `initialize` handshake against the URL. If the URL is right
and both Terminal windows are up, the connector saves and shows `poster-tools`.

### If you're on a Team or Enterprise plan

Only an Owner can add org connectors: **Organization settings → Connectors → Add →
Custom → Web → paste URL → Add**. Then each member enables it under
**Customize → Connectors → Connect**. For a personal pipeline, a Pro/Max individual
plan is simpler.

## Enable it in your conversation / project

Per-conversation, connectors are toggled on via the **"+"** button at the bottom-left of
the chat → **Connectors** → switch on `poster-tools`. Do this in the Cowork project where
you'll run the pipeline (and the scheduled task will have it available too).

## Verify the tools are live

In a normal Cowork chat in this project, ask:

> Using the poster-tools connector, call `check_easyrouter` with probe_endpoints true and show me the report.

What you want to see in the report:

1. `GET /models -> HTTP 200` and **`'gpt-image-2' present: True`** — this is the
   make-or-break line. It proves the server (running on your Mac) reaches EasyRouter.
   Because the call now originates from your Mac via the tunnel — not from the Cowork
   sandbox — the old `blocked-by-allowlist` wall does not apply.
2. `POST /images/edits -> HTTP <2xx>` → with-layout mode is fully available.
   If it's `404/405`, with-layout mode falls back to `/images/generations` (no layout
   reference) — the scheduled-task prompt already handles this "degraded" case.
3. `POST /chat/completions (image out) -> HTTP 2xx` and **"Image extracted successfully"**
   → from-brief mode works. If it 200s but extraction fails, paste the printed
   `message keys` / `top-level keys` back to me and I'll extend the extractor.

If `check_easyrouter` itself doesn't appear as a callable tool, the connector didn't
attach — recheck the URL (including the secret path) and that both Terminals are running.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Connector won't save / "couldn't connect" | tunnel or server down, or wrong path | Re-run the `curl https://<tunnel>/healthz`; confirm full URL incl. secret path |
| Tools missing after save | URL points at base, not the secret MCP path | Append your `MCP_PATH` to the tunnel URL |
| Worked yesterday, dead today | quick-tunnel URL changed on restart | Re-paste the new URL, or switch to a named tunnel (CLOUDFLARE_TUNNEL.md §2.4) |
| `check_easyrouter` shows models 200 but `gpt-image-2 present: False` | model id differs on your account | Read the "image-like model ids" line in the report; set `IMAGE_MODEL` to the real id and restart the server |
| 401 on every MCP call | `MCP_AUTH_TOKEN` is set but UI can't send the header | Unset `MCP_AUTH_TOKEN` and restart; rely on the secret path |

Next: run the pipeline once interactively, then attach the schedule — see `RUNBOOK.md`.
