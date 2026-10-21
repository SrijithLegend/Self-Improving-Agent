# Razorpay test-mode plan

A checklist to run **once, end to end, on the deployed HTTPS URL with test-mode keys** (`rzp_test_...`) before
switching to live keys. Nothing here moves real money.

Test card numbers, UPI ids and netbanking details are in Razorpay's own docs. Use those, not values copied here:

- Test cards: <https://razorpay.com/docs/payments/payments/test-card-details/>
- Test UPI: <https://razorpay.com/docs/payments/payments/test-upi-details/>
- Webhooks (setup, test events, retries): <https://razorpay.com/docs/webhooks/>

## Setup

1. In the Razorpay Dashboard, switch to **Test Mode**, create API keys, and set them on the server:
   `RAZORPAY_KEY_ID=rzp_test_...`, `RAZORPAY_KEY_SECRET=...`.
2. Add a webhook: URL `https://YOUR-HOST/v1/billing/webhook`, events **`order.paid`** and **`payment.captured`**,
   and a secret. Put the same secret in `RAZORPAY_WEBHOOK_SECRET`. Restart the server.
3. `sia doctor` should show `WARN Razorpay keys ... test mode` and `PASS RAZORPAY_WEBHOOK_SECRET`.
4. Create a test organization and sign in to `/dashboard` with its key:
   `sia create-org "Razorpay test"` (note the `tenant_id` and `api_key`).
5. Keep a shell on the server to read the database. Replace `ORG_ID`:
   ```bash
   # the organization's plan and paid_until (paid_until as a date)
   python -c "import sqlite3,datetime as d; r=sqlite3.connect('/data/sia.db').execute(\"SELECT plan, paid_until FROM tenants WHERE id='ORG_ID'\").fetchone(); print(r[0], r[1] and d.datetime.utcfromtimestamp(r[1]))"
   # its orders, newest first
   python -c "import sqlite3; [print(r) for r in sqlite3.connect('/data/sia.db').execute(\"SELECT order_id, plan, amount, status, payment_id FROM payments WHERE org_id='ORG_ID' ORDER BY created DESC\")]"
   ```
   Call these **PLAN** and **ORDERS** below.

Record the result of each row (pass / fail, date, order id) in the last column.

## Checklist

| # | Case | Steps | Expected API response | Expected DB state | Expected dashboard | Result |
|---|---|---|---|---|---|---|
| 1 | Successful card payment | On Free: **Plans & billing → Upgrade to Pro**, pay with a test card that succeeds | `POST /v1/billing/orders` 201 with `order_id`; `POST /v1/billing/verify` 200 `{"status": "paid", "plan": "pro", "paid_until": <now + 30 days>}` | ORDERS: the order `paid`, with a `pay_...` id. PLAN: `pro`, `paid_until` = payment time + 30 days | Toast "Payment confirmed. You're on the pro plan."; current plan Pro, "Paid through <date>"; history row `paid` | |
| 2 | UPI, webhook before the checkout callback | Pay Team (or extend) with the test UPI flow; the webhook often lands first. To force it, watch the server log: the `POST /v1/billing/webhook` line comes before `POST /v1/billing/verify` | Webhook: 200 `{"ok": true}`. Verify: 200 `{"status": "paid", ...}` (it sees the order already paid and doesn't apply it twice) | One `paid` row; PLAN extended by **30 days once**, not 60 | Same as row 1 for that plan | |
| 3 | Failed payment | Pay with Razorpay's test card or UPI id that fails | `orders` 201; no `verify` call | ORDERS: the order stays `created`. PLAN unchanged | Toast "Payment failed: ..."; plan unchanged; history row `created` | |
| 4 | Abandoned checkout | Open checkout, then close it without paying | `orders` 201 only | Order stays `created`. After 30 days `sia maintenance` deletes it (to test now: back-date it, `UPDATE payments SET created=created-31*86400 WHERE order_id='order_...'`, run `sia maintenance`, see `"unpaid_orders_deleted": 1`) | Toast "Payment window closed. Nothing was charged."; plan unchanged | |
| 5 | Duplicate webhook | Re-send the `order.paid` (or `payment.captured`) event from row 1 or 2 from the webhook's event log in the Razorpay Dashboard | 200 `{"ok": true}` | PLAN: `paid_until` **unchanged** (an order that is already paid is never applied again) | No change after a reload | |
| 6 | Webhook with a bad signature | `curl -i -X POST https://YOUR-HOST/v1/billing/webhook -H 'Content-Type: application/json' -H 'X-Razorpay-Signature: wrong' -d '{"event":"order.paid"}'` | `400 {"detail": "Invalid webhook signature."}` | No change | No change. The server log has a `sia.security` line with `"event": "invalid_webhook_signature"` and your IP | |
| 7 | Second payment for the same plan while it's active | While on Pro (from row 1): **Extend 30 days**, pay with a test card | `verify` 200 `{"status": "paid", "plan": "pro", "paid_until": <old paid_until + 30 days>}` | PLAN: `paid_until` = the **old** `paid_until` + 30 days (not now + 30) | "Paid through" moves 30 days later | |
| 8 | Paying for Team while on Pro | While on Pro: **Upgrade to Team**, pay | `verify` 200 `{"status": "paid", "plan": "team", "paid_until": <now + 30 days>}` | PLAN: `team`, `paid_until` = now + 30 days. **The rest of the Pro period is lost** (no proration yet: decide later whether to prorate or credit it) | Current plan Team | |
| 9 | Switch to live keys | After KYC: set `rzp_live_...` keys and a live webhook with its own secret, restart | — | — | `sia doctor` shows `PASS Razorpay keys live keys`, no Razorpay warning | |

## After the run

- Refund the test payments from the Razorpay Dashboard (test mode) to check the refund flow and its timing matches
  `web/refund.html`.
- Delete the test organization's data, or keep it on a staging server only.
- If any row failed, note the request id (`X-Request-ID` response header, or `request_id` in the log) and open an
  issue before going live.
