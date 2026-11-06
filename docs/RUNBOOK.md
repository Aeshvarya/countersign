# Run book (A13): Claude as the shopper's agent, step by step

The exact, repeatable steps the video is filmed from: connect Claude to the merchant MCP (Kit 10 R3), buy, change something mid-checkout, finish with an order number. The main take is Claude Desktop on Airwallex's merchant MCP; our own MCP in Claude Code is the fallback, labelled as ours. The screen layout and the beats are in [VIDEO_RUN.md](VIDEO_RUN.md); this file is what to type and what should come back. Names only here, never values.

## 1. Before the take

**Host** (Render's settings, see `render.yaml`): `COUNTERSIGN_PUBLIC_URL`, `COUNTERSIGN_ACP_HMAC_SECRET`, `COUNTERSIGN_LEVER_TOKEN`, `COUNTERSIGN_MERCHANT_SIGNING_KEY`, and for the live take `COUNTERSIGN_MERCHANT=airwallex`, `AIRWALLEX_CLIENT_ID`, `AIRWALLEX_API_KEY`, `AIRWALLEX_WEBHOOK_SECRET`, `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, `COUNTERSIGN_SYNC_CATALOG=1`, and the OAuth server's signing key `COUNTERSIGN_OAUTH_SIGNING_KEY` (C14). On our fallback MCP also `COUNTERSIGN_MCP_TOKEN`.

**The host is awake.** A sleeping free instance took 22.6 s to answer its first call on 2026-11-03 and 33.2 s on 2026-11-04 (0.3 s once awake), long enough to time out Airwallex's `create_checkout`, and Claude's sign-in too: Airwallex fetches our JWKS to check the access token (whether it caches it is not said) and gave up after 500 ms until 2026-11-04 (raised since, the new limit not given). Run `python scripts/ac_onboard.py --check-host` a minute before the take: it must say "401 from the ACP door" with no "slow" warning, and the OAuth server must pass with no "sign-in may fail" warning.

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
         "args": ["-y", "mcp-remote@0.16.0", "https://mcp.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp"]
       }
     }
   }
   ```

   `mcp-remote` is pinned to 0.16.0, the version that ran live on 2026-11-04 and 11-05: without a version, `npx` fetches whatever is newest on the day of the take, and no newer one has been tried.

4. Quit Claude Desktop fully (Cmd+Q; closing the window is not enough) and open it again.
5. A browser opens on our sign-in page: sign in with the passkey. Shoppers are guests, so there are no saved cards or addresses. Be quick: Claude Desktop gives a server 60 seconds to start, and the first sign-in on 2026-11-05 ran past it, so it took one reconnect (quit and reopen; the saved sign-in is used).
6. Start a new chat and check that the `agentic-commerce` tools are listed.

Before a take, the host's `/healthz` must say `"oauth": true` and `"keep_awake": true`. The OAuth server's key is `COUNTERSIGN_OAUTH_SIGNING_KEY` on the host; without it every restart makes a new key and signs everyone out.

**It does not connect:** `tail -f ~/Library/Logs/Claude/mcp-server-agentic-commerce.log`, and `python scripts/ac_onboard.py --check-host` (the OAuth server must pass). A 401 in the log after a good sign-in is the access token. First rule out a slow JWKS: `--check-host` prints how long it took, and a JWKS slower than Airwallex's timeout was the whole cause of another team's 401s (#commerce-kits-9-and-10, 2026-11-04). Airwallex's MCP accepted our tokens live on 2026-11-04 with header `typ` `JWT` (it refused `at+jwt`), `alg` ES256 and a `kid` in our JWKS, and `aud` the ACP MCP URL that `mcp-remote` sends as its `resource`. Its verifier fetches our JWKS several times per sign-in (user agent `Java/17`); `/healthz` lists the last five fetches under `jwks_fetches`, so an empty list after a 401 means Airwallex never reached our key.

**Sign in again from scratch** (a take that must show the sign-in, or a stuck one): quit Claude Desktop, delete `mcp-remote`'s saved sign-in in `~/.mcp-auth` (its documented default; not checked on the recording machine), and open Claude Desktop again.

**Not claude.ai on the web:** its connector check got 404 from Airwallex's MCP on 2026-11-05, and no request reached our OAuth server. Claude Desktop with `mcp-remote` is the guide's path and works.

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
I'm shopping from Germany. Using the countersign merchant, find the Pour-over kettle Lite and price one in EUR, with a ceiling of €92. Show me the total in euros and wait for my yes before paying. Keep replies short.
```

On Airwallex's MCP, say "the merchant" instead of "the countersign merchant", and add *"show me the options"*: asking for the options is what makes Claude call `render_products`, which returns Airwallex's card widget (Aeshvarya, 2026-11-05, seen with earbuds; not yet tried with the kettle). Film in euros (from 2026-11-05 the demo page has no rupees either).

**Expect:** Claude searches, prices `KIT-265-0`, shows the total in euros, the rate, delivery and the ceiling, says nothing was charged, and waits. Offline that is €89.23 ($96.99 at 0.92); live, the rate is Airwallex's. **Check the live total before going on:** the €92 ceiling must sit above it and below the total after the +12% lever, and section 4's new €101 ceiling above that total. Both hold only while the rate is between about 0.847 and 0.929 euros to the dollar. Outside that, start again with a ceiling about 3% above the total Claude shows, and in section 4 say a number about 1% above the new total. The approval lasts 15 minutes: say yes inside it, or the terms are quoted again.

Then:

```text
Yes, go ahead and pay.
```

**Expect:** an order number (`ORD-…`), the amount charged in euros, and a signed receipt. With `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, Claude first gives the approval link: approve on the phone, then tell it *"Approved on my phone."* With `COUNTERSIGN_MERCHANT=airwallex`, it gives a pay link: pay in Airwallex's Drop-in, then tell it *"Paid."*, and it reads back the order and the receipt.

**On Airwallex's MCP** (not rehearsed in this run book; run live twice on 2026-11-05, PROGRESS Log): Airwallex asks our `create_checkout` for the price, then creates and charges the payment itself.

- **Expect first:** the total in euros and two shipping options, Standard (5-7 business days, free) and Express (2 business days, €9.19 offline at 0.92). Our total never includes shipping: Airwallex adds the option the shopper picks (€98.42 offline with Express).
- **The ceiling stays with Claude:** `create_checkout` carries no ceiling, so on this path the €92 never reaches the merchant. Airwallex also has no way for `create_checkout` to tell the agent that an order needs someone's approval (#commerce-kits-9-and-10, 2026-11-05), so this path cannot ask again either.
- **Then:** the live runs went create, choose shipping, delegate the test card's token, complete. Sandbox test cards take any future expiry and any three-digit CVV (Airwallex, 2026-11-05). The order id Claude is given is our checkout id (`chk_…`).
- **The card goes into Airwallex's widget, never the chat** (Kit 10 R5). `render_products` returns Airwallex's checkout widget (`ui://widget/native-checkout.html`, MCP Apps), which has its own card form and calls `create_delegate_payment_token` and `complete_checkout_session` itself (PROGRESS Open questions, 2026-11-05). Never type the card number into the conversation, and never film a take where Claude asks for it. Whether Claude Desktop shows the widget is not confirmed yet (Aeshvarya is checking it, 2026-11-05): if it does not appear in a take, stop before paying.
- **After the payment:** Airwallex's webhook reaches us and W3 checks the paid intent against the quote it belongs to: the currency, the shipping option, the amount to the cent, and that it was created before the quote expired. All hold: the merchant signs the receipt and serves it at `https://<host>/receipts/<checkout id>`. Anything else, including a second payment for a checkout already paid: a full refund with the reason, and no receipt (the first receipt stays). Only `payment_intent.succeeded` settles. A payment whose quote was lost in a restart is not refunded: the console shows **Payment not checked** and there is no receipt (fixed 2026-11-05, `7db45cd`). W3 does not check a shopper passkey on this path.
- **Save the receipt at once** (it lives in memory, and a redeploy loses it): `curl -s https://<host>/receipts/<checkout id> -o countersign-receipt-<checkout id>.json`. That file is what `scripts/dispute_demo.py --receipt` takes for the dispute ending.

## 4. The exceptions

Each starts from a fresh purchase (step 3, first message only), then a lever before *"Yes, go ahead and pay."*:

| Exception | Lever, in the recording terminal | Expect |
| --- | --- | --- |
| Price change | `python scripts/change_event.py --sku KIT-265-0 --percent 12` | Not paid, nothing charged; the old and new totals in euros (€89.23 → €99.94 offline), over the €92 ceiling; Claude asks before doing anything |
| ... then a new yes | Type: *"OK, I'll go up to €101. Pay the new price."* | Claude sends the new ceiling, then pays: €99.94 offline, an order number, a receipt. Completing again without a new ceiling charges nothing |
| Out of stock | `python scripts/change_event.py --sku KIT-265-0 --stock 0` | Not bought, not charged; Claude says it sold out and asks what to do |
| Declined card (our MCP, Drop-in) | Pay with `4646 4646 4646 4644` (any future expiry, any CVC), which Airwallex's risk engine declines at any amount (`risk_declined`, Airwallex's test-card page, checked 2026-11-05) | Not charged; Claude says the card was declined and the order stays open for another card. Built offline, not rehearsed live; whether it declines inside Drop-in with a quote attached is still open (PROGRESS Open questions) |

On Airwallex's MCP (not rehearsed):

| Exception | Lever | Expect |
| --- | --- | --- |
| Out of stock | `python scripts/change_event.py --sku KIT-265-0 --stock 0` before the first message | Our `create_checkout` answers `available_quantity: 0`; how Claude words it is Airwallex's MCP |
| Express shipping | The shopper picks Express | The paid total is ours plus Express: €98.42 offline |
| Declined card | In Airwallex's card widget, pay with `4646 4646 4646 4644` (any future expiry, any CVC). Not the amount-triggered cards: they decline only at amounts like 80.05, which this take never reaches | Airwallex's risk engine declines it at any amount (`risk_declined`, "Declined by fraud risk engine"): no `payment_intent.succeeded`, so W3 signs no receipt and sends no refund, and nothing is charged. How Claude words the decline is Airwallex's MCP. Not documented whether the card declines the same way through the delegate token: rehearse once before filming. Backup if it does not: out of stock, above |
| Price change after the last update | `--percent 12` after the shopper's last change to the session, before paying | Paid at the price already quoted, with a receipt: W3 checks the payment against its own quote, never today's price (run live 2026-11-05) |
| Price change before an update | `--percent 12`, then the shopper changes the session (for example the shipping) | The update calls our `create_checkout` again and the total moves (₹8,794.23 → ₹9,673.75 on the 2026-11-05 live run, in rupees) with no message to the agent: only Claude can ask the shopper again. For the re-ask on screen, film it on our MCP (section 4, first table) |

After every exception: `python scripts/change_event.py --reset`.

## 5. Between takes

- Never redeploy during a session: open checkouts, the order counter, Airwallex's quotes and the receipts at `/receipts/<checkout id>` live in memory (order numbers restart at `ORD-1001`). A payment made after a quote but across a redeploy gets no receipt: refunded if it was paid after the restart, **Payment not checked** if before. Every push to `main` redeploys the host (`autoDeploy` in `render.yaml`), a PROGRESS line included: the receipt from the 2026-11-05 live run answered 404 a few minutes after the 12:55 IST merge.
- Offline, the payment id contains `fake` and Claude may say so; live, it is Airwallex's intent id.
- If Claude completes again on its own after "asked again", stop the take and re-record that beat.

## Rehearsed

Against our fallback MCP on a local host with the offline merchant, from a fresh session each time, in rupees (before the video moved to euros on 2026-11-05):

| Run | What | Result |
| --- | --- | --- |
| 1 | Purchase | Paid, `ORD-1001`, ₹8,156.86 |
| 2 | Price change +12% | Not charged; asked again. Found that the session's ceiling had been raised to the new total, and a second warning for the same change: both fixed in `edge/acp.py` |
| 3 | Price change +12%, then a new ceiling | Not charged, then paid ₹9,135.78 under the new ₹9,200 ceiling |
| 4 | Purchase | Paid, `ORD-1002`, ₹8,156.86 |
| 5 | Out of stock | Not charged; Claude asked what to do |

Run live outside a take (Aeshvarya, 2026-11-04 and 11-05, PROGRESS Log): Claude Desktop with `mcp-remote` 0.16.0, our OAuth sign-in, Airwallex's MCP, and two purchases paid with the test card, the second ending in a signed receipt.

Not rehearsed yet as a take: all of that from the clean profile in section 1, the passkey on a phone, Drop-in, and any exception on Airwallex's MCP.
