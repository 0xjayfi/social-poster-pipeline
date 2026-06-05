# social-poster-pipeline

An agentic pipeline that generates X-post poster images via EasyRouter's `gpt-image-2`,
exposed to Cowork/Claude as a remote **MCP server** (`poster-tools`).

> ## 🛠️ Migrating this to a Linux server? Start here:
> ### → [`poster_workspace/MIGRATE_TO_LINUX.md`](poster_workspace/MIGRATE_TO_LINUX.md)
>
> That document is the complete, top-to-bottom handoff for moving the MCP server off the
> user's Mac and onto a 24/7 Ubuntu/Debian box (systemd service + Cloudflare tunnel in
> tmux + connector re-registration). Read it in full before acting; it has explicit
> **⏸ USER** checkpoints where you must stop and ask.

## What this repo is

`poster_tools_server.py` is a Streamable-HTTP MCP server. It exposes five tools backed by
Pillow (local font rendering) and EasyRouter's `gpt-image-2` (image generation). Because the
Cowork sandbox is network-walled off from `easyrouter.io`, the server runs on a machine that
*can* reach it and is published to Claude as a remote connector over a Cloudflare tunnel.

- **Current state:** runs on the user's Mac, tunnel exposed manually.
- **Target state:** runs permanently on a Linux server. → see the migration doc above.

## Where things are

| Path | What it is |
|---|---|
| [`poster_workspace/MIGRATE_TO_LINUX.md`](poster_workspace/MIGRATE_TO_LINUX.md) | **The migration task.** Start here for the Linux move. |
| [`poster_workspace/RUNBOOK.md`](poster_workspace/RUNBOOK.md) | Single source of truth for running the current remote-MCP-over-tunnel setup. |
| [`poster_workspace/tools/poster_tools_server.py`](poster_workspace/tools/poster_tools_server.py) | The MCP server itself (PEP 723 inline deps; run with `uv run --script`). |
| [`poster_workspace/tools/CLOUDFLARE_TUNNEL.md`](poster_workspace/tools/CLOUDFLARE_TUNNEL.md) | Step 2 — expose the local server with a Cloudflare tunnel. |
| [`poster_workspace/tools/CONNECTOR_SETUP.md`](poster_workspace/tools/CONNECTOR_SETUP.md) | Step 3 — register the tunneled URL as a custom connector in Claude. |
| [`poster_workspace/tools/scheduled_task_prompt.md`](poster_workspace/tools/scheduled_task_prompt.md) | The agent's operating prompt (manual run now, schedulable later). |
| [`poster_workspace/tools/.env.example`](poster_workspace/tools/.env.example) | Env template. The real `.env` is gitignored — create it on the target box. |
| `poster_workspace/inputs/` | Pipeline inputs: `brief.md` + `brand_font.ttf`. |
| `poster_workspace/intermediate/` | Per-iteration artifacts (prompts, generated PNGs). |
| `poster_workspace/outputs/` | Final chosen poster. |
| [`poster_workspace/poster.py`](poster_workspace/poster.py) | Manual CLI fallback (human-in-the-loop generation, no MCP). |

> **Superseded docs** (kept for history, do not follow): `poster_workspace/HOW_TO_RUN.md`
> and `poster_workspace/tools/MCP_REGISTRATION.md` describe the old stdio / hand-run flow.
> `RUNBOOK.md` and the migration doc replace them.

## The MCP tool surface

Five tools, served over Streamable HTTP at the secret `MCP_PATH`:

| Tool | EasyRouter endpoint | Use |
|---|---|---|
| `check_easyrouter` | all (diagnostic) | First-run reachability + endpoint probe. Run this first. |
| `render_font_specimen` | none (Pillow) | Render the brand font to a specimen PNG. |
| `generate_poster` | `/v1/images/edits` → `/v1/images/generations` fallback | With-layout mode (a layout reference exists). |
| `generate_poster_from_brief_with_specimen` | `/v1/images/edits` | Brief + font specimen, no layout reference. |
| `generate_poster_from_brief` | `/v1/images/generations` | Brief only (typography described in the prompt text). |

## Quick orientation for an agent

The repo root here is the `x-poster-workspace` folder; the pipeline lives under
`poster_workspace/`. Dependencies are declared inline (PEP 723) in the server file and
resolved by `uv` — there is no separate install step beyond having `uv` on PATH. Secrets
live only in `poster_workspace/tools/.env` (gitignored); never print or commit the key.
