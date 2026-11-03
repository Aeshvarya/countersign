// The shopper's page, /approve/<id> or /pay/<id>: approve, wait for the agent, pay, keep the receipt.
//
// GET  /api/checkout/<id>          where the checkout stands (web/shopper.py): its stage drives this page
// GET  /acp/approvals/<id>         the passkey challenge for the current terms (U1)
// POST /acp/approvals/<id>         the passkey assertion, every field base64url
// POST /api/checkout/<id>/confirm  after Drop-in's success: the merchant checks the payment with Airwallex
//
// The passkey signs the challenge bytes the server sends, never the terms hash:
// the challenge also covers this session and the approval's expiry, so the same
// signature cannot approve an identical cart in another session. The challenge
// is fetched again right before signing, and if the terms changed since they
// were shown, nothing is signed until the shopper has seen the new ones.
//
// The server checks assertions against the SPKI public key the page sends, and
// only getPublicKey() at registration returns it. So the first approval on a
// device creates a passkey (no server call: the first valid signature binds the
// credential to its key) and this page keeps {credential id, public key} in
// localStorage. Neither is secret.
//
// The card goes only into Airwallex's Drop-in, loaded from Airwallex. This page
// gets the intent's client secret, which is made for the browser and can only
// pay that one intent.
"use strict";

// Integer minor units per currency, as countersign.terms._MINOR_UNITS (a test keeps them equal).
const MINOR_UNITS = {"JPY": 0, "KRW": 0, "VND": 0, "IDR": 0, "CLP": 0, "HUF": 0, "TWD": 0, "ISK": 0, "BHD": 3, "KWD": 3, "OMR": 3};
const STORE_KEY = "countersign.passkey.v1";
const ES256 = -7;
const DROP_IN_SDK = "https://static.airwallex.com/components/sdk/v1/index.js";

const $ = (id) => document.getElementById(id);
const sessionId = decodeURIComponent((location.pathname.match(/^\/(?:approve|pay)\/([^/]+)/) || [])[1] || "");
const enc = encodeURIComponent(sessionId);
const URLS = { state: "/api/checkout/" + enc, confirm: "/api/checkout/" + enc + "/confirm", approval: "/acp/approvals/" + enc };

let state = null;        // the last /api/checkout body
let shownHash = null;    // the terms hash the shopper is looking at
let memoryCred = null;   // used when localStorage is unavailable
let busy = false;
let pollTimer = null;
let started = Date.now();
let dropIn = { mounted: false, paid: false };
let shownReceipt = null;

// ---------------------------------------------------------------- base64url

function b64url(buffer) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromB64url(text) {
  const s = text.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// ---------------------------------------------------------------- the stored passkey

function readStore() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {}; } catch (e) { return {}; }
}

function writeStore(all) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(all)); } catch (e) { /* private window: memory only */ }
}

function loadCred(rpId) {
  const c = readStore()[rpId];
  if (c && typeof c.credential_id === "string" && typeof c.public_key_spki_der === "string") return c;
  return memoryCred && memoryCred.rp_id === rpId ? memoryCred : null;
}

function saveCred(rpId, cred) {
  memoryCred = { ...cred, rp_id: rpId };
  const all = readStore();
  all[rpId] = cred;
  writeStore(all);
}

function forgetCred(rpId) {
  memoryCred = null;
  const all = readStore();
  delete all[rpId];
  writeStore(all);
}

// ---------------------------------------------------------------- display

function money(minor, currency) {
  const code = String(currency || "").toUpperCase();
  const digits = Object.prototype.hasOwnProperty.call(MINOR_UNITS, code) ? MINOR_UNITS[code] : 2;
  return format(Number(minor) / Math.pow(10, digits), code, digits);
}

function decimalMoney(amount, currency) {
  const code = String(currency || "").toUpperCase();
  const digits = Object.prototype.hasOwnProperty.call(MINOR_UNITS, code) ? MINOR_UNITS[code] : 2;
  return format(Number(amount), code, digits);
}

function format(value, code, digits) {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: code,
      minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
  } catch (e) {
    return value.toFixed(digits) + " " + code;
  }
}

function when(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function status(text, kind) {
  const el = $("status");
  el.textContent = text || "";
  el.className = "status " + (kind || "info");
}

function setText(id, text) { $(id).textContent = text; }

function only(...ids) {
  for (const id of ["approve-step", "wait-step", "pay-step", "receipt-step"]) $(id).hidden = !ids.includes(id);
}

function steps(stage) {
  const order = { approve: 0, waiting: 1, pay: 1, paid: 2, failed: 1, canceled: -1 };
  const at = order[stage] ?? -1;
  const signedOrOff = state && (state.signed || !state.approval_required);
  document.querySelectorAll("#steps li").forEach((li, i) => {
    li.className = "";
    if (stage === "paid" && state.receipt) li.className = "done";
    else if (i < at || (i === 0 && at >= 1 && signedOrOff)) li.className = "done";
    else if (i === at) li.className = "now";
  });
  $("steps").hidden = stage === "canceled";
}

function renderTerms(t) {
  if (!t) { $("terms").hidden = true; return; }
  $("terms").hidden = false;
  shownHash = t.terms_hash;
  setText("merchant", "From " + t.merchant_id);
  const list = $("items");
  list.replaceChildren();
  for (const item of t.items) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = item.title || item.id;
    const qty = document.createElement("span");
    qty.className = "qty";
    qty.textContent = "× " + item.quantity;
    li.append(name, qty);
    list.append(li);
  }
  setText("delivery", t.delivery);
  const foreign = t.merchant_currency && t.merchant_currency !== t.currency;
  $("rate-row").hidden = !foreign;
  if (foreign) {
    const rate = t.rate ? " at 1 " + t.merchant_currency.toUpperCase() + " = " + t.rate + " " + t.currency.toUpperCase() : "";
    setText("merchant-total", money(t.merchant_total, t.merchant_currency) + rate);
  }
  setText("total", money(t.total, t.currency));
  renderChange(t);
  const ceiling = $("ceiling");
  ceiling.replaceChildren();
  if (t.ceiling !== null && Number(t.ceiling) > Number(t.total)) {
    ceiling.append("If the exchange rate moves before payment, the charge can change, but never above ");
    const b = document.createElement("b");
    b.textContent = money(t.ceiling, t.currency);
    ceiling.append(b, ".");
  } else {
    ceiling.append("You are charged exactly this amount, or asked again.");
  }
  if (t.terms_hash) {
    $("what").hidden = false;
    setText("hash", t.terms_hash);
    setText("session", sessionId);
    setText("expires", t.expires_at || "");
  }
  tick();
}

// Asked again after the shopper's yes: what they approved, struck through, and what it is now.
function renderChange(t) {
  const moved = state.stage === "approve" && state.changed && state.changed.previous_total !== null
    && Number(state.changed.previous_total) !== Number(t.total) ? state.changed : null;
  $("terms").classList.toggle("reasked", Boolean(moved));
  $("was").hidden = $("delta").hidden = !moved;
  if (!moved) return;
  const before = Number(moved.previous_total), now = Number(t.total);
  setText("was", money(before, t.currency));
  const diff = now - before, pct = before ? (diff / before) * 100 : 0;
  const chip = document.createElement("span");
  chip.textContent = "↺ " + (diff > 0 ? "+" : "−") + money(Math.abs(diff), t.currency)
    + " · " + (diff > 0 ? "+" : "−") + Math.abs(pct).toFixed(1) + "%";
  $("delta").replaceChildren(chip);
}

function tick() {
  const t = state && state.terms;
  const el = $("valid");
  if (!t || !t.expires_at) { el.textContent = ""; return; }
  const left = new Date(t.expires_at) - new Date();
  if (isNaN(left)) { el.textContent = ""; return; }
  if (left <= 0) {
    el.textContent = "These terms have expired. Ask your agent to quote again; this page updates by itself.";
    el.className = "valid late";
  } else {
    el.textContent = "Valid until " + when(t.expires_at) + " (" + Math.ceil(left / 60000) + " min)";
    el.className = "valid";
  }
}

// ---------------------------------------------------------------- the server

async function call(method, url, body) {
  const r = await fetch(url, {
    method, cache: "no-store", credentials: "omit",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) {
    const err = new Error(data && data.message ? data.message : "the merchant answered " + r.status);
    err.code = data && data.code;
    err.statusCode = r.status;
    throw err;
  }
  return data;
}

function poll(ms) {
  clearTimeout(pollTimer);
  const late = Date.now() - started > 10 * 60 * 1000;
  pollTimer = setTimeout(refresh, late ? Math.max(ms, 10000) : ms);
}

async function refresh() {
  if (!sessionId) {
    setText("title", "This link is incomplete");
    setText("sub", "Ask your agent for the checkout link again.");
    $("steps").hidden = true;
    return;
  }
  try {
    state = await call("GET", URLS.state);
  } catch (err) {
    if (err.statusCode === 404) {
      setText("title", "This link is not active");
      setText("sub", "The checkout may have been closed, or the merchant restarted. Nothing was charged here.");
      $("steps").hidden = true;
      only();
      return;
    }
    status("Could not reach the merchant: " + err.message + ". Retrying…", "warn");
    poll(5000);
    return;
  }
  render();
}

// ---------------------------------------------------------------- stages

function render() {
  const s = state;
  steps(s.stage);
  if (s.stage !== "paid") renderTerms(s.terms);
  if (s.stage === "approve") return renderApprove();
  if (s.stage === "waiting") return renderWaiting();
  if (s.stage === "pay") return renderPay();
  if (s.stage === "paid") return renderPaid();
  if (s.stage === "failed") return renderFailed();
  return renderClosed();
}

function renderApprove() {
  const changed = state.changed;
  if (changed) {
    const moved = changed.previous_total !== null;
    setText("title", moved ? "The price changed" : "The terms changed");
    setText("sub", "Nothing was charged. " + (moved
      ? "The total is no longer what you approved, so your agent has to ask you again."
      : changed.reasons.join(" ") + " Your agent has to ask you again."));
  } else {
    setText("title", "Approve this purchase");
    setText("sub", "Your agent asked to buy this for you. Check it, then approve on this device.");
  }
  if (state.terms) setText("approve", "Approve " + money(state.terms.total, state.terms.currency) + " with passkey");
  only("approve-step");
  const cred = loadCred(location.hostname);
  $("reset").hidden = !cred;
  const problem = cannotSign();
  $("approve").disabled = Boolean(problem) || busy;
  if (problem) status(problem, "error");
  else if (!busy && $("status").className.includes("ok")) status("");
  poll(5000); // a requote or an expiry shows up here
}

function renderWaiting() {
  setText("title", state.signed ? "Approved" : "Waiting for your agent");
  setText("sub", state.signed
    ? "You approved these exact terms. Your agent can complete the purchase now."
    : "Your agent is finishing the checkout.");
  only("wait-step");
  setText("wait-text", "Waiting for your agent to place the order. This page updates by itself.");
  if ($("status").className.includes("info")) status("");
  poll(2000);
}

function renderPay() {
  const o = state.order;
  setText("title", "Pay for your order");
  setText("sub", "Order " + o.order_number + ": " + decimalMoney(o.amount, o.currency)
    + " at the rate you approved. Your card goes only into Airwallex's form below.");
  if (dropIn.paid) {
    only("pay-step", "wait-step");
    setText("wait-text", "Payment sent. The merchant is checking it with Airwallex…");
    poll(2500);
  } else {
    only("pay-step");
    mountDropIn(state.payment);
    poll(5000); // a payment settled by the webhook shows up here too
  }
}

function renderPaid() {
  const o = state.order;
  $("terms").hidden = true;
  $("what").hidden = true;
  setText("title", "Paid");
  setText("sub", "");
  only("receipt-step");
  setText("order-line", "Order " + o.order_number + (state.terms ? " · " + state.terms.merchant_id : ""));
  setText("paid-amount", decimalMoney(o.amount, o.currency));
  status("");
  if (!state.receipt) {
    only("receipt-step", "wait-step");
    setText("paid-sub", "Payment confirmed.");
    setText("wait-text", "The merchant is signing your receipt…");
    poll(2000);
    return;
  }
  showReceipt(state.receipt);
}

function renderFailed() {
  setText("title", "Payment not taken");
  setText("sub", "Nothing was kept: " + (state.problems.length ? state.problems.join("; ") : "the payment did not complete") + ".");
  only();
  status("If money left your card, it is refunded in full. Ask your agent to start a new checkout.", "warn");
}

function renderClosed() {
  setText("title", "This checkout is closed");
  setText("sub", (state.messages[0] || "It was canceled or it expired.") + " Nothing was charged.");
  only();
  $("terms").hidden = true;
  $("what").hidden = true;
  status("");
}

// ---------------------------------------------------------------- U1: the passkey

function cannotSign() {
  if (!window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials) {
    return "This browser cannot use passkeys here. Open the link in a current browser over https.";
  }
  return null;
}

async function register(rpId) {
  // Creating the passkey is not sent to the server: it only gives this page the public key.
  const created = await navigator.credentials.create({ publicKey: {
    rp: { id: rpId, name: "Countersign" },
    user: { id: crypto.getRandomValues(new Uint8Array(16)), name: "countersign-shopper", displayName: "Countersign shopper" },
    challenge: crypto.getRandomValues(new Uint8Array(32)),
    pubKeyCredParams: [{ type: "public-key", alg: ES256 }],
    authenticatorSelection: { residentKey: "preferred", userVerification: "preferred" },
    attestation: "none",
    timeout: 120000,
  } });
  const response = created.response;
  if (typeof response.getPublicKey !== "function") {
    throw new Error("This browser cannot share a passkey's public key. Try a current Chrome, Safari, Edge or Firefox.");
  }
  if (typeof response.getPublicKeyAlgorithm === "function" && response.getPublicKeyAlgorithm() !== ES256) {
    throw new Error("This device made a passkey the merchant cannot check (it needs ES256).");
  }
  const spki = response.getPublicKey();
  if (!spki) throw new Error("This device did not share the passkey's public key.");
  return { credential_id: b64url(created.rawId), public_key_spki_der: b64url(spki) };
}

async function approve() {
  if (busy || !state || state.stage !== "approve") return;
  busy = true;
  $("approve").disabled = true;
  clearTimeout(pollTimer);
  let rpId = location.hostname;
  try {
    // The challenge for the terms on the session now. If they are not the ones on screen, stop.
    const fresh = await call("GET", URLS.approval);
    const w = fresh.webauthn;
    rpId = w.rp_id;
    setText("rp", w.origin);
    if (location.origin !== w.origin) {
      throw new Error("This page is not on " + w.origin + ", so a passkey signed here would not be accepted.");
    }
    if (fresh.terms.terms_hash !== shownHash) {
      state = await call("GET", URLS.state);
      render();
      status("The terms changed while this page was open. Check them again, then approve.", "warn");
      return;
    }
    if (fresh.signed) { state.signed = true; await refresh(); return; }
    let cred = loadCred(rpId);
    if (!cred) {
      status("Creating a passkey for this site on your device…", "info");
      cred = await register(rpId);
      saveCred(rpId, cred);
    }
    status("Confirm on your device to sign these terms…", "info");
    const assertion = await navigator.credentials.get({ publicKey: {
      challenge: fromB64url(w.challenge), // the server's challenge bytes, never the terms hash
      rpId,
      allowCredentials: [{ type: "public-key", id: fromB64url(cred.credential_id) }],
      userVerification: w.user_verification || "preferred",
      timeout: 120000,
    } });
    const r = assertion.response;
    status("Checking your signature with the merchant…", "info");
    await call("POST", URLS.approval, {
      credential_id: b64url(assertion.rawId),
      public_key_spki_der: cred.public_key_spki_der,
      authenticator_data: b64url(r.authenticatorData),
      client_data_json: b64url(r.clientDataJSON),
      signature: b64url(r.signature),
    });
    status("Approved. Your agent can complete the purchase now.", "ok");
  } catch (err) {
    $("reset").hidden = !loadCred(rpId);
    if (err && err.name === "NotAllowedError") {
      status("Nothing was signed: the passkey prompt was closed or timed out. You can try again.", "warn");
    } else if (err && err.code === "assertion_invalid") {
      status("The merchant did not accept the signature: " + err.message + ".", "error");
    } else {
      status((err && err.message) || "Something went wrong; nothing was signed.", "error");
    }
  } finally {
    busy = false;
    await refresh();
  }
}

// ---------------------------------------------------------------- the payment

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (window.AirwallexComponentsSDK) return resolve();
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Airwallex's payment form could not be loaded"));
    document.head.append(s);
  });
}

async function mountDropIn(p) {
  if (dropIn.mounted || !p) return;
  dropIn.mounted = true;
  $("test-card").hidden = p.env !== "demo";
  try {
    await loadScript(DROP_IN_SDK);
    const sdk = window.AirwallexComponentsSDK;
    await sdk.init({ env: p.env, enabledElements: ["payments"] });
    const options = { intent_id: p.intent_id, client_secret: p.client_secret, currency: p.currency };
    if (p.quote_id) options.quote_id = p.quote_id; // the locked rate; Drop-in applies it
    const el = await sdk.createElement("dropIn", options);
    el.mount("dropIn");
    el.on("success", () => { dropIn.paid = true; status(""); confirmPayment(0); render(); });
    el.on("error", (e) => {
      const message = e && e.detail && e.detail.error && e.detail.error.message;
      status("The payment did not go through" + (message ? ": " + message : "") + ". You can try again.", "error");
    });
  } catch (err) {
    dropIn.mounted = false;
    status((err && err.message) || "The payment form could not be loaded.", "error");
  }
}

async function confirmPayment(attempt) {
  // Drop-in's "success" is only a hint: the merchant fetches the payment from Airwallex itself.
  try {
    state = await call("POST", URLS.confirm);
    render();
    if (state.stage === "pay" && attempt < 5) setTimeout(() => confirmPayment(attempt + 1), 3500);
  } catch (err) {
    if (err.statusCode === 429 && attempt < 5) setTimeout(() => confirmPayment(attempt + 1), 3500);
    // otherwise the webhook settles it, and polling shows it
  }
}

// ---------------------------------------------------------------- the receipt

async function showReceipt(receipt) {
  if (shownReceipt === receipt.signature) return;
  shownReceipt = receipt.signature;
  const body = receipt.body;
  setText("paid-sub", "Signed by the merchant " + when(body.decided_at) + ". Keep it: it shows what you approved, what you paid, and which agent asked.");
  const json = JSON.stringify(receipt, null, 1);
  $("download").onclick = () => {
    const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "countersign-receipt-" + body.order_number + ".json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  // The fragment never reaches a server: /verify checks the receipt in the browser that opens it.
  $("verify-link").href = "/verify#r=" + b64url(new TextEncoder().encode(JSON.stringify(receipt)));
  const list = $("checks");
  list.replaceChildren();
  let result;
  try {
    result = await window.CountersignReceipt.verify(receipt, state.merchant_key.public_key);
  } catch (e) {
    result = { ok: false, merchant: null, checks: [] };
  }
  if (result.merchant === null) {
    try {
      const server = await call("POST", "/api/receipts/verify", { receipt, merchant_key: state.merchant_key.public_key });
      result = { ok: server.ok, checks: server.findings.map((text) => ({ ok: server.ok, text: text + " (checked by the server)" })) };
    } catch (e) { /* show what the browser could check */ }
  }
  for (const c of result.checks) {
    const li = document.createElement("li");
    li.textContent = c.text;
    if (c.ok === false) li.className = "bad";
    if (c.ok === null) li.className = "unknown";
    list.append(li);
  }
}

// ---------------------------------------------------------------- start

$("approve").addEventListener("click", approve);
$("reset").addEventListener("click", () => {
  forgetCred(location.hostname);
  $("reset").hidden = true;
  status("This page forgot its passkey. The next approval creates a new one.", "info");
});
setInterval(tick, 15000);
refresh();
