# Work plan

Who does what, by when, through submission on **Thu 2026-11-12** (deadline Fri 2026-11-13, 23:59 IST).

Every task is written so either builder can pick it up cold: why it exists, what it owns, what it needs first, and the test for done. Each builder has core engine work, an Airwallex integration, a Visa piece, writing of their own, and half the video.

**Status: proposed by Aeshvarya 2026-10-10. Varunika confirms or counters by Mon 2026-10-12** (V1).

**Build phase (from 2026-10-27): start at the Build phase board below.** Airwallex published the Merchant Onboarding API, and it changes where Countersign sits. The original board stays as the record.

## Build phase board (from 2026-10-27)

### What changed

Airwallex's Merchant Onboarding API ([airwallexdev.com/agentic-commerce](https://airwallexdev.com/agentic-commerce/), read 2026-10-27):

- The merchant onboards once (`POST /api/v1/pa/agentic_commerce/onboard`) with its PSP keys, a **`create_checkout` HTTPS URL** and an **HMAC-SHA256 secret**.
- Airwallex then hosts a merchant MCP per merchant (`/pa/ai/<merchantId>/acp/mcp`, also `ucp`) with `managed_payment: true`.
- Products go up as **Google Merchant Center JSON** (`POST /api/v1/pa/agentic_commerce/products`, at most 300 per call, `"price": "51.99 USD"`).
- Claude connects with `claude mcp add --transport http agentic-commerce https://api.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp` and a bearer token.

**So Airwallex calls our `create_checkout` when an agent checks out, and that call is the countersign.** Countersign becomes the merchant's checkout service behind Airwallex's own merchant MCP. The C7 API stays as the direct, TAP-only door.

```
Claude ⇄ Airwallex merchant MCP (our catalogue) → create_checkout (HMAC) → Countersign service
   verify HMAC → verify agent (TAP envelope, C12) → live terms + fresh rate lock → decide
   → PROCEED: Airwallex payment (C9) → order number + signed receipt (C8)
   → REASK: ACP-style message, Claude asks the shopper again → REFUSE: nothing charged
```

Kit 10 checklist from the builder guide (the test for "done"):

| # | Requirement | 10-27 |
|---|---|---|
| R1 | Merchant-side access and a sandbox storefront | no keys, no enablement yet |
| R2 | At least 100 synthetic products uploaded in Airwallex's format | catalogue in code; GMC export not built |
| R3 | The merchant MCP configured in Claude locally; Claude searches the catalogue through it | not yet |
| R4 | Confirm merchant, product, quantity, delivery and total before payment; ask again if any of them changes | done (`decide.py`) |
| R5 | Card details only in Airwallex's hosted component; none in Claude's context or logs | written (A6), never run |
| R6 | Merchant order number returned with the payment outcome | done offline |
| R7 | One changed-price, unavailable or declined path, with no duplicate payment | price + stock offline; declined not built |
| R8 | Video: setup, Claude configuration, purchase, exception, order confirmation | storyboard to redo |
| R9 | Follow Airwallex's retry rules; stop if the first attempt can't be verified | idempotency done; adapt to their contract |

**Still unknown, asked in the hackathon Slack 10-27:** the `create_checkout` request and response, the HMAC header and signed string, the order and retry contract, test-payment and decline instructions, whether `/onboard` works on a plain sandbox account, and the MCP bearer token. **If onboarding is still blocked on Sat 11-01:** we serve an ACP-shaped merchant MCP ourselves over the same checkout service, with Airwallex payment intents and Drop-in, and the README and video say exactly which part is ours.

### Phases

| Phase | Dates | Done when |
|---|---|---|
| 0 · Unblock | Tue 10-27 → Thu 10-29 | Sandbox keys in `.env` · `payment_spike.py` prints a real lock and intent · onboard probe result recorded · Render URL live (HTTPS, needed for `create_checkout`) |
| 1 · Spine on Airwallex | Fri 10-30 → Mon 11-02 | 120 products visible through the merchant MCP in Claude Code · one purchase end to end with an order number from our service · **C10–C12 frozen Sun 11-02** · fallback decided Sat 11-01 |
| 2 · Exceptions + identity | Tue 11-03 → Fri 11-06 | Price change, out of stock and declined card each recorded with no duplicate charge · TAP across the MCP hop · merchant console live |
| 3 · Story | Sat 11-07 → **Tue 11-10 freeze** | Video v1 (11-08) · SPEC and README narrative · audit 1 (11-08) |
| 4 · Ship | Wed 11-11 → Thu 11-12 | Repo public after audit 2 · video up · **submitted Thu 11-12** |

Aeshvarya's time is light until Fri 10-30 (another deadline). V4′ and V10 need no keys because the formats are public, so they can start now.

### Tasks

| ID | Task | Owner | Due | Status |
|---|---|---|---|---|
| A11 | Airwallex liaison in the hackathon Slack: the Kit 10 asks, every follow-up | A | today, ongoing | todo |
| A12 | Sandbox account, API key into `.env`, `scripts/ac_probe.py` (read config, try onboard), result in PROGRESS | A | Wed 10-28 | todo |
| A2/A6 | Live rate lock, intent, Drop-in run (`payment_spike.py --shopper INR`) | A | Wed 10-28 | waits on A12 |
| A7 | Render deploy: the HTTPS URL that `create_checkout` points at | A | Thu 10-29 | host code merged 10-30 (`web/host.py`: `/acp`, `/p`, `/img`, TAP door, webhook); the deploy itself waits on Aeshvarya's Render account |
| V4′ | **C11 catalogue in GMC format:** `catalog_gmc.py` maps `synthetic_catalog()` to GMC JSON (120 products, some variants via `item_group_id`); `scripts/upload_catalog.py` upserts at most 300 per call and is safe to re-run. Offline tests now, live the day keys land | V | code Thu 10-29 | done 10-29 offline (merged `a73efa1`); live upload waits on keys, the public URL and onboarding |
| V10 | **C10 checkout callback:** an `edge/` route for Airwallex's `create_checkout`: HMAC-SHA256 check with a time window, ACP-shaped session (create, update, complete; `messages` for out of stock and price change), `countersign` on complete, order number and receipt back, idempotent on retry. ACP parsing in one adapter so the core never changes when Airwallex confirms the exact shape | V | offline Sun 11-01, live Tue 11-03 | done 10-29 offline (merged `06d610d`); onboarding script 10-30 (`bb9144b`); live due 11-03, blocked on keys, host, onboarding |
| A14 | **C12 TAP across the MCP hop:** the agent signs the checkout body exactly as C2 does now (Content-Digest), the signature travels in the checkout payload or headers, and the merchant runs the same `tap.verify`. Proposal in PROGRESS first | A | proposal Fri 10-30, code Thu 11-05 | todo |
| A13 | Claude configuration and run book: `claude mcp add`, the token, a purchase prompt that works every time | A | Mon 11-02 | waits on A12 |
| V5′ | `AirwallexMerchant`: C3 over the onboarding API and C9 payments; `find_order` by idempotency key | A (moved 10-29) | Wed 11-04 | done offline 10-30 (`airwallex_merchant.py`, held receipts, `/webhooks/airwallex`); live run waits on keys |
| A16 | Declined path: a declining test card means no order, no retry, Claude reports it, and no duplicate charge | A | Wed 11-04 | todo |
| V6 | Change events on the real catalogue through upsert (price, stock, delivery), driven from the console | V | Fri 11-06 | todo |
| A15 | Merchant console: `web/` shows live checkout calls, decisions and receipts; the levers call V6 | A | Fri 11-06 | todo |
| V3′ | Registry shaped like Visa's agent directory (keep the reference-registry API); ask in Slack for a Visa contact | V | Fri 11-06 | todo |
| V8 | Visual identity: palette Wed 10-28, applied to the console and receipt Fri 11-06 | V | 11-06 | todo |
| V7 ✍ | SPEC claim and README narrative | V | Mon 11-09 | todo |
| V9 ✍ | Video script and voice: outline Mon 11-02, voice Mon 11-09 | V | 11-09 | todo |
| A9 | Video production: setup, Claude configuration, purchase, exception, order confirmation, receipt, bots | A | v1 Sun 11-08, final Wed 11-11 | todo |
| A10 | Audits 11-08, 11-11, 11-12; submission | A | Thu 11-12 | todo |

New contracts: **C10** checkout callback (Varunika), **C11** GMC catalogue mapping (Varunika), **C12** TAP envelope across the MCP hop (Aeshvarya). They freeze Sun 11-02; C1–C9 stay frozen. Same rules as before: one branch per task, `--no-ff` merges, offline tests, no secrets.

**Cut order if behind:** console polish → V3′ → C12 code (keep the proposal and a test vector) → the V6 lever (run the upsert by script). **Never cut:** R1–R9, the countersign, TAP at the edge, receipts, the video.

## How to use this file

1. Read the **Board** and your own tasks. Read the other builder's only where yours depend on them.
2. Finish a task → set its Status to `done` and add a Log entry in [PROGRESS.md](PROGRESS.md).
3. Due dates are hand-off dates: the latest a task lands, not the earliest. Start as soon as what it needs is on `main`.
4. A date will slip → say so in PROGRESS.md the day you know.

## Board

| ID | Task | Type | Pts | Due | Status |
|---|---|---|---|---|---|
| A1 | Contracts C1–C7 and the offline core | tech | 6 | Sat 10-10 | done 10-10 |
| A2 | Rate-lock spike on the sandbox | tech | 2 | Mon 10-12 | script ready 10-17 (`scripts/payment_spike.py`); waits on sandbox keys |
| A3 | TAP interop with Visa's reference verifier | tech | 2 | Tue 10-13 | done 10-11: both directions pass (`scripts/tap_interop.py`), fixed vector in tests |
| A4 | The shopping agent (Claude) | tech | 6 | offline Sat 10-17, live Sun 11-01 | offline done 10-11 (scripted model, in-process edge); `HttpShop` written against C7, waits on V2; live Claude run waits on an API key |
| A5 | Shopper screens: approve and ask-again | tech | 4 | Sun 10-18, final Thu 11-05 | v1 done 10-11: `uvicorn countersign.web.app:app`; identity from V8 still to apply |
| A6 | Payment: intent + rate lock + hosted component | tech | 4 | spike Sat 10-24, done Mon 11-02 | code + tests done 10-17 (`payments.py`, C9 proposed); live run waits on keys |
| A7 | Hosted demo | tech | 3 | Fri 11-06 | merged 10-17 (Dockerfile, Render blueprint, keep-warm); live once the Render account exists |
| A8 | Application text, with V ✍ | non-tech | 1.5 | Tue 10-20 | todo |
| A9 | Video production | non-tech | 4 | Wed 11-11 | todo |
| A10 | Compliance and submission | non-tech | 1.5 | checklist Sun 11-08, submit Thu 11-12 | todo |
| V1 | Confirm or counter this split | non-tech | 0.5 | Mon 10-12 | todo |
| V2 | Merchant edge: HTTP API (C7) behind TAP | tech | 6 | Sat 10-17 | done 10-11, merged 10-16: `HttpShop` passes the agent flows against a live edge; body cap merged 10-17 |
| V3 | Visa access and the key directory | tech | 3 | Wed 10-14, registry Sat 10-24 | code done 10-13, merged 10-16: `tap.RegistryDirectory` reads Visa's reference agent-registry API; the Slack ask is still to do |
| V4 | Airwallex merchant enablement and catalog | tech | 3 | follow-up Wed 10-14, upload within 2 days of access | todo |
| V5 | `AirwallexMerchant` (C3 on the real APIs) | tech | 6 | Wed 11-04 | todo |
| V6 | Change events on the merchant side | tech | 3 | Fri 11-06 | todo |
| V7 | SPEC claim and README narrative ✍ | non-tech | 3 | Mon 11-09 | todo |
| V8 | Visual identity | non-tech | 3 | palette Sun 10-18, rest Fri 11-06 | todo |
| V9 | Video script and voice ✍ | non-tech | 3 | outline Mon 11-02, voice Mon 11-09 | todo |

V = Varunika, A = Aeshvarya. One point ≈ one focused hour; estimates for balance, not promises. Proposed totals: A 34, V 30.5 (A1 is already done, so the remaining load is A 28, V 30.5). ✍ = text read as ours: the owner's own words.

## Rules

1. **One branch per task:** `aeshvarya/a2-fx-spike`, `varunika/v2-edge`.
2. **Small commits, pushed as they happen.** The message says why. Each builder commits as themselves. No trailers.
3. **Merge with a merge commit** (`git merge --no-ff`), never a squash, so authorship survives.
4. **Self-merge** when CI is green and the branch touches only your paths (see Ownership). Touching the other's paths needs their review; no reply in 24 hours counts as approval.
5. **Contracts C1–C8 freeze Sun 2026-10-18.** Changing one after that needs the other builder's OK in PROGRESS.md first.
6. **Tests run offline:** no network, no keys. Live calls sit behind CLI flags or env, and tests use the fakes.
7. **Secrets never land** in the repo, a log, a screenshot or a recording.
8. **Feature freeze Tue 2026-11-10.** After that, only fixes.
9. **Repo private until Wed 2026-11-11**, public after the audit (A10).

## Ownership

| Path | Owner |
|---|---|
| `terms.py`, `decide.py`, `checkout.py`, `wire.py` | Aeshvarya (contracts: changes need V's OK after 10-18) |
| `tap.py` | Shared. Aeshvarya owns `sign`; Varunika owns `verify`, the directory and the nonce cache |
| `fx.py`, `airwallex.py`, `receipt.py`, `payments.py` | Aeshvarya |
| `merchant.py` | Shared. Interface and `FakeMerchant`: Aeshvarya. `AirwallexMerchant` (new file `airwallex_merchant.py`): Varunika |
| `agent/` (new) | Aeshvarya |
| `edge/` (new: the merchant HTTP API) | Varunika |
| `web/` (shopper screens + demo app) | Aeshvarya; visual identity applied by Varunika |
| `scripts/tap_interop.py` | Aeshvarya |
| `tests/` | Each builder the tests for their own modules |
| `.github/`, `Dockerfile` | Aeshvarya |
| `SPEC.md` claim, README top | Varunika |
| README status, quickstart, layout | Aeshvarya |
| `PROGRESS.md`, `WORKPLAN.md` | Both. Log entries are append-only and name who did the work |

## Contracts

The seams between the two halves. They are on `main` now, in code, with tests, so neither builder waits on the other.

| ID | What | Where | Tests |
|---|---|---|---|
| C1 | `Terms`, `LineItem`, `FxLock`, `Approval`, canonical hash over the bound fields + ceiling | `terms.py` | `test_decide.py` |
| C2 | TAP wire format (Visa reference sample, byte for byte), `sign`, `verify`, `KeyDirectory`, `NonceCache` | `tap.py` | `test_tap.py` |
| C3 | `MerchantGateway`: `search`, `offer`, `find_order`, `pay`; `Product`, `Order` (order number ≠ payment id) | `merchant.py` | `test_checkout.py` |
| C4 | `FxGateway.lock(merchant_ccy, shopper_ccy, now) → FxLock` | `fx.py` | `test_checkout.py` |
| C5 | `decide(approval, live, now) → Decision` (proceed / reask / refuse) | `decide.py` | `test_decide.py` |
| C6 | `countersign(merchant, approval, key, now) → Result`: idempotency first, then offer, decide, pay | `checkout.py` | `test_checkout.py` |
| C7 | JSON on the wire; amounts as strings; the merchant HTTP API below | `wire.py` | `test_wire.py` |
| C9 | Card payment (proposed 10-17): intent in merchant currency → Drop-in with `quote_id` → `check_paid` → paid + receipt | `payments.py` | `test_payments.py` |
| C8 | Countersigned receipt: the merchant signs agent identity + approval hash + charge + order number; `receipt.verify` checks it offline | `receipt.py` | `test_receipt.py` |

### C7 · Merchant HTTP API (proposed; V2 builds it, A4 calls it)

Every request carries TAP headers. A failed check returns `401 {"refused": "<reason>"}` and nothing else runs.

| Method, path | Tag | Body → response |
|---|---|---|
| `GET /products?q=&limit=` | `agent-browser-auth` | → `[product]` |
| `POST /offers` | `agent-browser-auth` | `{"cart": {"sku": qty}, "shopper_currency": "INR"}` → `terms` |
| `POST /checkout` | `agent-payer-auth` | `{"approval": approval, "idempotency_key": "..."}` → `{"decision": decision, "order": order or null, "receipt": receipt or null}` |
| `GET /orders/{idempotency_key}` | `agent-payer-auth` | → `order` or `404` |

Shapes are the `*_to_json` functions in `wire.py`; the receipt is `receipt.sign(...)`'s output. The TAP-signed path is the request path **including its query string** (Visa's proxy signs `req.url`). The agent side of this contract is already written: `agent/shop.py` `HttpShop`. V2 is done when `HttpShop` pointed at the edge passes the same flow `LocalShop` does in `tests/test_agent.py`.

## Aeshvarya's tasks

### A1 · Contracts C1–C7 and the offline core · 6 pts · done 2026-10-10
The core runs offline: `python -m countersign demo` shows both contrast pairs; 35 tests pass.

### A2 · Rate-lock spike on the sandbox · 2 pts · Mon 10-12
Why: the cross-currency claim stands on Airwallex Multi-Currency Pricing working for our pair. Needs: sandbox keys in `.env`.
Steps: `python -m countersign fx --merchant USD --shopper INR`; confirm INR is in `convertible_shopper_currencies` (if not, pick the demo pair from the list); create a payment intent in USD, attach the quote, read back `amount` and `currency`; compare with `computed_shopper_total()` rounding.
Done when: one real lock printed, and the payment intent's shopper amount equals ours to the minor unit (or `terms.money` is changed to match theirs, with a test).

### A3 · TAP interop with Visa's reference verifier · 2 pts · Tue 10-13
Why: "TAP" must mean Visa's verifier accepts us, not that we look similar. Needs: Node, `github.com/visa/trusted-agent-protocol` cloned outside this repo.
Steps: run their agent-registry and cdn-proxy; register our public key (`python -m countersign keygen`); send one signed request through their proxy; then sign with their tap-agent and verify with our `tap.verify`.
Done when: both directions pass, and a test vector (fixed key, fixed nonce, expected signature) lives in `tests/test_tap.py`.

### A4 · The shopping agent · 6 pts · offline Sat 10-17, live Sun 11-01
Why: the agent is how a shopper meets Countersign. Needs: C2, C7; `ANTHROPIC_API_KEY` for live.
Tools the model gets: `search_products`, `get_offer`, `request_approval`, `checkout`. None of them takes an amount from the model: `request_approval` shows the code-built `Terms`; `checkout` sends the stored approval. Every HTTP call is TAP-signed (browse tag to look, payer tag to pay). On `reask`, the agent explains the change in plain words and asks; on `refuse`, it stops.
Done when: offline against `FakeMerchant` through the edge, the agent completes pair 1 A, B and C with no prompt changes between runs.

### A5 · Shopper screens · 4 pts · Sun 10-18, final Thu 11-05
The approve screen: items, delivery, merchant, the total in the shopper's currency, the rate and its expiry, and the ceiling ("up to ₹X"). The ask-again screen: old vs new for each change. One page, no framework needed.

### A6 · Payment · 4 pts · spike Sat 10-24, done Mon 11-02
Payment intent in the merchant currency → attach `conversion_quote_id` → confirm through the hosted component with test card `4035501000000008` → read the result. Check `find_order` before any retry.

### A7 · Hosted demo · 3 pts · Fri 11-06
One service, same origin: the edge serves the shopper screens and the API, so no base URL or CORS. A keep-warm ping. Done when it runs from a phone on another network.

### A8 · Application text ✍ · 1.5 pts · Tue 10-20 (with Varunika)
### A9 · Video production · 4 pts · Wed 11-11 (under five minutes)
### A10 · Compliance and submission · 1.5 pts · checklist Sun 11-08, submit Thu 11-12
Working demo link, video link, repo link with setup instructions; repo public; links opened from another network.

## Varunika's tasks

### V1 · Confirm or counter this split · 0.5 pt · Mon 10-12
Edit this file: accept, or change specific tasks. Add a Decisions line in PROGRESS.md.

### V2 · Merchant edge · 6 pts · Sat 10-17
Why: the merchant is where the countersign happens. Needs: C2, C3, C6, C7 (all on `main`).
Build `edge/`: the C7 API (FastAPI) over any `MerchantGateway` (start with `FakeMerchant`); TAP check as middleware before every route, `agent-payer-auth` required on `/checkout` and `/orders`; refusals as `401` with the reason; `/checkout` calls `checkout.countersign`.
Done when: tests drive the API with signed and unsigned requests, and pair 2 (signed, unsigned, replayed, wrong path) gives the four expected answers over HTTP.

### V3 · Visa access and the key directory · 3 pts · ask Wed 10-14, registry Sat 10-24
Ask in the Visa partner Slack whether teams can use a live TAP key directory or the Visa Intelligent Commerce sandbox, and how to register an agent key. Implement a `KeyDirectory` that reads it. Until then, `StaticDirectory`.

### V4 · Airwallex merchant enablement and catalog · 3 pts
Request sent 2026-10-10 (Aeshvarya's sandbox email). Follow up Wed 10-14 if nothing. When enabled: convert `synthetic_catalog()` to their catalog format and upload (≥100 products), connect the merchant MCP.

### V5 · `AirwallexMerchant` · 6 pts · Wed 11-04
C3 against the real APIs: search via the merchant MCP, offers with real prices and an `AirwallexFx` lock, `pay` through A6's payment path, `find_order` by idempotency key, and the merchant's **order number** in the result.

### V6 · Change events · 3 pts · Fri 11-06
Make price, stock and delivery changes happen on the real merchant (or its sandbox catalog) on cue, so pair 1 B and C run against Airwallex, not the fake.

### V7 · SPEC claim and README narrative ✍ · 3 pts · Mon 11-09
### V8 · Visual identity · 3 pts · palette Sun 10-18, rest Fri 11-06
### V9 · Video script and voice ✍ · 3 pts · outline Mon 11-02, voice Mon 11-09

## Timeline

| Date | Milestone |
|---|---|
| Mon 10-12 | Split agreed (V1). Real rate lock (A2) |
| Tue 10-13 | TAP interop both ways (A3) |
| Sat 10-17 | Edge API behind TAP (V2). Agent offline (A4) |
| Sun 10-18 | **Contracts frozen.** Shopper screens v1 (A5). Palette (V8) |
| Tue 10-20 | Application submitted (A8) |
| Sat 10-24 | Payment spike (A6). Visa directory if granted (V3) |
| 10-25 → 10-30 | Build period opens. Other commitments peak; keep to small tasks |
| Mon 11-02 | Payment done (A6). Video outline (V9) |
| Wed 11-04 | `AirwallexMerchant` (V5) |
| Fri 11-06 | Change events (V6). Hosted demo (A7) |
| **Tue 11-10** | **Feature freeze** |
| Wed 11-11 | Video uploaded (A9). Repo public |
| **Thu 11-12** | **Submitted** (A10) |
| Fri 11-13 | Deadline, 23:59 IST |

## Risks

| Risk | If it happens |
|---|---|
| Merchant-side enablement doesn't arrive by Wed 10-28 | Run the same countersign over `FakeMerchant` + real Airwallex payment intents and rate locks; say plainly which part is simulated |
| INR not a convertible shopper currency | Pick the demo pair from `convertible_shopper_currencies` (A2) |
| No live Visa key directory | `StaticDirectory` with our own registry, interop proven against Visa's reference verifier (A3) |
| The hosted component can't be driven by an agent | The agent hands the shopper a payment link for that one step; the countersign still runs first |

## Cut order, if behind
1. Visual polish beyond the palette. 2. V6 on the real merchant (keep it on the fake). 3. Multi-item carts. Never cut: the countersign, TAP at the edge, the order number, the video.
