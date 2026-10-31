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

$("check").addEventListener("click", check);
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
  if (m) {
    try {
      $("receipt").value = JSON.stringify(JSON.parse(fromB64url(m[1])), null, 1);
      check();
    } catch (e) {
      verdict("The link's receipt could not be read.", "bad");
    }
  }
})();
