"""The self-improving loop (SELF_IMPROVING_SPEC.md): an agent that does work, measures it with signals it can't fake,
learns, changes its own configuration, and keeps a change only when it beats the current one on held-out tests and
survives a live canary. Server-side code with no FastAPI import; it reuses the DevStore tables and release gate."""
