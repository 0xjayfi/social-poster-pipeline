# Operating prompt for the poster pipeline (manual run now, schedulable later)

This is the full operating instruction for the agent. Two ways to use it:

- **Manual run (now):** paste the fenced block below into a Cowork chat in this project,
  with the `poster-tools` connector enabled. The agent runs the whole loop autonomously
  and only stops for the high-stakes "ship" decision if you want to review.
- **Scheduled run (later, after the named tunnel is up):** paste the same block into a
  Cowork scheduled task and select model **`claude-opus-4-8`**. No runtime input needed.

## Context the prompt assumes (already true in this project)

1. The `poster-tools` remote connector is registered and enabled, exposing four tools:
   `check_easyrouter`, `render_font_specimen`, `generate_poster`,
   `generate_poster_from_brief`. They run on your Mac (reached via the Cloudflare
   tunnel), so they can talk to EasyRouter — the Cowork sandbox's egress block does not
   apply to them.
2. The working folder is `poster_workspace/`, with `inputs/` populated
   (`brand_font.ttf`, `brief.md`, and optionally `reference_poster.png`).
3. `/v1/images/edits` support on EasyRouter is confirmed-by-probe at run time via
   `check_easyrouter`; the prompt branches on the result so a missing edits endpoint
   degrades gracefully instead of failing.

---

```
You are running a poster generation pipeline. Your working folder is `poster_workspace/`.
Four MCP tools are available from the `poster-tools` connector: `check_easyrouter`,
`render_font_specimen`, `generate_poster`, and `generate_poster_from_brief`. These tools
run on the user's Mac (reached over a Cloudflare tunnel), so they CAN reach EasyRouter
even though the Cowork sandbox cannot. Operate autonomously through all steps; do not ask
the user for input. The only human-relevant decision is the final ship, which the rubric
makes for you.

STEP 0 — Reachability gate (RUN ONCE, FIRST).
- Call `check_easyrouter(probe_endpoints=true)`.
- If step [1] (GET /models) did NOT report "NETWORK REACHABLE", STOP immediately.
  Write the full report to `outputs/run_log.md` and do nothing else. This means the MCP
  server on the user's Mac is down, the tunnel is down, or the URL changed — none of which
  the agent can fix. Say so plainly in the log.
- From the report, record whether `/images/edits` exists. Call this EDITS_OK (true/false).
- If `gpt-image-2` is reported NOT present, read the "image-like model ids" line and use
  the closest matching id for generation; note the substitution in the log.
- If the chat-completions probe [3] reported an extractor failure, STOP and write the
  report to `outputs/run_log.md` — from-brief mode will fail until the extractor is updated.

STEP 1 — Read inputs and choose mode.
- Read `inputs/brief.md` to understand what the poster should communicate.
- Check whether `inputs/reference_poster.png` exists.
    * If YES and EDITS_OK → mode = "with_layout" (use `generate_poster`).
      View the reference to understand desired layout, palette, mood, focal hierarchy.
      Treat it as a layout TEMPLATE (where things sit), not content to copy.
    * If YES but NOT EDITS_OK → mode = "with_layout_degraded". You will still call
      `generate_poster`, but it will fall back to /images/generations and the layout
      reference will NOT be sent. Compensate by describing the reference's layout
      explicitly in prose in the prompt. Note this in the log.
    * If NO → mode = "from_brief" (use `generate_poster_from_brief`).
- Call `render_font_specimen` on `inputs/brand_font.ttf`, output to
  `intermediate/font_specimen.png`. View the specimen to understand the typography.
- Record the chosen mode (and EDITS_OK) in `intermediate/mode.txt`.

STEP 2 — Draft the generation prompt.
- Compose a prompt that:
  * places the text content described in the brief (quote the EXACT wording to render,
    including the numbers, so the model spells them correctly)
  * matches the typography style shown in the font specimen
  * (with_layout)        preserves the reference layout: grid, balance, focal
                         hierarchy, negative space, palette, mood
  * (with_layout_degraded / from_brief) describes the composition explicitly in prose:
                         focal hierarchy, palette, mood, where text sits, supporting graphics
- Save to `intermediate/iter_01_prompt.txt`.

STEP 3 — Generate (branch on mode).
- with_layout / with_layout_degraded:
    generate_poster(
      prompt = <contents of iter_01_prompt.txt>,
      reference_images = ["inputs/reference_poster.png", "intermediate/font_specimen.png"],
      output_path = "intermediate/iter_01_generated.png",
      size = "1024x1024")
- from_brief:
    generate_poster_from_brief(
      prompt = <contents of iter_01_prompt.txt>,
      font_specimen_path = "intermediate/font_specimen.png",
      output_path = "intermediate/iter_01_generated.png",
      size = "1024x1024")

STEP 4 — Critique.
- View `intermediate/iter_01_generated.png`.
- Score each dimension 1-5. Two dimensions branch on mode:
    text_legibility:   text is readable, correctly spelled, well placed
    typography_match:  font style resembles the specimen
    overall_vibe:      matches the brief's intent
    (with_layout*) layout_fidelity:  matches the reference grid and focal hierarchy
    (with_layout*) brand_palette:    colors match the reference
    (from_brief)   layout_quality:   composition works as a poster on its own merits
    (from_brief)   palette_cohesion: colors are cohesive and fit the brief's intent
- Write rubric, scores, and revision notes to `intermediate/iter_01_critique.md`.
- Decision rule (this IS the ship decision — make it yourself):
    * If total >= 22 of 25 AND no individual score < 4 → SHIP.
    * Else revise the prompt and repeat steps 3-4 using iter_02_*, iter_03_*, etc.
    * Anti-spiral: if iteration N scores LOWER than iteration N-1, ship the best-so-far.

STEP 5 — Iteration cap.
- Maximum 6 generation attempts. At iteration 6, ship the highest-scoring image
  regardless of whether it crossed the threshold. After writing iter_06 artifacts,
  also write a file `intermediate/STOP` so any follow-on check can refuse a 7th.

STEP 6 — Finalize.
- Copy the chosen image to `outputs/final_poster.png`.
- Write `outputs/run_log.md` summarizing:
    * the check_easyrouter result (reachable? edits supported? model substitution?)
    * which mode was used and why
    * which iteration was chosen and why
    * the final scores
    * a one-line summary of each attempted iteration
- Stop. Report the final poster path and the winning scores to the user.

Do not edit `inputs/`. Do not delete anything in `intermediate/`. The whole history is
preserved for review.
```

## Attaching this as a recurring schedule (do this AFTER the named tunnel is live)

You chose "manual only" for now, which is right until the tunnel is stable. When you want
it unattended:

1. Stand up the **named tunnel** (`CLOUDFLARE_TUNNEL.md` §2.4) so the connector URL is
   permanent, and install the server + cloudflared as background services so they're up
   without a Terminal open.
2. In Cowork, create a scheduled task, select model **`claude-opus-4-8`**, paste the
   fenced block above as the task prompt, and set your cadence.
3. Heads-up on data freshness: the KPIs in `inputs/brief.md` are hardcoded for the
   May 23–29 week. A recurring schedule will keep regenerating that same week's poster
   until either you update `brief.md` each period, or you extend the pipeline to pull the
   current week's numbers (e.g. an X-analytics step) before generating. Decide that before
   turning on a daily/weekly cadence.
