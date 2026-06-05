> ⚠️ SUPERSEDED (2026-05-30). This was the original **human-in-the-loop script** flow
> (`poster.py`, you run each generate step in Terminal). The project now uses an
> **autonomous remote-MCP pipeline** — the agent calls the tools itself over a Cloudflare
> Tunnel. Use **`RUNBOOK.md`** instead. `poster.py` still works as a manual fallback if
> you ever want to drive generation by hand, which is why this is kept.

# How to run the poster pipeline

Runs on **your Mac** (not the Cowork sandbox — that's network-walled off from EasyRouter).
Uses `uv`, so nothing installs into your system Python.

## One-time setup

1. Confirm `uv` is installed:
   ```
   which uv      # prints a path? you're set.
   # if not found:
   curl -LsSf https://astral.sh/uv/install.sh | sh   # then open a new terminal tab
   ```
2. The API key is already in `tools/.env`, and `poster.py` loads it automatically.
   **No `export` needed, ever.** (Rotate the key after testing; update `tools/.env`.)

## Put your inputs in `inputs/` (exact names)

```
inputs/brand_font.ttf        your font (.ttf or .otf — if .otf, use that extension)
inputs/brief.md              prose: what the poster says + how it should feel
inputs/reference_poster.png  OPTIONAL layout reference
```

Including `reference_poster.png` selects **with-layout** mode (`/v1/images/edits`,
preserves the reference's layout). Omitting it selects **from-brief** mode (model
invents the composition).

## The commands

Every command is prefixed with `uv run --with httpx --with Pillow`. Run from
`~/x-poster-workspace/poster_workspace`.

```
# 0. confirm connectivity (prints your models; expect: gpt-image-2 present: True)
uv run --with httpx --with Pillow python3 poster.py check

# 1. render the font specimen (once)
uv run --with httpx --with Pillow python3 poster.py specimen --font inputs/brand_font.ttf

# 2a. WITH layout reference — Claude gives you the prompt file each iteration:
uv run --with httpx --with Pillow python3 poster.py edits \
    --prompt-file intermediate/iter_01_prompt.txt \
    --reference inputs/reference_poster.png \
    --reference intermediate/font_specimen.png

# 2b. WITHOUT layout reference:
uv run --with httpx --with Pillow python3 poster.py generate \
    --prompt-file intermediate/iter_01_prompt.txt \
    --specimen intermediate/font_specimen.png \
    --route images          # or: --route chat

# 3. finalize the iteration you and Claude pick:
uv run --with httpx --with Pillow python3 poster.py finalize --iter 03
```

Add `--dry-run` to any `edits`/`generate` command to print the request without
sending it or writing files.

## The critique loop (human-in-the-loop)

1. You run a generate step → it writes `intermediate/iter_NN_generated.png`.
2. In the Cowork chat, ask Claude to view that PNG and score it on the rubric.
3. Claude writes a revised prompt to the next `iter_NN_prompt.txt` (or hands it to you).
4. You run the next generate step. Repeat (cap: 6 iterations).
5. Run `finalize --iter NN` on the winner → `outputs/final_poster.png`.

## Notes

- New terminal tab later? Just `cd` in and run — the key loads from `.env`.
- Behind a corporate VPN/proxy? The script bypasses proxy env vars by default. If you
  actually need the proxy, set `EASYROUTER_USE_PROXY=1` in `tools/.env`.
- Iterations are auto-numbered; pass `--iter NN` to force a specific number.
- Nothing in `intermediate/` is deleted — the full history is preserved.
```
