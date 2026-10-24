"""Hard LLM-call budgets for the loop (G5).

A cycle's cost is estimated before it starts (spec §7.3) and refused if it would exceed settings.loop.
max_calls_per_cycle or what is left of the organization's monthly quota. While it runs, every model it uses goes
through Budget.wrap, so it stops at the cap even if the estimate was wrong.
"""
import calendar
import sqlite3
import time
from contextlib import closing


class BudgetExhausted(Exception):
    pass


class _Metered:
    """An LLM whose calls are charged to a Budget (and refused once it is spent)."""

    def __init__(self, llm, budget):
        self._llm, self._budget = llm, budget

    @property
    def calls(self):
        return self._llm.calls

    def __getattr__(self, name):
        return getattr(self._llm, name)

    def text(self, *a, **k):
        self._budget.charge()
        return self._llm.text(*a, **k)

    def json(self, *a, **k):
        self._budget.charge()
        return self._llm.json(*a, **k)


class Budget:
    def __init__(self, cap):
        self.cap, self.used = cap, 0

    def charge(self, n=1):
        if self.used + n > self.cap:
            raise BudgetExhausted(f"the cycle's budget of {self.cap} LLM calls is spent")
        self.used += n

    def wrap(self, llm):
        return _Metered(llm, self)

    @property
    def left(self):
        return self.cap - self.used


def estimate(*, candidates, screen_n, dev_n, holdout_n, trials, actor_calls, judge_calls=0, operator_calls=None):
    """Spec §7.3: LLM calls one cycle can be expected to make. actor_calls is per task, judge_calls per answer."""
    per = actor_calls + judge_calls
    ops = candidates if operator_calls is None else operator_calls
    return int(round(ops                                    # operators: at most one call each
                     + candidates * screen_n * per          # stage 1: cheap screen, 1 trial
                     + 2 * dev_n * trials * per             # stage 2: two survivors on the full dev split
                     + dev_n * trials * per                 # the active config on the full dev split
                     + 2 * holdout_n * trials * per))       # stage 3: active and best on the holdout


# --- the organization's monthly quota (hosted server only) --------------------------------------------------------
def _month_start():
    t = time.gmtime()
    return calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0))  # the same UTC month the API's quota uses


def quota_left(db, org):
    """LLM calls left this month on the organization's plan; None if there is no plan to enforce (local mode)."""
    from sia.store import PLANS, Store
    with closing(sqlite3.connect(db, timeout=30)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='tenants'").fetchone():
            return None
    st = Store(db)
    t = st.tenant(org)
    if not t or t["plan"] not in PLANS:
        return None
    return max(0, PLANS[t["plan"]]["llm_calls_per_month"] - st.llm_calls_since(org, _month_start()))


def record_usage(db, org, pid, action, calls):
    """Meter the loop's calls like any other LLM-using action (hosted server only)."""
    with closing(sqlite3.connect(db, timeout=30)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='usage'").fetchone():
            return
    from sia.store import Store
    Store(db).record_usage(org, None, action, calls, project_id=pid)
