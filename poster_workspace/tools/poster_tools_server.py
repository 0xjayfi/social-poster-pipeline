# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp>=1.10.0",
#     "httpx>=0.27.0",
#     "Pillow>=10.0.0",
#     "uvicorn>=0.30.0",
#     "starlette>=0.40.0",
# ]
# ///
"""
Remote-capable MCP server for the Cowork poster pipeline.

Exposes four tools:
  - render_font_specimen        (Pillow only, no network)
  - generate_poster             (EasyRouter /v1/images/edits, with-layout mode)
  - generate_poster_from_brief  (EasyRouter /v1/chat/completions, from-brief mode)
  - check_easyrouter            (diagnostic: proves network reachability + endpoint shapes)

DEPLOYMENT MODEL (this is the important part):
  This server runs on YOUR Mac, where EasyRouter is reachable. The Cowork sandbox
  cannot reach easyrouter.io (egress allowlist: X-Proxy-Error: blocked-by-allowlist),
  and Cowork's desktop app connects to REMOTE MCP servers over HTTP, not local stdio
  ones. So the design is:

      [your Mac] poster_tools_server.py  --(Streamable HTTP on 127.0.0.1:PORT)-->
      [cloudflared] quick tunnel  --(public HTTPS)-->  [Cowork] remote MCP connector

  Transport is Streamable HTTP (the MCP endpoint is served at MCP_PATH, default /mcp).
  Set MCP_TRANSPORT=stdio to fall back to a local stdio server for quick local tests.

SECURITY:
  A Cloudflare quick tunnel URL is public. Set MCP_AUTH_TOKEN to require an
  `Authorization: Bearer <token>` header on every request to MCP_PATH; without the
  right token the EasyRouter-backed tools cannot be invoked and your API key is safe.
  If MCP_AUTH_TOKEN is unset the server runs open (fine for a 5-minute local test,
  not for a tunnel you leave up).

EasyRouter facts confirmed from docs (2026-05):
  - Base URL is https://easyrouter.io ; OpenAI-compatible SDK base is .../v1
  - Auth: `Authorization: Bearer sk-...`
  - Documented image endpoint: POST /v1/images/generations
  - Documented chat endpoint:  POST /v1/chat/completions
  - /v1/images/edits is NOT in EasyRouter's published capability table. with-layout
    mode depends on it; check_easyrouter probes it so we learn at runtime whether
    it exists, and generate_poster falls back to /images/generations if it 404s.
"""
import os
import io
import sys
import base64
import json
import secrets
from pathlib import Path
from typing import List, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont
from mcp.server.fastmcp import FastMCP


# ---- .env loader (so EASYROUTER_API_KEY loads without a manual export) ------
def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env file into os.environ if not already set.

    Real environment variables ALWAYS win (an explicit `export` overrides the file).
    Search order, first hit wins: $POSTER_ENV_FILE, ./.env (cwd), <this dir>/.env.
    This is why `healthz` can report easyrouter_key_loaded:true even if you launched
    the server without exporting the key yourself.
    """
    here = Path(__file__).resolve().parent
    candidates = [os.environ.get("POSTER_ENV_FILE"), Path.cwd() / ".env", here / ".env"]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.is_file():
            continue
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:  # real env wins
                os.environ[key] = val
        break  # only load the first .env found


_load_dotenv()

# ---- transport / network config (read before constructing the server) ------
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http").strip()
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1").strip()
MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))
# MCP_PATH doubles as a secret: a long unguessable path (e.g. /mcp-9f3a7c...) is the
# practical access control over a Cloudflare quick tunnel, because Claude's connector
# UI accepts a full URL but has no field for a custom Authorization header.
MCP_PATH = os.environ.get("MCP_PATH", "/mcp").strip() or "/mcp"
if not MCP_PATH.startswith("/"):
    MCP_PATH = "/" + MCP_PATH
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

# Hosts the server will accept in the HTTP Host header. The MCP SDK rejects unknown
# hosts with HTTP 421 "Misdirected Request" BEFORE the MCP handshake runs — which is what
# breaks tunnel access, because a Cloudflare tunnel sends Host: <name>.trycloudflare.com,
# not 127.0.0.1. Default "*" trusts any host (safe here: the secret URL path is the real
# access control). Set MCP_ALLOWED_HOSTS to a comma-separated list to lock it to your
# specific tunnel hostname(s) instead, e.g. "abc.trycloudflare.com".
MCP_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "*").split(",") if h.strip()
] or ["*"]

# json_response=True  -> responses are plain JSON, not SSE streams. REQUIRED for
#   Cloudflare *quick* tunnels, which do not support Server-Sent Events. Keep this ON.
# stateless_http -> default OFF (normal session-based streamable HTTP). Claude's connector
#   registration does a discovery probe on the MCP path; in STATELESS mode that bare GET
#   returns a non-200 ("missing session id"), which Claude's flow can misread as
#   "this server wants OAuth", producing the "Couldn't register with sign-in service /
#   add an OAuth Client ID" error on an authless server. Session mode answers the probe
#   cleanly and still works over a quick tunnel because json_response avoids SSE. Set
#   MCP_STATELESS_HTTP=1 only if you specifically need stateless (it is NOT needed for the
#   quick tunnel — that requirement was json_response, not statelessness).
_JSON_RESPONSE = os.environ.get("MCP_JSON_RESPONSE", "1") != "0"
_STATELESS = os.environ.get("MCP_STATELESS_HTTP", "0") != "0"

# ---- DNS-rebinding / Host-header protection -------------------------------------------
# THE actual cause of the "421 Misdirected Request" + "Invalid Host header" failures over a
# Cloudflare tunnel. The MCP streamable-HTTP transport validates the incoming Host header
# against an allowlist that defaults to the bound host:port (e.g. "127.0.0.1:8765"). A
# tunnel delivers a different Host (the trycloudflare name, or a rewritten "127.0.0.1"
# WITHOUT the port) — none of which match — so the request is rejected with 421 BEFORE the
# MCP handshake, and Claude's connector then falls back to OAuth discovery and fails.
#
# Fix: build a TransportSecuritySettings that turns the protection OFF (or, if you set
# MCP_ALLOWED_HOSTS, that allowlists your specific tunnel host). Constructed defensively so
# it degrades safely across SDK versions. Disabling is safe here because the unguessable
# secret URL path (MCP_PATH) is the real access control, and the server binds to 127.0.0.1
# (only the tunnel can reach it).
def _build_transport_security():
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception:
        try:
            # Older/newer module location fallback.
            from mcp.server.streamable_http import TransportSecuritySettings  # type: ignore
        except Exception:
            return None  # SDK without this knob: nothing to configure.

    if "*" in MCP_ALLOWED_HOSTS:
        # Accept any Host: disable the rebinding check outright.
        for kwargs in (
            {"enable_dns_rebinding_protection": False},
            {"allowed_hosts": ["*"], "allowed_origins": ["*"]},
            {},
        ):
            try:
                return TransportSecuritySettings(**kwargs)
            except Exception:
                continue
        return None

    # Lock to specific hosts the user named (include common port variants).
    hosts = []
    for h in MCP_ALLOWED_HOSTS:
        hosts.append(h)
        if ":" not in h:
            hosts.append(f"{h}:{MCP_PORT}")
            hosts.append(f"{h}:443")
    for kwargs in (
        {"allowed_hosts": hosts, "allowed_origins": [f"https://{h}" for h in MCP_ALLOWED_HOSTS]},
        {"allowed_hosts": hosts},
    ):
        try:
            return TransportSecuritySettings(**kwargs)
        except Exception:
            continue
    return None


_TRANSPORT_SECURITY = _build_transport_security()

_fastmcp_kwargs = dict(
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_PATH,
    json_response=_JSON_RESPONSE,
    stateless_http=_STATELESS,
)
if _TRANSPORT_SECURITY is not None:
    _fastmcp_kwargs["transport_security"] = _TRANSPORT_SECURITY

try:
    mcp = FastMCP("poster-tools", **_fastmcp_kwargs)
except TypeError:
    # This SDK build doesn't accept transport_security as a kwarg; construct without it.
    _fastmcp_kwargs.pop("transport_security", None)
    mcp = FastMCP("poster-tools", **_fastmcp_kwargs)

EASYROUTER_BASE_URL = os.environ.get("EASYROUTER_BASE_URL", "https://easyrouter.io/v1").rstrip("/")
EASYROUTER_API_KEY = os.environ.get("EASYROUTER_API_KEY", "")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
HTTP_TIMEOUT = float(os.environ.get("EASYROUTER_TIMEOUT", "180"))


def _auth_headers(json_body: bool = False) -> dict:
    if not EASYROUTER_API_KEY:
        raise RuntimeError(
            "EASYROUTER_API_KEY is not set. Provide it via the MCP server's env "
            "config (see tools/.env) before calling image tools."
        )
    h = {"Authorization": f"Bearer {EASYROUTER_API_KEY}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


# ----------------------------------------------------------------------------
# Tool 1: font specimen (no network)
# ----------------------------------------------------------------------------
@mcp.tool()
def render_font_specimen(font_path: str, output_path: str, size: int = 1024) -> str:
    """Render a TTF/OTF font as a specimen sheet PNG for use as a visual reference.

    Pillow-only, no network. Shows A-Z, a-z, 0-9, punctuation, and sample phrases
    at multiple sizes so gpt-image-2 can imitate the typography style.

    Args:
        font_path: Absolute path to a .ttf or .otf file.
        output_path: Where to save the PNG.
        size: Output canvas size in px (square).

    Returns:
        Absolute path of the saved PNG.
    """
    if not Path(font_path).exists():
        raise FileNotFoundError(f"Font not found: {font_path}")

    img = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(img)

    samples = [
        ("ABCDEFGHIJKLM", 64),
        ("NOPQRSTUVWXYZ", 64),
        ("abcdefghijklmnop", 56),
        ("qrstuvwxyz", 56),
        ("0123456789  .,!?:;&@#", 48),
        ("The quick brown fox", 72),
        ("jumps over the lazy dog", 48),
        ("Sphinx of black quartz,", 40),
        ("judge my vow. 1234567890", 40),
    ]

    # Vertically lay out the samples, scaling down if they would overflow.
    margin = 40
    y = margin
    for text, font_size in samples:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception as e:
            raise RuntimeError(f"Pillow could not load font {font_path}: {e}")
        draw.text((margin, y), text, fill="black", font=font)
        # advance using the font's actual line height where available
        try:
            ascent, descent = font.getmetrics()
            line_h = ascent + descent
        except Exception:
            line_h = font_size
        y += line_h + 22
        if y > size - margin:
            break

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG")
    return str(Path(output_path).resolve())


# ----------------------------------------------------------------------------
# Tool 2: generate_poster — with-layout mode (/v1/images/edits)
# ----------------------------------------------------------------------------
@mcp.tool()
def generate_poster(
    prompt: str,
    reference_images: List[str],
    output_path: str,
    size: str = "1024x1024",
) -> str:
    """Generate a poster via gpt-image-2 using reference images (with-layout mode).

    Calls EasyRouter's /v1/images/edits (multipart). The first reference is the
    edit target (layout template); the rest are style guidance (font specimen).

    NOTE: /v1/images/edits is not in EasyRouter's published docs. If the gateway
    returns 404/405 for it, this falls back to /v1/images/generations (which cannot
    take reference images, so the layout anchor is lost — the critique step should
    catch the resulting drop in layout_fidelity). Run check_easyrouter first to
    learn which endpoints actually exist.

    Args:
        prompt: Generation prompt drafted by the orchestrator.
        reference_images: Absolute paths to reference PNGs (layout template + font specimen).
        output_path: Where to save the generated PNG.
        size: e.g. "1024x1024".

    Returns:
        Absolute path of the saved PNG.
    """
    for p in reference_images:
        if not Path(p).exists():
            raise FileNotFoundError(f"Reference image not found: {p}")

    url = f"{EASYROUTER_BASE_URL}/images/edits"
    headers = _auth_headers()

    file_handles = []
    files = []
    try:
        for ref_path in reference_images:
            fh = open(ref_path, "rb")
            file_handles.append(fh)
            files.append(("image[]", (Path(ref_path).name, fh, "image/png")))

        data = {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": "1"}

        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
            if resp.status_code in (404, 405):
                raise _EditsUnsupported(resp.status_code, resp.text)
            resp.raise_for_status()
            payload = resp.json()
    except _EditsUnsupported as e:
        # Fallback: /images/generations (no references possible).
        return _generate_via_generations(prompt, output_path, size, note=str(e))
    finally:
        for fh in file_handles:
            fh.close()

    return _save_images_response(payload, output_path)


class _EditsUnsupported(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"/images/edits returned {status}; falling back to /images/generations. Body: {body[:300]}")


def _generate_via_generations(prompt: str, output_path: str, size: str, note: str = "") -> str:
    """Fallback path: documented /v1/images/generations (JSON, no reference images)."""
    url = f"{EASYROUTER_BASE_URL}/images/generations"
    headers = _auth_headers(json_body=True)
    body = {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()
    return _save_images_response(payload, output_path)


def _save_images_response(payload: dict, output_path: str) -> str:
    """Handle the OpenAI-style images response (b64_json or url)."""
    try:
        item = payload["data"][0]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected images response shape: {json.dumps(payload)[:500]}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    if "b64_json" in item and item["b64_json"]:
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(item["b64_json"]))
    elif "url" in item and item["url"]:
        with httpx.Client(timeout=60) as client:
            img_resp = client.get(item["url"])
            img_resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(img_resp.content)
    else:
        raise RuntimeError(f"No image data in response item: {json.dumps(item)[:500]}")
    return str(Path(output_path).resolve())


# ----------------------------------------------------------------------------
# Tool 3: generate_poster_from_brief — from-brief mode (/v1/chat/completions)
# ----------------------------------------------------------------------------
@mcp.tool()
def generate_poster_from_brief(
    prompt: str,
    font_specimen_path: str,
    output_path: str,
    size: str = "1024x1024",
) -> str:
    """Generate a poster from a text brief when no layout template is available.

    Routes through EasyRouter's documented **/v1/images/generations** endpoint
    (text-to-image, JSON). This is the supported from-brief path on EasyRouter.

    IMPORTANT — typography handling: /images/generations is TEXT-ONLY; it cannot
    accept the font specimen PNG as a visual input (only /images/edits and, where
    supported, /chat/completions can). So the font style must be conveyed in the
    PROMPT TEXT. `font_specimen_path` is accepted for signature compatibility and is
    used only to (a) verify the specimen was rendered and (b) remind the orchestrator
    in the returned note to describe the typography in words (e.g. "a bold geometric
    sans-serif with tall x-height and circular bowls, as in the brand font"). If you
    need the specimen used as an actual visual reference, use `generate_poster` with a
    layout reference instead (that path does send images).

    An optional legacy route via /chat/completions (which DOES take the specimen image)
    is available by setting FROM_BRIEF_ROUTE=chat, but EasyRouter currently returns
    "operation unsupported" for gpt-image-2 chat image output, so the default is the
    images/generations route that works.

    Args:
        prompt: Generation prompt drafted by the orchestrator. Should describe the
            typography in words since no image is sent on this route.
        font_specimen_path: Absolute path to the font specimen PNG (existence-checked;
            not uploaded on the default route).
        output_path: Where to save the generated PNG.
        size: e.g. "1024x1024".

    Returns:
        Absolute path of the saved PNG.
    """
    if not Path(font_specimen_path).exists():
        raise FileNotFoundError(f"Font specimen not found: {font_specimen_path}")

    route = os.environ.get("FROM_BRIEF_ROUTE", "images").strip().lower()

    if route == "chat":
        # Legacy/opt-in path: multimodal chat with the specimen as a style image.
        # Kept for the day EasyRouter enables gpt-image-2 chat image output.
        return _generate_from_brief_via_chat(prompt, font_specimen_path, output_path, size)

    # Default, supported path: text-to-image via /v1/images/generations.
    # The specimen can't be sent here, so fold a typography instruction into the prompt.
    full_prompt = (
        f"{prompt}\n\n"
        f"Render all text in the poster using the brand's typography style. Match the "
        f"letterforms described above as closely as possible. Text must be sharp, "
        f"correctly spelled, and legible."
    )
    return _generate_via_generations(full_prompt, output_path, size,
                                     note="from_brief via /images/generations (text-only; "
                                          "typography conveyed in prompt)")


def _generate_from_brief_via_chat(prompt: str, font_specimen_path: str, output_path: str, size: str) -> str:
    """Opt-in (FROM_BRIEF_ROUTE=chat): /v1/chat/completions with specimen as style image.

    EasyRouter currently returns HTTP 400 'operation unsupported' for gpt-image-2 here;
    this is retained so it starts working automatically if/when that changes.
    """
    url = f"{EASYROUTER_BASE_URL}/chat/completions"
    headers = _auth_headers(json_body=True)

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
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{specimen_b64}"}},
                ],
            }
        ],
        "modalities": ["image", "text"],
    }

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"/chat/completions image output failed ({resp.status_code}): {resp.text[:300]}. "
                f"This route is unsupported on EasyRouter for {IMAGE_MODEL}; unset FROM_BRIEF_ROUTE "
                f"to use the default /images/generations route instead."
            )
        body = resp.json()

    image_bytes = _extract_image_from_chat_response(body)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_bytes)
    return str(Path(output_path).resolve())


# ----------------------------------------------------------------------------
# Tool 5: generate_poster_from_brief_with_specimen — brief + font, NO layout
#         (uses /v1/images/edits with the specimen as a STYLE-ONLY image reference)
# ----------------------------------------------------------------------------
@mcp.tool()
def generate_poster_from_brief_with_specimen(
    prompt: str,
    font_specimen_path: str,
    output_path: str,
    size: str = "1024x1024",
) -> str:
    """Best tool when you have a BRIEF + a FONT SPECIMEN but NO layout reference.

    This is the only path that both (a) works for gpt-image-2 on EasyRouter and
    (b) lets the model actually SEE your font. gpt-image-2 has no chat/completions
    image output, and /images/generations is text-only — so to use the specimen as a
    real visual reference we go through /v1/images/edits with the specimen as the sole
    input image, but with prompt framing that says: treat it as TYPOGRAPHY STYLE ONLY
    and invent a fresh poster composition (do NOT just edit the specimen sheet).

    Difference from the sibling tools:
      - generate_poster:                  needs a layout reference; first image is the
                                          edit target. Use when you HAVE a layout poster.
      - generate_poster_from_brief:       /images/generations, text-only, specimen NOT
                                          sent (font described in words). Use if you have
                                          no specimen or don't need exact typography.
      - generate_poster_from_brief_with_specimen (THIS):  /images/edits, specimen sent as
                                          style-only image, model invents the layout.

    Typography fidelity here is better than the text-only route (the model sees the
    glyphs) but still approximate — gpt-image-2 imitates, it does not embed the font.

    Args:
        prompt: The brief / generation prompt drafted by the orchestrator.
        font_specimen_path: Absolute path to the font specimen PNG (sent as style ref).
        output_path: Where to save the generated PNG.
        size: e.g. "1024x1024".

    Returns:
        Absolute path of the saved PNG.
    """
    if not Path(font_specimen_path).exists():
        raise FileNotFoundError(f"Font specimen not found: {font_specimen_path}")

    url = f"{EASYROUTER_BASE_URL}/images/edits"
    headers = _auth_headers()

    framed_prompt = (
        "Create an ORIGINAL poster composition from the brief below. "
        "The single attached image is a FONT SPECIMEN provided as a TYPOGRAPHY STYLE "
        "REFERENCE ONLY: imitate its letterforms (weight, proportions, character) when "
        "rendering text. Do NOT reproduce the specimen's grid or lay text out as a sample "
        "sheet, and do NOT treat the specimen as a background to edit — invent the layout, "
        "focal hierarchy, palette, and supporting graphics yourself to fit the brief. "
        "Render all text sharp, correctly spelled, and legible.\n\n"
        f"Brief:\n\n{prompt}"
    )

    fh = None
    try:
        fh = open(font_specimen_path, "rb")
        files = [("image[]", (Path(font_specimen_path).name, fh, "image/png"))]
        data = {"model": IMAGE_MODEL, "prompt": framed_prompt, "size": size, "n": "1"}
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
            if resp.status_code in (404, 405):
                raise RuntimeError(
                    f"/images/edits returned {resp.status_code}; this tool needs the edits "
                    f"endpoint. Run check_easyrouter — if edits is unavailable, fall back to "
                    f"generate_poster_from_brief (text-only). Body: {resp.text[:200]}"
                )
            resp.raise_for_status()
            payload = resp.json()
    finally:
        if fh is not None:
            fh.close()

    return _save_images_response(payload, output_path)


def _extract_image_from_chat_response(body: dict) -> bytes:
    """Extract image bytes from a chat-completions response.

    Image output via chat-completions is a newer, less-standardized surface;
    the response shape varies across providers/gateway versions. Handles four
    known shapes. If none match, raises with the message keys so the new shape
    can be added.
    """
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"No choices/message in chat response: {json.dumps(body)[:500]}")

    # Shape A: content is a list of typed blocks containing an image block.
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "image" and isinstance(block.get("image"), dict) and block["image"].get("b64_json"):
                return base64.b64decode(block["image"]["b64_json"])
            if btype == "image_url":
                url_field = (block.get("image_url") or {}).get("url", "")
                if url_field.startswith("data:image/"):
                    return base64.b64decode(url_field.split(",", 1)[1])
            # Shape A': output_image / inline b64 on the block itself
            if btype in ("output_image", "image_generation") and block.get("b64_json"):
                return base64.b64decode(block["b64_json"])

    # Shape B: a separate "images" array on the message.
    if isinstance(message.get("images"), list):
        for img in message["images"]:
            if not isinstance(img, dict):
                continue
            url_field = (img.get("image_url") or {}).get("url", "")
            if url_field.startswith("data:image/"):
                return base64.b64decode(url_field.split(",", 1)[1])
            if img.get("b64_json"):
                return base64.b64decode(img["b64_json"])
            if isinstance(img.get("url"), str) and img["url"].startswith("data:image/"):
                return base64.b64decode(img["url"].split(",", 1)[1])

    # Shape C: top-level data[] like the images endpoint (some gateways mirror this).
    if isinstance(body.get("data"), list) and body["data"]:
        item = body["data"][0]
        if isinstance(item, dict) and item.get("b64_json"):
            return base64.b64decode(item["b64_json"])

    raise RuntimeError(
        "Could not extract image from chat completions response. "
        f"Message keys: {list(message.keys())}. Top-level keys: {list(body.keys())}. "
        "Add the new shape to _extract_image_from_chat_response. "
        f"Snippet: {json.dumps(body)[:400]}"
    )


# ----------------------------------------------------------------------------
# Tool 4: check_easyrouter — first-run diagnostic
# ----------------------------------------------------------------------------
@mcp.tool()
def check_easyrouter(probe_endpoints: bool = True) -> str:
    """Diagnostic. Proves this MCP server can reach EasyRouter and reports which
    endpoints/response-shapes are live. RUN THIS FIRST after registering the server.

    Steps:
      1. GET /v1/models  -> confirms network reachability + lists models; checks
         whether IMAGE_MODEL (gpt-image-2) is present.
      2. (if probe_endpoints) trivial POST /v1/images/edits with a 1x1 PNG ->
         learns whether the edits endpoint exists (with-layout mode depends on it).
      3. (if probe_endpoints) minimal POST /v1/chat/completions image request ->
         captures the real chat image-output response shape.

    Returns a human-readable multi-line report. Never raises for an expected
    "endpoint missing" condition — it reports it.
    """
    report = []
    report.append(f"Base URL: {EASYROUTER_BASE_URL}")
    report.append(f"Model:    {IMAGE_MODEL}")
    report.append(f"API key:  {'set (' + EASYROUTER_API_KEY[:6] + '...)' if EASYROUTER_API_KEY else 'NOT SET'}")
    report.append("")

    # --- Step 1: models ---
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(f"{EASYROUTER_BASE_URL}/models", headers=_auth_headers())
        report.append(f"[1] GET /models -> HTTP {r.status_code}")
        if r.status_code == 200:
            try:
                data = r.json().get("data", [])
                ids = [m.get("id") for m in data if isinstance(m, dict)]
                report.append(f"    {len(ids)} models returned.")
                present = IMAGE_MODEL in ids
                report.append(f"    '{IMAGE_MODEL}' present: {present}")
                if not present:
                    img_like = [i for i in ids if i and ("image" in i.lower() or "gpt-image" in i.lower())]
                    report.append(f"    image-like model ids: {img_like[:10]}")
            except Exception as e:
                report.append(f"    (could not parse models json: {e})")
            report.append("    => NETWORK REACHABLE. The sandbox allowlist does NOT apply here. ✅")
        else:
            report.append(f"    Body: {r.text[:300]}")
            report.append("    => If this is a proxy 403 'blocked-by-allowlist', the MCP runtime shares")
            report.append("       the sandbox allowlist and we need another path. If it's a 401/EasyRouter")
            report.append("       403, the network is fine but the key/account needs attention.")
    except Exception as e:
        report.append(f"[1] GET /models -> EXCEPTION: {type(e).__name__}: {e}")
        report.append("    => Could not even connect. Network not reachable from this MCP context.")
        return "\n".join(report)

    if not probe_endpoints:
        return "\n".join(report)

    # tiny 1x1 white PNG, reused for probes
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buf, "PNG")
    tiny_png = buf.getvalue()
    tiny_b64 = base64.b64encode(tiny_png).decode("utf-8")

    # --- Step 2: images/edits existence ---
    report.append("")
    try:
        files = [("image[]", ("probe.png", tiny_png, "image/png"))]
        data = {"model": IMAGE_MODEL, "prompt": "probe", "size": "1024x1024", "n": "1"}
        with httpx.Client(timeout=60) as client:
            r = client.post(f"{EASYROUTER_BASE_URL}/images/edits", headers=_auth_headers(), data=data, files=files)
        report.append(f"[2] POST /images/edits -> HTTP {r.status_code}")
        if r.status_code in (404, 405):
            report.append("    => /images/edits NOT supported. with-layout mode must use the")
            report.append("       /images/generations fallback (no reference image -> weaker layout fidelity).")
        elif r.status_code < 300:
            report.append("    => /images/edits EXISTS. with-layout mode is viable. ✅")
        else:
            report.append(f"    Body: {r.text[:300]}")
            report.append("    => Non-fatal error; inspect body (may be quota/validation, not a missing endpoint).")
    except Exception as e:
        report.append(f"[2] POST /images/edits -> EXCEPTION: {type(e).__name__}: {e}")

    # --- Step 2.5: images/generations (the DEFAULT from-brief route) ---
    report.append("")
    try:
        body_gen = {"model": IMAGE_MODEL, "prompt": "a small solid blue square, minimal", "size": "1024x1024", "n": 1}
        with httpx.Client(timeout=120) as client:
            r = client.post(f"{EASYROUTER_BASE_URL}/images/generations", headers=_auth_headers(json_body=True), json=body_gen)
        report.append(f"[2.5] POST /images/generations -> HTTP {r.status_code}")
        if r.status_code < 300:
            try:
                item = r.json().get("data", [{}])[0]
                has_img = bool(item.get("b64_json") or item.get("url"))
                report.append(f"    => /images/generations WORKS (image payload present: {has_img}). "
                              f"from-brief mode is viable. ✅")
            except Exception as ex:
                report.append(f"    => 2xx but couldn't parse data[]: {ex}. Body: {r.text[:200]}")
        else:
            report.append(f"    Body: {r.text[:300]}")
            report.append("    => from-brief mode would FAIL on this route. Check model/quota.")
    except Exception as e:
        report.append(f"[2.5] POST /images/generations -> EXCEPTION: {type(e).__name__}: {e}")

    # --- Step 3: chat/completions image-output shape (legacy from-brief route, opt-in) ---
    report.append("")
    try:
        payload = {
            "model": IMAGE_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Generate a tiny 1024x1024 test image: a solid blue square."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_b64}"}},
                ],
            }],
            "modalities": ["image", "text"],
        }
        with httpx.Client(timeout=120) as client:
            r = client.post(f"{EASYROUTER_BASE_URL}/chat/completions", headers=_auth_headers(json_body=True), json=payload)
        report.append(f"[3] POST /chat/completions (image out) -> HTTP {r.status_code}")
        if r.status_code < 300:
            body = r.json()
            try:
                _extract_image_from_chat_response(body)
                report.append("    => Image extracted successfully with a known shape. ✅")
            except Exception as ex:
                report.append(f"    => Got 200 but extractor failed: {ex}")
                msg = body.get("choices", [{}])[0].get("message", {}) if isinstance(body.get("choices"), list) else {}
                report.append(f"    message keys: {list(msg.keys()) if isinstance(msg, dict) else 'n/a'}")
                report.append(f"    top-level keys: {list(body.keys())}")
                report.append("    ACTION: paste this report back so the extractor can be extended.")
        else:
            report.append(f"    Body: {r.text[:300]}")
    except Exception as e:
        report.append(f"[3] POST /chat/completions -> EXCEPTION: {type(e).__name__}: {e}")

    return "\n".join(report)


# ----------------------------------------------------------------------------
# Health route + bearer-auth, then transport-aware launch
# ----------------------------------------------------------------------------
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request):
    """Liveness probe. cloudflared and you can hit this to confirm the server is up.
    Returns 200 with no secrets. Does NOT require the auth token (so you can health-check
    a tunnel without leaking it)."""
    return JSONResponse(
        {
            "status": "ok",
            "server": "poster-tools",
            "transport": MCP_TRANSPORT,
            "mcp_path": MCP_PATH,
            "auth_required": bool(MCP_AUTH_TOKEN),
            "easyrouter_key_loaded": bool(EASYROUTER_API_KEY),
            "image_model": IMAGE_MODEL,
        }
    )


def _build_http_app():
    """Return the Streamable-HTTP Starlette app, host-checked and optionally bearer-auth'd.

    TrustedHost is applied ALWAYS (so tunnel hostnames don't get rejected with HTTP 421
    before the MCP handshake). Bearer-auth is applied only if MCP_AUTH_TOKEN is set, and
    only on the MCP endpoint path; /healthz stays open so a tunnel can be liveness-checked.
    """
    app = mcp.streamable_http_app()

    # ---- (1) Bearer-auth, added to the Starlette app FIRST (while it still has
    # .add_middleware), only if a token is set, only on the MCP path. /healthz stays open.
    if MCP_AUTH_TOKEN:
        from starlette.middleware.base import BaseHTTPMiddleware

        class BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path.rstrip("/") == MCP_PATH.rstrip("/"):
                    expected = f"Bearer {MCP_AUTH_TOKEN}"
                    presented = request.headers.get("authorization", "")
                    if not secrets.compare_digest(presented, expected):
                        return JSONResponse(
                            {"error": "unauthorized",
                             "detail": "Missing or invalid bearer token for the poster-tools MCP endpoint."},
                            status_code=401,
                        )
                return await call_next(request)

        app.add_middleware(BearerAuthMiddleware)
    else:
        print(
            "[poster-tools] WARNING: MCP_AUTH_TOKEN is not set — the MCP endpoint is OPEN. "
            "Anyone who learns the tunnel URL can call the image tools and spend your "
            "EasyRouter key. Set MCP_AUTH_TOKEN before exposing this via a tunnel.",
            file=sys.stderr, flush=True,
        )

    # ---- (2) Host-header normalization, wrapped OUTERMOST (runs before anything else).
    # This is the definitive fix for "421 Misdirected Request" / "Invalid Host header" over a
    # Cloudflare tunnel. The MCP transport's DNS-rebinding check compares the inbound Host
    # against the bound host:port and rejects everything else with 421 BEFORE the MCP
    # handshake — which is why the tunnel connects but the connector can't register (Claude
    # then falls back to OAuth discovery and fails). _build_transport_security() above also
    # tries to relax this via the SDK setting; this ASGI rewrite is the version-independent
    # belt-and-suspenders: it overwrites the Host header with exactly "<bound-host>:<port>",
    # the one value the transport always trusts, so the 421 cannot occur regardless of SDK
    # internals. Safe: the secret URL path is the real access control, and the socket binds
    # only to 127.0.0.1 (reachable solely through your tunnel).
    _canonical_host = f"{MCP_HOST}:{MCP_PORT}".encode("latin-1")
    _inner_app = app

    async def _host_rewrite_asgi(scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            headers = [(k, v) for (k, v) in scope.get("headers", []) if k.lower() != b"host"]
            headers.append((b"host", _canonical_host))
            scope = dict(scope)
            scope["headers"] = headers
        await _inner_app(scope, receive, send)

    print(f"[poster-tools] Host header normalized to {_canonical_host.decode()} for all "
          f"requests (defeats the 421 / Invalid-Host rejection over a tunnel).",
          file=sys.stderr, flush=True)
    return _host_rewrite_asgi


def main():
    if MCP_TRANSPORT == "stdio":
        # Local stdio mode — no HTTP, no tunnel, no auth. For quick local tests only.
        print("[poster-tools] starting in STDIO mode (local only).", file=sys.stderr, flush=True)
        mcp.run(transport="stdio")
        return

    if MCP_TRANSPORT in ("streamable-http", "http", "streamable_http"):
        import uvicorn

        app = _build_http_app()
        banner = [
            "[poster-tools] starting Streamable HTTP server",
            f"    local URL : http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}",
            f"    health    : http://{MCP_HOST}:{MCP_PORT}/healthz",
            f"    auth      : {'BEARER TOKEN REQUIRED' if MCP_AUTH_TOKEN else 'OPEN (no token set)'}",
            f"    easyrouter: key {'loaded' if EASYROUTER_API_KEY else 'NOT LOADED'}, model {IMAGE_MODEL}",
            "    -> point `cloudflared tunnel --url http://%s:%s` at this to get a public HTTPS URL,"
            % (MCP_HOST, MCP_PORT),
            f"       then register <public-url>{MCP_PATH} as a remote MCP connector in Cowork.",
        ]
        print("\n".join(banner), file=sys.stderr, flush=True)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
        return

    if MCP_TRANSPORT == "sse":
        # Legacy SSE transport, only if a client specifically needs it.
        print("[poster-tools] starting in legacy SSE mode.", file=sys.stderr, flush=True)
        mcp.run(transport="sse")
        return

    raise SystemExit(
        f"Unknown MCP_TRANSPORT={MCP_TRANSPORT!r}. "
        "Use 'streamable-http' (default), 'stdio', or 'sse'."
    )


if __name__ == "__main__":
    main()
