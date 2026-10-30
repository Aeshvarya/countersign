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

_Updated 2026-10-30 (evening)_

- **2026-10-30, Aeshvarya's code side is unblocked:** A7's host code is merged: one service serves the demo, `/acp/checkout_sessions`, `/p/<sku>`, `/img/<sku>.svg`, the TAP door, the merchant's receipt key and `/webhooks/airwallex`. V5′ `AirwallexMerchant` is merged offline (pending orders, confirm from Airwallex's record, refund on mismatch, receipt signed only after `check_paid`). Half-even rounding waits on Varunika's review (`aeshvarya/c9-half-even`, touches her GMC test). **What is left needs Aeshvarya's hands, not code:** A12 sandbox account (SG or UK) and keys, then the Render deploy with the variables in `render.yaml`, then `ac_onboard.py --check-host`. Varunika: review c9-half-even; V10 live once the host is up; U1's C13 shape by 11-02; V8.

- **Build phase is on (10-27 → 11-13).** Airwallex published the Merchant Onboarding API: Airwallex hosts the merchant MCP, and our service is the merchant's `create_checkout`. **Work from the [Build phase board](WORKPLAN.md#build-phase-board-from-2026-10-27); it replaces Next action below.** Varunika: V4′ (GMC catalogue) and V10 (checkout callback) need no keys, so start there. Aeshvarya: A11 Kit 10 questions to Airwallex in the hackathon Slack, A12 sandbox keys + onboard probe, A7 Render.
- **State (10-19):** the whole product runs offline. `uvicorn countersign.web.app:app` serves the demo: a shopper asks, the agent (Claude, or the scripted stand-in without a key) searches, gets exact terms with a rate lock, pauses for approval with a ceiling in the shopper's currency, and checks out; the merchant's edge verifies every call with TAP and the countersign charges, asks again or refuses; a paid order comes with a signed receipt anyone can verify. Levers move the rate, the price, the delivery, or send bots. TAP interop with Visa's reference code is proven both ways. 111 tests pass offline. Nothing has run against Airwallex yet.
- **Next action:** Aeshvarya: sandbox keys into `.env`, then `python scripts/payment_spike.py --shopper INR` (A2 + A6 live in one run); deploy A7 to Render. Varunika: V1; read the C9 proposal below before starting V5; V3 Visa Slack ask. Aeshvarya: the TAP validity cap is merged (see Decisions 2026-10-19); object there if it should come out. Both: the application text (closes Fri 10-23).
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
- **2026-10-19** C2: `tap.verify` refuses a signature valid for more than 480 s (`tap.MAX_WINDOW`, Visa's tap-agent window). Merged by Varunika after the freeze without Aeshvarya's OK first; he can object here and it comes back out. (Varunika)
- **2026-10-29** Build-phase split, agreed by both. Aeshvarya: A12 sandbox + keys + onboarding probe, A2/A6 live run, A7 Render (cold start + Claude spend cap), A11 Slack follow-ups, V5′ `AirwallexMerchant` (moved from Varunika), A15 console, A9 video, A10 audits + submission, the U1 approval page, and the fallback MCP if triggered. Varunika: V4′ catalogue, V10 checkout callback (offline, then live), U1 verifier, V6 change events, V8, V7, V9, U3 dispute ending, A13 Claude configuration + `docs/RUNBOOK.md`, A16 declined path, U6 directory (spare time only). V1 closed. (Varunika, recording the agreement)
- **2026-10-29** C9 confirmed with five additions, built in V5′: fulfil from the `payment_intent.succeeded` webhook verified with Airwallex's signature; a refund path when `check_paid` fails after a charge; `find_order` sees started-but-unpaid intents; the receipt is signed after `check_paid` (C6/C8 change, agreed here); `terms.money` rounds half-even like Airwallex. (both)
- **2026-10-29** U1 agreed: the shopper approves the exact terms with a passkey. New contract C13: an optional `shopper_assertion` on `Approval` and in the receipt, additive, so old receipts still verify. Verifier in `edge/` (Varunika), approval page in `web/` (Aeshvarya). (both)
- **2026-10-29** C12 dropped. On the merchant-MCP door the caller is Airwallex (HMAC, C10) and the shopper's passkey carries the intent; TAP stays on the direct door and on our fallback door. It comes back only if Airwallex confirms it forwards extension fields to `create_checkout`. (both)
- **2026-10-29** Fallback rule: if Kit 10 onboarding is not live on our account by Sun 2026-11-01 20:00 IST, we build our own ACP-shaped merchant MCP over the same `/acp` routes as insurance. We submit through Airwallex's merchant MCP if it works on our account by 2026-11-09, otherwise through ours, labelled as ours in the README and video. (both)
- **2026-10-29** Submission: one entry, the shortlisted idea. Repo goes public at submission. Video unlisted on Aeshvarya's channel; final render starts 2026-11-10. Submit Thu 2026-11-12. (both)
- **2026-10-29** The 2026-10-19 TAP validity cap stays. (Aeshvarya)
- **2026-10-29** Sandbox registered in Singapore or the UK: India is not a supported business location for Airwallex card acceptance (airwallex.com/docs/payments/payment-methods). (both)
- **2026-10-29** C10, proposed for Aeshvarya's OK (freezes Sun 2026-11-02): the checkout API Airwallex calls is mounted at `/acp/checkout_sessions` (create; `/{id}` read and update; `/{id}/complete`; `/{id}/cancel`), shaped on ACP 2026-04-17, all ACP JSON in `edge/acp.py`. Authenticated by the shared HMAC secret, one scheme by config, default Airwallex's webhook convention (`x-timestamp` ms, `x-signature` hex over timestamp + raw body). Idempotency per ACP: replay with `Idempotent-Replayed: true`, 422 `idempotency_conflict`, 409 `idempotency_in_flight` with `Retry-After`, 5xx never stored. Amounts in integer minor units; the shopper's ceiling is our extension field `ceiling`. Completing runs `countersign()` under the merchant idempotency key `acp:<session id>`. (Varunika)

## Open questions

- **Is Kit 10 onboarding on our account?** (2026-10-30) Sandbox maintenance ended 2026-10-30 01:52 IST (#announcements); no Kit 10 onboarding announcement yet and no answer to our 2026-10-29 post. Once keys exist, `scripts/ac_onboard.py` answers it in one call: route 404 = not on the account, 404 `MERCHANT_CONFIG_NOT_FOUND` = live and ready to onboard. Per the onboarding docs `managed_payment` is a response field, not something we send.
- ~~**Nothing serves the edge with the ACP door yet**~~ Answered 2026-10-30: `web/host.py` serves `/acp`, `/p`, `/img` and the TAP door from the deployed service (A7).

- ~~**The public host must serve the catalogue pages**~~ Answered 2026-10-30, same change.
- **How does a pending card payment look through the ACP door?** (2026-10-30) With `COUNTERSIGN_MERCHANT=airwallex`, `complete` gets a pending order with no receipt yet, and `acp.py` reports the session `completed`. Depends on what `managed_payment: true` sends on complete; Varunika's call once the contract is known.
- **Does Airwallex accept SVG for `image_link`?** Google Merchant Center itself wants JPEG, PNG, GIF, WebP, BMP or TIFF. If the live upload fails rows on the image, render PNGs instead.
- **Does Airwallex accept GMC fields beyond its example** (brand, condition, product_type, identifier_exists)? Sent only with `--extended` until confirmed.

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

### 2026-10-30
- **A7 host merged** (Aeshvarya, `601f935`): `web/host.py` puts one long-lived house merchant behind the real edge inside the deployed service, so Airwallex's `create_checkout` (`/acp`), the catalogue links (`/p`, `/img`) and the TAP door all answer on the public URL; the demo keeps every other path. Receipts are signed with `COUNTERSIGN_MERCHANT_SIGNING_KEY` and the public key is at `/.well-known/countersign-merchant-key`. Smoke-tested under uvicorn: every door answers as designed. `render.yaml` lists every variable.
- **V5′ merged offline** (Aeshvarya, `467073a`): `AirwallexMerchant` returns a pending order with the intent started and stock held; `confirm()` fetches the intent (never trusts the event), paid only if `check_paid` passes, else a full refund and the stock back. C6: the receipt body is held until then and `release()` signs it; a retry under a failed or refunded key is refused. `/webhooks/airwallex` on the host verifies the event and settles. `COUNTERSIGN_MERCHANT=airwallex` switches the house merchant. 247 tests.
- **Half-even rounding** (Aeshvarya, branch `aeshvarya/c9-half-even`, for Varunika's review): `terms.money` rounds like Airwallex; HUF, TWD and ISK are zero-decimal. Flips one expectation in `test_catalog_gmc.py`.
- Three lines reworded so the local pre-push word check passes on main (two nonce-cache comments, one line in Now).
- **V10 live, part 1:** `scripts/ac_onboard.py` merged (`bb9144b`): probe, host check (an unsigned call must be refused by the `/acp` door, else nothing is sent), one-time onboard, no secret ever printed. 220 tests. The live run itself is blocked on A12 keys, A7 serving `/acp`, and Kit 10 onboarding. (Varunika)

### 2026-10-29
- **Build-phase split agreed** (both): owners, C9 additions, U1 and C13, C12 dropped, the 11-01 fallback rule and the submission plan, all in Decisions. Hackathon Slack read in full; questions posted in `#help` and `#commerce-kits-9-and-10`. (Varunika)
- **CI on main was red since about 10-26; fixed** (Varunika): `MemoryNonceCache` pruned on the wall clock while `tap.verify` used the clock it was given, so once the real date passed the fixed test time the replay test let a replay through. Live replays were still refused (one clock in production). The cache now takes verify's clock. 114 tests.
- **V4′ done offline** (Varunika): `catalog_gmc.py` maps the 100 SKUs to GMC rows (Airwallex's documented 8 fields; 4 more behind `--extended`), the upload packs whole products per call because an upsert replaces a product with all its variants, `scripts/upload_catalog.py` (dry run without keys, exit 1 on any failed row), and public `/p/<sku>` and `/img/<sku>.svg` pages at the edge, the only routes without TAP. 151 tests. Live upload waits on sandbox keys and Kit 10 onboarding.
- **V10 done offline** (Varunika): `edge/acp.py` is the ACP checkout API for `create_checkout`: sessions, a fresh rate lock and `decide()` at complete, REASK as `price_change` / `terms_changed` / `expired` warnings with nothing charged, `out_of_stock`, the U1 gate (`pending_approval`, `approval_required`), and U4 observe mode (header names and body keys only). `edge/hmac_auth.py` and `edge/idempotency.py` hold the HMAC check and ACP idempotency rules. 199 tests. C10 proposed in Decisions.

### 2026-10-27
- **Build phase planned** (Aeshvarya): Kit 10 guide and the Merchant Onboarding API read again. Countersign moves behind Airwallex's merchant MCP as the merchant's `create_checkout` service. New board in WORKPLAN: R1–R9 checklist, phases, tasks, contracts C10–C12 (freeze 11-02), fallback decision on 11-01.

### 2026-10-19
- **TAP validity cap** (Varunika), branch `varunika/tap-window-cap`: `tap.verify` refuses a signature valid for more than 480 s, the window Visa's own tap-agent signs with (`created + 8 * 60` in `agent_app.py`). The nonce cache lives in memory and empties on every restart, so a longer window is a longer replay window. 112 tests. It changes C2 after the freeze; merged by Varunika the same day, before Aeshvarya's OK (see Decisions).
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
