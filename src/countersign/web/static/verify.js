// /verify: check a Countersign receipt in this browser (window.CountersignReceipt, receipt.js).
// A link from the shopper's page carries the receipt in the fragment (#r=<base64url JSON>),
// which browsers never send to a server.
"use strict";

const $ = (id) => document.getElementById(id);

function fromB64url(text) {
  const s = text.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(out);
}

function verdict(text, kind) {
  $("verdict").textContent = text;
  $("verdict").className = "verdict " + kind;
}

async function loadKey() {
  try {
    const r = await fetch("/.well-known/countersign-merchant-key", { cache: "no-store", credentials: "omit" });
    if (!r.ok) return;
    const k = await r.json();
    if (!$("key").value) $("key").value = k.public_key;
    $("key-hint").textContent = "Loaded this merchant's published key (" + k.key_id + "). Paste another to check against it instead.";
  } catch (e) { /* paste one */ }
}

async function check() {
  const list = $("checks");
  list.replaceChildren();
  let receipt;
  try {
    receipt = JSON.parse($("receipt").value);
  } catch (e) {
    verdict("That is not JSON. Paste the whole receipt, braces included.", "bad");
    return;
  }
  const key = $("key").value.trim();
  if (!key) {
    verdict("Add the merchant's public key to check the signature against.", "unknown");
    return;
  }
  const result = await window.CountersignReceipt.verify(receipt, key);
  for (const c of result.checks) {
    const li = document.createElement("li");
    li.textContent = c.text;
    if (c.ok === false) li.className = "bad";
    if (c.ok === null) li.className = "unknown";
    list.append(li);
  }
  if (result.merchant === null) {
    verdict("This browser cannot check Ed25519 signatures. Use a current browser, or python -m countersign verify-receipt.", "unknown");
  } else if (result.ok) {
    verdict("Valid: the merchant signed exactly this, and the charge stayed within what the shopper approved.", "ok");
  } else {
    verdict("Not valid: see the check that failed.", "bad");
  }
}

// /receipt/<checkout id>: the link the agent gives the shopper after paying through Airwallex's checkout.
// The receipt is signed when Airwallex reports the payment, usually within seconds, so wait for it.
const ORDER = /^\/receipt\/(chk_[0-9a-f]{8,64})$/.exec(location.pathname);

function save() {
  const blob = new Blob([$("receipt").value + "\n"], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "countersign-receipt-" + (ORDER ? ORDER[1] : "order") + ".json";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function loadOrder(id) {
  $("title").textContent = "Your receipt";
  document.title = "Your receipt · Countersign";
  document.querySelector(".eyebrow").textContent = "Order " + id;
  document.querySelector(".sub").textContent = "The merchant signs this receipt when Airwallex confirms your payment. "
    + "It is checked below, in this browser. Save it: it proves what you were charged, to you, your bank or a dispute team.";
  document.querySelector(".how").hidden = true;
  for (let tries = 0; tries < 40; tries++) {
    let r;
    try {
      r = await fetch("/receipts/" + id, { cache: "no-store", credentials: "omit" });
    } catch (e) {
      r = null;
    }
    if (r && r.ok) {
      $("receipt").value = JSON.stringify(await r.json(), null, 1);
      $("save").hidden = false;
      await check();
      return;
    }
    verdict("Waiting for Airwallex to confirm the payment…", "unknown");
    await new Promise((done) => setTimeout(done, 3000));
  }
  verdict("No signed receipt for this order yet. If you have just paid, reload in a minute. "
    + "A payment that did not match the merchant's quote is refunded instead of receipted.", "unknown");
}

$("check").addEventListener("click", check);
$("save").addEventListener("click", save);
$("file").addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  $("receipt").value = await file.text();
  check();
});

(async () => {
  const k = /(?:^|&)k=([^&]+)/.exec(location.hash.slice(1));
  if (k) {
    // A demo visit's own merchant key, carried by the link: say so, since a link can carry any key.
    $("key").value = decodeURIComponent(k[1]);
    $("key-hint").textContent = "Key from the link (this demo visit's own merchant). A real merchant publishes its key at /.well-known/countersign-merchant-key.";
  } else {
    await loadKey();
  }
  const m = /(?:^|&)r=([A-Za-z0-9_-]+)/.exec(location.hash.slice(1));
  if (ORDER) {
    await loadOrder(ORDER[1]);
  } else if (m) {
    try {
      $("receipt").value = JSON.stringify(JSON.parse(fromB64url(m[1])), null, 1);
      check();
    } catch (e) {
      verdict("The link's receipt could not be read.", "bad");
    }
  }
})();
