# Countersign: the spec

The statement both builders work against. If a feature does not serve this, it does not ship.

## Problem

An AI agent buys something for a person. Between the moment the person says yes and the moment money moves, things change: the price, the stock, the delivery date, and, when the shop is in another country, the exchange rate. And the merchant can't easily tell a shopper's agent from a scraper or a bot replaying a request.

Today either the agent re-asks for everything (and nobody uses it) or it charges whatever the checkout says (and nobody trusts it). And afterwards, if the shopper disputes the charge, the merchant usually cannot show what was agreed.

## What Countersign does

On its own door (`/acp/checkout_sessions`, and our fallback MCP), the merchant countersigns before it charges. Two checks, both in code:

| Check | Question | How |
|---|---|---|
| **Who is asking** | Is this a known agent, signing for this merchant and this page, right now, once? | Visa Trusted Agent Protocol: Ed25519 signature over `@authority` and `@path`, validity window, nonce, `agent-payer-auth` tag to pay |
| **What was approved** | Is this exactly what the shopper said yes to, at or under the price they accepted, in their own currency? | A hash of the terms they saw, plus a ceiling in their currency; the live offer is rebuilt with a fresh rate lock and compared |

Then exactly one outcome:

| Outcome | When | What the shopper sees |
|---|---|---|
| **Proceed** | Terms unchanged and the price in their currency is at or under the ceiling | Nothing more to do. The merchant's order number |
| **Ask again** | Price, items, quantity, delivery, merchant or currency changed, the approval expired, or the rate moved past the ceiling | The exact change, old and new, in their currency |
| **Refuse** | The request is broken: unsigned, replayed, tampered, inconsistent numbers, stale rate lock | Nothing is charged; the reason is logged |

## The claim we defend

**On the merchant's own door**, an agent's purchase is charged only if the merchant can verify the agent and the approved terms still cover the live price, under a ceiling in the shopper's own currency. Anything else asks the shopper again or refuses, before any money moves. With passkeys on, those terms are signed with a passkey on the merchant's own page; without them, the agent sends the ceiling.

**On Airwallex's path**, Airwallex's merchant MCP takes the payment. Its `create_checkout` call reaches the merchant signed by Airwallex (HMAC), with no TAP signature from the agent and no field for the shopper's ceiling. So the check runs after the payment, against the last quote the merchant gave. The payment gets a signed receipt if its amount, currency and shipping option match that quote and it was paid before the quote expired, and a full refund with the reason if not. If the quote was lost, the payment is left unverified, not signed.

Either way, the code that decides is small enough to read, and every receipt can be checked offline with the merchant's public key.

## Rules the code keeps

1. **The model reads, the code decides.** Claude searches, compares and explains. No amount, limit or outcome ever comes from model output.
2. **Never pay twice.** Every charge carries an idempotency key; the merchant checks for an existing order before any retry.
3. **The rate lock is how we deliver the price, not what the shopper decided.** A fresh lock that stays under the ceiling is still what they agreed to; anything they actually chose (items, delivery, merchant) changing asks again.
4. **Fail closed.** Missing data is a refusal, never a default.

## Stack

| Piece | Job |
|---|---|
| Airwallex merchant-side Agentic Commerce | Catalog, merchant MCP for product discovery, checkout, merchant order number |
| Airwallex Multi-Currency Pricing | `conversion_quotes`: locks the rate when the shopper's currency is not the merchant's; on our own door the quote id rides on the payment intent, so the shopper is charged at exactly that rate |
| Airwallex payment intents + hosted payment component | Card details only ever in Airwallex's component |
| Visa Trusted Agent Protocol | Agent identity on the merchant's own door (an open protocol, checked against Visa's reference code) |
| Claude | The shopping agent |
