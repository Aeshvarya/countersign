# Countersign

A merchant checkout that countersigns a verified agent and the shopper's approved terms before any money moves.

<!-- Pitch, why it matters, prior art, team: owner's words go here (WORKPLAN V8). -->

## Status

Pre-build. The product runs offline end to end: approvals bound to the terms a shopper saw, the decision that charges or asks again, Visa Trusted Agent Protocol signing and verification, rate locks, and a 100-product synthetic merchant. Nothing has run against the Airwallex merchant-side Agentic Commerce APIs yet; that needs enablement from Airwallex.

## Quickstart

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m countersign demo                    # the contrast pairs and a dispute, offline, no keys
.venv/bin/uvicorn countersign.web.app:app --port 8000   # the demo app: http://127.0.0.1:8000
.venv/bin/python -m unittest discover -s tests          # tests, offline
```

The app uses Claude when `ANTHROPIC_API_KEY` is set, and a scripted stand-in otherwise (`COUNTERSIGN_MODEL=scripted` forces it). Open `/?q=Buy+me+a+kettle` to start straight away, or `/?s=<session id>` to watch a running session from a second screen.

Against the Airwallex sandbox (copy `.env.example` to `.env` and fill it in):

```bash
set -a; . ./.env; set +a
.venv/bin/python -m countersign fx --merchant USD --shopper INR   # lock a real rate
```

## How it works

1. **The agent proves who it is.** Every request to the merchant carries a Visa Trusted Agent Protocol signature (Ed25519, bound to the merchant's host and the exact path, with a nonce and a validity window). The merchant's edge refuses unsigned, expired, replayed or misdirected requests before anything else runs. `src/countersign/tap.py`
2. **The shopper approves exact terms.** Merchant, items, delivery, totals and a ceiling in the shopper's own currency, hashed. `src/countersign/terms.py`
3. **The merchant countersigns before it charges.** It rebuilds the live offer with a fresh rate lock and runs one deterministic function: proceed, ask again, or refuse. No model output reaches it. `src/countersign/decide.py`, `src/countersign/checkout.py`
4. **The charge happens in the shopper's currency at the locked rate**, through Airwallex Multi-Currency Pricing, and returns the merchant's order number. `src/countersign/fx.py`, `src/countersign/merchant.py`
5. **The merchant signs a receipt** binding the agent's identity, the approval's hash and ceiling, the charge and the order number. Anyone with the merchant's public key can check it later, offline: `python -m countersign verify-receipt`. `src/countersign/receipt.py`

TAP interop with Visa's reference implementation is checked in both directions by `scripts/tap_interop.py`.

## Layout

| Path | What |
|---|---|
| `src/countersign/terms.py` | C1 terms, approval, canonical hash |
| `src/countersign/tap.py` | C2 TAP sign and verify, key directory, nonce cache |
| `src/countersign/merchant.py` | C3 merchant interface, synthetic catalog, offline merchant |
| `src/countersign/fx.py` | C4 rate locks: Airwallex conversion quotes, offline fake |
| `src/countersign/decide.py` | C5 the decision |
| `src/countersign/checkout.py` | C6 the countersign step |
| `src/countersign/receipt.py` | C8 the countersigned receipt |
| `src/countersign/agent/` | the shopping agent: tool loop, approval pause, TAP-signed shop clients |
| `src/countersign/web/` | the demo app |
| `src/countersign/airwallex.py` | Airwallex REST client |
| `scripts/tap_interop.py` | interop check against Visa's TAP reference code |
| `PROGRESS.md`, `WORKPLAN.md` | where the work stands, who does what |

## Licence

Apache 2.0.
