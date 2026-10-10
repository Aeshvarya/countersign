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

_Updated 2026-10-10_

- **State:** the offline core runs end to end. `python -m countersign demo` shows both contrast pairs: same cart → proceeds when the rate moves under the ceiling, asks again past it or when delivery slips; same checkout → a TAP-signed agent is accepted, an unsigned bot, a replayed signature and a signature for another page are refused. 35 tests pass offline. Nothing has run against Airwallex yet.
- **Next action:** Varunika: V1 (confirm or counter the split, by Mon 10-12), then V2. Aeshvarya: A2 with sandbox keys, then A3.
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

## Open questions

- **Work split:** proposed in WORKPLAN.md. Varunika confirms or counters by Mon 2026-10-12.
- **Application fields** for the 10-23 close: copy them from the HackerEarth Submissions tab into this section.
- **Is INR a convertible shopper currency** on our sandbox? A2 answers it.
- **Does Airwallex round the shopper amount half-up?** A2 compares their payment intent amount with `computed_shopper_total()`.
- **Live Visa TAP key directory or VIC sandbox for teams?** V3 asks.
- **Merchant-side enablement:** when, and does it include the Agent Trust & Risk SDK?

## Quick reference

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m countersign demo
.venv/bin/python -m unittest discover -s tests
```

## Log

Newest first.

### 2026-10-10
- **Repo created, A1 done** (Aeshvarya): contracts C1–C7 in code with tests (terms + hash, TAP sign/verify, merchant interface + 100-product synthetic catalog, rate locks, the decision, the countersign step, JSON wire format), an Airwallex REST client, `python -m countersign demo | keygen | fx`. 35 tests, offline.
- Access requests for merchant-side Agentic Commerce (Track 10) and Airi CLI (Track 9) sent to Airwallex devhelp.
- WORKPLAN proposed; waiting on Varunika (V1).
