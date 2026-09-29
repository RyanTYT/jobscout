# packets/ — application packets

Turns one selected posting into a complete, claim-checked application
packet in `var/applications/<company-slug>-<date>/`.

## orchestrator.py — `prepare_packet(conn, posting_id)`

The pipeline (status: `needs_input` with reasons | `ready`):

1. **Fill sheet** (`field_map.build_fill_sheet`): the "values a job
   application needs", from the master resume — every required field or
   a reason.
2. **Tailor** (`tailor.py`, quality tier): produces a selection PLAN
   (JSON — which bullets, order, connective-only rephrasing), validated
   against the resume's real IDs; unknown references are stripped, never
   trusted.
3. **Cover letter** (`cover_letter.py`, quality tier): hook/fit/proof/
   close, 250–350 words, evidence-gated (may only state facts from the
   resume or research notes).
4. **Claim check** (`claim_check.py`): every number/entity in the
   generated text must trace to the resume — unsupported claims gate
   `ready`.
5. **Render** (`render.py`): resume.md always; resume.pdf via typst
   (template copied beside the packet data — typst resolves paths
   relative to the template file).

Files per packet: `packet.yaml` (manifest) · `fill_sheet.yaml` ·
`tailor.yaml` · `cover_letter.md` · `resume.md` · `resume.pdf` ·
`claim_check.yaml`. Status flows: drafting → needs_input → ready →
filled → applied.

## field_map.py — the requiredness registry

`CHECKLIST` is THE single source of truth for "fields an application
needs + which are required" — the fill sheet, the profile editor's
"missing required" badge, and packet readiness all read it (they can
never disagree).

**Tests:** `tests/test_packets.py` (end-to-end skeleton with the fake
models, claim-check gate, typst PDF render).
