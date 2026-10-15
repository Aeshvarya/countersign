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

_Updated 2026-10-11_

- **State:** the whole product runs offline. `uvicorn countersign.web.app:app` serves the demo: a shopper asks, the agent (Claude, or the scripted stand-in without a key) searches, gets exact terms with a rate lock, pauses for approval with a ceiling in the shopper's currency, and checks out; the merchant's edge verifies every call with TAP and the countersign charges, asks again or refuses; a paid order comes with a signed receipt anyone can verify. Levers move the rate, the price, the delivery, or send bots. TAP interop with Visa's reference code is proven both ways. 55 tests pass offline. Nothing has run against Airwallex yet.
- **Next action:** Varunika: V1 (was due Mon 10-12); merge `varunika/v2-edge`, then `varunika/v3-registry` (CI green, 93 tests; `main` has 55); V3 ask in the Visa Slack Wed 10-14. Aeshvarya: sandbox keys into `.env`, then A2; the edge is ready for A7 to serve.
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

## Open questions

- **Work split:** proposed in WORKPLAN.md. Varunika confirms or counters by Mon 2026-10-12.
- **Application fields** for the 10-23 close: copy them from the HackerEarth Submissions tab into this section.
- **Is INR a convertible shopper currency** on our sandbox? A2 answers it.
- **Does Airwallex round the shopper amount half-up?** A2 compares their payment intent amount with `computed_shopper_total()`.
- **Live Visa TAP key directory or VIC sandbox for teams?** V3 asks.
- **Merchant-side enablement:** when, and does it include the Agent Trust & Risk SDK?
- **Does code written before the 10-25 build start count?** Most of the product was built 10-10 to 10-11. The rules tab is behind the HackerEarth login; whoever is logged in, copy the exact rule here.
- **C2: should payment requests bind the body?** The `/checkout` signature covers `@authority` and `@path` only, so the approval it carries is not bound to the agent's signature. Airwallex's starter Kit 10 (`everyai-com/airwallex-hackathon`, `src/core/tap.ts`) covers an RFC 9421 `content-digest` on payment requests. Decide before the 10-18 freeze; interop with Visa's reference verifier (A3) would need re-checking.

## Quick reference

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m countersign demo
.venv/bin/python -m unittest discover -s tests
```

## Log

Newest first.

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
