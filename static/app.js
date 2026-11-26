"use strict";
// PartsBridge PK front end. No inline script (strict CSP). Everything from the API is escaped before rendering.

const $ = id => document.getElementById(id);
const pkr = n => n == null ? "—" : "PKR " + Math.round(n).toLocaleString("en-PK");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl = u => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : null; } catch { return null; } };
const link = (u, text) => { const s = safeUrl(u); return s ? `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer nofollow">${esc(text)}</a>` : esc(text); };
const ICON = {think:"◆", plan:"◇", search:"⌕", pick:"→", read:"▤", follow:"↳", found:"✓", verify:"✓", drop:"×",
              warn:"!", wait:"…", oem:"#", cost:"₨", done:"✓", error:"!"};

let sort = "cheapest", lastQuery = null, busy = false, searchT0 = 0;
// Product analytics (self-hosted Umami). Never sends personal data: only the part searched, toggles, outcome counts
// and which kind of link was clicked. No-op when the tracker is blocked or not loaded (local dev).
const track = (name, data) => { try { if (window.umami) window.umami.track(name, data); } catch { /* ignore */ } };
const bucket = p => p == null ? "none" : p < 2000 ? "<2k" : p < 5000 ? "2-5k" : p < 10000 ? "5-10k" : p < 25000 ? "10-25k" : p < 100000 ? "25-100k" : "100k+";
const tok = new URLSearchParams(location.search).get("token");
const headers = {"Content-Type": "application/json", ...(tok ? {"X-Access-Token": tok} : {})};

// ---------------- form ----------------
function body() {
  const f = new FormData($("f"));
  const conds = ["new","used","refurbished"].filter(c => f.get("c_" + c));
  const s = k => (f.get(k) || "").toString().trim();
  return {
    make: s("make"), model: s("model"), year: s("year") ? +s("year") : null,
    part_name: s("part_name"), oem_number: s("oem_number") || null, customer_city: s("customer_city"),
    is_tax_filer: !!f.get("is_tax_filer"), min_vendor_rating: +f.get("min_vendor_rating"),
    conditions: conds.length ? conds : ["new","used","refurbished"],
    authenticity: f.get("oem_only") ? ["oem"] : ["oem","oes","aftermarket"], sort,
    scope: f.get("local_only") ? "local" : "worldwide",
    include_social: !!f.get("include_social"),
  };
}

// ---------------- live research feed ----------------
const feed = {
  queue: [], typing: false, current: null, steps: 0, t0: 0, timer: null, gen: 0,
  stats: {pages: 0, searches: 0, prices: 0, markets: new Set()},
  open: new Map(), lastEvt: 0, idle: false,

  start() {
    this.gen++; this.queue = []; this.current = null; this.steps = 0; this.t0 = Date.now(); this.typing = false;
    this.stats = {pages: 0, searches: 0, prices: 0, markets: new Set()};
    this.open = new Map(); this.lastEvt = Date.now(); this.idle = false;
    $("live-log").innerHTML = ""; $("live-now").textContent = ""; $("live-ico").textContent = "";
    $("live").className = "live"; $("live").hidden = false; $("live-tip").hidden = false;
    $("live-tip").textContent = $("f").elements.local_only.checked
      ? "Searching Pakistani websites, marketplaces and social media shops takes about a minute. Keep this tab open; results appear here as soon as they're verified."
      : "Deep research across 12 countries takes 1–2 minutes. Keep this tab open; results appear here as soon as they're verified.";
    $("live-title").textContent = "Researching";
    this.renderStats(); clearInterval(this.timer);
    this.timer = setInterval(() => this.tick(), 1000); this.tick();
  },
  tick() {
    const s = Math.floor((Date.now() - this.t0) / 1000), m = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    $("live-clock").textContent = m;
    if (busy) document.title = `Researching… ${m} · PartsBridge PK`;
    // Quiet stretch (the AI is reading pages in the background): say what is still in flight, from real state.
    if (busy && !this.typing && Date.now() - this.lastEvt > 7000) {
      const names = [...this.open.values()];
      const where = names.length ? `Still working on ${names.slice(0, 3).join(", ")}${names.length > 3 ? ` and ${names.length - 3} more` : ""}` : "Still working";
      const secs = Math.round((Date.now() - this.lastEvt) / 1000);
      $("live-now").textContent = `${where}: the AI is reading their pages and checking every price against the page text… (${secs} s)`;
      $("live-ico").className = "ico k-wait"; $("live-ico").textContent = "…";
      this.idle = true;
    }
  },
  push(evt) {
    this.steps++; this.lastEvt = Date.now();
    if (this.idle && this.current) { $("live-now").textContent = this.current.text; this.idle = false; }
    const st = this.stats;
    if (evt.country) {
      if (evt.kind === "search" && evt.text.startsWith("Searching ")) this.open.set(evt.country, evt.text.slice(10).split(":")[0]);
      if (evt.kind === "found" || evt.kind === "drop") this.open.delete(evt.country);
    }
    if (evt.kind === "read" || evt.kind === "follow") st.pages++;
    if (evt.kind === "search") st.searches++;
    if (evt.kind === "verify") st.prices++;
    if (evt.kind === "found" && evt.country) st.markets.add(evt.country);
    this.renderStats();
    this.queue.push(evt);
    if (!this.typing) this.next();
  },
  renderStats() {
    const st = this.stats;
    $("live-stats").innerHTML = [["Searches", st.searches], ["Pages opened", st.pages], ["Prices verified", st.prices],
      ["Markets done", st.markets.size]].map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join("");
  },
  archive() {
    if (!this.current) return;
    const li = document.createElement("li");
    li.innerHTML = `<span class="ico k-${esc(this.current.kind)}">${ICON[this.current.kind] || "•"}</span><span>${esc(this.current.text)}</span><span class="t">${fmtT(this.current.t)}</span>`;
    const log = $("live-log"), stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.appendChild(li);
    if (stick) log.scrollTop = log.scrollHeight;
  },
  next() {
    const evt = this.queue.shift();
    if (!evt) { this.typing = false; return; }
    this.typing = true;
    this.archive();
    this.current = evt;
    $("live-ico").className = "ico k-" + evt.kind;
    $("live-ico").textContent = ICON[evt.kind] || "•";
    const el = $("live-now"), text = evt.text;
    // Type like a model "thinking"; speed up when events back up so the feed never lags reality.
    const backlog = this.queue.length;
    if (backlog > 8 || document.hidden || matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = text; return this.later(backlog > 8 ? 30 : 250); }
    const per = evt.kind === "think" ? 1 : backlog > 3 ? 6 : 3, delay = evt.kind === "think" ? 22 : 14;
    let i = 0; el.textContent = "";
    const gen = this.gen;
    const step = () => {
      if (gen !== this.gen) return;            // search finished or restarted: stop typing
      i = Math.min(text.length, i + per);
      el.textContent = text.slice(0, i);
      if (i < text.length) setTimeout(step, delay);
      else this.later(evt.kind === "think" ? 700 : 260);
    };
    step();
  },
  later(ms) { const gen = this.gen; setTimeout(() => { if (gen === this.gen) this.next(); }, this.queue.length ? Math.min(ms, 120) : ms); },
  finish(ok, msg) {
    this.gen++;
    this.queue.forEach(e => { this.archive(); this.current = e; });
    this.queue = []; this.typing = false;
    clearInterval(this.timer); this.tick();
    const secs = Math.round((Date.now() - this.t0) / 1000);
    $("live").classList.add(ok ? "done" : "failed");
    $("live-title").textContent = ok ? `Research complete · ${secs} s · ${this.steps} steps` : "Search stopped";
    $("live-tip").hidden = true;
    if (msg) { this.archive(); this.current = {kind: ok ? "done" : "error", text: msg, t: secs}; }
    $("live-ico").className = "ico k-" + (ok ? "done" : "error");
    $("live-ico").textContent = ok ? "✓" : "!";
    $("live-now").textContent = this.current ? this.current.text : "";
    $("live-log").scrollTop = $("live-log").scrollHeight;
    document.title = "PartsBridge PK";
  },
};
const fmtT = t => t == null ? "" : `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

// ---------------- streaming search ----------------
async function readStream(resp, onEvent) {
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const {value, done} = await reader.read();
    if (done) return;
    buf += value;
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      let event = "message", data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) { try { onEvent(event, JSON.parse(data)); } catch (e) { /* ignore malformed */ } }
    }
  }
}

async function errorText(r) {
  try { const j = await r.json(); return typeof j.detail === "string" ? j.detail : "Please check the form fields."; }
  catch { return `HTTP ${r.status}`; }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// If the stream drops (mobile network, proxy), the server keeps researching and caches the result.
async function recover(q) {
  feed.push({kind: "wait", text: "The live connection dropped, but the research is still running on the server. Reconnecting…"});
  track("stream_reconnect");
  for (let n = 0; n < 16; n++) {
    await sleep(n ? 15000 : 5000);
    let r;
    try { r = await fetch("/api/search", {method: "POST", headers, body: JSON.stringify(q)}); } catch { continue; }
    if (r.ok) return r.json();
    if (r.status !== 429) throw new Error(await errorText(r));
  }
  throw new Error("The search is taking too long. Please try again.");
}

async function search() {
  if (busy) return;
  const q = body();
  lastQuery = q; busy = true; searchT0 = Date.now();
  track("search_started", {make: q.make, model: q.model, part: q.part_name, year: q.year || "", city: q.customer_city,
    scope: q.scope, social: q.include_social ? "on" : "off"});
  $("go").disabled = true; $("go").textContent = "Researching…";
  $("results").hidden = true; $("examples").hidden = true; $("out").innerHTML = ""; D = null; F = null;
  feed.start();
  let result = null, err = null;
  try {
    const r = await fetch("/api/search/stream", {method: "POST", headers, body: JSON.stringify(q)});
    if (!r.ok) throw new Error(await errorText(r));
    await readStream(r, (ev, data) => {
      if (ev === "step") feed.push(data);
      else if (ev === "result") result = data;
      else if (ev === "error") err = data.message || "Search failed.";
    });
    if (!result && !err) result = await recover(q);
  } catch (e) {
    if (!result && !err) {
      if (e instanceof TypeError) { try { result = await recover(q); } catch (e2) { err = e2.message; } }
      else err = e.message;
    }
  }
  busy = false;
  $("go").disabled = false; $("go").textContent = "Search";
  const secs = Math.round((Date.now() - searchT0) / 1000);
  if (err || !result) { feed.finish(false, err || "Search failed."); track("search_failed", {reason: String(err || "unknown").slice(0, 80), secs}); return; }
  {
    const priced = allPriced(result), ds = (result.diagnostics || {}).web_deep_search || {};
    const best = priced.length ? Math.min(...priced.map(o => o.total_landed_pkr)) : null;
    track("search_completed", {secs, priced: priced.length, contact: result.contact_only.length, cheapest: bucket(best),
      scope: result.query.scope, source: ds.cache || "live", part: result.query.part_name});
    if (!priced.length && !result.contact_only.length) track("no_results", {make: q.make, model: q.model, part: q.part_name});
  }
  feed.finish(true, summaryLine(result));
  render(result);
  $("results").scrollIntoView({behavior: "smooth", block: "start"});
}

function summaryLine(d) {
  const priced = d.local.length + d.international.reduce((n, g) => n + Object.values(g.regions).reduce((m, v) => m + v.length, 0), 0);
  if (!priced && !d.contact_only.length) return "Finished, but no verified listing was found. Try another part name or the OEM number.";
  const best = d.cheapest_overall;
  return `Found ${priced} priced offers and ${d.contact_only.length} dealers to contact.` +
    (best ? ` Cheapest landed: ${pkr(best.total_landed_pkr)} from ${best.offer.vendor.name}.` : "");
}

// ---------------- results ----------------
let fx = {}, city = "";

function vendorLine(v) {
  const bits = [v.address, v.phone, v.email].filter(Boolean).map(esc).join(" · ");
  const rating = v.rating != null ? `★ ${esc(v.rating)} (${esc(v.review_count)} reviews)` : "no rating";
  const where = v.country === "XX" ? "location not confirmed" : `${esc(v.region)}, ${esc(v.country)}`;
  return `<div class="mute">${where} · ${rating}${v.website ? ` · ${link(v.website, "website")}` : ""}</div><div class="mute">${bits}</div>`;
}

function trustBlock(o) {
  const checks = o.offer.checks || [];
  const extra = (o.offer.fits_note ? 1 : 0) + (o.offer.oem_number ? 1 : 0);
  if (!checks.length && !extra) return "";
  return `<details><summary>What we checked (${checks.length + extra})</summary><ul class="checks">${checks.map(x => `<li>${esc(x)}</li>`).join("")}${o.offer.fits_note ? `<li>Fitment on page: “${esc(o.offer.fits_note)}”</li>` : ""}${o.offer.oem_number ? `<li>Part number on page: ${esc(o.offer.oem_number)}</li>` : ""}</ul></details>`;
}

// Every landed total is shown as the sum of its parts, so the numbers can be checked by hand.
function breakdown(o) {
  if (o.total_landed_pkr == null) return "";
  const s = o.shipping, off = o.offer, rows = [];
  let itemNote = "";
  if (off.currency !== "PKR" && off.price != null) {
    const rate = fx[off.currency];
    itemNote = `${esc(off.currency)} ${esc(off.price.toLocaleString())}${rate ? ` × ${esc(rate)} PKR` : ""}`;
  }
  rows.push(["Item price", o.item_price_pkr, itemNote]);
  if (s) {
    rows.push([o.is_local ? "Delivery to " + esc(city || "you") : "International shipping", s.cost_pkr,
      `${esc(s.carrier)}${s.mode && s.mode !== "local" ? ` (${esc(s.mode)})` : ""} · ${esc(s.chargeable_kg)} kg chargeable${off.weight_is_estimate ? ", estimated" : ""}`]);
    if (!o.is_local) rows.push(["Pakistan customs duties & taxes", s.duties_pkr || 0,
      o.customs ? `HS ${esc(o.customs.hs_code)}${o.customs.de_minimis_applied ? " · below de minimis" : ""}` : ""]);
  }
  return `<table class="bd">${rows.map(([k, v, n]) => `<tr><td>${k}${n ? `<div class="mute">${n}</div>` : ""}</td><td class="n">${pkr(v)}</td></tr>`).join("")}<tr class="sum"><td>Total delivered to your door</td><td class="n">${pkr(o.total_landed_pkr)}</td></tr></table>`;
}

function contactBar(o) {
  const v = o.offer.vendor, out = [];
  const page = safeUrl(o.offer.url);
  const ev = n => `data-ev="${n}" data-cc="${esc(v.country)}" data-social="${/facebook|instagram|tiktok/.test(o.offer.url || "") ? 1 : 0}"`;
  if (page) out.push(`<a class="btn" ${ev("open_product")} href="${esc(page)}" target="_blank" rel="noopener noreferrer nofollow">Open product page ↗</a>`);
  const site = safeUrl(v.website);
  if (site && page && new URL(site).origin !== new URL(page).origin) out.push(`<a class="btn ghost" ${ev("seller_website")} href="${esc(new URL(site).origin)}" target="_blank" rel="noopener noreferrer nofollow">Seller website ↗</a>`);
  if (v.phone) {
    const digits = v.phone.replace(/[^\d+]/g, "");
    out.push(`<a class="btn ghost" ${ev("contact_call")} href="tel:${esc(digits)}">Call ${esc(v.phone)}</a>`);
    const wa = digits.replace(/^\+/, "").replace(/^0(?=3\d{9}$)/, "92");
    if (/^923\d{9}$/.test(wa)) out.push(`<a class="btn ghost" ${ev("contact_whatsapp")} href="https://wa.me/${esc(wa)}" target="_blank" rel="noopener noreferrer">WhatsApp</a>`);
  }
  if (v.email) out.push(`<a class="btn ghost" ${ev("contact_email")} href="mailto:${esc(v.email)}">Email</a>`);
  return out.length ? `<div class="actions">${out.join("")}</div>` : "";
}

function optCard(o) {
  const s = o.shipping, c = o.customs, v = o.offer.vendor;
  const tags = o.tags.map(t => `<span class="tag">${esc(t)}</span>`).join("");
  let ship = "";
  if (o.all_shipping_quotes.length > 1) {
    ship = `<details><summary>Compare shipping options (${o.all_shipping_quotes.length})</summary><div class="tablewrap"><table><tr><th>Carrier</th><th>Mode</th><th class="n">Shipping</th><th class="n">Duties</th><th class="n">Landed</th><th class="n">Days</th></tr>` +
      o.all_shipping_quotes.map(q => `<tr><td>${q.carrier_website ? link(q.carrier_website, q.carrier) : esc(q.carrier)}<div class="mute">${esc(q.rate_basis)}</div></td><td>${esc(q.mode)}</td><td class="n">${pkr(q.cost_pkr)}</td><td class="n">${pkr(q.duties_pkr)}</td><td class="n">${pkr(q.landed_total_pkr)}</td><td class="n">${esc(q.transit_days_min)}–${esc(q.transit_days_max)}</td></tr>`).join("") + `</table></div></details>`;
  }
  let cust = "";
  if (c) {
    cust = `<details><summary>How the ${pkr(c.total_duties_pkr)} customs is calculated · clearance ${esc(c.clearance_days_min)}–${esc(c.clearance_days_max)} days</summary>
      <div class="mute">${esc(c.hs_description)} · channel: ${esc(c.channel)} · assessable value ${pkr(c.assessable_value_pkr)}${c.de_minimis_applied ? " · below de minimis, no duty" : ""}</div>
      <div class="tablewrap"><table><tr><th>Levy</th><th class="n">Rate</th><th class="n">On</th><th class="n">Amount</th></tr>${c.lines.map(l => `<tr><td>${esc(l.name)}</td><td class="n">${esc(l.rate_pct)}%</td><td class="n">${pkr(l.base_pkr)}</td><td class="n">${pkr(l.amount_pkr)}</td></tr>`).join("")}</table></div>
      ${c.warnings.map(w => `<div class="warn">${esc(w)}</div>`).join("")}
      <ul class="mute">${c.rules.map(r => `<li>${esc(r)}</li>`).join("")}</ul></details>`;
  }
  return `<div class="opt" id="o-${idOf.get(o) ?? ""}"><div class="row"><div class="grow"><span class="flag">${flag(v.country)}</span> <b>${esc(v.name)}</b> ${tags}<div>${esc(o.offer.title)} · ${esc(o.offer.condition)} · ${esc(o.offer.authenticity)}</div>${vendorLine(v)}
    ${o.offer.evidence ? `<div class="mute">Price on the seller's page: “${esc(o.offer.evidence)}”</div>` : ""}
    <div class="mute">${esc(o.days_min ?? "?")}–${esc(o.days_max ?? "?")} days to your door</div></div>
    <div class="right"><div class="total">${pkr(o.total_landed_pkr)}</div>${breakdown(o)}</div></div>${contactBar(o)}${trustBlock(o)}${ship}${cust}</div>`;
}

// ---------------- flags, filters, chart ----------------
const flag = cc => /^[A-Z]{2}$/.test(cc || "") && cc !== "XX"
  ? String.fromCodePoint(...[...cc].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : "🌐";
let D = null, ALL = [], cname = {}, F = null, idOf = new Map();

function allPriced(d) {
  const out = [...d.local];
  for (const g of d.international) for (const v of Object.values(g.regions)) out.push(...v);
  return out.filter(o => o.total_landed_pkr != null);
}

function render(d) {
  const fresh = !D || !F;
  D = d; fx = d.fx_rates_pkr || {}; city = d.query.customer_city;
  ALL = allPriced(d);
  idOf = new Map(ALL.map((o, i) => [o, i]));
  cname = {PK: "Pakistan", XX: "Location not confirmed"};
  for (const g of d.international) cname[g.country] = g.country_name;
  const prices = ALL.map(o => o.total_landed_pkr);
  const lo = prices.length ? Math.min(...prices) : 0, hi = prices.length ? Math.max(...prices) : 0;
  const ccs = [...new Set([...ALL, ...d.contact_only].map(o => o.offer.vendor.country))];
  const conds = [...new Set([...ALL, ...d.contact_only].map(o => o.offer.condition))];
  if (fresh || !F) F = {min: lo, max: hi, lo, hi, countries: new Set(ccs), conds: new Set(conds)};
  else { F.min = lo; F.max = hi; }
  buildSide(ccs, conds);
  $("results").hidden = false;
  update();
}

// Log-scaled slider: equal slider distance = equal price RATIO, so PKR 1,000-2,000 gets as much room as 50,000-100,000.
const toPrice = t => F.min >= F.max ? F.max : Math.exp(Math.log(F.min) + (t / 1000) * (Math.log(F.max) - Math.log(F.min)));
const toPos = p => F.min >= F.max ? 1000 : Math.round(1000 * (Math.log(p) - Math.log(F.min)) / (Math.log(F.max) - Math.log(F.min)));
const nice = p => { const m = Math.pow(10, Math.max(0, Math.floor(Math.log10(p)) - 1)); return Math.round(p / m) * m; };

function buildSide(ccs, conds) {
  const count = cc => ALL.filter(o => o.offer.vendor.country === cc).length + D.contact_only.filter(o => o.offer.vendor.country === cc).length;
  $("f-country").innerHTML = ccs.sort((a, b) => (a === "PK" ? -1 : b === "PK" ? 1 : 0) || count(b) - count(a)).map(cc =>
    `<label class="chk"><input type="checkbox" data-cc="${esc(cc)}" ${F.countries.has(cc) ? "checked" : ""}> <span class="flag">${flag(cc)}</span> ${esc(cname[cc] || cc)} <span class="mute">(${count(cc)})</span></label>`).join("");
  $("f-cond").innerHTML = conds.map(c => `<label class="chk"><input type="checkbox" data-cond="${esc(c)}" ${F.conds.has(c) ? "checked" : ""}> ${esc(c)}</label>`).join("");
  $("f-country").querySelectorAll("input").forEach(i => i.onchange = () => { i.checked ? F.countries.add(i.dataset.cc) : F.countries.delete(i.dataset.cc); update(); });
  $("f-cond").querySelectorAll("input").forEach(i => i.onchange = () => { i.checked ? F.conds.add(i.dataset.cond) : F.conds.delete(i.dataset.cond); update(); });
  const disabled = ALL.length < 2 || F.min >= F.max;
  $("r-min").disabled = $("r-max").disabled = disabled;
  $("r-min").value = toPos(Math.max(F.lo, F.min)); $("r-max").value = toPos(Math.min(F.hi, F.max));
  // budget presets at the 25th / 50th / 75th percentile of delivered prices
  const ps = ALL.map(o => o.total_landed_pkr).sort((a, b) => a - b);
  const q = f => ps[Math.min(ps.length - 1, Math.floor(f * (ps.length - 1)))];
  const marks = ps.length >= 4 ? [...new Set([q(0.25), q(0.5), q(0.75)].map(nice))].filter(v => v > F.min && v < F.max) : [];
  $("presets").innerHTML = marks.map(v => `<button type="button" class="chip" data-max="${v}">Under ${pkr(v)}</button>`).join("");
  $("presets").querySelectorAll("button").forEach(b => b.onclick = () => { F.lo = F.min; F.hi = +b.dataset.max; syncSliders(); update(); });
}

function syncSliders() { $("r-min").value = toPos(Math.max(F.lo, F.min)); $("r-max").value = toPos(Math.min(F.hi, F.max)); }

function inFilter(o) {
  return F.countries.has(o.offer.vendor.country) && F.conds.has(o.offer.condition);
}

function update() {
  $("range-out").textContent = ALL.length ? `${pkr(F.lo)} – ${pkr(F.hi)}` : "No prices yet";
  const key = sortKey();
  const eligible = ALL.filter(inFilter);
  const shown = eligible.filter(o => o.total_landed_pkr >= F.lo - 0.5 && o.total_landed_pkr <= F.hi + 0.5).sort(key);
  drawChart(eligible);
  $("out").innerHTML = renderList(shown, eligible);
  $("out").querySelectorAll("[data-setmax]").forEach(b => b.onclick = () => { F.hi = +b.dataset.setmax; syncSliders(); update(); });
  $("out").querySelectorAll("[data-setmin]").forEach(b => b.onclick = () => { F.lo = +b.dataset.setmin; syncSliders(); update(); });
}

function sortKey() {
  if (sort === "fastest") return (a, b) => a.days_max - b.days_max || a.total_landed_pkr - b.total_landed_pkr;
  if (sort === "best_value") return (a, b) => (b.value_score || 0) - (a.value_score || 0) || a.total_landed_pkr - b.total_landed_pkr;
  if (sort === "top_rated") return (a, b) => (b.offer.vendor.rating || 0) - (a.offer.vendor.rating || 0) || a.total_landed_pkr - b.total_landed_pkr;
  return (a, b) => a.total_landed_pkr - b.total_landed_pkr;
}

function bestBox(b, label) {
  const v = b.offer.vendor;
  return `<h2>${label}</h2><div class="best"><div class="row"><div class="grow">
      <div class="total">${pkr(b.total_landed_pkr)}</div>
      <div><span class="flag">${flag(v.country)}</span> <b>${esc(v.name)}</b> · ${esc(b.offer.title)}</div>
      <div class="mute">${v.country === "PK" ? `${esc(v.region !== "-" ? v.region : "Pakistan")}, Pakistan` : `${esc(v.region)}, ${esc(cname[v.country] || v.country)}`} · ${b.is_local ? "local seller" : "imported via " + esc(b.shipping?.carrier)} · ${esc(b.days_min)}–${esc(b.days_max)} days to your door</div>
      ${v.address ? `<div class="mute">${esc(v.address)}</div>` : ""}
      ${b.offer.evidence ? `<div class="mute">Price on the seller's page: “${esc(b.offer.evidence)}”</div>` : ""}</div>
      <div class="right">${breakdown(b)}</div></div>${contactBar(b)}</div>`;
}

function renderList(shown, eligible) {
  const d = D, local = d.query.scope === "local";
  let h = "";
  if (shown.length) {
    const cheapest = shown.reduce((a, b) => a.total_landed_pkr <= b.total_landed_pkr ? a : b);
    h += bestBox(cheapest, F.lo > F.min || F.hi < F.max ? "Cheapest in your price range" : "Cheapest option");
  } else if (eligible.length) {
    // Nothing in range: show the closest offers and say exactly how far to move the budget.
    const above = eligible.filter(o => o.total_landed_pkr > F.hi).sort((a, b) => a.total_landed_pkr - b.total_landed_pkr).slice(0, 3);
    const below = eligible.filter(o => o.total_landed_pkr < F.lo).sort((a, b) => b.total_landed_pkr - a.total_landed_pkr).slice(0, 3);
    const near = above.length ? above : below;
    const target = above.length ? Math.ceil(above[above.length - 1].total_landed_pkr) : Math.floor(below[below.length - 1].total_landed_pkr);
    const span = F.lo <= F.min + 0.5 ? `under ${pkr(F.hi)}` : F.hi >= F.max - 0.5 ? `above ${pkr(F.lo)}` : `between ${pkr(F.lo)} and ${pkr(F.hi)}`;
    h += `<div class="warn">No verified offer ${span}. ${above.length
      ? `The closest start at ${pkr(above[0].total_landed_pkr)} (${Math.round(100 * (above[0].total_landed_pkr / F.hi - 1))}% above your maximum). <button type="button" class="chip" data-setmax="${target}">Raise maximum to ${pkr(target)}</button>`
      : `The closest are below your minimum. <button type="button" class="chip" data-setmin="${target}">Lower minimum to ${pkr(target)}</button>`}</div>`;
    h += `<h2>Closest offers</h2>` + near.map(optCard).join("");
  } else if (ALL.length) {
    h += `<div class="warn">No offers match the seller-location / condition filters. Tick more boxes in the sidebar.</div>`;
  }
  if (shown.length) {
    const pkShown = shown.filter(o => o.is_local), imp = shown.filter(o => !o.is_local);
    h += `<h2>${flag("PK")} Pakistani sellers with prices <span class="mute">(${pkShown.length})</span></h2>` +
      (pkShown.length ? pkShown.map(optCard).join("") : `<p class="mute">No priced Pakistani listing in this range.</p>`);
    if (!local && imp.length) {
      h += `<h2>Imports <span class="mute">(${imp.length})</span></h2>`;
      const by = {};
      for (const o of imp) (by[o.offer.vendor.country] ||= []).push(o);
      for (const [cc, list] of Object.entries(by).sort((a, b) => a[1][0].total_landed_pkr - b[1][0].total_landed_pkr))
        h += `<h3><span class="flag">${flag(cc)}</span> ${esc(cname[cc] || cc)}: from ${pkr(list[0].total_landed_pkr)} delivered</h3>` + list.map(optCard).join("");
    }
  }
  const contact = d.contact_only.filter(inFilter);
  if (contact.length) {
    h += `<h2>Sellers to contact for a price <span class="mute">(${contact.length})</span></h2>` + contact.map(o => `<div class="opt"><span class="flag">${flag(o.offer.vendor.country)}</span> <b>${esc(o.offer.vendor.name)}</b> <span class="tag">${esc(cname[o.offer.vendor.country] || o.offer.vendor.country)}</span><div>${esc(o.offer.title)}</div>${vendorLine(o.offer.vendor)}${contactBar(o)}${trustBlock(o)}</div>`).join("");
  }
  if (local) h += `<p class="mute">Showing Pakistani sellers only. Turn off “Pakistan sellers only” and search again to compare imports from 12 countries.</p>`;
  const a = d.analysis || {};
  if (!local && a.import_vs_local) {
    const iv = a.import_vs_local, sp = a.cheapest_import_cost_split_pct;
    h += `<h2>Import vs local</h2><div class="opt"><div><b>${iv.import_saves_pkr >= 0 ? `Importing saves ${pkr(iv.import_saves_pkr)} (${esc(iv.import_saves_pct)}% below local)` : `Buy locally: importing costs ${(iv.best_import_pkr / iv.best_local_pkr).toFixed(1)}× the local price`}</b> · best local ${pkr(iv.best_local_pkr)} vs best import ${pkr(iv.best_import_pkr)} · import takes ${esc(iv.extra_days)} more days (worst case)</div>`
      + (a.duty_break_even_note ? `<div class="mute">Duty sensitivity: ${esc(a.duty_break_even_note)}</div>` : "")
      + (sp ? `<div class="mute">Cheapest import cost split: item ${esc(sp.item)}% · shipping ${esc(sp.shipping)}% · duties & taxes ${esc(sp.duties_taxes)}%</div>` : "") + `</div>`;
  }
  h += `<h2>Notes</h2><ul class="mute">${d.disclaimers.map(x => `<li>${esc(x)}</li>`).join("")}<li>FX: ${esc(d.fx_as_of)}. Sources: ${d.sources_used.map(esc).join(", ")}</li></ul>`;
  const ds = (d.diagnostics || {}).web_deep_search;
  if (ds && !ds.error) {
    const hunt = ds.hunt && ds.hunt.rounds ? ` · price hunt: ${esc(ds.hunt.rounds)} extra round(s), cheapest ${pkr(ds.hunt.start_min_pkr)} → ${pkr(ds.hunt.end_min_pkr)}` : "";
    h += `<details><summary>Web research stats</summary><div class="mute">${esc(ds.queries)} searches → ${esc(ds.search_results)} results → ${esc(ds.pages_selected)} pages picked + ${esc(ds.pages_followed ?? 0)} product pages followed → ${esc(ds.pages_scraped)} read (${esc(ds.scraper)}) → ${esc(ds.extracted)} extracted → ${esc(ds.kept)} kept (${esc(ds.dropped_ungrounded)} discarded as not on the page)${ds.oem_pass && ds.oem_pass.oem ? ` · part-number pass ${esc(ds.oem_pass.oem)}: +${esc(ds.oem_pass.new_offers ?? 0)}` : ""}${hunt}${ds.saved_llm_calls ? ` · ${esc(ds.saved_llm_calls)} AI calls saved by the knowledge base` : ""} · ${esc(ds.seconds ?? "?")} s${ds.cache ? ` · ${esc(ds.cache)}` : ""}</div></details>`;
  }
  return h;
}

// Dot strip: one row per seller group, x = delivered price (log scale when prices span > 8x). Hover for details,
// click a dot to jump to its card. Colours: categorical slots 1-2 (validated light + dark, CVD-safe).
function drawChart(eligible) {
  const wrap = $("chart-wrap");
  if (eligible.length < 2) { $("chart").hidden = true; return; }
  $("chart").hidden = false;
  const W = Math.max(300, wrap.clientWidth || 600), padL = 78, padR = 16, rowH = 40, top = 22;
  const rows = [["Pakistan", eligible.filter(o => o.is_local), "s-pk"], ["Imports", eligible.filter(o => !o.is_local), "s-imp"]].filter(r => r[1].length);
  const H = top + rows.length * rowH + 28;
  const ps = eligible.map(o => o.total_landed_pkr), mn = Math.min(...ps), mx = Math.max(...ps);
  const useLog = mx / mn > 8;
  const lo = useLog ? mn / 1.15 : Math.max(0, mn - (mx - mn) * 0.06), hi = useLog ? mx * 1.15 : mx + (mx - mn) * 0.06 || mx + 1;
  const X = p => padL + (W - padL - padR) * (useLog ? (Math.log(p) - Math.log(lo)) / (Math.log(hi) - Math.log(lo)) : (p - lo) / (hi - lo));
  const ticks = []; {
    if (useLog) { for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo && v <= hi) ticks.push(v); } }
    else { const step = 10 ** Math.floor(Math.log10((hi - lo) / 4)) * ([1, 2, 5].find(m => (hi - lo) / (10 ** Math.floor(Math.log10((hi - lo) / 4)) * m) <= 6) || 10); for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v); }
  }
  const short = v => v >= 1e6 ? (v / 1e6).toFixed(v % 1e6 ? 1 : 0) + "M" : v >= 1e3 ? (v / 1e3).toFixed(v % 1e3 ? 1 : 0) + "k" : String(Math.round(v));
  const bx0 = X(Math.max(F.lo, lo)), bx1 = X(Math.min(F.hi, hi));
  let g = `<rect class="band" x="${bx0}" y="${top - 6}" width="${Math.max(0, bx1 - bx0)}" height="${rows.length * rowH + 4}" rx="4"/>`;
  g += ticks.map(v => `<line class="grid" x1="${X(v)}" x2="${X(v)}" y1="${top - 6}" y2="${top + rows.length * rowH}"/><text class="axis" x="${X(v)}" y="${H - 8}" text-anchor="middle">${short(v)}</text>`).join("");
  const cheapest = eligible.reduce((a, b) => a.total_landed_pkr <= b.total_landed_pkr ? a : b);
  rows.forEach(([name, list, cls], r) => {
    const cy = top + r * rowH + rowH / 2;
    g += `<text class="rowlab" x="${padL - 10}" y="${cy + 4}" text-anchor="end">${name}</text><line class="base" x1="${padL}" x2="${W - padR}" y1="${cy}" y2="${cy}"/>`;
    list.forEach((o, i) => {
      const inR = o.total_landed_pkr >= F.lo - 0.5 && o.total_landed_pkr <= F.hi + 0.5;
      const jy = cy + ((i % 3) - 1) * 7;
      g += `<circle class="dot ${cls}${inR ? "" : " out"}${o === cheapest ? " min" : ""}" cx="${X(o.total_landed_pkr).toFixed(1)}" cy="${jy}" r="5" data-i="${idOf.get(o)}"/>`;
    });
  });
  const cx = X(cheapest.total_landed_pkr);
  g += `<text class="minlab" x="${Math.min(Math.max(cx, padL + 40), W - padR - 40)}" y="${top - 10}" text-anchor="middle">Cheapest ${pkr(cheapest.total_landed_pkr)}</text>`;
  wrap.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Delivered price of ${eligible.length} offers, cheapest ${pkr(cheapest.total_landed_pkr)}">${g}</svg>`;
  $("chart-sub").textContent = `· ${eligible.length} offers · ${useLog ? "log scale" : "linear scale"} · shaded = your price range`;
  const tip = $("tip");
  wrap.querySelectorAll("circle.dot").forEach(c => {
    const o = ALL[+c.dataset.i];
    c.addEventListener("mouseenter", () => {
      tip.innerHTML = `<b>${pkr(o.total_landed_pkr)}</b><br>${flag(o.offer.vendor.country)} ${esc(o.offer.vendor.name)}<br><span class="mute">${esc(o.offer.title.slice(0, 70))}</span>`;
      tip.hidden = false;
      const r = c.getBoundingClientRect(), pr = $("chart").getBoundingClientRect();
      tip.style.left = Math.min(r.left - pr.left + 10, pr.width - 220) + "px"; tip.style.top = (r.top - pr.top + 14) + "px";
    });
    c.addEventListener("mouseleave", () => { tip.hidden = true; });
    c.addEventListener("click", () => { const el = document.getElementById("o-" + c.dataset.i); if (el) { el.scrollIntoView({behavior: "smooth", block: "center"}); el.classList.add("pulse-card"); setTimeout(() => el.classList.remove("pulse-card"), 1600); } });
  });
}

// ---------------- autocomplete (make → model → year, part, city) ----------------
let CAT = {makes: {}, parts: []};
const CITIES = ["Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar", "Quetta", "Hyderabad",
  "Gujranwala", "Sialkot", "Sargodha", "Bahawalpur", "Sukkur", "Abbottabad", "Mardan", "Larkana", "Sheikhupura", "Gujrat",
  "Rahim Yar Khan", "Sahiwal", "Okara", "Mirpur", "Muzaffarabad", "Gwadar", "Jhelum", "Kasur", "Dera Ghazi Khan", "Nawabshah"];
const norm = s => (s || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, " ").trim();
const compact = s => norm(s).replace(/ /g, "");

function lev1(a, b) {          // true if edit distance <= 1 (typo tolerance)
  if (Math.abs(a.length - b.length) > 1) return false;
  let i = 0, j = 0, edits = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { i++; j++; continue; }
    if (++edits > 1) return false;
    if (a.length > b.length) i++; else if (b.length > a.length) j++; else { i++; j++; }
  }
  return edits + (a.length - i) + (b.length - j) <= 1;
}

// Score: exact > prefix > word prefix > compact prefix (crv -> CR-V) > substring > typo-tolerant prefix > subsequence.
function score(q, label) {
  const n = norm(label), qq = norm(q), c = compact(label), cq = compact(q);
  if (!qq) return 1;
  if (n === qq) return 100;
  if (n.startsWith(qq)) return 90 - n.length / 100;
  if (n.split(" ").some(w => w.startsWith(qq))) return 75;
  if (c.startsWith(cq)) return 70;
  if (n.includes(qq) || c.includes(cq)) return 55;
  if (cq.length >= 3 && lev1(cq, c.slice(0, cq.length))) return 40;
  let k = 0; for (const ch of c) if (ch === cq[k]) k++;
  return k === cq.length && cq.length >= 2 ? 20 : 0;
}

function combo(input, list, source, onPick, newestFirst = false) {
  let items = [], active = -1;
  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
  const pick = it => {
    input.value = it.value; close(); onPick && onPick(it); input.dispatchEvent(new Event("change"));
    track(it.custom ? "autocomplete_custom" : "autocomplete_pick", {field: input.name, value: it.custom ? it.value.slice(0, 60) : it.value});
  };
  const show = () => {
    const q = input.value;
    const opts = source();
    items = opts.map(o => ({...o, s: score(q, o.label) + (o.boost || 0)})).filter(o => o.s > 0)
      .sort((a, b) => b.s - a.s || (newestFirst ? b.label.localeCompare(a.label) : a.label.localeCompare(b.label))).slice(0, 8);
    const exact = items.some(o => norm(o.value) === norm(q));
    if (q.trim() && !exact) items.push({value: q.trim(), label: `Use “${q.trim()}”`, custom: true});
    if (!items.length) return close();
    list.innerHTML = items.map((o, i) => `<li role="option" id="${list.id}-${i}" class="${o.custom ? "custom" : ""}" aria-selected="false">${esc(o.label)}${o.hint ? ` <span class="mute">${esc(o.hint)}</span>` : ""}</li>`).join("");
    list.querySelectorAll("li").forEach((li, i) => li.addEventListener("mousedown", e => { e.preventDefault(); pick(items[i]); }));
    list.hidden = false; input.setAttribute("aria-expanded", "true"); active = -1;
  };
  const move = d => {
    if (list.hidden) return show();
    active = (active + d + items.length) % items.length;
    list.querySelectorAll("li").forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
    input.setAttribute("aria-activedescendant", `${list.id}-${active}`);
    list.children[active]?.scrollIntoView({block: "nearest"});
  };
  input.addEventListener("input", show);
  input.addEventListener("focus", show);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter" && !list.hidden && active >= 0) { e.preventDefault(); pick(items[active]); }
    else if (e.key === "Escape") close();
  });
}

const makeKey = v => Object.keys(CAT.makes).find(m => norm(m) === norm(v));

function setupCombos() {
  const f = $("f");
  combo($("i-make"), $("l-make"), () => Object.keys(CAT.makes).map(m => ({value: m, label: m})), () => {
    const mk = makeKey(f.make.value);       // models never cross makes: drop a model that belongs to another make
    if (mk && f.model.value && !CAT.makes[mk].some(m => norm(m) === norm(f.model.value))) f.model.value = "";
    f.model.focus();
  });
  combo($("i-model"), $("l-model"), () => {
    const mk = makeKey(f.make.value);
    if (mk) return CAT.makes[mk].map(m => ({value: m, label: m}));
    if (f.make.value.trim()) return [];                       // custom make: free text, no foreign suggestions
    return Object.entries(CAT.makes).flatMap(([mk2, ms]) => ms.map(m => ({value: m, label: m, hint: mk2, make: mk2})));
  }, it => { if (it.make && !f.make.value.trim()) f.make.value = it.make; f.year.focus(); });
  const now = new Date().getFullYear() + 1;
  combo($("i-year"), $("l-year"), () => Array.from({length: now - 1979}, (_, i) => String(now - i)).map(y => ({value: y, label: y})), () => f.part_name.focus(), true);
  combo($("i-part"), $("l-part"), () => CAT.parts.map(p => ({value: p, label: p})));
  combo($("i-city"), $("l-city"), () => CITIES.map(c => ({value: c, label: c})));
  f.make.addEventListener("change", () => {
    const mk = makeKey(f.make.value);
    if (mk) f.make.value = mk;                                 // normalise "toyota" -> "Toyota"
  });
}

// ---------------- wiring ----------------
document.addEventListener("DOMContentLoaded", () => {
  fetch("/static/catalog.json?v=1").then(r => r.json()).then(c => { CAT = c; }).catch(() => {});
  setupCombos();
  $("f").addEventListener("submit", e => {
    e.preventDefault();
    const y = $("f").year.value.trim();
    if (y && !/^(19[5-9]\d|20[0-3]\d)$/.test(y)) { $("f").year.setCustomValidity("Enter a year between 1950 and 2030"); $("f").year.reportValidity(); return; }
    $("f").year.setCustomValidity("");
    sort = "cheapest"; setTab(); search();
  });
  $("f").year.addEventListener("input", () => $("f").year.setCustomValidity(""));
  document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => { sort = b.dataset.s; setTab(); if (D) update(); track("sort_change", {sort}); }));
  // one delegated listener for every tracked link / control in the results
  document.addEventListener("click", e => {
    const a = e.target.closest("[data-ev]");
    if (a) track(a.dataset.ev, {country: a.dataset.cc || "", social: a.dataset.social === "1" ? "yes" : "no"});
    if (e.target.closest("#presets .chip")) track("price_preset", {max: bucket(+e.target.closest(".chip").dataset.max)});
    if (e.target.closest("[data-setmax],[data-setmin]")) track("budget_suggestion_used");
    if (e.target.closest("circle.dot")) track("chart_dot_click");
  });
  $("f").elements.local_only.addEventListener("change", e => track("toggle_pakistan_only", {on: e.target.checked ? "on" : "off"}));
  $("f").elements.include_social.addEventListener("change", e => track("toggle_social", {on: e.target.checked ? "on" : "off"}));
  $("side").addEventListener("change", e => { if (e.target.dataset.cc) track("filter_country", {country: e.target.dataset.cc, on: e.target.checked ? "on" : "off"}); if (e.target.dataset.cond) track("filter_condition", {condition: e.target.dataset.cond}); });
  let slideT; for (const id of ["r-min", "r-max"]) $(id).addEventListener("change", () => { clearTimeout(slideT); slideT = setTimeout(() => track("price_slider", {min: bucket(F.lo), max: bucket(F.hi)}), 400); });
  for (const id of ["r-min", "r-max"]) $(id).addEventListener("input", () => {
    let a = +$("r-min").value, b = +$("r-max").value;
    if (a > b) { if (id === "r-min") b = a; else a = b; $("r-min").value = a; $("r-max").value = b; }
    F.lo = a <= 0 ? F.min : nice(toPrice(a)); F.hi = b >= 1000 ? F.max : nice(toPrice(b));
    update();
  });
  $("r-reset").addEventListener("click", () => { if (!F) return; F.lo = F.min; F.hi = F.max; syncSliders(); update(); });
  document.querySelectorAll(".ex").forEach(b => b.addEventListener("click", () => {
    track("example_search", {example: b.textContent});
    const q = JSON.parse(b.dataset.q), f = $("f");
    for (const [k, v] of Object.entries(q)) if (f.elements[k]) f.elements[k].value = v;
    f.requestSubmit();
  }));
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => D && drawChart(ALL.filter(inFilter)), 150); });
  window.addEventListener("beforeunload", e => { if (busy) { track("search_abandoned", {secs: Math.round((Date.now() - searchT0) / 1000)}); e.preventDefault(); e.returnValue = ""; } });
});
function setTab() { document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("on", x.dataset.s === sort)); }
