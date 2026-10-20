"""SIA DevTools: turn AI-agent failures into tested improvements.

    from sia_devtools import SIAClient
    sia = SIAClient(project="my-agent")            # local mode; pass api_key=... for the hosted API
"""
from .client import Dataset, Prompt, Run, SIAClient, __version__
from .errors import (AuthError, Conflict, NotFound, PayloadTooLarge, PermissionDenied, QuotaExceeded, RateLimited,
                     ServerError, SIAError, ValidationError)
from .scoring import Scorer, compare_scores, contains, exact_match, gate, json_match, llm_judge, regex, scorer

__all__ = ["SIAClient", "Run", "Dataset", "Prompt", "Scorer", "scorer", "exact_match", "contains", "json_match",
           "regex", "llm_judge", "compare_scores", "gate", "SIAError", "ValidationError", "AuthError",
           "PermissionDenied", "NotFound", "Conflict", "QuotaExceeded", "RateLimited", "PayloadTooLarge",
           "ServerError", "__version__"]
