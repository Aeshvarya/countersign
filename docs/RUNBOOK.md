# Run book (A13): Claude as the shopper's agent, step by step

The exact, repeatable steps the video is filmed from: connect Claude to the merchant MCP (Kit 10 R3), buy, change something mid-checkout, finish with an order number. The main take is Claude Desktop on Airwallex's merchant MCP; our own MCP in Claude Code is the fallback, labelled as ours. The screen layout and the beats are in [VIDEO_RUN.md](VIDEO_RUN.md); this file is what to type and what should come back. Names only here, never values.

## 1. Before the take

**Host** (Render's settings, see `render.yaml`): `COUNTERSIGN_PUBLIC_URL`, `COUNTERSIGN_ACP_HMAC_SECRET`, `COUNTERSIGN_LEVER_TOKEN`, `COUNTERSIGN_MERCHANT_SIGNING_KEY`, and for the live take `COUNTERSIGN_MERCHANT=airwallex`, `AIRWALLEX_CLIENT_ID`, `AIRWALLEX_API_KEY`, `AIRWALLEX_WEBHOOK_SECRET`, `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, `COUNTERSIGN_SYNC_CATALOG=1`, and the OAuth server's signing key (C14; its name comes with `web/oauth.py`). On our fallback MCP also `COUNTERSIGN_MCP_TOKEN`.

**The host is awake.** A sleeping free instance took 22.6 s to answer its first call on 2026-11-03 (0.3 s once awake), long enough to time out Airwallex's `create_checkout`. Run `python scripts/ac_onboard.py --check-host` a minute before the take: it must say "401 from the ACP door" with no "slow" warning, and the OAuth server must pass.

**Recording terminal** (from `.env`, loaded with `set -a; . ./.env; set +a`): `COUNTERSIGN_PUBLIC_URL` and `COUNTERSIGN_LEVER_TOKEN` (for the levers), and on our MCP `COUNTERSIGN_MCP_TOKEN`.

**A clean Claude profile.** Claude reads the recording machine's own instructions and memory, and in rehearsal it brought them into its replies. In Claude Desktop, record from an account with no personal preferences and memory off, and a config with no MCP server but `agentic-commerce`. For our fallback in Claude Code, record from a profile that has none:

```bash
export CLAUDE_CONFIG_DIR="$HOME/countersign-recording"   # a fresh profile: no CLAUDE.md, memory, hooks or plugins
claude                                                     # sign in once (the recording key), then /exit
```

Start every take from an empty folder, so no project instructions load either.

## 2. Connect the merchant MCP

**Airwallex's merchant MCP in Claude Desktop** (the main take). Needs Kit 10 onboarding (`scripts/ac_onboard.py --onboard`) and our OAuth server (C14). Claude reaches Airwallex's MCP through `mcp-remote`, a community bridge, and signs the shopper in through our OAuth server; no token goes in a shell.

1. Node.js LTS from nodejs.org, then check both answer:

   ```bash
   node --version && npx --version
   ```

2. The merchant id: `python scripts/ac_onboard.py` (read only) prints `mcp acp: /pa/ai/<merchantId>/acp/mcp`.
3. Open `~/Library/Application Support/Claude/claude_desktop_config.json` (create it if it is missing) and merge this at the top level, keeping any other settings:

   ```json
   {
     "mcpServers": {
       "agentic-commerce": {
         "command": "npx",
         "args": ["-y", "mcp-remote", "https://mcp.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp"]
       }
     }
   }
   ```

4. Quit Claude Desktop fully (Cmd+Q; closing the window is not enough) and open it again.
5. A browser opens on our sign-in page: sign in with the passkey. Shoppers are guests, so there are no saved cards or addresses.
6. Start a new chat and check that the `agentic-commerce` tools are listed.

**It does not connect:** `tail -f ~/Library/Logs/Claude/mcp-server-agentic-commerce.log`, and `python scripts/ac_onboard.py --check-host` (the OAuth server must pass). A 401 in the log after a good sign-in is the access token: which claims Airwallex's MCP checks is not documented yet (PROGRESS Open questions).

**Sign in again from scratch** (a take that must show the sign-in, or a stuck one): quit Claude Desktop, delete `mcp-remote`'s saved sign-in in `~/.mcp-auth` (its documented default; not checked on the recording machine), and open Claude Desktop again.

**Our fallback MCP** in Claude Code (say so on screen):

```bash
claude mcp add --transport http countersign https://<host>/acp/mcp \
  --header "Authorization: Bearer $COUNTERSIGN_MCP_TOKEN"
claude mcp list
```

Put `--header` last: it takes several values, so placed before the name it swallows the name and the URL. Type the variable, never the value.

## 3. The purchase

Start a new chat in Claude Desktop (on our fallback: `claude` in the empty folder) and type, exactly:

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

**On Airwallex's MCP** (not rehearsed): Airwallex asks our `create_checkout` for the price, then creates and charges the payment itself.

- **Expect first:** the total in rupees and two shipping options, Standard (5-7 business days, free) and Express (2 business days, ₹840.16 offline at 84.10). Our total never includes shipping: Airwallex adds the option the shopper picks (₹8,997.02 offline with Express).
- **The ceiling stays with Claude:** `create_checkout` carries no ceiling, so on this path the ₹8,400 never reaches the merchant.
- **Then:** the shopper pays on Airwallex's payment page. Our webhook check then signs the receipt, or refunds in full with the reason when the payment does not match a live quote we signed, or, with `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, when the shopper's passkey did not sign those exact terms (W3; how the shopper signs on this path is not built yet).

## 4. The exceptions

Each starts from a fresh purchase (step 3, first message only), then a lever before *"Yes, go ahead and pay."*:

| Exception | Lever, in the recording terminal | Expect |
| --- | --- | --- |
| Price change | `python scripts/change_event.py --sku KIT-265-0 --percent 12` | Not paid, nothing charged; the old and new totals in rupees (₹8,156.86 → ₹9,135.78 offline), over the ₹8,400 ceiling; Claude asks before doing anything |
| ... then a new yes | Type: *"OK, I'll go up to ₹9,200. Pay the new price."* | Claude sends the new ceiling, then pays: ₹9,135.78, an order number, a receipt. Completing again without a new ceiling charges nothing |
| Out of stock | `python scripts/change_event.py --sku KIT-265-0 --stock 0` | Not bought, not charged; Claude says it sold out and asks what to do |
| Declined card (our MCP, Drop-in) | Pay with a test card that declines (which one: PROGRESS Open questions) | Not charged; Claude says the card was declined and the order stays open for another card. Built offline, not rehearsed live |

On Airwallex's MCP (not rehearsed):

| Exception | Lever | Expect |
| --- | --- | --- |
| Out of stock | `python scripts/change_event.py --sku KIT-265-0 --stock 0` before the first message | Our `create_checkout` answers `available_quantity: 0`; how Claude words it is Airwallex's MCP |
| Express shipping | The shopper picks Express | The paid total is ours plus Express: ₹8,997.02 offline |
| Price change after the quote | `--percent 12` between the price shown and the payment | Decided by the payment webhook check against the quote that payment belongs to (W3, not built yet) |

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

Not rehearsed yet: Claude Desktop with `mcp-remote`, our OAuth sign-in, Airwallex's MCP and its payment page, the passkey on a phone, Drop-in, the clean profile from section 1.
