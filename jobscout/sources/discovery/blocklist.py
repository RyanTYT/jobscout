"""Aggregator / non-company domains — never become candidates (P4)."""

BLOCKED_DOMAINS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "jooble.org", "adzuna.com", "builtin.com", "lever.co", "greenhouse.io",
    "ashbyhq.com", "smartrecruiters.com", "workable.com", "wellfound.com",
    "workatastartup.com", "ycombinator.com", "news.ycombinator.com",
    "github.com", "gitlab.com", "medium.com", "substack.com", "twitter.com",
    "x.com", "reddit.com", "youtube.com", "facebook.com", "wikipedia.org",
    "google.com", "apple.com", "microsoft.com", "amazon.com", "stackoverflow.com",
    "wikidata.org", "crunchbase.com", "pitchbook.com", "techcrunch.com",
    "coindesk.com", "venturebeat.com", "arstechnica.com", "wired.com",
)


def is_blocked(domain: str) -> bool:
    d = (domain or "").lower().removeprefix("www.").rstrip(".")
    return any(d == b or d.endswith("." + b) for b in BLOCKED_DOMAINS)
