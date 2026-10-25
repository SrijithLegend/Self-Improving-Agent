"""Model clients for the loop.

A request-time task can use the caller's key (X-LLM-Key), which is never stored. Background jobs (improvement
cycles, canary checks, evaluations) run later with no request, so they use a server-side key per provider:
SIA_LLM_KEY_<PROVIDER> (e.g. SIA_LLM_KEY_GEMINI, SIA_LLM_KEY_ANTHROPIC). Their calls count against the organization's
monthly llm_calls quota like any other.
"""
import os

from sia.llm import LLM
from sia_devtools.errors import ValidationError

factory = LLM  # tests swap this for a scripted model


def server_key(provider):
    """SIA_LLM_KEY_<PROVIDER>; for gemini, the older SIA_DEFAULT_LLM_KEY still works (sia doctor warns about it)."""
    return os.getenv(f"SIA_LLM_KEY_{provider.upper()}", "") or (
        os.getenv("SIA_DEFAULT_LLM_KEY", "") if provider == "gemini" else "")


def make(provider, chain, key=None):
    """An LLM for `provider` trying the models in `chain` in order. key=None uses the server-side key."""
    key = key if key is not None else server_key(provider)
    if not key and provider != "ollama":
        raise ValidationError(f"No model key for {provider}: set SIA_LLM_KEY_{provider.upper()} on the server (background "
                       "jobs can't use a key sent with a request; those are never stored).")
    return factory(provider, key, ",".join(chain) if isinstance(chain, (list, tuple)) else chain)


def for_config(cfg, key=None):
    llm = make(cfg["model"]["provider"], cfg["model"]["chain"], key)
    llm.thinking = cfg["model"].get("thinking")
    return llm
