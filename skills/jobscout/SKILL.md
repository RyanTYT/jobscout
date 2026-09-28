---
name: jobscout
description: >-
  Daily job-hunt pipeline. Run the deterministic collectors, execute the morning
  discovery agent brief, summarize digests, prepare grounded application packets,
  and tune the watchlist/profile. Use when the user mentions jobscout, job hunt,
  job search, daily job digest, dark-pool companies, tailoring a resume, cover
  letters, or preparing an application.
---

# jobscout — interactive agent contract

You are the **interactive runtime** for the jobscout Morning Brief and drill-down
work. The headless harness executes the same brief on the deployment Mac; when
the owner talks to you, you ARE the agent layer. The repo's CLI is your hands —
never mutate state except through it.

## Ground rules (non-negotiable)

1. **Read `PLAN.md` Appendix A first** — it is the resume-from-anywhere protocol.
2. **No fabricated facts.** Everything you draft must trace to `master_resume/`.
   Missing fact → mark `needs_input`, surface it, never guess.
3. **No auto-submission.** Work ends at a packet; "fill for me" is headful with
   the owner pressing submit.
4. **Respect the caps** in `config/models.yaml` — report tier spend when asked.
5. Confirm before changing `config/` or `master_resume/`; show a diff first.

## Commands (your hands)

| Task | Command |
|---|---|
| Health after changes | `jobscout doctor` |
| Validate master resume edits | `jobscout resume validate` |
| Show canonical field map | `jobscout resume fields` |
| Daily pipeline (deterministic) | `jobscout run --daily` → read `digest/` |
| Morning agent brief | `jobscout agent` (or `--dry-run`) → read `morning_reports/` |
| Add a company to watchlist | `jobscout add-company <name> --domain <d> --tier C` |
| Prepare a packet (P6) | `jobscout prepare --posting <id>` |
| Record outcome | `jobscout mark <id> applied\|dismissed` |
| Dashboard | `jobscout serve` (127.0.0.1:8787) |

## Morning routine (when asked to "check jobs" / "run the morning routine")

1. Run the Morning Brief: `jobscout agent` (deterministic sweep + agent harness
   if an LLM key is set; `--dry-run` exercises the harness loop without a key).
   The 07:00 launchd agent (`bootstrap --with-agent`) may have already run it —
   check `morning_reports/$(date +%F).md` first.
2. Ensure today's pipeline ran (or run it): `jobscout run --daily` → `digest/`.
3. Read the morning report AND the digest. Summarize: what the agent added
   (candidates, signals, research notes) with its one-line rationales; top 5
   postings WITH fit rationale + red flags; dark-pool leads with their signals;
   anything closing soon; source-health anomalies.
4. Ask which postings to prepare packets for. Do NOT prepare unrequested.
5. For packet prep, follow PLAN §5.4–§5.8 exactly, ending at `needs_input`/`ready`
   with the claim-check report shown.

## Acting AS the discovery agent (interactive runtime)

The headless harness (jobscout/agent/) and you execute the SAME Morning Brief
(PLAN §1.2). When asked to research/discover interactively:
- Resolve unresolved mentions: signals with company_id NULL (RSS funding names
  without domains) — web-search the company, then `jobscout add-company` with
  the real domain.
- Deep-dive dark-pool leads: fetch careers pages/blogs/news, then write
  `research/<company-slug>.md` with sources and dates.
- Propose watchlist promotions (never apply without the owner's confirmation).
- The headless harness has the same caps discipline: be frugal, cite sources,
  never invent domains.

## Dark-pool drill-down (when asked to research a company)

Open-ended web research on the company (funding, team page changes, engineering
blog, news), then write `research/<company-slug>.md` with sources and dates, and
propose (never auto-apply) a watchlist action: add tier C / flag / discard,
with a one-line rationale. This is the one place you use your native web search
freely — the deterministic collectors handle bulk, you handle judgment.
