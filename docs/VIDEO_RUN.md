# Video run sheet (A9)

What is on screen, what to type, and what the viewer should see, beat by beat. The words are Varunika's (V9); this is the production side. Kit 10 asks for five beats (R8): **setup, Claude configuration, purchase, exception, order confirmation**. We add the dispute ending (U3). Target under five minutes. Rewritten 2026-11-06 for the take as it now runs: Claude Desktop on Airwallex's MCP, in euros, the card in Airwallex's widget. How to connect and every expected total are in [RUNBOOK.md](RUNBOOK.md); this sheet only orders the shots.

## Before recording

1. **No deploy from here to the end of the take.** Quotes and receipts live in memory, and the first MCP call after a deploy answers 401 once (Airwallex fetching our keys again; seen twice on 2026-11-06). After the last deploy, make one throwaway call: ask Claude in a scratch chat to list the merchant's products, and retry once if it fails.
2. `https://<host>/healthz` says `"acp": true`, `"oauth": true`, `"keep_awake": true` and `"mcp_payments": true`.
3. Reset the levers: `python scripts/change_event.py --reset`.
4. Claude Desktop: a clean profile with only `agentic-commerce` (`mcp-remote@0.16.0`, RUNBOOK section 2), already signed in with the passkey, the tools listed in a new chat. For the re-ask beat, Claude Code with our fallback MCP in a second terminal.
5. Open the console as `https://<host>/console#t=<token>` **before** recording (the token leaves the address bar at once). Never put the Render dashboard, `.env`, `~/.mcp-auth`, or a terminal with `env` output on camera.
6. Screens: Claude Desktop (left), the console (right), a browser tab for the receipt page.
7. The product is the Pour-over kettle Lite (`KIT-265-0`). Check it has stock on the console. If the widget does not show for it, use the earbuds (`AUD-018-0`), which it did show for on 2026-11-05.

## The beats

| # | Beat | Time | On screen | Type or click | The viewer should see |
|---|---|---|---|---|---|
| 1 | Setup | 0:20 | Browser, then the console | `/healthz`, then the console, empty | One service is the merchant: the price Airwallex asks for (`create_checkout`), the catalogue, the TAP door, the receipts. Airwallex hosts the merchant MCP and takes the payment |
| 2 | Claude configuration | 0:25 | `claude_desktop_config.json`, then Claude Desktop | The `agentic-commerce` entry (`npx -y mcp-remote@0.16.0 https://mcp.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp`), then the tools list in a new chat | No token in any file: the shopper signed in with a passkey on our OAuth page (show the sign-in only if filming from scratch, RUNBOOK section 2) |
| 3 | Purchase | 1:10 | Claude Desktop, then the console | RUNBOOK section 3's prompt, Airwallex variant ("the merchant", and *"show me the options"*). In the widget: Standard shipping, Airwallex's sandbox test card, any future expiry, any CVC | Claude calls `render_products` and Airwallex's checkout widget appears in the chat. The card goes into the widget, never the chat (R5). Console: **Airwallex asked for a price: quoted** (the total in euros), then **Payment confirmed with Airwallex** · receipt signed |
| 4a | Exception: declined card | 0:40 | Claude Desktop, then the console | Before 3's payment, or as a second take: pay in the widget with `4646 4646 4646 4644`, then with the good card | Airwallex declines it (`risk_declined`, run live 2026-11-06): nothing charged, no receipt, no refund. The good card then pays the same checkout once: one charge, one receipt (live: the declined intent stays `REQUIRES_PAYMENT_METHOD`, uncharged) |
| 4b | Exception: the countersign asks again | 0:50 | Claude Code on our fallback MCP (say so on screen), the console | RUNBOOK section 4, first table: the price lever `--percent 12` after the quote, then *"Yes, go ahead and pay."* | Console: amber **Asked the shopper again: the price changed; nothing charged**. Claude shows the old and new totals in euros and asks; a new yes pays the new price. On Airwallex's path a re-quote is silent (RUNBOOK section 4), which is why this beat runs on our door |
| 5 | Order confirmation | 0:25 | Claude Desktop, then the receipt page | Claude gives the order id (`chk_…`). Open `https://<host>/receipt/<chk id>` (the link Claude gives once `aeshvarya/acp-receipt-permalink` is merged; type it before then) | **Paid … EUR to Larch Lane**, then each check passing in the browser: the merchant's signature, the charge not above the quoted price, the order and the payment. **Save the receipt**: the file is what beat 6 uses |
| 6 | Dispute ending | 0:30 | Terminal (no `.env` on screen) | `python scripts/dispute_demo.py --receipt countersign-receipt-<chk id>.json --structured` | `1 dispute opened … CHARGEBACK`, `2 receipt VERIFIED`, `3 evidence sent … CHALLENGED (receipt PDF … plus product_description, refund_refusal_reason)`, `4 decided … WON`. Say that the sandbox's simulator was asked to decide (`resolve_for_merchant`); what is real is the dispute on a real MCP payment, the receipt verifying, and the evidence being accepted (run live 2026-11-06) |
| + | Bots (if time) | 0:15 | Terminal and console | `curl -X POST https://<host>/checkout -H 'content-type: application/json' -d '{"approval":{},"idempotency_key":"bot"}'` | Console: red **TAP refused a request to /checkout**. Only a Visa TAP-signed agent gets through our own door |

**Time budget:** beats 1 to 6 come to 4:30, under the five minutes Kit 10 asks for, with 0:30 spare for titles. Bots (0:15) goes in only if a take runs short. Cut in this order if it runs long: bots, then 4a (keep the declined card as a line of voice over the console), then 2 down to the config file alone.

## Things that go wrong on camera

- **The first call answers 401:** the host was just deployed. Retry once (Before recording, 1).
- **No widget, only text:** Claude did not call `render_products`. Ask *"show me the options"* again. If Claude then asks for the card in the chat, **stop the take**: never type a card number into the conversation.
- **The receipt page says "Waiting for Airwallex to confirm the payment":** it asks the host every 3 seconds, and the host looks the payment up on Airwallex itself, so a late webhook doesn't matter. Two minutes without a receipt: check the console for **Payment refunded** (the payment didn't match the quote) or **Payment not checked** (the host restarted between the quote and the payment; start again from beat 3).
- **Claude Desktop drops the server at start-up:** it gives a server 60 seconds; quit (Cmd+Q) and reopen, and the saved sign-in is reused.
- **Everything is gone after a pause:** the host slept or redeployed (state is in memory). Start the take again from beat 3.
