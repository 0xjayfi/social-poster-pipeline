# Poster Generation Pipeline — Cowork Handoff

A self-contained brief for picking this work up in a fresh Cowork session driven by Claude Opus 4.8.

## Goal

Generate X-post-suitable poster images from three inputs:

1. A reference poster used as a **layout template** (where things go on the canvas).
2. A path to a desired font (`.ttf` or `.otf`).
3. A free-form prose description of the text and intent for the poster.

Output: one final PNG in `outputs/final_poster.png`, plus a full audit trail of intermediate iterations.

## Design decisions (settled, do not re-litigate)

- **Harness**: Cowork. Claude Opus 4.8 (model ID `claude-opus-4-8`, released May 28, 2026) is the orchestrator. The agentic loop is driven by the harness, not by custom API code. Select this model explicitly when creating the scheduled task in Cowork.
- **Image model**: `gpt-image-2` via EasyRouter (`https://easyrouter.io/v1`). Released April 2026, accepts up to 16 reference images. Two endpoints used depending on mode: `/v1/images/edits` when a layout reference is provided, `/v1/chat/completions` when only the font specimen is available.
- **Reference role**: layout reference is **optional**. The orchestrator decides at runtime which generation path to take based on whether `inputs/reference_poster.png` exists. With layout: edits endpoint, reference treated as layout template. Without layout: chat-completions endpoint, font specimen treated as style-only guidance.
- **Text rendering**: Option B. `gpt-image-2` renders text inline. The font file is handled by pre-rendering it as a visual specimen image, since the model cannot load TTF/OTF files directly.
- **Critique**: self-critique loop. The agent views its own output, scores it against a fixed rubric, and iterates. The rubric branches on whether a layout reference exists.
- **Aspect ratio**: 1:1, 1024x1024 (upgrade to 2048x2048 once verified working).
- **Iteration cap**: 6 attempts maximum. Cap is enforced in the prompt, not the harness.
- **Persistence**: every intermediate is saved (prompt, generated image, critique).

## Workspace layout

Cowork Project root is `poster_workspace/`. Folder access scoped to this directory only.

```
poster_workspace/
  inputs/
    reference_poster.png        OPTIONAL layout template. Presence/absence drives tool choice.
    brand_font.ttf              the desired font (required)
    brief.md                    free-form prose: what the poster should say and feel like (required)
  intermediate/
    font_specimen.png           generated once at start of run
    iter_01_prompt.txt
    iter_01_generated.png
    iter_01_critique.md
    iter_02_prompt.txt
    iter_02_generated.png
    iter_02_critique.md
    ...
  outputs/
    final_poster.png            chosen iteration, copied here
    run_log.md                  summary of all iterations
  tools/
    poster_tools_server.py      local MCP server with PEP 723 inline deps
    .env                        EASYROUTER_API_KEY=sk-...
    # requirements.txt and .venv/ only needed for the pip/venv fallback path
```

## Tool surface

Three tools, exposed via a local MCP server. Critique does not need a tool. Opus 4.8 in Cowork can view PNGs natively.

### Tool 1: `render_font_specimen(font_path, output_path, size=1024)`

Rasterizes the `.ttf`/`.otf` into a PNG showing A-Z, a-z, 0-9, punctuation, and sample phrases at multiple sizes. Used as a visual reference for `gpt-image-2` to imitate typography style. Called once per run regardless of which generation tool the orchestrator picks.

### Tool 2: `generate_poster(prompt, reference_images, output_path, size="1024x1024")`

**Use when a layout reference exists.** Calls `gpt-image-2` via EasyRouter's `/v1/images/edits` endpoint with multipart form upload. Accepts a list of reference image paths (typically layout template + font specimen). The model treats the first reference as the edit target and uses the others as style guidance. Saves the result and returns the absolute path.

### Tool 3: `generate_poster_from_brief(prompt, font_specimen_path, output_path, size="1024x1024")`

**Use when only a font specimen and brief are available, no layout reference.** Calls `gpt-image-2` via EasyRouter's `/v1/chat/completions` endpoint. The font specimen is passed as a multimodal input and interpreted as style-only guidance, not as a canvas to edit. The model invents the layout from the prompt prose. Saves the result and returns the absolute path.

The orchestrator chooses between Tool 2 and Tool 3 at runtime based on whether `inputs/reference_poster.png` exists.

### MCP server skeleton

Save as `tools/poster_tools_server.py`. Register with Cowork as a local MCP server. The PEP 723 metadata block at the top declares dependencies inline so `uv run --script` can reconstruct the environment with no separate `requirements.txt`.

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.0.0",
#     "httpx>=0.27.0",
#     "Pillow>=10.0.0",
# ]
# ///
"""
Local MCP server for the Cowork poster pipeline.
Exposes two tools backed by Pillow and EasyRouter's gpt-image-2.
"""
import os
import base64
from pathlib import Path
from typing import List

import httpx
from PIL import Image, ImageDraw, ImageFont
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("poster-tools")

EASYROUTER_BASE_URL = os.environ.get("EASYROUTER_BASE_URL", "https://easyrouter.io/v1")
EASYROUTER_API_KEY = os.environ["EASYROUTER_API_KEY"]
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")


@mcp.tool()
def render_font_specimen(font_path: str, output_path: str, size: int = 1024) -> str:
    """Render a TTF/OTF font as a specimen sheet PNG for use as a visual reference.

    Args:
        font_path: Absolute path to .ttf or .otf file.
        output_path: Where to save the PNG.
        size: Output canvas size in px (square).

    Returns:
        Absolute path of saved PNG.
    """
    img = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(img)

    samples = [
        ("ABCDEFGHIJKLM", 64),
        ("NOPQRSTUVWXYZ", 64),
        ("abcdefghijklmnop", 56),
        ("qrstuvwxyz", 56),
        ("0123456789 .,!?", 48),
        ("The quick brown fox", 72),
        ("jumps over the lazy dog", 48),
    ]

    y = 40
    for text, font_size in samples:
        font = ImageFont.truetype(font_path, font_size)
        draw.text((40, y), text, fill="black", font=font)
        y += font_size + 24

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG")
    return str(Path(output_path).resolve())


@mcp.tool()
def generate_poster(
    prompt: str,
    reference_images: List[str],
    output_path: str,
    size: str = "1024x1024",
) -> str:
    """Generate a poster via gpt-image-2 using reference images.

    Args:
        prompt: Generation prompt drafted by the orchestrator.
        reference_images: Absolute paths to reference PNGs (layout template + font specimen).
        output_path: Where to save the generated PNG.
        size: e.g. "1024x1024".

    Returns:
        Absolute path of saved PNG.
    """
    url = f"{EASYROUTER_BASE_URL}/images/edits"
    headers = {"Authorization": f"Bearer {EASYROUTER_API_KEY}"}

    file_handles = []
    files = []
    try:
        for ref_path in reference_images:
            fh = open(ref_path, "rb")
            file_handles.append(fh)
            files.append(("image[]", (Path(ref_path).name, fh, "image/png")))

        data = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "n": 1,
        }

        with httpx.Client(timeout=180) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
            resp.raise_for_status()
            payload = resp.json()
    finally:
        for fh in file_handles:
            fh.close()

    item = payload["data"][0]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if "b64_json" in item:
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(item["b64_json"]))
    elif "url" in item:
        with httpx.Client(timeout=60) as client:
            img_resp = client.get(item["url"])
            img_resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(img_resp.content)
    else:
        raise RuntimeError(f"Unexpected image response shape: {item}")

    return str(Path(output_path).resolve())


@mcp.tool()
def generate_poster_from_brief(
    prompt: str,
    font_specimen_path: str,
    output_path: str,
    size: str = "1024x1024",
) -> str:
    """Generate a poster via gpt-image-2 chat completions, using the font specimen
    as a style-only reference. Use when no layout template is available.

    Unlike `generate_poster` (which uses /v1/images/edits and treats the first
    reference as an edit target), this routes through /v1/chat/completions where
    image inputs are interpreted as style guidance and the model invents the
    layout from the prompt prose.

    Args:
        prompt: Generation prompt drafted by the orchestrator.
        font_specimen_path: Absolute path to font specimen PNG (style reference only).
        output_path: Where to save the generated PNG.
        size: e.g. "1024x1024".

    Returns:
        Absolute path of saved PNG.
    """
    url = f"{EASYROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {EASYROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    with open(font_specimen_path, "rb") as f:
        specimen_b64 = base64.b64encode(f.read()).decode("utf-8")

    user_text = (
        f"Generate a {size} poster image.\n\n"
        f"The attached image is a font specimen. Use it as a TYPOGRAPHY STYLE "
        f"REFERENCE ONLY. Do not reproduce the specimen's layout, do not lay text "
        f"out as a font sample sheet. Invent a poster composition that fits the "
        f"brief below and renders any text in the style shown in the specimen.\n\n"
        f"Brief:\n\n{prompt}"
    )

    payload = {
        "model": IMAGE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{specimen_b64}"
                        },
                    },
                ],
            }
        ],
        "modalities": ["image", "text"],
    }

    with httpx.Client(timeout=180) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()

    image_bytes = _extract_image_from_chat_response(body)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_bytes)

    return str(Path(output_path).resolve())


def _extract_image_from_chat_response(body: dict) -> bytes:
    """Extract image bytes from a chat-completions response.

    Image-output via chat-completions is a newer surface and the exact response
    shape varies across providers and gateway versions. This handles the three
    most common shapes; add more as needed.
    """
    message = body["choices"][0]["message"]

    # Shape A: content is a list of typed blocks containing an image block.
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            btype = block.get("type")
            if btype == "image" and "image" in block and "b64_json" in block["image"]:
                return base64.b64decode(block["image"]["b64_json"])
            if btype == "image_url":
                url_field = block.get("image_url", {}).get("url", "")
                if url_field.startswith("data:image/"):
                    return base64.b64decode(url_field.split(",", 1)[1])

    # Shape B: a separate "images" array on the message.
    if "images" in message:
        for img in message["images"]:
            url_field = img.get("image_url", {}).get("url", "")
            if url_field.startswith("data:image/"):
                return base64.b64decode(url_field.split(",", 1)[1])
            if "b64_json" in img:
                return base64.b64decode(img["b64_json"])

    raise RuntimeError(
        "Could not extract image from chat completions response. "
        f"Message keys: {list(message.keys())}. "
        "Add the new shape to _extract_image_from_chat_response."
    )


if __name__ == "__main__":
    mcp.run()
```

`.env`:

```
EASYROUTER_API_KEY=sk-YOUR_KEY_HERE
EASYROUTER_BASE_URL=https://easyrouter.io/v1
IMAGE_MODEL=gpt-image-2
```

## Python environment and dependency isolation

**Use `uv` as the primary dependency manager.** uv is a Rust-built Python package manager that replaces `pip`, `venv`, `pyenv`, `pip-tools`, and `pipx` with a single tool. It is 10x to 100x faster than pip and handles virtual environments automatically.

A common misconception is that uv eliminates virtual environments. It does not. uv still creates and uses venvs under the hood (cached in `~/.cache/uv/`), it just manages them for you so you never run `python -m venv` or `source .venv/bin/activate` manually. That automatic management is the whole point.

For this MCP server, the cleanest pattern is **PEP 723 inline script metadata**. Dependencies are declared as a comment block at the top of `poster_tools_server.py` (see the script above), and uv reconstructs the environment on demand via `uv run --script`. No `requirements.txt`, no project-local `.venv/` to manage.

### Prerequisite: install uv inside the Cowork VM

Run once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
# or
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

uv installs to `~/.local/bin/uv` (or `%USERPROFILE%\.local\bin\uv.exe` on Windows). Verify with `uv --version`.

### Registering the MCP server with uv

Cowork's MCP server config takes a `command` and `args`. Point `command` at `uv` and tell it to run the script. uv reads the PEP 723 header, materializes the environment if needed, and executes.

```json
{
  "mcpServers": {
    "poster-tools": {
      "command": "uv",
      "args": [
        "run",
        "--script",
        "/absolute/path/to/poster_workspace/tools/poster_tools_server.py"
      ],
      "env": {
        "EASYROUTER_API_KEY": "sk-YOUR_KEY_HERE",
        "EASYROUTER_BASE_URL": "https://easyrouter.io/v1",
        "IMAGE_MODEL": "gpt-image-2"
      }
    }
  }
}
```

If `uv` is not on the PATH that Cowork sees when it spawns the MCP server, replace `"command": "uv"` with the absolute path, typically `/Users/<you>/.local/bin/uv` on macOS.

### What happens on first run vs subsequent runs

- **First run**: uv reads the PEP 723 header, resolves the dependency set, downloads wheels (cached globally in `~/.cache/uv/`), creates a venv keyed to that exact set, and runs the script. Cold start adds a few seconds.
- **Subsequent runs**: uv reuses the cached venv. Effectively instant startup.
- **Editing deps**: bump a version in the PEP 723 block, next run automatically resolves to a new or existing cached env. No manual reinstall step.

### Fallback: pip and venv (only if uv is unavailable)

If you cannot install uv inside the Cowork VM, fall back to a manual venv. Add `tools/requirements.txt` with the same three lines that appear in the PEP 723 header:

```
mcp>=1.0.0
httpx>=0.27.0
Pillow>=10.0.0
```

Then:

```bash
cd poster_workspace/tools
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

Update the MCP config `command` to point at the venv's Python directly:

```json
"command": "/absolute/path/to/poster_workspace/tools/.venv/bin/python",
"args": ["/absolute/path/to/poster_workspace/tools/poster_tools_server.py"]
```

### Why not install globally inside the VM

Even though Cowork's VM is sandboxed from your host, multiple MCP servers and plugins inside the VM share a single global Python by default. A global `pip install Pillow==10.0` for this pipeline can break a different MCP server that expects Pillow 11. uv's per-script cached envs solve this without you having to think about it.

## Scheduled-task prompt for Claude Opus 4.8

Paste this into the Cowork scheduled task. The agent uses this as its full operating instruction. No further input required at run time.

```
You are running a poster generation pipeline. Your working folder is `poster_workspace/`.
Three MCP tools are available: `render_font_specimen`, `generate_poster`, and
`generate_poster_from_brief`.

STEP 1 — Read inputs and choose mode.
- Read `inputs/brief.md` to understand what the poster should communicate.
- Check whether `inputs/reference_poster.png` exists.
    * If YES → mode = "with_layout". You will use `generate_poster`.
      View the reference to understand the desired layout, palette, mood, and
      focal hierarchy. Treat it as a layout template, not as content to copy.
    * If NO  → mode = "from_brief". You will use `generate_poster_from_brief`.
      The composition will come from the brief; you will not have a layout anchor.
- Call `render_font_specimen` on `inputs/brand_font.ttf` and save the output to
  `intermediate/font_specimen.png`. View the specimen so you understand the typography.
- Record the chosen mode in `intermediate/mode.txt` for the run log.

STEP 2 — Draft the generation prompt.
- Compose a prompt that:
  * places the text content described in the brief
  * matches the typography style shown in the font specimen
  * (with_layout only) preserves the reference layout: grid, balance, focal
    hierarchy, negative space, palette, and mood
  * (from_brief only) describes the composition explicitly in prose: focal
    hierarchy, palette, mood, where text sits on the canvas, supporting graphics
- Save the prompt to `intermediate/iter_01_prompt.txt`.

STEP 3 — Generate (branch on mode).
- If mode == "with_layout":
    Call `generate_poster` with:
      prompt = (contents of iter_01_prompt.txt)
      reference_images = ["inputs/reference_poster.png",
                          "intermediate/font_specimen.png"]
      output_path = "intermediate/iter_01_generated.png"
      size = "1024x1024"
- If mode == "from_brief":
    Call `generate_poster_from_brief` with:
      prompt = (contents of iter_01_prompt.txt)
      font_specimen_path = "intermediate/font_specimen.png"
      output_path = "intermediate/iter_01_generated.png"
      size = "1024x1024"

STEP 4 — Critique.
- View `intermediate/iter_01_generated.png`.
- Score on this rubric (each 1 to 5). Two dimensions branch on mode:
    text_legibility:    text is readable, correctly spelled, well placed
    typography_match:   font style resembles the specimen
    overall_vibe:       matches the brief's intent
    (with_layout)  layout_fidelity:  matches the reference grid and focal hierarchy
    (with_layout)  brand_palette:    colors match the reference
    (from_brief)   layout_quality:   composition works as a poster on its own merits
    (from_brief)   palette_cohesion: colors are cohesive and fit the brief's intent
- Write the rubric, scores, and revision notes to `intermediate/iter_01_critique.md`.
- Decision rule:
    * If total score >= 22 of 25 AND no individual score < 4, SHIP.
    * Else, revise the prompt and repeat steps 3 and 4 using iter_02_*, iter_03_*, etc.

STEP 5 — Iteration cap.
- Maximum 6 generation attempts. At iteration 6, ship the highest-scoring image
  regardless of whether it crossed the threshold.

STEP 6 — Finalize.
- Copy the chosen image to `outputs/final_poster.png`.
- Write `outputs/run_log.md` summarizing:
    * which mode was used and why
    * which iteration was chosen and why
    * the final scores
    * a one-line summary of each attempted iteration
- Stop.

Do not edit `inputs/`. Do not delete anything in `intermediate/`. The whole history is
preserved for review.
```

## Verify before first run

Five items to confirm in a throwaway test session before running the real pipeline.

1. **EasyRouter routing for `gpt-image-2` edits endpoint.** EasyRouter's public quick-start only documents `/v1/images/generations`. Run `GET /v1/models` and confirm `gpt-image-2` appears, then `POST /v1/images/edits` with a trivial multipart payload to confirm the endpoint is exposed. Required for `generate_poster` (with-layout mode).

2. **EasyRouter routing for `gpt-image-2` chat completions endpoint.** Required for `generate_poster_from_brief` (from-brief mode). Send a minimal `POST /v1/chat/completions` with `model: gpt-image-2`, a text+image_url multimodal user message, and `"modalities": ["image", "text"]`. Confirm the response includes an image (either as a typed content block, an `images` array, or a base64 payload). Note the exact response shape and adjust `_extract_image_from_chat_response` in the MCP server if needed.

3. **`mcp` Python package availability.** The skeleton uses `mcp.server.fastmcp`. Confirm uv can resolve it from PyPI (run `uv run --script tools/poster_tools_server.py --help` once to trigger the install).

4. **Font specimen quality.** Render a specimen from any TTF you have on hand and view it. If text in subsequent generations does not respect the style, increase specimen size variety or add color samples.

5. **Cowork MCP registration.** Confirm the local MCP server is discoverable from inside Cowork's scheduled task (not just from an interactive Cowork session). Scheduled sessions spin up fresh, so the MCP config must be persistent.

## Known issues and mitigations

**Cowork has no audit logging.** Per Anthropic, Cowork activity is not in audit logs, compliance API, or exports. Acceptable for an X content pipeline. Do not extend this pattern to anything regulated.

**The iteration cap is soft.** Cowork will not hard-stop after N tool calls. The cap lives in the prompt. If the agent ever ignores the cap, add a sentinel mechanism: have the agent write `intermediate/STOP` after iteration 6, and have a follow-on bash check refuse to start a 7th.

**Self-critique can spiral.** The fixed rubric in the prompt is the first line of defense. The 6-iteration cap is the second. If you see the agent regressing across iterations (later attempts scoring lower than earlier ones), tighten the decision rule to "ship the best so far if iteration N is worse than iteration N-1."

**Typography fidelity under Option B is approximate.** `gpt-image-2` will get close to your font but will not be pixel-exact. If brand teams reject outputs on typography grounds, swap the `generate_poster` tool for a two-step `generate_background` + `composite_text_pillow` pair. Same harness, same prompt structure, just two tools instead of one. This is Option A from the original design.

**Chat completions image-output response shape is unstable.** The `/v1/chat/completions` surface for `gpt-image-2` image output is newer than the dedicated images endpoints and the response shape varies across providers and gateway versions. `_extract_image_from_chat_response` in the MCP server handles three known shapes (typed content blocks, `image_url` data URIs, and a separate `images` array). If your first chat-completions call raises `RuntimeError`, inspect the printed message keys and add the new shape to the helper. This is also why verification check 2 above is important.

**Layout reference presence is a load-bearing signal.** The orchestrator decides which generation tool to call based on whether `inputs/reference_poster.png` exists. If you ever want both modes available for the same run (e.g. A/B comparison), add an explicit `mode` field to `brief.md` and update the scheduled task prompt to read it instead of doing a file existence check.

## Open TODOs (for the next Cowork session)

- [ ] Decide whether this run is "with_layout" or "from_brief" mode for the first test
- [ ] (with_layout) Drop a real reference poster into `inputs/reference_poster.png`
- [ ] Drop the brand font into `inputs/brand_font.ttf`
- [ ] Write `inputs/brief.md` for the first real run
- [ ] Install `uv` inside the Cowork VM (see "Python environment and dependency isolation" section)
- [ ] Register `tools/poster_tools_server.py` as a local MCP server in Cowork using the `uv run --script` config
- [ ] Run the five verification checks above (especially #2 for chat-completions response shape)
- [ ] Run the pipeline once interactively in each mode and review `outputs/run_log.md`
- [ ] Once stable, attach the scheduled task and set cadence

## Reference: API surfaces

EasyRouter base URL: `https://easyrouter.io/v1`

Endpoints expected:
- `GET /v1/models` — list available models, confirm `gpt-image-2` is there
- `POST /v1/images/edits` — primary path, multipart form with reference images
- `POST /v1/images/generations` — fallback, JSON only, no reference images
- `POST /v1/chat/completions` — alternative path via chat with image inputs

Auth header for all calls: `Authorization: Bearer sk-YOUR_EASYROUTER_KEY`

Orchestrator (Claude Opus 4.8, `claude-opus-4-8`) runs inside Cowork on the user's Anthropic subscription. It does not use EasyRouter. Only the image step does.
