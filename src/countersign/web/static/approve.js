// U1: the shopper's approval page, /approve/<session id>.
//
// GET  /acp/approvals/<id>  the terms to show, plus webauthn.challenge, rp_id and origin
// POST /acp/approvals/<id>  the passkey assertion, every field base64url
//
// The passkey signs the challenge bytes the server sends, never the terms hash:
// the challenge also covers this session and the approval's expiry, so the same
// signature cannot approve an identical cart in another session.
//
// The server checks assertions against the SPKI public key the page sends, and
// only getPublicKey() at registration returns it. So the first approval on a
// device creates a passkey (no server call: the first valid signature binds the
// credential to its key) and this page keeps {credential id, public key} in
// localStorage. Neither is secret. Later approvals only ask the device to sign.
"use strict";

// Integer minor units per currency, as countersign.terms._MINOR_UNITS (a test keeps them equal).
const MINOR_UNITS = {"JPY": 0, "KRW": 0, "VND": 0, "IDR": 0, "CLP": 0, "BHD": 3, "KWD": 3, "OMR": 3};
const STORE_KEY = "countersign.passkey.v1";
const ES256 = -7;

const $ = (id) => document.getElementById(id);
const sessionId = decodeURIComponent(location.pathname.replace(/^\/approve\//, "").split("/")[0] || "");
const endpoint = "/acp/approvals/" + encodeURIComponent(sessionId);
let current = null;    // the last GET body
let memoryCred = null; // used when localStorage is unavailable
let busy = false;

// ---------------------------------------------------------------- base64url

function b64url(buffer) {
  const bytes = new Uint8Array(buffer);
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

function loadCred(rpId) {
  try {
    const all = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    const c = all[rpId];
    if (c && typeof c.credential_id === "string" && typeof c.public_key_spki_der === "string") return c;
  } catch (e) { /* private window or blocked storage: fall through */ }
  return memoryCred && memoryCred.rp_id === rpId ? memoryCred : null;
}

function saveCred(rpId, cred) {
  memoryCred = { ...cred, rp_id: rpId };
  try {
    const all = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    all[rpId] = cred;
    localStorage.setItem(STORE_KEY, JSON.stringify(all));
  } catch (e) { /* kept in memory for this visit */ }
}

function forgetCred(rpId) {
  memoryCred = null;
  try {
    const all = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    delete all[rpId];
    localStorage.setItem(STORE_KEY, JSON.stringify(all));
  } catch (e) { /* nothing stored */ }
}

// ---------------------------------------------------------------- display

function money(minor, currency) {
  const code = String(currency || "").toUpperCase();
  const digits = Object.prototype.hasOwnProperty.call(MINOR_UNITS, code) ? MINOR_UNITS[code] : 2;
  const value = Number(minor) / Math.pow(10, digits);
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

function render(data) {
  const t = data.terms;
  current = data;
  $("terms").hidden = false;
  $("what").hidden = false;
  setText("sub", "Your agent asked to buy this for you. Check it, then approve on this device.");
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
  const ceiling = $("ceiling");
  ceiling.replaceChildren();
  if (Number(t.ceiling) > Number(t.total)) {
    ceiling.append("If the exchange rate moves before payment, the charge can change, but never above ");
    const b = document.createElement("b");
    b.textContent = money(t.ceiling, t.currency);
    ceiling.append(b, ".");
  } else {
    ceiling.append("You are charged exactly this amount, or asked again.");
  }
  setText("hash", t.terms_hash);
  setText("session", data.session_id);
  setText("expires", t.expires_at);
  setText("rp", data.webauthn.origin);
  tick();
  $("reset").hidden = !loadCred(data.webauthn.rp_id);
  if (data.signed) {
    done();
    return;
  }
  const problem = cannotSign(data);
  $("approve").disabled = Boolean(problem);
  if (problem) status(problem, "error");
}

function tick() {
  if (!current) return;
  const left = new Date(current.terms.expires_at) - new Date();
  const el = $("valid");
  if (isNaN(left)) { el.textContent = ""; return; }
  if (left <= 0) {
    el.textContent = "These terms have expired. Ask your agent to quote again, then reload this page.";
    el.className = "valid late";
  } else {
    const minutes = Math.ceil(left / 60000);
    el.textContent = "Valid until " + when(current.terms.expires_at) + " (" + minutes + " min)";
    el.className = "valid";
  }
}

function done() {
  $("approve").disabled = true;
  $("approve").textContent = "Approved";
  $("reset").hidden = true;
  status("Approved. Your agent can complete the purchase now; you can close this page.", "ok");
}

function cannotSign(data) {
  if (!window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials) {
    return "This browser cannot use passkeys here. Open the link in a current browser over https.";
  }
  if (location.origin !== data.webauthn.origin) {
    return "This page is not on " + data.webauthn.origin + ", so a passkey signed here would not be accepted.";
  }
  return null;
}

// ---------------------------------------------------------------- server calls

async function call(method, body) {
  const r = await fetch(endpoint, {
    method, cache: "no-store", credentials: "omit",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) {
    const message = data && data.message ? data.message : "the merchant answered " + r.status;
    const err = new Error(message);
    err.code = data && data.code;
    err.statusCode = r.status;
    throw err;
  }
  return data;
}

async function load() {
  if (!sessionId) {
    setText("sub", "This approval link is incomplete.");
    return;
  }
  try {
    render(await call("GET"));
  } catch (err) {
    setText("sub", err.statusCode === 404 ? "This approval link is not active." : "These terms cannot be approved right now.");
    status(err.message, "error");
  }
}

// ---------------------------------------------------------------- the passkey

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
  if (busy || !current || current.signed) return;
  busy = true;
  $("approve").disabled = true;
  const w = current.webauthn;
  try {
    let cred = loadCred(w.rp_id);
    if (!cred) {
      status("Creating a passkey for this site on your device…", "info");
      cred = await register(w.rp_id);
      saveCred(w.rp_id, cred);
    }
    status("Confirm on your device to sign these terms…", "info");
    const assertion = await navigator.credentials.get({ publicKey: {
      challenge: fromB64url(w.challenge), // the server's challenge bytes, never the terms hash
      rpId: w.rp_id,
      allowCredentials: [{ type: "public-key", id: fromB64url(cred.credential_id) }],
      userVerification: w.user_verification || "preferred",
      timeout: 120000,
    } });
    const r = assertion.response;
    status("Checking your signature with the merchant…", "info");
    const result = await call("POST", {
      credential_id: b64url(assertion.rawId),
      public_key_spki_der: cred.public_key_spki_der,
      authenticator_data: b64url(r.authenticatorData),
      client_data_json: b64url(r.clientDataJSON),
      signature: b64url(r.signature),
    });
    current.signed = Boolean(result.signed);
    if (current.signed) done();
  } catch (err) {
    $("approve").disabled = false;
    $("reset").hidden = !loadCred(w.rp_id);
    if (err && err.name === "NotAllowedError") {
      status("Nothing was signed: the passkey prompt was closed or timed out. You can try again.", "warn");
    } else if (err && err.code === "assertion_invalid") {
      status("The merchant did not accept the signature: " + err.message + ". Reload to get the current terms.", "error");
    } else {
      status((err && err.message) || "Something went wrong; nothing was signed.", "error");
    }
  } finally {
    busy = false;
  }
}

$("approve").addEventListener("click", approve);
$("reset").addEventListener("click", () => {
  if (!current) return;
  forgetCred(current.webauthn.rp_id);
  $("reset").hidden = true;
  status("This page forgot its passkey. The next approval creates a new one.", "info");
});
setInterval(tick, 15000);
load();
