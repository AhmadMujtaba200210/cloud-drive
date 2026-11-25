"use strict";
// PartsBridge PK front end. No inline script (strict CSP). Everything from the API is escaped before rendering.

const $ = id => document.getElementById(id);
const pkr = n => n == null ? "—" : "PKR " + Math.round(n).toLocaleString("en-PK");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl = u => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : null; } catch { return null; } };
const link = (u, text) => { const s = safeUrl(u); return s ? `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer nofollow">${esc(text)}</a>` : esc(text); };
const ICON = {think:"◆", plan:"◇", search:"⌕", pick:"→", read:"▤", follow:"↳", found:"✓", verify:"✓", drop:"×",
              warn:"!", wait:"…", oem:"#", cost:"₨", done:"✓", error:"!"};

let sort = "cheapest", lastQuery = null, busy = false;
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
  lastQuery = q; busy = true;
  $("go").disabled = true; $("go").textContent = "Researching…";
  $("tabs").hidden = true; $("examples").hidden = true; $("out").innerHTML = "";
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
  if (err || !result) { feed.finish(false, err || "Search failed."); return; }
  feed.finish(true, summaryLine(result));
  $("tabs").hidden = false;
  render(result);
  $("out").scrollIntoView({behavior: "smooth", block: "start"});
}

function summaryLine(d) {
  const priced = d.local.length + d.international.reduce((n, g) => n + Object.values(g.regions).reduce((m, v) => m + v.length, 0), 0);
  if (!priced && !d.contact_only.length) return "Finished, but no verified listing was found. Try another part name or the OEM number.";
  const best = d.cheapest_overall;
  return `Found ${priced} priced offers and ${d.contact_only.length} dealers to contact.` +
    (best ? ` Cheapest landed: ${pkr(best.total_landed_pkr)} from ${best.offer.vendor.name}.` : "");
}

// Sort tabs re-ask the server; the research is cached for 6 h, so this is instant and costs no quota.
async function resort() {
  if (!lastQuery || busy) return;
  let r;
  try { r = await fetch("/api/search", {method: "POST", headers, body: JSON.stringify({...lastQuery, sort})}); }
  catch { return; }
  if (r.ok) render(await r.json());
  else $("out").insertAdjacentHTML("afterbegin", `<div class="warn">Couldn't re-sort: ${esc(await errorText(r))}</div>`);
}

// ---------------- results ----------------
function vendorLine(v) {
  const bits = [v.address, v.phone, v.email].filter(Boolean).map(esc).join(" · ");
  const rating = v.rating != null ? `★ ${esc(v.rating)} (${esc(v.review_count)} reviews)` : "no rating";
  return `<div class="mute">${esc(v.region)}, ${esc(v.country)}${v.location_evidence ? ` (location: ${esc(v.location_evidence)})` : ""} · ${rating}${v.website ? ` · ${link(v.website, "site")}` : ""}</div><div class="mute">${bits}</div>`;
}

function trustBlock(o) {
  const c = o.offer.confidence, checks = o.offer.checks || [];
  if (!c && !checks.length) return "";
  return `<details><summary>${c ? `${esc(c[0].toUpperCase() + c.slice(1))} confidence: ` : ""}what we checked (${checks.length})</summary><ul class="checks">${checks.map(x => `<li>${esc(x)}</li>`).join("")}${o.offer.fits_note ? `<li>Fitment on page: “${esc(o.offer.fits_note)}”</li>` : ""}${o.offer.oem_number ? `<li>Part number on page: ${esc(o.offer.oem_number)}</li>` : ""}</ul></details>`;
}

function confBadge(o) {
  const c = o.offer.confidence;
  return c ? `<span class="conf conf-${esc(c)}">${esc(c)} confidence</span>` : "";
}

function optCard(o) {
  const s = o.shipping, c = o.customs, v = o.offer.vendor;
  const tags = o.tags.map(t => `<span class="tag">${esc(t)}</span>`).join("") + confBadge(o);
  let ship = "";
  if (o.all_shipping_quotes.length) {
    ship = `<details><summary>Shipping options (${o.all_shipping_quotes.length})</summary><div class="tablewrap"><table><tr><th>Carrier</th><th>Mode</th><th class="n">Shipping</th><th class="n">Duties</th><th class="n">Landed</th><th class="n">Days</th></tr>` +
      o.all_shipping_quotes.map(q => `<tr><td>${q.carrier_website ? link(q.carrier_website, q.carrier) : esc(q.carrier)}<div class="mute">${esc(q.rate_basis)}</div></td><td>${esc(q.mode)}</td><td class="n">${pkr(q.cost_pkr)}</td><td class="n">${pkr(q.duties_pkr)}</td><td class="n">${pkr(q.landed_total_pkr)}</td><td class="n">${esc(q.transit_days_min)}–${esc(q.transit_days_max)}</td></tr>`).join("") + `</table></div></details>`;
  }
  let cust = "";
  if (c) {
    cust = `<details><summary>Pakistan customs: ${pkr(c.total_duties_pkr)} · HS ${esc(c.hs_code)} · clearance ${esc(c.clearance_days_min)}–${esc(c.clearance_days_max)} days</summary>
      <div class="mute">${esc(c.hs_description)} · channel: ${esc(c.channel)} · assessable value ${pkr(c.assessable_value_pkr)}${c.de_minimis_applied ? " · below de minimis, no duty" : ""}</div>
      <div class="tablewrap"><table><tr><th>Levy</th><th class="n">Rate</th><th class="n">On</th><th class="n">Amount</th></tr>${c.lines.map(l => `<tr><td>${esc(l.name)}</td><td class="n">${esc(l.rate_pct)}%</td><td class="n">${pkr(l.base_pkr)}</td><td class="n">${pkr(l.amount_pkr)}</td></tr>`).join("")}</table></div>
      ${c.warnings.map(w => `<div class="warn">${esc(w)}</div>`).join("")}
      <ul class="mute">${c.rules.map(r => `<li>${esc(r)}</li>`).join("")}</ul></details>`;
  }
  return `<div class="opt"><div class="row"><div><b>${esc(v.name)}</b> ${tags}<div>${esc(o.offer.title)} · ${esc(o.offer.condition)} · ${esc(o.offer.authenticity)}</div>${vendorLine(v)}</div>
    <div class="right"><div class="total">${pkr(o.total_landed_pkr)}</div><div class="mute">item ${pkr(o.item_price_pkr)}${o.offer.currency !== "PKR" && o.offer.price != null ? ` (${esc(o.offer.currency)} ${esc(o.offer.price.toLocaleString())})` : ""}</div>
    <div class="mute">${s ? esc(s.carrier) + " · " : ""}${esc(o.days_min ?? "?")}–${esc(o.days_max ?? "?")} days door-to-door</div>
    ${o.offer.weight_is_estimate ? `<div class="mute">shipping weight estimated (${esc(o.offer.weight_kg)} kg), confirm with seller</div>` : ""}
    ${o.offer.evidence ? `<div class="mute">price on page: “${esc(o.offer.evidence)}” · ${link(o.offer.url, "source")}</div>` : ""}</div></div>${trustBlock(o)}${ship}${cust}</div>`;
}

function render(d) {
  let h = "";
  if (d.cheapest_overall) {
    const b = d.cheapest_overall;
    h += `<h2>Cheapest way to get it</h2><div class="best"><b>${pkr(b.total_landed_pkr)}</b> from ${esc(b.offer.vendor.name)} (${esc(b.offer.vendor.region)}, ${esc(b.offer.vendor.country)}) · ${b.is_local ? "local" : "imported via " + esc(b.shipping?.carrier)} · ${esc(b.days_min)}–${esc(b.days_max)} days ${confBadge(b)}</div>`;
  }
  const a = d.analysis || {};
  if (a.priced_offers) {
    const iv = a.import_vs_local, sp = a.cheapest_import_cost_split_pct;
    h += `<h2>Market analysis</h2><div class="opt">`;
    if (iv) h += `<div><b>${iv.import_saves_pkr >= 0 ? `Importing saves ${pkr(iv.import_saves_pkr)} (${esc(iv.import_saves_pct)}% below local)` : `Buy locally: importing costs ${(iv.best_import_pkr / iv.best_local_pkr).toFixed(1)}× the local price`}</b> · best local ${pkr(iv.best_local_pkr)} vs best import ${pkr(iv.best_import_pkr)} · import takes ${esc(iv.extra_days)} more days (worst case)</div>`;
    if (a.duty_break_even_note) h += `<div class="mute">Duty sensitivity: ${esc(a.duty_break_even_note)}</div>`;
    if (sp) h += `<div class="mute">Cheapest import cost split: item ${esc(sp.item)}% · shipping ${esc(sp.shipping)}% · duties & taxes ${esc(sp.duties_taxes)}%</div>`;
    h += `<div class="tablewrap"><table><tr><th>Country</th><th class="n">Offers</th><th class="n">Min landed</th><th class="n">Median</th><th class="n">Max</th></tr>` +
      Object.entries(a.landed_by_country || {}).map(([cc, q]) => `<tr><td>${esc(cc)}</td><td class="n">${esc(q.n)}</td><td class="n">${pkr(q.min)}</td><td class="n">${pkr(q.median)}</td><td class="n">${pkr(q.max)}</td></tr>`).join("") + `</table></div></div>`;
  }
  h += `<h2>In Pakistan</h2>` + (d.local.length ? d.local.map(optCard).join("") : `<p class="mute">No priced local listing found.</p>`);
  if (d.contact_only.length) {
    h += `<h2>Dealers with no listed price (contact for a quote)</h2>` + d.contact_only.map(o => `<div class="opt"><b>${esc(o.offer.vendor.name)}</b> <span class="tag">${o.is_local ? "Pakistan" : o.offer.vendor.country === "XX" ? "Location unverified" : esc(o.offer.vendor.country)}</span>${confBadge(o)}<div>${esc(o.offer.title)}</div>${vendorLine(o.offer.vendor)}${trustBlock(o)}</div>`).join("");
  }
  h += `<h2>Worldwide (by country, cheapest first)</h2>`;
  if (!d.international.length) h += `<p class="mute">No priced international listing with a confirmed seller location.</p>`;
  for (const g of d.international) {
    h += `<h3>${esc(g.country_name)}: from ${pkr(g.best_total_pkr)} landed</h3>`;
    for (const [region, opts] of Object.entries(g.regions)) h += `<div class="mute"><b>${esc(region)}</b></div>` + opts.map(optCard).join("");
  }
  h += `<h2>Notes</h2><ul class="mute">${d.disclaimers.map(x => `<li>${esc(x)}</li>`).join("")}<li>FX: ${esc(d.fx_as_of)}. Sources: ${d.sources_used.map(esc).join(", ")}</li></ul>`;
  const ds = (d.diagnostics || {}).web_deep_search;
  if (ds && !ds.error) h += `<details><summary>Web research stats</summary><div class="mute">${esc(ds.queries)} searches → ${esc(ds.search_results)} results → ${esc(ds.pages_selected)} pages picked + ${esc(ds.pages_followed ?? 0)} product pages followed → ${esc(ds.pages_scraped)} read (${esc(ds.scraper)}) → ${esc(ds.extracted)} extracted → ${esc(ds.kept)} kept (${esc(ds.dropped_ungrounded)} discarded as not on the page)${ds.oem_pass && ds.oem_pass.oem ? ` · part-number pass ${esc(ds.oem_pass.oem)}: +${esc(ds.oem_pass.new_offers ?? 0)}` : ""} · ${esc(ds.seconds ?? "?")} s${ds.cache ? " · cached" : ""}</div></details>`;
  $("out").innerHTML = h;
}

// ---------------- wiring ----------------
document.addEventListener("DOMContentLoaded", () => {
  $("f").addEventListener("submit", e => { e.preventDefault(); sort = "cheapest"; setTab(); search(); });
  document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => { sort = b.dataset.s; setTab(); resort(); }));
  document.querySelectorAll(".ex").forEach(b => b.addEventListener("click", () => {
    const q = JSON.parse(b.dataset.q), f = $("f");
    for (const [k, v] of Object.entries(q)) if (f.elements[k]) f.elements[k].value = v;
    f.requestSubmit();
  }));
  window.addEventListener("beforeunload", e => { if (busy) { e.preventDefault(); e.returnValue = ""; } });
});
function setTab() { document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("on", x.dataset.s === sort)); }
