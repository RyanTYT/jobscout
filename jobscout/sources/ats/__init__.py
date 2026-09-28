"""ATS board connector registry."""

from jobscout.sources.ats import ashby, greenhouse, lever, smartrecruiters

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "smartrecruiters": smartrecruiters.fetch,
}
