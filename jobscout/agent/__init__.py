"""The agent layer (P5): Morning Brief harness riding on the deterministic
collectors. Headless tool loop — the model decides what to search; the tools
mutate state only through core functions (PLAN §1.2, §5.9, §10 P5)."""

from jobscout.agent.harness import FakeAgentModel, run_morning  # noqa: F401
