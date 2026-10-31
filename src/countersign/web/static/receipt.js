// Countersign receipts, checked in the browser: the same checks as countersign.receipt.verify.
//
// 1. The merchant's Ed25519 signature over the canonical body (sorted keys, no
//    spaces, UTF-8, not ASCII-escaped: receipt._canonical), with WebCrypto.
// 2. Charged in the approved currency, and not above the approved ceiling.
// 3. C13, when the receipt carries it: the shopper's passkey signature, checked
//    offline. The challenge is recomputed from the receipt alone (label, session
//    id, approval hash, approval expiry), then the six WebAuthn checks.
// Nothing here asks the merchant's server; a browser without Ed25519 in WebCrypto
// gets `merchant: null` and the page can ask /api/receipts/verify instead.
"use strict";

window.CountersignReceipt = (function () {
  const enc = new TextEncoder();
  const LABEL = "countersign-u1-v1";

  function canonical(value) {
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + canonical(value[k])).join(",") + "}";
  }

  function bytesFromBase64(text, url) {
    if (typeof text !== "string") throw new Error("not base64");
    let s = url ? text.replace(/-/g, "+").replace(/_/g, "/") : text;
    s = s.replace(/=+$/, "");
    if (!/^[A-Za-z0-9+/]*$/.test(s)) throw new Error("not base64");
    const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function concat(a, b) {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function equal(a, b) {
    if (a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
    return diff === 0;
  }

  async function sha256(bytes) {
    return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  }

  // Exact decimal comparison of two amount strings ("7568.16"), never through floats.
  function compareAmounts(a, b) {
    const parse = (x) => {
      const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(x));
      if (!m) throw new Error("not an amount: " + x);
      return { neg: m[1] === "-", int: m[2], frac: m[3] || "" };
    };
    const pa = parse(a), pb = parse(b);
    const width = Math.max(pa.frac.length, pb.frac.length);
    const big = (p) => (p.neg ? -1n : 1n) * BigInt(p.int + p.frac.padEnd(width, "0"));
    const x = big(pa), y = big(pb);
    return x < y ? -1 : x > y ? 1 : 0;
  }

  // ECDSA signatures from WebAuthn are DER; WebCrypto wants r || s, 32 bytes each.
  function derToRaw(der) {
    let i = 0;
    if (der[i++] !== 0x30) throw new Error("not a DER signature");
    let len = der[i++];
    if (len & 0x80) i += len & 0x7f;
    const int = () => {
      if (der[i++] !== 0x02) throw new Error("not a DER signature");
      const n = der[i++];
      let v = der.slice(i, i + n);
      i += n;
      while (v.length > 32 && v[0] === 0) v = v.slice(1);
      if (v.length > 32) throw new Error("not a P-256 signature");
      const out = new Uint8Array(32);
      out.set(v, 32 - v.length);
      return out;
    };
    return concat(int(), int());
  }

  async function merchantSignature(receipt, publicKeyB64) {
    let key;
    try {
      key = await crypto.subtle.importKey("raw", bytesFromBase64(publicKeyB64), { name: "Ed25519" }, false, ["verify"]);
    } catch (e) {
      if (e && e.name === "NotSupportedError") return null; // this browser has no Ed25519 in WebCrypto
      return false;
    }
    try {
      return await crypto.subtle.verify({ name: "Ed25519" }, key, bytesFromBase64(receipt.signature),
                                        enc.encode(canonical(receipt.body)));
    } catch (e) {
      return false;
    }
  }

  // The shopper's passkey signature as the receipt records it (C13). A reason string, or {userVerified}.
  async function shopperSignature(record, hash, expiresAt) {
    let clientBytes, client, authData, spki, sig;
    try {
      clientBytes = bytesFromBase64(record.client_data_json, true);
      client = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(clientBytes));
      authData = bytesFromBase64(record.authenticator_data, true);
      spki = bytesFromBase64(record.public_key_spki_der, true);
      sig = derToRaw(bytesFromBase64(record.signature, true));
      for (const name of ["session_id", "origin", "rp_id"]) {
        if (typeof record[name] !== "string" || !record[name]) throw new Error(name);
      }
    } catch (e) {
      return "the recorded signature is incomplete";
    }
    const challenge = await sha256(enc.encode([LABEL, record.session_id, hash, expiresAt].join("\n")));
    if (!client || typeof client !== "object" || client.type !== "webauthn.get") return "clientDataJSON type is not webauthn.get";
    let signedChallenge;
    try { signedChallenge = bytesFromBase64(client.challenge, true); } catch (e) { return "the challenge is missing or not base64url"; }
    if (!equal(signedChallenge, challenge)) return "the challenge is not for these terms";
    if (client.origin !== record.origin) return "the origin does not match";
    if (client.crossOrigin === true) return "the assertion was made in a cross-origin frame";
    if (authData.length < 37) return "authenticatorData is too short";
    if (!equal(authData.slice(0, 32), await sha256(enc.encode(record.rp_id)))) return "the RP id hash does not match";
    if (!(authData[32] & 0x01)) return "the user-present flag is not set";
    let key;
    try {
      key = await crypto.subtle.importKey("spki", spki, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    } catch (e) {
      return "the public key is not ECDSA P-256";
    }
    const signed = concat(authData, await sha256(clientBytes));
    const ok = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, sig, signed);
    return ok ? { userVerified: Boolean(authData[32] & 0x04) } : "the signature does not verify";
  }

  // Every check, in order. checks: [{ok: true|false|null, text}]; ok overall; merchant: true|false|null.
  async function verify(receipt, publicKeyB64) {
    const checks = [];
    if (!receipt || typeof receipt !== "object" || !receipt.body || typeof receipt.signature !== "string") {
      return { ok: false, merchant: false, checks: [{ ok: false, text: "this is not a Countersign receipt" }] };
    }
    const merchant = await merchantSignature(receipt, publicKeyB64);
    if (merchant === false) {
      return { ok: false, merchant, checks: [{ ok: false, text: "the merchant's signature does not match this receipt" }] };
    }
    checks.push(merchant === null
      ? { ok: null, text: "this browser cannot check Ed25519 signatures itself" }
      : { ok: true, text: "signed by the merchant's key (" + receipt.key_id + "), checked in this browser" });
    const body = receipt.body, charged = body.charged || {}, approved = body.approval || {};
    if (charged.currency !== approved.currency) {
      checks.push({ ok: false, text: "charged in " + charged.currency + ", approved in " + approved.currency });
      return { ok: false, merchant, checks };
    }
    let over;
    try { over = compareAmounts(charged.amount, approved.ceiling) > 0; } catch (e) { over = true; }
    if (over) {
      checks.push({ ok: false, text: "charged " + charged.amount + " above the approved ceiling " + approved.ceiling });
      return { ok: false, merchant, checks };
    }
    checks.push({ ok: true, text: "charged " + charged.amount + " " + charged.currency + ", within the approved " + approved.ceiling });
    if (approved.shopper_assertion) {
      const result = await shopperSignature(approved.shopper_assertion, approved.hash, approved.expires_at);
      if (typeof result === "string") {
        checks.push({ ok: false, text: "the shopper's passkey signature does not check: " + result });
        return { ok: false, merchant, checks };
      }
      checks.push({ ok: true, text: "a passkey for " + approved.shopper_assertion.rp_id + " signed this approval ("
        + (result.userVerified ? "user verified" : "user present") + ")" });
    }
    checks.push(body.agent
      ? { ok: true, text: "requested by agent key " + body.agent.key_id + " (" + body.agent.tag + ")" }
      : { ok: true, text: "no agent identity recorded (the call came through the platform, not the TAP door)" });
    checks.push({ ok: true, text: "order " + body.order_number + ", payment " + body.payment_id });
    return { ok: merchant !== null, merchant, checks };
  }

  return { canonical, verify, compareAmounts, derToRaw };
})();
