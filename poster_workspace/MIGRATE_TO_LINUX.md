# Handoff: migrate the poster-tools MCP server to a 24/7 Linux server

**Audience:** a Claude Code agent operating on (or with SSH access to) the target Linux box.
**Goal:** run `poster_tools_server.py` permanently on an Ubuntu/Debian server living on the
user's ESXi host, exposed over a Cloudflare quick tunnel kept alive in a `tmux` session, and
registered as a custom connector in the user's Claude/Cowork account.

This server currently runs on the user's Mac. Nothing about the application logic changes —
only *where* it runs and *how* it's supervised. Read this whole file before acting, then work
top to bottom. Stop and ask the user at the explicit checkpoints (marked **⏸ USER**).

---

## 0. Context you need (don't rediscover this)

The server is a **Streamable-HTTP MCP server** exposing 5 tools backed by Pillow + EasyRouter's
`gpt-image-2`. It was already debugged into a working state on macOS. The three things that make
the tunnel connection succeed — carry them over exactly:

1. **Streamable HTTP transport** (not stdio). Serves on `127.0.0.1:<port>`; the tunnel exposes it.
2. **`MCP_JSON_RESPONSE=1`** — Cloudflare *quick* tunnels don't support SSE; this forces plain
   JSON responses. Keep it on.
3. **Host-header normalization** — the MCP SDK rejects any `Host` that isn't the bound
   `host:port` with **HTTP 421 "Misdirected Request"**, which manifests in Claude as a bogus
   "Couldn't register with sign-in service / add an OAuth Client ID" error. The server already
   contains an ASGI wrapper that rewrites the inbound Host header to `127.0.0.1:<port>` so this
   can't happen. **Do not remove it.** If you ever see `Invalid Host header` + `421` in the log,
   that wrapper isn't running (usually: server not restarted after an edit).

Authentication model: the server is **authless**; access control is an **unguessable secret
URL path** (`MCP_PATH=/mcp-<random hex>`), because Claude's connector UI accepts a full URL but
has no field for an `Authorization` header. Keep this scheme.

Full background lives in `RUNBOOK.md` (esp. the "Why it works" section) and
`tools/CLOUDFLARE_TUNNEL.md` / `tools/CONNECTOR_SETUP.md`. Don't contradict them.

Why quick tunnel (not named): the box is up 24/7, so a quick tunnel's only real weakness — the
URL changing on `cloudflared` restart — rarely bites. In a persistent `tmux` session the URL is
stable until a reboot or a manual kill, with no Cloudflare account or domain required. The
trade-off: after a server reboot you get a new URL and re-paste it into the connector once. The
named-tunnel upgrade (permanent URL) is documented in §9 for when the user wants zero re-pasting.

---

## 1. Prerequisites to install on the Linux server (Ubuntu/Debian)

```bash
sudo apt-get update
sudo apt-get install -y git tmux curl ca-certificates

# uv (manages the PEP 723 inline deps; no system Python pollution)
curl -LsSf https://astral.sh/uv/install.sh | sh
# uv lands in ~/.local/bin — make sure it's on PATH for the service user:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
uv --version    # confirm

# cloudflared (Cloudflare's apt repo)
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
cloudflared --version    # confirm
```

If the apt repo route fails on this distro/arch, fall back to the direct binary from
`https://github.com/cloudflare/cloudflared/releases` (pick the linux build matching the arch:
`amd64` for x86-64 VMs, `arm64` for ARM), then `chmod +x` and move onto PATH.

---

## 2. Get the code onto the server (git clone)

The user will provide a git remote for the `x-poster-workspace` repo (the parent of
`poster_workspace/`).

**⏸ USER — ask for:**
- The git clone URL (and a deploy token / SSH key if the repo is private).
- The directory to clone into (default suggestion: `/opt/poster-tools` or `~/poster-tools`).

```bash
# example; substitute the real remote + path
git clone <REMOTE_URL> ~/poster-tools
cd ~/poster-tools
# the server lives at: poster_workspace/tools/poster_tools_server.py
```

> ⚠️ **Secrets must not be in git.** `poster_workspace/tools/.env` on the Mac contains a live
> EasyRouter key. Before/after cloning, ensure `.env` is gitignored and NOT present in the repo
> history. If the repo was created from the Mac folder, verify:
> ```bash
> git -C ~/poster-tools log --all --full-history -- '*/tools/.env'   # must print nothing
> grep -R "tools/.env" ~/poster-tools/.gitignore || echo "ADD tools/.env TO .gitignore"
> ```
> If `.env` was ever committed, tell the user — the key is compromised and must be rotated.

---

## 3. Create the `.env` on the server with a FRESH key

Per the user's choice, generate a **new** EasyRouter key (rotate; don't reuse the Mac's key).

**⏸ USER — ask the user to:**
1. Create a fresh key in the EasyRouter console.
2. Paste it here, OR place it directly on the server themselves (preferred — avoids the key
   passing through chat). The agent must **not** print the key back.

```bash
cd ~/poster-tools/poster_workspace/tools
cat > .env <<'EOF'
EASYROUTER_API_KEY=sk-REPLACE_ON_SERVER
EASYROUTER_BASE_URL=https://easyrouter.io/v1
IMAGE_MODEL=gpt-image-2
EOF
chmod 600 .env       # owner-only
```

The server auto-loads this file (search order: `$POSTER_ENV_FILE`, `./.env`, `<script dir>/.env`).

---

## 4. Smoke-test the server locally (before any tunnel)

Pick a port (default 8765) and a secret path. Generate the secret path once and reuse it:

```bash
cd ~/poster-tools/poster_workspace/tools
export MCP_PATH="/mcp-$(openssl rand -hex 16)"
echo "SECRET PATH (save this): $MCP_PATH"
export MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT=8765

# First run resolves PEP 723 deps via uv (a few seconds), then serves.
uv run --script poster_tools_server.py
```

In a second shell, confirm health and that the key loaded:

```bash
curl -s http://127.0.0.1:8765/healthz
# expect: {"status":"ok",...,"easyrouter_key_loaded":true,"image_model":"gpt-image-2"}
```

Confirm the MCP endpoint answers an initialize handshake with **HTTP 200** (NOT 421):

```bash
curl -s -o /dev/null -w "MCP -> HTTP %{http_code}\n" -X POST "http://127.0.0.1:8765$MCP_PATH" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

If health is OK and MCP returns 200, the application is good on this box. Stop the foreground
server (Ctrl-C) before setting up supervision.

---

## 5. Supervise the SERVER with systemd (auto-restart, starts on boot)

The server should be a managed service so it survives crashes and reboots. (The *tunnel* is run
in tmux per the user's preference — see §6. The server itself is better under systemd.)

Create `/etc/systemd/system/poster-tools.service` (adjust `User`, paths, and `MCP_PATH` to the
secret generated in §4; keep the same value):

```ini
[Unit]
Description=poster-tools MCP server (Streamable HTTP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/poster-tools/poster_workspace/tools
Environment=PATH=/home/YOUR_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=MCP_TRANSPORT=streamable-http
Environment=MCP_HOST=127.0.0.1
Environment=MCP_PORT=8765
Environment=MCP_PATH=/mcp-REPLACE_WITH_YOUR_SECRET_HEX
Environment=MCP_JSON_RESPONSE=1
# EASYROUTER_API_KEY etc. come from tools/.env automatically.
ExecStart=/home/YOUR_USER/.local/bin/uv run --script /home/YOUR_USER/poster-tools/poster_workspace/tools/poster_tools_server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now poster-tools
systemctl status poster-tools --no-pager
journalctl -u poster-tools -n 30 --no-pager     # look for the startup banner + Host-normalize line
curl -s http://127.0.0.1:8765/healthz            # re-confirm
```

> Note on uv + systemd: `uv run --script` needs `HOME` and a writable cache. Running as a normal
> `User=` (not `nobody`) with the `PATH` above is the simplest correct setup; uv caches under
> that user's `~/.cache/uv`. If you prefer an isolated service account, set `Environment=HOME=...`
> to a dir that user owns.

---

## 6. Run the quick tunnel in a persistent tmux session

```bash
tmux new -s tunnel
# inside tmux:
cloudflared tunnel --url http://127.0.0.1:8765
```

Copy the printed `https://<random>.trycloudflare.com`. **Detach** (don't close) the session:
`Ctrl-b` then `d`. The tunnel keeps running. Reattach anytime with `tmux attach -t tunnel`.

The **full connector URL** = that tunnel URL **+ the secret path from §4**, e.g.
`https://<random>.trycloudflare.com/mcp-<secret-hex>`.

Verify from anywhere:
```bash
curl -s https://<random>.trycloudflare.com/healthz   # same OK JSON
```

> The URL is stable as long as this tmux/cloudflared process lives. It changes on reboot or if
> the session is killed → re-paste into the connector once (see §7). For a URL that never
> changes, do §9 instead.

---

## 7. Register the connector in Claude / Cowork

This is done in the user's Claude account UI, not on the server.

**⏸ USER — instruct the user to:**
1. Settings → Connectors → "+" → "Add custom connector".
2. Paste the **full URL (tunnel + secret path)** from §6. Leave OAuth fields blank (authless).
3. Add, then enable it in the relevant project/conversation.
4. Verify by asking Claude to call `check_easyrouter(probe_endpoints=true)` and confirm:
   - `GET /models 200`, `gpt-image-2 present: True`
   - `/images/edits 200` (with-layout + brief-with-specimen modes work)
   - `/images/generations 200` (brief-only mode works)
   - chat route may report unsupported — expected for gpt-image-2; not used by default.

If Claude shows a "sign-in service / OAuth" error, the Host-header fix isn't active → check
`journalctl -u poster-tools` for `421` / `Invalid Host header` and confirm the service is running
the current code.

---

## 8. The 5 tools (so you can sanity-check the connector surface)

| Tool | Endpoint | Use |
|---|---|---|
| `check_easyrouter` | all | First-run reachability gate. |
| `render_font_specimen` | none (Pillow) | Render the brand font to a specimen PNG. |
| `generate_poster` | /images/edits → /images/generations | With-layout mode (have a layout reference). |
| `generate_poster_from_brief_with_specimen` | /images/edits | Brief + font specimen, no layout. |
| `generate_poster_from_brief` | /images/generations | Brief only (font described in text). |

Relevant env vars: `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_JSON_RESPONSE`(=1),
`MCP_STATELESS_HTTP`(=0), `MCP_ALLOWED_HOSTS`(=`*`), `MCP_AUTH_TOKEN`(unset), `FROM_BRIEF_ROUTE`
(=`images`), `EASYROUTER_API_KEY` (from .env), `IMAGE_MODEL`. See `RUNBOOK.md` for full notes.

---

## 9. OPTIONAL upgrade — permanent URL via a named tunnel as a systemd service

Do this only if the user wants the connector URL to survive reboots with zero re-pasting. Needs a
Cloudflare account and a domain added to it.

```bash
cloudflared tunnel login                      # browser auth (run where a browser is reachable)
cloudflared tunnel create poster-tools        # writes ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns poster-tools poster-mcp.<yourdomain>
```

`~/.cloudflared/config.yml`:
```yaml
tunnel: poster-tools
credentials-file: /home/YOUR_USER/.cloudflared/<UUID>.json
ingress:
  - hostname: poster-mcp.<yourdomain>
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Install as a service so it starts on boot (replaces the tmux step):
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Connector URL becomes `https://poster-mcp.<yourdomain>/mcp-<secret-hex>` — permanent. Optionally
lock the server to that host: set `Environment=MCP_ALLOWED_HOSTS=poster-mcp.<yourdomain>` in the
systemd unit (the Host-rewrite makes this optional, but it's good defense in depth).

---

## 10. Definition of done

- [ ] `systemctl status poster-tools` = active (running); survives `sudo reboot`.
- [ ] `curl http://127.0.0.1:8765/healthz` → `easyrouter_key_loaded:true`.
- [ ] Tunnel up (tmux quick tunnel, or §9 named service); `curl https://<url>/healthz` works.
- [ ] Connector registered in Claude; all 5 tools visible.
- [ ] `check_easyrouter` reports models 200 + `/images/edits` 200 + `/images/generations` 200.
- [ ] `.env` is `chmod 600`, NOT in git; old Mac key rotated.
- [ ] Mac server + tunnel can be shut down — Linux is now the home.

## Gotchas (carried over from the macOS debugging)

- **421 / "sign-in service" error** = Host-header check rejecting the tunnel host. The ASGI
  rewrite in the server prevents it; ensure the running process has the current code.
- **`easyrouter_key_loaded:false`** = `.env` not found/read. Check `WorkingDirectory` and that
  `.env` sits in `tools/`.
- **Quick-tunnel URL changed** = cloudflared restarted (reboot / tmux killed). Re-paste, or use §9.
- **Editing the server code** requires `sudo systemctl restart poster-tools` to take effect.
- **uv first run is slow** (resolves deps); subsequent starts are fast (cached). Don't mistake the
  cold-start delay for a hang.
```
