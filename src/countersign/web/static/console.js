// A15, the merchant console: the live feed, checkouts and orders, and the levers (web/console.py, web/levers.py).
// Open it as /console#t=<COUNTERSIGN_LEVER_TOKEN>. The token moves to this tab's sessionStorage and the
// fragment is wiped from the address bar at once, so a screen recording never shows it.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "countersign.console.token";
const SLOW_DELIVERY = "standard, 12-15 business days";
let token = null;
let seq = 0;
let picked = null;          // the product the levers act on
const originals = {};       // first-seen values per sku, for Restore
let lastSessionId = null;

// ---------------------------------------------------------------- token

function takeToken() {
  const m = /(?:^|&)t=([^&]+)/.exec(location.hash.slice(1));
  if (m) {
    try { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1])); } catch (e) { /* memory only */ }
    token = decodeURIComponent(m[1]);
    history.replaceState(null, "", location.pathname);
  }
  if (!token) { try { token = sessionStorage.getItem(TOKEN_KEY); } catch (e) { token = null; } }
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method, cache: "no-store", credentials: "omit",
    headers: { Authorization: "Bearer " + token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) {
    const err = new Error((data && (data.error || data.detail)) || "HTTP " + r.status);
    err.statusCode = r.status;
    throw err;
  }
  return data;
}

function showLogin(message) {
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").textContent = message || "";
}

// ---------------------------------------------------------------- feed

function time(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderEvent(ev, fresh) {
  const li = el("li", ev.level + (fresh ? " fresh" : ""));
  li.dataset.id = ev.id;
  li.append(el("span", "bar"));
  const box = el("div");
  box.append(el("div", "ev-title", ev.title + (ev.count > 1 ? "  ×" + ev.count : "")));
  if (ev.detail) box.append(el("div", "ev-detail", ev.detail));
  const meta = el("div", "ev-meta");
  meta.append(el("span", "", time(ev.at)));
  if (ev.door) meta.append(el("span", "chip", ev.door));
  if (ev.session) meta.append(el("span", "", ev.session));
  if (ev.order) meta.append(el("span", "", ev.order));
  box.append(meta);
  li.append(box);
  return li;
}

async function pollFeed() {
  try {
    const out = await api("GET", "/api/console/feed?since=" + seq);
    const list = $("feed");
    for (const ev of out.events) {
      // A repeat of an event comes back with the same id and a higher count: update that line.
      const li = renderEvent(ev, true);
      const old = list.querySelector('li[data-id="' + ev.id + '"]');
      if (old) old.remove();
      list.prepend(li);
    }
    if (list.children.length > 1) { const empty = $("feed-empty"); if (empty) empty.remove(); }
    while (list.children.length > 200) list.lastChild.remove();
    seq = out.seq;
  } catch (err) {
    if (err.statusCode === 401) return showLogin("That token was not accepted.");
    if (err.statusCode === 404) return showLogin("The console is not enabled on this host (set COUNTERSIGN_LEVER_TOKEN).");
  }
  setTimeout(pollFeed, 1500);
}

// ---------------------------------------------------------------- state

function b64url(text) {
  const bytes = new TextEncoder().encode(text);
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pill(text, on) { const p = el("span", "pill" + (on ? " on" : ""), text); return p; }

function renderState(st) {
  const c = st.config;
  $("pills").replaceChildren(
    pill("create_checkout " + (c.acp ? "on" : "off"), c.acp),
    pill("shopper passkey " + (c.shopper_passkey ? "required" : "off"), c.shopper_passkey),
    pill("merchant: " + c.merchant, c.merchant === "airwallex"),
    pill("fallback MCP " + (c.fallback_mcp ? "on" : "off"), c.fallback_mcp),
    pill("TAP " + st.tap.verified + " verified · " + st.tap.refused + " refused", st.tap.verified > 0),
  );
  const sessions = $("sessions");
  sessions.replaceChildren();
  if (!st.sessions.length) {
    const tr = el("tr"); const td = el("td", "empty", "No checkouts yet."); td.colSpan = 4; tr.append(td); sessions.append(tr);
  }
  for (const s of st.sessions.slice(0, 8)) {
    const tr = el("tr");
    tr.append(el("td", "num", s.id.slice(0, 11) + "…"));
    tr.append(el("td", "", s.items.map((i) => i.quantity + "× " + i.title).join(", ") || "–"));
    tr.append(el("td", "num", s.total || "–"));
    const td = el("td");
    const label = s.order ? "completed" : s.awaiting_shopper ? "waiting" : s.status.replace(/_/g, " ");
    td.append(el("span", "status " + (s.order ? "completed" : s.awaiting_shopper ? "waiting" : s.status), s.awaiting_shopper ? "shopper to sign" : label));
    tr.append(td);
    sessions.append(tr);
  }
  const orders = $("orders");
  orders.replaceChildren();
  if (!st.orders.length) {
    const tr = el("tr"); const td = el("td", "empty", "No orders yet: nothing is charged until the countersign says yes."); td.colSpan = 4; tr.append(td); orders.append(tr);
  }
  for (const o of st.orders.slice(0, 8)) {
    const tr = el("tr");
    tr.append(el("td", "num", o.order_number));
    tr.append(el("td", "num", (o.amount || "–") + " " + (o.currency || "")));
    const td = el("td"); td.append(el("span", "status " + o.status, o.status)); tr.append(td);
    const rt = el("td");
    if (o.receipt) {
      const a = el("a", "", "check ↗");
      a.href = "/verify#r=" + b64url(JSON.stringify(o.receipt));
      a.target = "_blank"; a.rel = "noopener";
      rt.append(a);
    } else rt.textContent = o.status === "pending" ? "after payment" : "–";
    tr.append(rt);
    orders.append(tr);
  }
  // Quick picks: the items in the newest checkout, the ones a lever should hit for the demo.
  const latest = st.sessions.find((s) => s.items.length);
  if (latest && latest.id !== lastSessionId) {
    lastSessionId = latest.id;
    const quick = $("quick");
    quick.replaceChildren();
    for (const item of latest.items) {
      const b = el("button", "", item.title);
      b.type = "button";
      b.onclick = () => pickById(item.id);
      quick.append(b);
    }
    $("quick-wrap").hidden = false;
  }
}

async function pollState() {
  try { renderState(await api("GET", "/api/console/state")); } catch (err) { if (err.statusCode === 401) return; }
  setTimeout(pollState, 3000);
}

// ---------------------------------------------------------------- levers

async function search(q) {
  const out = await api("GET", "/api/console/products?q=" + encodeURIComponent(q));
  const list = $("products");
  list.replaceChildren();
  for (const p of out.products) {
    const li = el("li");
    const b = el("button");
    b.type = "button";
    b.setAttribute("aria-pressed", String(picked && picked.id === p.id));
    b.append(el("span", "", p.title), el("span", "chip", p.price + " " + p.currency + (p.stock ? "" : " · out")));
    b.onclick = () => pick(p);
    li.append(b);
    list.append(li);
  }
  return out.products;
}

async function pickById(id) {
  const found = (await search(id)).find((p) => p.id === id);
  if (found) pick(found);
}

function pick(p) {
  picked = p;
  if (!originals[p.id]) originals[p.id] = { price: p.price, stock: p.stock, delivery: p.delivery };
  $("picked").hidden = false;
  $("p-title").textContent = p.title;
  $("p-id").textContent = p.id;
  $("p-price").textContent = p.price + " " + p.currency;
  $("p-stock").textContent = String(p.stock);
  $("p-delivery").textContent = p.delivery;
  $("toast").textContent = "";
}

async function lever(kind) {
  if (!picked) return;
  const sku = picked.id, o = originals[sku];
  const buttons = document.querySelectorAll("[data-lever]");
  buttons.forEach((b) => { b.disabled = true; });
  const toast = $("toast");
  toast.className = "toast";
  try {
    let changes = [];
    if (kind === "price-up") changes = [await api("POST", "/api/levers/price", { sku, percent: 12 })];
    if (kind === "out") changes = [await api("POST", "/api/levers/stock", { sku, stock: 0 })];
    if (kind === "slow") changes = [await api("POST", "/api/levers/delivery", { sku, delivery: SLOW_DELIVERY })];
    if (kind === "restore") {
      changes = [await api("POST", "/api/levers/price", { sku, price: o.price }),
                 await api("POST", "/api/levers/stock", { sku, stock: o.stock }),
                 await api("POST", "/api/levers/delivery", { sku, delivery: o.delivery })];
    }
    const message = changes.map((c) => c.field + ": " + c.before + " → " + c.after
      + (c.sync ? (c.sync.ok ? " (Airwallex catalogue updated)" : " (Airwallex catalogue not updated: " + (c.sync.error || "failed rows") + ")") : "")).join(" · ");
    await pickById(sku); // show the product's new values
    toast.textContent = message;
  } catch (err) {
    toast.className = "toast bad";
    toast.textContent = err.message;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// ---------------------------------------------------------------- start

function start() {
  $("login").hidden = true;
  $("app").hidden = false;
  pollFeed();
  pollState();
  search("");
}

document.querySelectorAll("[data-lever]").forEach((b) => b.addEventListener("click", () => lever(b.dataset.lever)));
let searchTimer = null;
$("q").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => search(e.target.value), 200); });
$("login-go").addEventListener("click", async () => {
  token = $("token").value.trim();
  try {
    await api("GET", "/api/console/state");
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch (e) { /* memory only */ }
    start();
  } catch (err) {
    showLogin(err.statusCode === 404 ? "The console is not enabled on this host." : "That token was not accepted.");
  }
});

takeToken();
if (token) start(); else showLogin();
