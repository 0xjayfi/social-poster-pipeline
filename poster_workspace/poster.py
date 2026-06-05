#!/usr/bin/env python3
"""
poster.py — X-post poster generation pipeline (runs on your Mac).

Why this is a plain script and not an MCP server:
  - The Cowork sandbox is network-walled off from easyrouter.io (egress allowlist).
  - Your Mac is NOT walled. The image API works from here (verified: both
    /v1/images/generations and /v1/images/edits return HTTP 200 with b64_json).
  - The desktop app only registers REMOTE mcp servers, so a local stdio server
    can't be wired in anyway.
  => Simplest correct design: a script you run in your Terminal, writing into the
     shared workspace folder that Claude can also read for the critique step.

The self-critique loop is human-in-the-loop:
    1. you run a generate step  -> writes intermediate/iter_NN_generated.png
    2. Claude (in the Cowork chat) reads that PNG and scores it on the rubric
    3. Claude gives you a revised prompt
    4. you run the next generate step with the new prompt
    5. when satisfied, run `finalize` to copy the chosen iteration to outputs/

Dependencies (install once):
    pip3 install httpx Pillow
    # or: python3 -m pip install --user httpx Pillow

Auth (do NOT hardcode the key):
    export EASYROUTER_API_KEY="sk-..."
    # optional overrides:
    export EASYROUTER_BASE_URL="https://easyrouter.io/v1"   # default
    export IMAGE_MODEL="gpt-image-2"                         # default

Usage (run from inside poster_workspace/):
    # 0. render the font specimen (once)
    python3 poster.py specimen --font inputs/brand_font.ttf

    # 1a. WITH a layout reference (with-layout mode -> /v1/images/edits):
    python3 poster.py edits \
        --prompt-file intermediate/iter_01_prompt.txt \
        --reference inputs/reference_poster.png \
        --reference intermediate/font_specimen.png

    # 1b. WITHOUT a layout reference (from-brief mode):
    python3 poster.py generate \
        --prompt-file intermediate/iter_01_prompt.txt \
        --specimen intermediate/font_specimen.png \
        --route images           # or: --route chat

    # each generate auto-picks the next iter number unless you pass --iter NN
    # 2. finalize the chosen iteration:
    python3 poster.py finalize --iter 03

    # handy: confirm connectivity + model + endpoints from your Mac
    python3 poster.py check
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("Missing dependency 'httpx'. Run:  uv run --with httpx --with Pillow python3 poster.py ...")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Missing dependency 'Pillow'. Run:  uv run --with httpx --with Pillow python3 poster.py ...")


def _load_dotenv():
    """Load KEY=VALUE lines from a .env file so you don't have to `export` each session.

    Search order (first match wins): $POSTER_ENV_FILE, ./.env (cwd),
    <workspace_root>/.env, <workspace_root>/tools/.env.
    Real environment variables ALWAYS win over .env values (so an explicit
    `export` still overrides the file). Lines starting with # are ignored;
    surrounding quotes on values are stripped.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("POSTER_ENV_FILE"),
        Path.cwd() / ".env",
        here / ".env",
        here / "tools" / ".env",
    ]
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
            if key and key not in os.environ:   # real env wins
                os.environ[key] = val
        break  # only load the first .env found


_load_dotenv()

BASE_URL = os.environ.get("EASYROUTER_BASE_URL", "https://easyrouter.io/v1").rstrip("/")
API_KEY = os.environ.get("EASYROUTER_API_KEY", "")
MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
TIMEOUT = float(os.environ.get("EASYROUTER_TIMEOUT", "300"))

# Talk to EasyRouter directly, ignoring any ambient HTTP(S)/SOCKS proxy env vars.
# (A forced proxy is exactly what blocks this in sandboxed/VPN environments; a direct
# connection is what worked from the Mac in testing. Set EASYROUTER_USE_PROXY=1 to
# opt back into honoring proxy env vars if you genuinely need one.)
_USE_PROXY = os.environ.get("EASYROUTER_USE_PROXY", "") == "1"


def _client(timeout):
    """httpx client that bypasses proxy env vars unless EASYROUTER_USE_PROXY=1."""
    if _USE_PROXY:
        return httpx.Client(timeout=timeout)
    return httpx.Client(timeout=timeout, trust_env=False)

# Resolve paths relative to the workspace root (the dir containing this script),
# so the script works no matter what cwd you launch it from.
ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "inputs"
INTER = ROOT / "intermediate"
OUTPUTS = ROOT / "outputs"


def _need_key():
    if not API_KEY:
        sys.exit("EASYROUTER_API_KEY is not set.  export EASYROUTER_API_KEY='sk-...'")


def _auth(json_body=False):
    h = {"Authorization": f"Bearer {API_KEY}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _resolve(p: str) -> Path:
    """Accept paths relative to cwd OR to the workspace root."""
    pp = Path(p)
    if pp.is_absolute():
        return pp
    if (Path.cwd() / pp).exists():
        return (Path.cwd() / pp).resolve()
    return (ROOT / pp).resolve()


def _next_iter_num() -> int:
    INTER.mkdir(parents=True, exist_ok=True)
    nums = []
    for f in INTER.glob("iter_*_generated.png"):
        m = re.match(r"iter_(\d+)_generated\.png", f.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _save_b64_or_url_item(item: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        out_path.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        with _client(120) as c:
            r = c.get(item["url"])
            r.raise_for_status()
        out_path.write_bytes(r.content)
    else:
        raise RuntimeError(f"No image data in item: {json.dumps(item)[:300]}")
    return out_path


# ---------------------------------------------------------------------------
# subcommand: specimen
# ---------------------------------------------------------------------------
def cmd_specimen(args):
    font_path = _resolve(args.font)
    if not font_path.exists():
        sys.exit(f"Font not found: {font_path}")
    out = _resolve(args.out) if args.out else (INTER / "font_specimen.png")
    size = args.size

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
    margin, y = 40, 40
    for text, fs in samples:
        try:
            font = ImageFont.truetype(str(font_path), fs)
        except Exception as e:
            sys.exit(f"Pillow could not load font {font_path}: {e}")
        draw.text((margin, y), text, fill="black", font=font)
        try:
            asc, desc = font.getmetrics()
            line_h = asc + desc
        except Exception:
            line_h = fs
        y += line_h + 22
        if y > size - margin:
            break
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    print(f"specimen written: {out}")


# ---------------------------------------------------------------------------
# subcommand: edits  (with-layout mode, /v1/images/edits, multipart)
# ---------------------------------------------------------------------------
def cmd_edits(args):
    prompt = _read_prompt(args)
    refs = [_resolve(r) for r in args.reference]
    for r in refs:
        if not r.exists():
            sys.exit(f"Reference not found: {r}")
    url = f"{BASE_URL}/images/edits"

    # Dry-run is read-only: print the assembled request and touch nothing.
    if args.dry_run:
        n_preview = args.iter if args.iter else _next_iter_num()
        print(f"[dry-run] POST {url}\n  model={MODEL} size={args.size} n=1")
        print(f"  would write: intermediate/iter_{n_preview:02d}_generated.png")
        print(f"  references: {[str(r) for r in refs]}")
        print(f"  prompt[{len(prompt)} chars]: {prompt[:200]}...")
        return

    _need_key()
    n = args.iter if args.iter else _next_iter_num()
    out = INTER / f"iter_{n:02d}_generated.png"
    _save_prompt(n, prompt)

    fhs, files = [], []
    try:
        for r in refs:
            fh = open(r, "rb")
            fhs.append(fh)
            files.append(("image[]", (r.name, fh, "image/png")))
        data = {"model": MODEL, "prompt": prompt, "size": args.size, "n": "1"}
        with _client(TIMEOUT) as c:
            resp = c.post(url, headers=_auth(), data=data, files=files)
            resp.raise_for_status()
            payload = resp.json()
    finally:
        for fh in fhs:
            fh.close()
    item = payload["data"][0]
    _save_b64_or_url_item(item, out)
    print(f"iter {n:02d} written: {out}")
    print(f"NEXT: ask Claude in the Cowork chat to view {out.name} and score it.")


# ---------------------------------------------------------------------------
# subcommand: generate (from-brief mode)
#   --route images  -> /v1/images/generations (JSON, no reference image)
#   --route chat    -> /v1/chat/completions   (specimen as style ref)
# ---------------------------------------------------------------------------
def cmd_generate(args):
    prompt = _read_prompt(args)

    # Dry-run is read-only: print the assembled request and touch nothing.
    if args.dry_run:
        n_preview = args.iter if args.iter else _next_iter_num()
        if args.route == "images":
            url = f"{BASE_URL}/images/generations"
            body = {"model": MODEL, "prompt": prompt, "size": args.size, "n": 1}
            print(f"[dry-run] POST {url}\n  {json.dumps(body)[:300]}")
        else:
            specimen = _resolve(args.specimen) if args.specimen else (INTER / "font_specimen.png")
            print(f"[dry-run] POST {BASE_URL}/chat/completions\n  model={MODEL}; specimen={specimen}")
            print(f"  prompt[{len(prompt)} chars]: {prompt[:200]}...")
        print(f"  would write: intermediate/iter_{n_preview:02d}_generated.png")
        return

    _need_key()
    n = args.iter if args.iter else _next_iter_num()
    out = INTER / f"iter_{n:02d}_generated.png"
    _save_prompt(n, prompt)

    if args.route == "images":
        url = f"{BASE_URL}/images/generations"
        body = {"model": MODEL, "prompt": prompt, "size": args.size, "n": 1}
        with _client(TIMEOUT) as c:
            resp = c.post(url, headers=_auth(json_body=True), json=body)
            resp.raise_for_status()
            payload = resp.json()
        item = payload["data"][0]
        _save_b64_or_url_item(item, out)

    else:  # chat route
        specimen = _resolve(args.specimen) if args.specimen else (INTER / "font_specimen.png")
        if not specimen.exists():
            sys.exit(f"Specimen not found: {specimen} (run `specimen` first or pass --specimen)")
        b64 = base64.b64encode(specimen.read_bytes()).decode()
        user_text = (
            f"Generate a {args.size} poster image.\n\n"
            "The attached image is a font specimen. Use it as a TYPOGRAPHY STYLE "
            "REFERENCE ONLY. Do not reproduce the specimen's layout or lay text out "
            "as a sample sheet. Invent a poster composition that fits the brief and "
            "renders any text in the style shown in the specimen.\n\n"
            f"Brief:\n\n{prompt}"
        )
        url = f"{BASE_URL}/chat/completions"
        body = {
            "model": MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            "modalities": ["image", "text"],
        }
        with _client(TIMEOUT) as c:
            resp = c.post(url, headers=_auth(json_body=True), json=body)
            resp.raise_for_status()
            payload = resp.json()
        img_bytes = _extract_chat_image(payload)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(img_bytes)

    print(f"iter {n:02d} written: {out}")
    print(f"NEXT: ask Claude in the Cowork chat to view {out.name} and score it.")


def _extract_chat_image(body: dict) -> bytes:
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"No choices/message: {json.dumps(body)[:400]}")
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "image" and isinstance(b.get("image"), dict) and b["image"].get("b64_json"):
                return base64.b64decode(b["image"]["b64_json"])
            if b.get("type") == "image_url":
                u = (b.get("image_url") or {}).get("url", "")
                if u.startswith("data:image/"):
                    return base64.b64decode(u.split(",", 1)[1])
            if b.get("b64_json"):
                return base64.b64decode(b["b64_json"])
    if isinstance(msg.get("images"), list):
        for im in msg["images"]:
            if not isinstance(im, dict):
                continue
            u = (im.get("image_url") or {}).get("url", "")
            if u.startswith("data:image/"):
                return base64.b64decode(u.split(",", 1)[1])
            if im.get("b64_json"):
                return base64.b64decode(im["b64_json"])
    if isinstance(body.get("data"), list) and body["data"]:
        it = body["data"][0]
        if isinstance(it, dict) and it.get("b64_json"):
            return base64.b64decode(it["b64_json"])
    raise RuntimeError(
        "Could not find image in chat response. "
        f"message keys={list(msg.keys())} top-level={list(body.keys())}. "
        f"Snippet: {json.dumps(body)[:400]}"
    )


# ---------------------------------------------------------------------------
# subcommand: finalize
# ---------------------------------------------------------------------------
def cmd_finalize(args):
    src = INTER / f"iter_{int(args.iter):02d}_generated.png"
    if not src.exists():
        sys.exit(f"No such iteration: {src}")
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    dst = OUTPUTS / "final_poster.png"
    shutil.copyfile(src, dst)
    print(f"final poster: {dst}  (from {src.name})")


# ---------------------------------------------------------------------------
# subcommand: check  (connectivity from the Mac)
# ---------------------------------------------------------------------------
def cmd_check(args):
    _need_key()
    print(f"Base: {BASE_URL}  Model: {MODEL}  Key: {API_KEY[:6]}...")
    with _client(30) as c:
        r = c.get(f"{BASE_URL}/models", headers=_auth())
    print(f"GET /models -> HTTP {r.status_code}")
    if r.status_code == 200:
        ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        print(f"  {len(ids)} models; '{MODEL}' present: {MODEL in ids}")
    else:
        print(f"  body: {r.text[:200]}")


# ---------------------------------------------------------------------------
# shared prompt helpers
# ---------------------------------------------------------------------------
def _read_prompt(args) -> str:
    if getattr(args, "prompt_file", None):
        pf = _resolve(args.prompt_file)
        if not pf.exists():
            sys.exit(f"Prompt file not found: {pf}")
        return pf.read_text().strip()
    if getattr(args, "prompt", None):
        return args.prompt.strip()
    sys.exit("Provide --prompt-file or --prompt")


def _save_prompt(n: int, prompt: str):
    INTER.mkdir(parents=True, exist_ok=True)
    pf = INTER / f"iter_{n:02d}_prompt.txt"
    if not pf.exists():
        pf.write_text(prompt)


def main():
    p = argparse.ArgumentParser(description="X-post poster generation pipeline (runs on your Mac).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("specimen", help="render the font specimen PNG")
    sp.add_argument("--font", required=True)
    sp.add_argument("--out", default=None)
    sp.add_argument("--size", type=int, default=1024)
    sp.set_defaults(func=cmd_specimen)

    se = sub.add_parser("edits", help="with-layout mode (/v1/images/edits)")
    se.add_argument("--prompt-file")
    se.add_argument("--prompt")
    se.add_argument("--reference", action="append", required=True,
                    help="repeatable; first = layout target, then style refs")
    se.add_argument("--iter", type=int, default=None)
    se.add_argument("--size", default="1024x1024")
    se.add_argument("--dry-run", action="store_true")
    se.set_defaults(func=cmd_edits)

    sg = sub.add_parser("generate", help="from-brief mode (no layout reference)")
    sg.add_argument("--prompt-file")
    sg.add_argument("--prompt")
    sg.add_argument("--specimen", default=None)
    sg.add_argument("--route", choices=["images", "chat"], default="images")
    sg.add_argument("--iter", type=int, default=None)
    sg.add_argument("--size", default="1024x1024")
    sg.add_argument("--dry-run", action="store_true")
    sg.set_defaults(func=cmd_generate)

    sf = sub.add_parser("finalize", help="copy chosen iteration to outputs/final_poster.png")
    sf.add_argument("--iter", required=True)
    sf.set_defaults(func=cmd_finalize)

    sc = sub.add_parser("check", help="confirm connectivity/model from this machine")
    sc.set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
