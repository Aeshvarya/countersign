# Progress

Where Countersign is, what happens next, and what has already happened. **Now**, **Decisions** and **Open questions** are enough to pick up the work. The **Log** is history.

## How to use this file

1. **Start of a session:** read Now, Decisions and Open questions.
2. **End of any session that touched the repo:** add a Log entry at the top and update Now. Commit it with the work.
3. **Log output, not effort.** What shipped, what broke, what is next, who did it. Five lines at most per day.
4. **Decisions are not reopened** without new evidence. A change is a new dated line below the old one; never edit the old one.
5. **Dates are absolute:** 2026-10-10, never "next week".
6. **Never in this file:** keys, tokens, card numbers other than Airwallex test cards, or anything from an employer or another project. This repo goes public on 2026-11-11.

## Now

_Updated 2026-10-19_

- **State:** the whole product runs offline. `uvicorn countersign.web.app:app` serves the demo: a shopper asks, the agent (Claude, or the scripted stand-in without a key) searches, gets exact terms with a rate lock, pauses for approval with a ceiling in the shopper's currency, and checks out; the merchant's edge verifies every call with TAP and the countersign charges, asks again or refuses; a paid order comes with a signed receipt anyone can verify. Levers move the rate, the price, the delivery, or send bots. TAP interop with Visa's reference code is proven both ways. 111 tests pass offline. Nothing has run against Airwallex yet.
- **Next action:** Aeshvarya: sandbox keys into `.env`, then `python scripts/payment_spike.py --shopper INR` (A2 + A6 live in one run); deploy A7 to Render. Varunika: V1; read the C9 proposal below before starting V5; V3 Visa Slack ask. `varunika/tap-window-cap` waits on Aeshvarya's OK (C2 after the freeze). Both: the application text (closes Fri 10-23).
- **Waiting on:** Airwallex devhelp for merchant-side Agentic Commerce enablement and Airi CLI access (requested 2026-10-10, auto-reply only).
- **Deadline:** Fri 2026-11-13 23:59 IST. **Target submission: Thu 2026-11-12.** Plan: [WORKPLAN.md](WORKPLAN.md).

## Decisions

- **2026-10-10** Track 10, Merchant-Enabled Agentic Checkout. Access requested for Track 9 too. (Aeshvarya)
- **2026-10-10** Countersign builds on Airwallex's own agentic commerce stack rather than beside it: if the sandbox exposes the Agent Trust & Risk SDK or Token Vault, we call them instead of rebuilding them. What we add is the countersign step: the shopper's approval checked in their own currency against a fresh rate lock, plus agent identity via Visa TAP. (Aeshvarya)
- **2026-10-10** Rate locks come from Airwallex Multi-Currency Pricing (`/pa/conversion_quotes/create`, attached to the payment intent as `conversion_quote_id`), verified in the docs 2026-10-10. (Aeshvarya)
- **2026-10-10** The shopper approves a ceiling in their own currency. Rate drift that stays at or under it proceeds; any change to what they chose (items, quantity, delivery, merchant, currency) asks again, even if cheaper. (Aeshvarya)
- **2026-10-10** TAP wire format matches Visa's reference sample byte for byte (`sig2`, `"; "` separators, `keyId`, Ed25519), so interop with their verifier is testable. (Aeshvarya)
- **2026-10-10** Python ≥ 3.11. Core is the standard library plus `cryptography`; Claude, FastAPI and uvicorn are extras. Tests are offline with fakes. (Aeshvarya)
- **2026-10-10** The model never sets an amount, a limit or an outcome. (Aeshvarya)
- **2026-10-10** Repo `Aeshvarya/countersign`, private until 2026-11-11. Each builder commits as themselves. (Aeshvarya)
- **2026-10-11** The merchant signs a receipt at charge time (C8): agent identity, approval hash and ceiling, the charge at the locked rate, and the order number, under the merchant's Ed25519 key. It answers "my agent bought this without asking" offline, for the shopper, their bank or a dispute team. (Aeshvarya; new contract before the 10-18 freeze, Varunika to confirm with V1)
- **2026-10-11** The agent is a manual tool loop so it can pause for the shopper's approval between web requests. Its tools never take an amount: approval screens and checkout read the merchant's stored offer. Default model `claude-opus-5` (env `COUNTERSIGN_AGENT_MODEL`), with the API's server-side refusal fallback on. (Aeshvarya)
- **2026-10-11** A refusal at the merchant's edge is a tool result the agent reports, never an exception that ends the session. (Aeshvarya)
- **2026-10-16** An idempotency key reused under a different approval is refused; orders record the approval hash they were charged under. (Aeshvarya, from Varunika's 10-11 hand-off)
- **2026-10-17** C2 body binding and the A7 hosted demo merged after Varunika built `edge-body-cap` on them and tested all three together (105 tests). (Aeshvarya)
- **2026-10-17** **C9, proposed for V5:** with a real card, payment completes in the shopper's browser. Countersign PROCEED → `payments.start()` creates the intent in the merchant currency (order number + approval hash in it, idempotency key as Airwallex `request_id`) → the browser mounts Drop-in with `quote_id` (Drop-in applies the quote; we do not attach it server-side) → the merchant fetches the intent and `check_paid()` compares status, amount and currency, order number, approval hash and rate lock → only then is the order paid and the receipt signed. Until V5, the demo keeps `FakeMerchant`'s instant payment. Varunika to confirm or counter before V5 starts. (Aeshvarya)
- **2026-10-16** Proposed, needs Varunika's OK before the freeze: a request with a body signs its RFC 9530 `Content-Digest` (covered component `content-digest`), and the edge refuses a payment that does not. Requests without a body keep Visa's exact two-component format, so A3 interop is unchanged (re-run 10-16: passes). Branch `aeshvarya/c2-content-digest`. (Aeshvarya, answering Varunika's 10-11 question)

## Open questions

- **Work split:** proposed in WORKPLAN.md. Varunika confirms or counters by Mon 2026-10-12.
- **Application fields** for the 10-23 close: copy them from the HackerEarth Submissions tab into this section.
- **Is INR a convertible shopper currency** on our sandbox? A2 answers it.
- **Does Airwallex round the shopper amount half-up?** A2 compares their payment intent amount with `computed_shopper_total()`. Their MCP docs (read 2026-10-19) say half to even, on the cart total; A2's live run confirms it.
- **Live Visa TAP key directory or VIC sandbox for teams?** V3 asks.
- **Merchant-side enablement:** when, and does it include the Agent Trust & Risk SDK?
- **Does code written before the 10-25 build start count?** The public page is silent. Asked Airwallex devhelp directly on 2026-10-16 (same thread as the access request). Still worth copying the HackerEarth rules tab here.
- **C2: should payment requests bind the body?** Answered by `aeshvarya/c2-content-digest` (see Decisions 2026-10-16); waiting on Varunika's review.

## Quick reference

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m countersign demo
.venv/bin/python -m unittest discover -s tests
```

## Log

Newest first.

### 2026-10-19
- **TAP validity cap** (Varunika), branch `varunika/tap-window-cap`: `tap.verify` refuses a signature valid for more than 480 s, the window Visa's own tap-agent signs with (`created + 8 * 60` in `agent_app.py`). The nonce cache lives in memory and empties on every restart, so a longer window is a longer replay window. 112 tests. It changes C2 after the freeze, so it merges only after Aeshvarya's OK here.
- **Airwallex MCP docs, read for C9** (Varunika): once a quote is applied, the payment intent reads back in the shopper's currency and amount, as `check_paid` assumes; a create retried with the same `request_id` returns the original intent, as `start` assumes; the shopper total is `round_half_even(merchant total × rate)`, while `terms.money` rounds half up, so on an exact tie our shown total is one minor unit above the charge (the ceiling still holds). Their rounding page also lists HUF, TWD and ISK as zero-decimal, which `_MINOR_UNITS` does not. INR and EUR are unaffected.

### 2026-10-17
- **Merged** (Aeshvarya): `aeshvarya/c2-content-digest`, `aeshvarya/a7-hosted-demo`, `varunika/edge-body-cap`, in that order; 105 tests.
- **A6 code done** (Aeshvarya): `payments.py`: `AirwallexPayments` (create, retrieve, attach_quote, `start` → Drop-in handoff) and `check_paid` (every field or no); `scripts/payment_spike.py` runs A2 + A6 against the sandbox in one command (real rate lock, real intent, Drop-in with the test card, server-side check, our rounding vs theirs). Waits on sandbox keys.
- **A5** (Aeshvarya): a +1% rate nudge next to +5%, so the demo shows a move under the ceiling and one over it.

### 2026-10-17
- **Edge body cap** (Varunika), branch `varunika/edge-body-cap`, stacked on `aeshvarya/c2-content-digest`: with C2 the edge reads every POST body before the TAP check, so an unsigned caller could make it hold a body of any size. Now a body over 64 KiB is refused with 413 before it is read, and a chunked body with no `Content-Length` with 411. 105 tests pass, and still 105 with `a7-hosted-demo` merged on top. Touches only `edge/`; merges after c2.

### 2026-10-16
- **Merged V2 + V3** (reviewed by Aeshvarya): `varunika/v3-registry` (with `v2-edge`) into `main`, 93 tests.
- **C6 key reuse** (Aeshvarya, on `main`): a reused key under another approval is refused instead of reported as paid. Varunika's `SlowMerchant` test double updated for the new `pay(..., approval_hash=)` argument.
- **C2 body binding** (Aeshvarya, branch `aeshvarya/c2-content-digest`, for review): `Content-Digest` signed on requests with a body; the edge requires it on payments; a swapped approval is refused at the edge. Interop with Visa's reference code re-run: passes (the script now runs Visa's proxy on a free upstream port).
- **A7 ready to deploy** (Aeshvarya, branch `aeshvarya/a7-hosted-demo`, stacked on c2): every demo visitor gets their own merchant behind the real `edge` app; `InProcessShop` sends real HTTP requests through it without a socket; the bots lever fires five real attacks (unsigned, stranger's key, browse tag paying, approval swapped in transit, replay). Dockerfile, Render blueprint, `/healthz`, keep-warm ping. Container built and driven end to end locally.

### 2026-10-13
- **V3 code** (Varunika), branch `varunika/v3-registry`, stacked on V2: `tap.RegistryDirectory` reads agent keys from a registry with the API of Visa's reference agent-registry. Visa's sample data gives two agents a key called `primary` and its `GET /keys/{key_id}` ignores deactivated agents, so keys are resolved per agent from `GET /agents`, falling back to `/keys/{key_id}` bound to `agent_domain`. Cached 60 s; an unreachable registry is a refusal with its own reason. Found and fixed: the edge checked TAP on its event loop, so one slow key lookup held other agents' requests (0.93 s in a test); now off the loop, nonce cache locked. Not yet run against Visa's real registry service.

### 2026-10-11
- **V2 done** (Varunika), branch `varunika/v2-edge`: `edge/` serves C7 behind a TAP check on every route, verified against the merchant's own host, not the caller's `Host`. Browse vs payer tags per route; a broken approval is a REFUSE, not a 500. Found and fixed: two simultaneous checkouts under one key both reached `pay()` (now serialised per key). Orders and receipts are shown only to the agent that placed them. `HttpShop` passes the four agent flows against a live edge. Hand-off (C6, `checkout.py`): a key reused with a different approval still returns the old order.
- **A3 done** (Aeshvarya): `scripts/tap_interop.py` runs Visa's own cdn-proxy against our signatures (accepted; replay, wrong page and unsigned refused) and Visa's own tap-agent signer against our `verify` (accepted). A fixed Ed25519 vector now guards the wire format.
- **C8 receipts** (Aeshvarya): `receipt.py`; `countersign()` signs one on PROCEED and returns the same one on retry; `python -m countersign verify-receipt`; the demo ends with a dispute settled by the receipt.
- **A4 offline** (Aeshvarya): `agent/`: tool loop with the approval pause, strict tool schemas, `LocalShop` (in-process merchant behind a TAP-verifying edge) and `HttpShop` (C7). Found and fixed: an edge refusal during search crashed the session.
- **A5 v1** (Aeshvarya): the demo app (FastAPI + one page). `/?q=...` starts a session, `/?s=<id>` attaches a second screen to it.

### 2026-10-10
- **Repo created, A1 done** (Aeshvarya): contracts C1–C7 in code with tests (terms + hash, TAP sign/verify, merchant interface + 100-product synthetic catalog, rate locks, the decision, the countersign step, JSON wire format), an Airwallex REST client, `python -m countersign demo | keygen | fx`. 35 tests, offline.
- Access requests for merchant-side Agentic Commerce (Track 10) and Airi CLI (Track 9) sent to Airwallex devhelp.
- WORKPLAN proposed; waiting on Varunika (V1).
