# Video run sheet (A9)

What is on screen, what to type, and what the viewer should see, beat by beat. The words are Varunika's (V9); this is the production side. Kit 10 asks for five beats (R8): **setup, Claude configuration, purchase, exception, order confirmation**. Target under five minutes.

## Before recording

1. Deploy is green: `python scripts/smoke.py --base-url https://<host>` with the secrets from `.env` (every line PASS, nothing skipped). Then one dry run: `python scripts/smoke.py --base-url https://<host> --buy`.
2. Host variables for the recording: `COUNTERSIGN_REQUIRE_SHOPPER_SIGNATURE=1`, `COUNTERSIGN_MERCHANT=airwallex` (real sandbox payment) or the default (instant, offline), `COUNTERSIGN_LEVER_TOKEN`, and `COUNTERSIGN_MCP_TOKEN` if we record on our MCP.
3. The uptime monitor has kept the host awake for an hour (a cold start forgets nothing that matters, but costs a minute on camera).
4. Screens: Claude Code terminal (left), the merchant console (right), the shopper's phone mirrored (or a second browser window with a passkey).
5. Open the console as `https://<host>/console#t=<token>` **before** recording: the token leaves the address bar at once. Never put the Render dashboard, `.env`, or a terminal with `env` output on camera.
6. Pick the product now (a kettle reads well), and check it has stock in the console's levers panel.

## The beats

| # | Beat (R8) | On screen | Type or click | The viewer should see |
|---|---|---|---|---|
| 1 | Setup | Render service page (Events tab, not Environment), then the console | Show `/healthz` in a browser | One service is the merchant: the checkout Airwallex calls, the catalogue pages, the TAP door. The console is empty, waiting |
| 2 | Claude configuration | Terminal | Airwallex's MCP: `claude mcp add --transport http agentic-commerce https://api.sandbox.airwallex.com/pa/ai/<merchantId>/acp/mcp --header "Authorization: Bearer <token>"`. Ours (fallback, say so on screen): `claude mcp add --transport http countersign https://<host>/acp/mcp --header "Authorization: Bearer $COUNTERSIGN_MCP_TOKEN"` (type the variable, never the value) | `claude mcp list` shows it connected |
| 3 | Purchase | Terminal, then the phone | Prompt: *"Buy me a pour-over kettle. I pay in rupees, and I want to approve the exact total myself."* | Claude searches, creates the checkout, shows the total in INR and gives the approval link. Console: **Checkout created**. Phone: the terms, **Approve with passkey**, Face ID / Touch ID. Console: **A passkey signed the exact terms** |
| 4 | Exception | Console, then terminal, then phone | Before Claude completes: console → levers → the kettle in *In the latest checkout* → **Raise price 12%**. Then tell Claude: *"Go ahead and pay."* | Console: **Merchant changed price**, then amber **Asked the shopper again: the price changed; nothing charged**. Claude explains the new total and asks. Phone shows the new total; approve again |
| 5 | Order confirmation | Terminal, phone, console, `/verify` | Claude completes. With the Airwallex merchant, open the pay link Claude gives and pay with the sandbox card `4035 5010 0000 0008` | Console: green **Countersigned: the terms still match, order placed**, then **Payment confirmed with Airwallex**. Claude reports the order number. Phone: **Paid**, the receipt, every check ticked in the browser. **Check it anywhere** opens `/verify`: valid. Change the amount in the JSON and check again: *the merchant's signature does not match* |
| + | Bots (if time) | Terminal and console | `curl -X POST https://<host>/checkout -H 'content-type: application/json' -d '{"approval":{},"idempotency_key":"bot"}'` | Console: red **TAP refused a request to /checkout**. Only a Visa TAP-signed agent gets through the direct door |

## Things that go wrong on camera

- **The approval link opens but says the passkey can't be used:** the page must be on the host's own https origin (`COUNTERSIGN_PUBLIC_URL`), not a preview URL or an IP.
- **Drop-in shows nothing:** check the browser console for a blocked script. The page allows `*.airwallex.com` only; tell Aeshvarya the blocked host.
- **The payment stays "waiting":** the shopper page asks the merchant to check with Airwallex after Drop-in's success, and the webhook settles it too. Check `AIRWALLEX_WEBHOOK_SECRET` and that the sandbox webhook points at `https://<host>/webhooks/airwallex`.
- **Claude retries complete on its own after "asked again":** stop the take; the MCP instructions say to ask the shopper first. Re-record beat 4.
- **Everything is gone after a pause:** the host slept or redeployed (state is in memory). Start the take again from beat 3.
