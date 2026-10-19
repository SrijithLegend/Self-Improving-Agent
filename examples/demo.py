"""End-to-end demo on a real (free) model: watch an agent get better at a domain.

    python examples/demo.py          # uses GENAI_API_KEY from .env (free Gemini key)

The policy is deliberately fiddly (proration, fee floor/cap, overriding exceptions, finance approval,
escalation contacts, a mandatory reply format) so the baseline has room to improve.
"""
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sia.agent import Agent  # noqa: E402
from sia.llm import LLM  # noqa: E402
from sia.store import Store  # noqa: E402

load_dotenv()
POLICY = """Acme Cloud refund policy (v4, 2026). Apply every rule; section 3 overrides sections 1-2.

1. Monthly plans. Full refund only if requested within 7 days of the charge AND usage in that period is under
20 GB. Otherwise no refund. Monthly plans are never prorated.

2. Annual plans. Count days since purchase (purchase day = day 0).
- Day 0-30: full refund of the price paid.
- Day 31-182: prorated cash refund. Prorated amount = price x (365 - days since purchase) / 365, minus the
early-termination fee.
- Day 183 or later: no cash refund. The prorated amount (same formula, no fee) is issued as account credit.
Early-termination fee: 20% of the prorated amount, but at least $50 and at most $250. The fee is waived for
customers who have been with Acme for 3 years or more. Round only the final amount, down to the cent.

3. Exceptions.
- EU customers on monthly plans: full refund within 14 days of the charge regardless of usage (statutory
withdrawal right).
- Accounts suspended for abuse (spam, crypto mining, fraud) get no refund and no credit under any plan or region.
This overrides every other rule, including the EU right.
- Any refund over $1,000 (card, PayPal or credit note; not account credit) needs finance approval: say it is
pending finance approval and add 3 business days to the payout timeline.

4. Payout. Card or PayPal: back to the original payment method in 5-7 business days. Invoice customers: a credit
note within 10 business days. Account credit: applied to the account immediately.

5. Escalation. Whenever the customer does not get a full refund, give the appeal contact
billing-appeals@acme.example - except abuse cases, which must be given trust@acme.example instead.

6. Reply format (mandatory). Every reply starts with exactly these five lines, then at most two sentences:
DECISION: FULL REFUND | PARTIAL REFUND | ACCOUNT CREDIT | NO REFUND
AMOUNT: $<amount with cents>
CALCULATION: <the arithmetic, or n/a>
PAYOUT: <method and timeline>
ESCALATION: <email, or none>"""

# Hand-checked expected answers; each needs several rules at once.
EXAMPLES = [
    ("Annual plan, $3,650, bought 50 days ago. I've been a customer for 1 year and paid by card. "
     "I want to cancel - what do I get back?",
     "DECISION: PARTIAL REFUND\nAMOUNT: $2900.00\nCALCULATION: 3650 x 315/365 = 3150.00; fee 20% = 630, capped "
     "at 250; 3150.00 - 250 = 2900.00\nPAYOUT: original card, pending finance approval, 8-10 business days\n"
     "ESCALATION: billing-appeals@acme.example"),
    ("We bought the annual plan for $730 exactly 200 days ago and want out. We've been with Acme 4 years. Refund?",
     "DECISION: ACCOUNT CREDIT\nAMOUNT: $330.00\nCALCULATION: day 200 is past day 182, so credit with no fee: "
     "730 x 165/365 = 330.00\nPAYOUT: account credit, applied immediately\nESCALATION: billing-appeals@acme.example"),
    ("I'm in Germany on the $49 monthly plan. Charged 10 days ago and I've used 45 GB. Can I get a refund?",
     "DECISION: FULL REFUND\nAMOUNT: $49.00\nCALCULATION: n/a - EU 14-day withdrawal right overrides the usage "
     "limit\nPAYOUT: original payment method, 5-7 business days\nESCALATION: none"),
    ("Our company pays by invoice. Annual plan, $1,825, purchased 40 days ago; we've been customers for 5 years. "
     "Please cancel and refund.",
     "DECISION: PARTIAL REFUND\nAMOUNT: $1625.00\nCALCULATION: 1825 x 325/365 = 1625.00; fee waived (5+ years)\n"
     "PAYOUT: credit note, pending finance approval, within 13 business days\nESCALATION: billing-appeals@acme.example"),
    ("My account was suspended for crypto mining. I'm in France, monthly plan charged 3 days ago, barely used it. "
     "I want my money back.",
     "DECISION: NO REFUND\nAMOUNT: $0.00\nCALCULATION: n/a - abuse suspension overrides every rule, including the "
     "EU right\nPAYOUT: none\nESCALATION: trust@acme.example"),
    ("Annual plan, $365, bought 150 days ago. Customer for 1 year, paid with PayPal. How much is my refund?",
     "DECISION: PARTIAL REFUND\nAMOUNT: $165.00\nCALCULATION: 365 x 215/365 = 215.00; fee 20% = 43, raised to the "
     "$50 minimum; 215.00 - 50 = 165.00\nPAYOUT: PayPal, 5-7 business days\nESCALATION: billing-appeals@acme.example"),
]
TASKS = [
    "Annual plan, $2,190, bought 90 days ago. I've been with you 2 years, paid by card. Cancel and refund please.",
    "I'm in France, monthly plan $29, charged 5 days ago, used 25 GB. Refund?",
    "I'm in Texas, monthly plan $29, charged 5 days ago, used 25 GB. Refund?",
    "We pay by invoice. Annual plan $1,095 bought 250 days ago, want to cancel. What do we get?",
    "Our account got suspended for sending spam. Annual plan bought 10 days ago - refund?",
    "Annual plan $1,460 bought 20 days ago, paid by card. I changed my mind, refund please.",
]

db = Path(tempfile.mkdtemp()) / "demo.db"
store = Store(str(db))
llm = LLM("gemini", os.environ["GENAI_API_KEY"], os.getenv("DEMO_MODEL"))
record = store.create_agent("demo", "Acme support", "customer support for Acme Cloud billing and refunds",
                            "Answer customers directly and briefly. Apply the policy exactly.")
agent = Agent(store, llm, record)
store.add_doc(record["id"], "Refund policy", POLICY)
for q, a in EXAMPLES:
    store.add_example(record["id"], q, a, "user")

print(f"model: {llm.model}")
print(f"baseline eval score (no lessons, no playbook): {agent.evaluate('', store.examples(record['id'])):.2f}")

for task in TASKS:
    r = agent.run(task)
    print(f"\nTASK: {task}\n  score {r['score']} after {r['attempts']} attempt(s); new lessons: {r['new_lessons']}")

s = agent.study(rounds=3)
print(f"\nstudied {s['studied']} self-generated questions: {[x['score'] for x in s['results']]}")

# Eval on the hand-written examples only (user examples sort first); study's synthetic ones are unverified.
imp = agent.improve(max_examples=len(EXAMPLES))
print(f"\nimprove (both sides use the learned lessons): {imp['old_score']:.2f} -> {imp['new_score']:.2f}  "
      f"adopted={imp['adopted']}")
print("playbook:\n" + imp["playbook"])
print("\nlessons:", *[f"  ({l['utility']}) {l['text']}" for l in store.lessons(record["id"])], sep="\n")
print(f"\nstats: {store.stats(record['id'])}\nLLM calls: {llm.calls}")
