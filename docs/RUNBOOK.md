# Run book (A13): Claude as the shopper's agent, step by step

The exact, repeatable steps the video is filmed from: connect Claude to the merchant MCP (Kit 10 R3), buy, change something mid-checkout, finish with an order number. The screen layout and the beats are in [VIDEO_RUN.md](VIDEO_RUN.md); this file is what to type and what should come back. Names only here, never values.

## 1. Before the take

**Host** (Render's settings, see `render.yaml`): `COUNTERSIGN_PUBLIC_URL`, `COUNTERSIGN_ACP_HMAC_SECRET`, `COUNTERSIGN_LEVER_TOKEN`, `COUNTERSIGN_MERCHANT_SIGNING_KEY`, and for the live take `COUNTERSIGN_MERCHANT=airwallex`, `AIRWALLEX_CLIENT_ID`, `AIRWALLEX_API_KEY`, `AIRWALLEX_WEBHOOK_SECRET`, `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, `COUNTERSIGN_SYNC_CATALOG=1`. On our fallback MCP also `COUNTERSIGN_MCP_TOKEN`.

**Recording terminal** (from `.env`, loaded with `set -a; . ./.env; set +a`): `AIRWALLEX_CLIENT_ID` and `AIRWALLEX_API_KEY` (for the MCP token), `COUNTERSIGN_PUBLIC_URL` and `COUNTERSIGN_LEVER_TOKEN` (for the levers), and on our MCP `COUNTERSIGN_MCP_TOKEN`.

**A clean Claude profile.** Claude reads the recording machine's own instructions and memory, and in rehearsal it brought them into its replies. Record from a profile that has none:

```bash
export CLAUDE_CONFIG_DIR="$HOME/countersign-recording"   # a fresh profile: no CLAUDE.md, memory, hooks or plugins
claude                                                     # sign in once (the recording key), then /exit
```

Start every take from an empty folder, so no project instructions load either.

## 2. Connect the merchant MCP

**Airwallex's merchant MCP** (Kit 10). The path comes from onboarding (`python scripts/ac_onboard.py --check-host`, `mcp_endpoints.acp`). Its bearer token is the normal Airwallex login token and lasts about 30 minutes:

```bash
eval "$(python scripts/mcp_token.py)"      # sets AIRWALLEX_MCP_TOKEN; refuses to print it to the screen
claude mcp add --transport http agentic-commerce \
  https://api.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp \
  --header "Authorization: Bearer $AIRWALLEX_MCP_TOKEN"
claude mcp list                            # agentic-commerce: connected
```

**Our fallback MCP** (say so on screen):

```bash
claude mcp add --transport http countersign https://<host>/acp/mcp \
  --header "Authorization: Bearer $COUNTERSIGN_MCP_TOKEN"
claude mcp list
```

Put `--header` last: it takes several values, so placed before the name it swallows the name and the URL. Type the variable, never the value.

**Token expired mid-session** (Airwallex's only, after about 30 minutes):

```bash
eval "$(python scripts/mcp_token.py)"
claude mcp remove agentic-commerce && claude mcp add --transport http agentic-commerce \
  https://api.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp --header "Authorization: Bearer $AIRWALLEX_MCP_TOKEN"
```

## 3. The purchase

Start `claude` in the empty folder and type, exactly:

```text
I'm shopping from India. Using the countersign merchant, find the Pour-over kettle Lite and price one in INR, with a ceiling of ₹8,400. Show me the total in rupees and wait for my yes before paying. Keep replies short.
```

On Airwallex's MCP, say "the merchant" instead of "the countersign merchant".

**Expect:** Claude searches, prices `KIT-265-0`, shows the total in rupees, the rate, delivery and the ceiling, says nothing was charged, and waits. Offline that is ₹8,156.86 ($96.99 at 84.10); live, the rate is Airwallex's. The approval lasts 15 minutes: say yes inside it, or the terms are quoted again.

Then:

```text
Yes, go ahead and pay.
```

**Expect:** an order number (`ORD-…`), the amount charged in rupees, and a signed receipt. With `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, Claude first gives the approval link: approve on the phone, then tell it *"Approved on my phone."* With `COUNTERSIGN_MERCHANT=airwallex`, it gives a pay link: pay in Airwallex's Drop-in, then tell it *"Paid."*, and it reads back the order and the receipt.

## 4. The exceptions

Each starts from a fresh purchase (step 3, first message only), then a lever before *"Yes, go ahead and pay."*:

| Exception | Lever, in the recording terminal | Expect |
| --- | --- | --- |
| Price change | `python scripts/change_event.py --sku KIT-265-0 --percent 12` | Not paid, nothing charged; the old and new totals in rupees (₹8,156.86 → ₹9,135.78 offline), over the ₹8,400 ceiling; Claude asks before doing anything |
| ... then a new yes | Type: *"OK, I'll go up to ₹9,200. Pay the new price."* | Claude sends the new ceiling, then pays: ₹9,135.78, an order number, a receipt. Completing again without a new ceiling charges nothing |
| Out of stock | `python scripts/change_event.py --sku KIT-265-0 --stock 0` | Not bought, not charged; Claude says it sold out and asks what to do |
| Declined card | not built yet (A16) | |

After every exception: `python scripts/change_event.py --reset`.

## 5. Between takes

- Never redeploy during a session: open checkouts and the order counter live in memory (order numbers restart at `ORD-1001`).
- Offline, the payment id contains `fake` and Claude may say so; live, it is Airwallex's intent id.
- If Claude completes again on its own after "asked again", stop the take and re-record that beat.

## Rehearsed

Against our fallback MCP on a local host with the offline merchant, from a fresh session each time:

| Run | What | Result |
| --- | --- | --- |
| 1 | Purchase | Paid, `ORD-1001`, ₹8,156.86 |
| 2 | Price change +12% | Not charged; asked again. Found that the session's ceiling had been raised to the new total, and a second warning for the same change: both fixed in `edge/acp.py` |
| 3 | Price change +12%, then a new ceiling | Not charged, then paid ₹9,135.78 under the new ₹9,200 ceiling |
| 4 | Purchase | Paid, `ORD-1002`, ₹8,156.86 |
| 5 | Out of stock | Not charged; Claude asked what to do |

Not rehearsed yet: Airwallex's MCP, the passkey on a phone, Drop-in, the clean profile from section 1.
