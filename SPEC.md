# Countersign: the spec

The statement both builders work against. If a feature does not serve this, it does not ship.

<!-- Problem and claim are drafts: the final words are the owners' (WORKPLAN V7). -->

## Problem

An AI agent buys something for a person. Between the moment the person says yes and the moment money moves, things change: the price, the stock, the delivery date, and, when the shop is in another country, the exchange rate. And the merchant can't easily tell a shopper's agent from a scraper or a bot replaying a request.

Today either the agent re-asks for everything (and nobody uses it) or it charges whatever the checkout says (and nobody trusts it).

## What Countersign does

The merchant countersigns before it charges. Two checks, both in code:

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

An agent's purchase is charged only if the merchant can verify the agent and the shopper's approval still covers the live price in the shopper's own currency. Every other case asks again or refuses, and the code that decides is small enough to read.

## Rules the code keeps

1. **The model reads, the code decides.** Claude searches, compares and explains. No amount, limit or outcome ever comes from model output.
2. **Never pay twice.** Every charge carries an idempotency key; the merchant checks for an existing order before any retry.
3. **The rate lock is how we deliver the price, not what the shopper decided.** A fresh lock that stays under the ceiling is still what they agreed to; anything they actually chose (items, delivery, merchant) changing asks again.
4. **Fail closed.** Missing data is a refusal, never a default.

## Stack

| Piece | Job |
|---|---|
| Airwallex merchant-side Agentic Commerce | Catalog, merchant MCP for product discovery, checkout, merchant order number |
| Airwallex Multi-Currency Pricing | `conversion_quotes`: locks the rate; the quote id rides on the payment intent so the shopper is charged at exactly that rate |
| Airwallex payment intents + hosted payment component | Card details only ever in Airwallex's component |
| Visa Trusted Agent Protocol | Agent identity at the merchant's edge |
| Claude | The shopping agent |
