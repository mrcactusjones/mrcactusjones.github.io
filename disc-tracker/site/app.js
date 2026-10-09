/* Disc Tracker dashboard (DESIGN.md section 6).
 *
 * Plain JS, no build step. Everything scraped (titles, store names, URLs) is untrusted:
 * it only ever reaches the DOM through textContent / createTextNode (see h()), and URLs
 * only become href after safeHttpUrl() accepts them. No HTML strings are ever parsed from data.
 */
(() => {
"use strict";

const CHART_SRC = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js";
const CHART_TIMEOUT_MS = 10000;
const PAGE_SIZE = 100;
const GAP_DAYS = 3;                       // a longer hole between points breaks the line
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const NONE = "__none__";                  // select value for discs without a disc_type
const CONDS = ["new", "used"];
const COND_LABEL = { new: "New", used: "Used" };
const RANGES = [["30", "30d"], ["90", "90d"], ["all", "All"]];
const KINDS = [["all", "All"], ["retail", "Retail"], ["marketplace", "Marketplace"]];   // chart source switch
const KIND_NOUN = { all: "all sources", retail: "retail stores", marketplace: "marketplace listings" };
// A sale the DB cannot confirm is only ever shown with this wording (DESIGN.md 10.4); never as "sold".
const INFERRED_LABEL = "Likely sold (inferred, low confidence - the seller may have delisted it)";
const CONFIRMED_LABEL = "Sold (confirmed)";
const INFERRED_SOURCE = "inferred_disappeared";
const SORTS = {                           // key -> default direction when first chosen
  name: "asc", type: "asc", min: "asc", median: "asc", stores: "desc", c7: "asc", c30: "asc",
};
const SORT_LABEL = {
  name: "Disc", type: "Type", min: "Lowest price", median: "Median price",
  stores: "Stores in stock", c7: "7-day change", c30: "30-day change",
};

/* ------------------------------------------------------------------ helpers */

const $ = (sel, root = document) => root.querySelector(sel);

/** Create an element. Text goes in as text nodes; event-handler and style attributes are refused. */
function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (/^on/i.test(k) || k === "style" || k === "srcdoc") throw new Error("refusing attribute " + k);
      if (k === "class") node.className = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));
/** The parser's edition vocabulary is lower case ("tour series"); show it like the other name parts. Display only. */
const titleCase = (s) => s.replace(/(^|[\s-])(\p{L})/gu, (m, sep, ch) => sep + ch.toUpperCase());
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** http(s) only, no embedded credentials; anything else (javascript:, data:, //host, junk) is refused.
 *  Browsers read a backslash like a slash and drop tabs and newlines, and open "https:host" as a host, so a string
 *  with any of those can lead somewhere other than the host it appears to name: it is refused outright. */
function safeHttpUrl(value) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!/^https?:\/\/[^\s\\\/?#]/i.test(text) || /[\\\s\x00-\x1f\x7f-\x9f]/.test(text)) return null;
  let u;
  try { u = new URL(text); } catch { return null; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  if (u.username || u.password) return null;
  return u.href;
}

/** Small outlined tag that marks a price or listing as coming from a marketplace (text, so never colour alone). */
const mpBadge = () => h("span", { class: "badge badge-mp" }, "Marketplace");

function extLink(url, ...kids) {
  return h("a", { class: "ext", href: url, target: "_blank", rel: "noopener noreferrer" },
    kids, h("span", { class: "sr" }, " (opens in a new tab)"));
}

let moneyFmts = {};
function setCurrency(code) {
  moneyFmts = {};
  const ok = /^[A-Z]{3}$/.test(str(code));
  for (const digits of [0, 2]) {
    try {
      moneyFmts[digits] = new Intl.NumberFormat("en-US", {
        style: "currency", currency: ok ? code : "USD",
        minimumFractionDigits: digits, maximumFractionDigits: digits,
      });
    } catch {
      moneyFmts[digits] = new Intl.NumberFormat("en-US", {
        style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
      });
    }
  }
}
setCurrency("USD");
const money = (v, digits = 2) => (num(v) == null ? "–" : moneyFmts[digits].format(v));

const DATE_FMT = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
const DATE_LONG = new Intl.DateTimeFormat("en-US", { weekday: "short", year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
const TICK_FMT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const TICK_FMT_Y = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "2-digit", timeZone: "UTC" });

/** "2026-10-08" -> days since epoch (UTC), NaN when malformed. */
function toDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(str(iso));
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) / 864e5 : NaN;
}
const dayDate = (day) => new Date(day * 864e5);
const SHORT_FMT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const fmtShort = (iso) => { const t = toDay(iso); return Number.isNaN(t) ? "\u2013" : SHORT_FMT.format(dayDate(t)); };
const fmtDate = (iso) => { const t = toDay(iso); return Number.isNaN(t) ? "–" : DATE_FMT.format(dayDate(t)); };

/** Fractional change as a badge: arrow + percent, never colour alone. Green = cheaper, red = dearer. */
function changeBadge(frac) {
  if (num(frac) == null) {
    return h("span", { class: "chg chg-na" }, h("span", { "aria-hidden": "true" }, "–"), h("span", { class: "sr" }, "not enough history"));
  }
  const pct = Math.round(Math.abs(frac) * 1000) / 10;
  if (pct === 0) {
    return h("span", { class: "chg chg-flat" }, h("span", { class: "ico", "aria-hidden": "true" }, "▬"), h("span", { class: "sr" }, "unchanged "), "0.0%");
  }
  const down = frac < 0;
  return h("span", { class: "chg " + (down ? "chg-down" : "chg-up") },
    h("span", { class: "ico", "aria-hidden": "true" }, down ? "▼" : "▲"),
    h("span", { class: "sr" }, down ? "down " : "up "), pct.toFixed(1) + "%");
}

// One shared collator: localeCompare(..., options) builds a collator per call, which made sorting thousands of discs lag.
const cmpStr = new Intl.Collator("en", { sensitivity: "base", numeric: true }).compare;

function tokenize(s) {
  return str(s).normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

/* -------------------------------------------------------------------- state */

const app = $("#app");
let data = null;                 // index.json
let discs = [];                  // prepared discs
const bySlug = new Map();
const historyCache = new Map();
let home = defaultHome();
let homeUI = null;
let homeScroll = 0;
let homeShown = PAGE_SIZE;       // rows revealed with "Show more", restored together with the scroll position
let currentRoute = null;
let routeToken = 0;
let activeView = null;
let chartPromise = null;
let mpFilter = false;            // true when the data has marketplace prices to include or exclude

function defaultHome() {
  return { q: "", mfr: "", type: "", cond: "new", stock: false, mp: true, sort: "name", dir: "asc", shown: PAGE_SIZE };
}

function homeParams(s) {
  const p = new URLSearchParams();
  const d = defaultHome();
  if (s.q) p.set("q", s.q);
  if (s.mfr) p.set("mfr", s.mfr);
  if (s.type) p.set("type", s.type);
  if (s.cond !== d.cond) p.set("c", s.cond);
  if (s.stock) p.set("stock", "1");
  if (!s.mp) p.set("mp", "0");
  if (s.sort !== d.sort || s.dir !== d.dir) { p.set("sort", s.sort); p.set("dir", s.dir); }
  return p;
}
function homeHash() {
  const qs = homeParams(home).toString();
  return "#/" + (qs ? "?" + qs : "");
}
function homeFromParams(p, mfrs, types) {
  const s = defaultHome();
  s.q = (p.get("q") || "").slice(0, 200);
  const mfr = p.get("mfr") || "";
  if (mfrs.includes(mfr)) s.mfr = mfr;
  const type = p.get("type") || "";
  if (types.includes(type)) s.type = type;
  if (CONDS.includes(p.get("c"))) s.cond = p.get("c");
  s.stock = p.get("stock") === "1";
  s.mp = !mpFilter || p.get("mp") !== "0";
  if (Object.hasOwn(SORTS, p.get("sort"))) {
    s.sort = p.get("sort");
    s.dir = p.get("dir") === "desc" ? "desc" : p.get("dir") === "asc" ? "asc" : SORTS[s.sort];
  }
  return s;
}

function discHref(slug, cond) {
  return "#/disc/" + slug + (cond === "used" ? "?c=used" : "");
}

function parseRoute() {
  const raw = location.hash.slice(1) || "/";
  const qi = raw.indexOf("?");
  const path = qi < 0 ? raw : raw.slice(0, qi);
  const params = new URLSearchParams(qi < 0 ? "" : raw.slice(qi + 1));
  const m = /^\/disc\/([^/]+)\/?$/.exec(path);
  if (m) {
    let slug = "";
    try { slug = decodeURIComponent(m[1]); } catch { /* malformed escape: treated as not found */ }
    return { name: "disc", slug, params };
  }
  return { name: "home", params };
}

/* ------------------------------------------------------------ data loading */

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(path + ": HTTP " + res.status);
  return res.json();
}

function prepare(index) {
  setCurrency(index.currency);
  discs = [];
  bySlug.clear();
  const block = (b) => {
    if (!b || typeof b !== "object") return null;
    return {
      min: num(b.min), median: num(b.median),
      stores_in_stock: num(b.stores_in_stock) ?? 0, stores_listing: num(b.stores_listing) ?? 0,
      change_7d: num(b.change_7d), change_30d: num(b.change_30d),
    };
  };
  mpFilter = false;
  for (const raw of index.discs) {
    if (!raw || typeof raw !== "object") continue;
    const d = {
      slug: str(raw.slug), manufacturer: str(raw.manufacturer), mold: str(raw.mold), plastic: str(raw.plastic),
      edition: titleCase(str(raw.edition)), player: str(raw.player), disc_type: str(raw.disc_type),
      last_seen: str(raw.last_seen), new: block(raw.new), used: block(raw.used),
      // Only discs with marketplace data carry `retail`: the same blocks computed from the retail stores alone.
      retail: raw.retail && typeof raw.retail === "object" ? { new: block(raw.retail.new), used: block(raw.retail.used) } : null,
    };
    if (d.retail) mpFilter = true;
    d._ok = SLUG_RE.test(d.slug);
    d._live = !!(d.new || d.used);          // false: every store delisted it, only its history remains
    const words = tokenize([d.manufacturer, d.mold, d.plastic, d.edition, d.player].join(" "));
    d._hay = words.join(" ");
    d._compact = words.join("");
    d._fields = [d.manufacturer, d.mold, d.plastic, d.edition, d.player];
    discs.push(d);
    if (d._ok) bySlug.set(d.slug, d);
  }
  rankDiscs();
}

/** Sort positions are computed once, so re-sorting thousands of discs on every keystroke is plain number compares.
 *  Names compare field by field (collation ignores control characters, so a joined string loses the field
 *  boundaries and "Aviar3" would sort before "Aviar"). */
function rankDiscs() {
  const order = discs.map((d, i) => i).sort((i, j) => {
    const a = discs[i]._fields, b = discs[j]._fields;
    for (let k = 0; k < a.length; k++) { const c = cmpStr(a[k], b[k]); if (c) return c; }
    return i - j;
  });
  order.forEach((i, rank) => { discs[i]._rank = rank; });
  const types = [...new Set(discs.map((d) => d.disc_type).filter(Boolean))].sort(cmpStr);
  const typeRank = new Map();
  types.forEach((t, i) => typeRank.set(t, i && cmpStr(types[i - 1], t) === 0 ? typeRank.get(types[i - 1]) : i));
  for (const d of discs) d._type = typeRank.get(d.disc_type) ?? -1;      // untyped discs are told apart before ranks are compared
}

/** {new: [...], used: [...]} of validated, date-sorted points. */
function normSeries(src) {
  const out = {};
  for (const c of CONDS) {
    const arr = Array.isArray(src && src[c]) ? src[c] : [];
    out[c] = arr
      .map((p) => ({ date: str(p && p.date), x: toDay(p && p.date), min: num(p && p.min), median: num(p && p.median), stores: num(p && p.stores_in_stock) }))
      .filter((p) => !Number.isNaN(p.x) && p.min != null && p.median != null)
      .sort((a, b) => a.x - b.x);
  }
  return out;
}

function normHistory(raw) {
  if (!raw || typeof raw !== "object") throw new Error("unexpected history format");
  const series = normSeries(raw.series);
  // Present only for discs with marketplace data; older files and retail-only discs have none (no source switch then).
  const sbk = raw.series_by_kind;
  const seriesByKind = sbk && typeof sbk === "object" ? { retail: normSeries(sbk.retail), marketplace: normSeries(sbk.marketplace) } : null;
  const listings = (Array.isArray(raw.listings) ? raw.listings : [])
    .filter((l) => l && typeof l === "object")
    .map((l) => ({
      store: str(l.store), title: str(l.title), url: safeHttpUrl(l.url),
      storeKind: l.store_kind === "marketplace" ? "marketplace" : "retail",
      condition: l.condition === "used" ? "used" : "new",
      weight: num(l.weight_g), price: num(l.price), compareAt: num(l.compare_at),
      available: l.available === true, lastSeen: str(l.last_seen),
    }))
    .sort((a, b) => (a.price ?? Infinity) - (b.price ?? Infinity));
  const sales = (Array.isArray(raw.sales) ? raw.sales : [])
    .filter((x) => x && typeof x === "object")
    .map((x) => ({
      date: str(x.date), x: toDay(x.date), price: num(x.price),
      condition: x.condition === "used" ? "used" : "new",
      // Confirmed only if the data says so in so many words; anything else (unknown value, missing field, an
      // "inferred_disappeared" source) is treated and worded as an inferred guess.
      confirmed: x.confidence === "confirmed" && x.source !== INFERRED_SOURCE,
      store: str(x.store), url: safeHttpUrl(x.url),
    }))
    .filter((x) => x.price != null && x.price > 0)
    .sort((a, b) => (Number.isNaN(b.x) ? -Infinity : b.x) - (Number.isNaN(a.x) ? -Infinity : a.x));   // newest first
  return { series, seriesByKind, listings, sales };
}

function loadHistory(slug) {
  if (!historyCache.has(slug)) {
    const p = getJSON("data/history/" + encodeURIComponent(slug) + ".json").then(normHistory);
    p.catch(() => historyCache.delete(slug));
    historyCache.set(slug, p);
  }
  return historyCache.get(slug);
}

/** Chart.js is only needed on disc pages, so it is fetched lazily and a slow CDN never blocks the list. */
function loadChart() {
  if (window.Chart) return Promise.resolve(window.Chart);
  if (!chartPromise) {
    chartPromise = new Promise((resolve) => {
      const s = document.createElement("script");
      const done = (v) => { clearTimeout(timer); resolve(v); };
      const timer = setTimeout(() => done(null), CHART_TIMEOUT_MS);
      s.addEventListener("load", () => done(window.Chart || null));
      s.addEventListener("error", () => { s.remove(); done(null); });
      s.src = CHART_SRC;
      s.async = true;
      s.crossOrigin = "anonymous";
      s.referrerPolicy = "no-referrer";
      document.head.append(s);
    });
    chartPromise.then((c) => { if (!c) chartPromise = null; });   // let a later visit retry
  }
  return chartPromise;
}

/* ------------------------------------------------------------ shared widgets */

let segCounter = 0;
/** Radio group styled as a segmented control. Real radios, so arrow keys and focus just work. */
function segmented({ label, hideLabel, options, value, onChange, small }) {
  const name = "seg" + ++segCounter;
  const labelId = name + "-label";
  const inputs = new Map();
  const group = h("div", { class: "seg" + (small ? " small" : ""), role: "radiogroup", "aria-labelledby": labelId });
  for (const [val, text] of options) {
    const input = h("input", { type: "radio", name, value: val });
    input.checked = val === value;
    input.addEventListener("change", () => { if (input.checked) onChange(val); });
    inputs.set(val, input);
    group.append(h("label", null, input, h("span", { class: "opt" }, text)));
  }
  return {
    el: h("div", { class: "field" }, h("span", { class: "lbl" + (hideLabel ? " sr" : ""), id: labelId }, label), group),
    set(val) { for (const [k, i] of inputs) i.checked = k === val; },
    disable(val, off, why) {
      const i = inputs.get(val);
      i.disabled = off;
      if (off && why) i.parentElement.setAttribute("title", why); else i.parentElement.removeAttribute("title");
    },
  };
}

function mount(node, title) {
  app.replaceChildren(node);
  document.title = title ? title + " · Disc Tracker" : "Disc Tracker";
}

function focusHeading() {
  const hd = $("h1", app);
  if (!hd) return;
  hd.setAttribute("tabindex", "-1");
  hd.focus({ preventScroll: true });
}

function messageView(title, body, ...extra) {
  return h("div", { class: "card empty" }, h("h1", null, title), h("p", null, body), extra);
}

/* ---------------------------------------------------------------- home view */

/** The block shown for a disc: the all-stores one, or, with marketplace prices excluded and the disc having marketplace
 *  data, the retail-only one the exporter provides (min/median/change cannot be derived from the blended numbers). */
function blockOf(d, cond, mp) {
  return mp || !d.retail ? d[cond] : d.retail[cond];
}

/** True when the lowest in-stock price of a disc comes from a marketplace: no retail store has it in stock, or the
 *  marketplace is strictly cheaper (ties go to retail). Only knowable for discs with marketplace data. */
function lowestFromMarketplace(d, cond) {
  const b = d[cond], r = d.retail && d.retail[cond];
  if (!d.retail || !b || b.stores_in_stock === 0) return false;
  return !r || r.stores_in_stock === 0 || b.min < r.min;
}

function matches(d, s, tokens) {
  const b = blockOf(d, s.cond, s.mp);
  // No block for this condition: the disc only exists in the other condition (hide it), or it has no live
  // listing at all (every store delisted it). Those stay findable, because their price history is still there.
  // With marketplace prices excluded a disc that has marketplace data but no live retail listing counts as
  // marketplace-only: it has no retail block, `d.retail` is set, so it is hidden here too.
  if (!b && (s.mp || !d.retail ? d._live : true)) return false;
  if (s.mfr && d.manufacturer !== s.mfr) return false;
  if (s.type && (d.disc_type || NONE) !== s.type) return false;
  if (s.stock && !(b && b.stores_in_stock > 0)) return false;
  for (const t of tokens) {
    if (!d._hay.includes(t) && !(t.length >= 3 && d._compact.includes(t))) return false;
  }
  return true;
}

const SORT_VALUE = {                      // a delisted disc has no block: null, which always sorts last
  min: (b) => b && b.min,
  median: (b) => b && b.median,
  stores: (b) => (b ? b.stores_in_stock * 1000 + b.stores_listing : null),
  c7: (b) => b && b.change_7d,
  c30: (b) => b && b.change_30d,
};

function sorter(s) {
  const sign = s.dir === "desc" ? -1 : 1;
  const byName = (a, b) => a._rank - b._rank;
  if (s.sort === "name") return (a, b) => sign * byName(a, b);
  if (s.sort === "type") {
    return (a, b) => {
      if (!a.disc_type !== !b.disc_type) return a.disc_type ? -1 : 1;     // untyped last either way
      return sign * (a._type - b._type) || byName(a, b);
    };
  }
  const val = SORT_VALUE[s.sort];
  return (a, b) => {
    const x = val(blockOf(a, s.cond, s.mp)), y = val(blockOf(b, s.cond, s.mp));
    if (x == null && y == null) return byName(a, b);
    if (x == null) return 1;                                              // missing values always last
    if (y == null) return -1;
    return sign * (x - y) || byName(a, b);
  };
}

const NO_BLOCK = { min: null, median: null, stores_in_stock: 0, stores_listing: 0, change_7d: null, change_30d: null };

const HEADERS = [
  ["name", "Disc", "c-name", false],
  ["type", "Type", "c-type", false],
  ["min", "Lowest", "c-min", true],
  ["median", "Median", "c-med", true],
  ["stores", "In stock", "c-stores", true],
  ["c7", "7d change", "c-7", true],
  ["c30", "30d change", "c-30", true],
];

function discRow(d, cond, mp) {
  const b = blockOf(d, cond, mp) || NO_BLOCK;
  const href = d._ok ? discHref(d.slug, cond) : null;
  const delisted = !blockOf(d, cond, mp);
  const out = delisted || b.stores_in_stock === 0;
  const sub = [d.manufacturer, d.edition, d.player].filter(Boolean).join(" · ");
  const tr = h("tr", { role: "row", class: out ? "is-out" : null, "data-slug": d._ok ? d.slug : null, "data-href": href });
  tr.append(
    h("td", { role: "cell", class: "c-name" },
      h(href ? "a" : "span", { href }, d.mold || "(unnamed)"),
      d.plastic ? [" ", h("span", { class: "plastic" }, d.plastic)] : null,
      sub ? h("span", { class: "sub" }, sub) : null),
    h("td", { role: "cell", class: "c-type" }, d.disc_type || h("span", { class: "muted" }, "–")),
    h("td", { role: "cell", class: "num price c-min", "data-label": "Lowest" }, money(b.min),
      out && !delisted ? h("span", { class: "note" }, "last known") : null,
      mp && !out && lowestFromMarketplace(d, cond) ? h("span", { class: "note" }, mpBadge()) : null),
    h("td", { role: "cell", class: "num c-med", "data-label": "Median" }, money(b.median)),
    h("td", { role: "cell", class: "num c-stores", "data-label": "In stock" },
      delisted ? h("span", { class: "oos" }, "No live listings")
        : out ? h("span", { class: "oos" }, "Out of stock")
          : [b.stores_in_stock + " of " + b.stores_listing, h("span", { class: "only-narrow" }, " in stock")]),
    h("td", { role: "cell", class: "num c-7", "data-label": "7d" }, changeBadge(b.change_7d)),
    h("td", { role: "cell", class: "num c-30", "data-label": "30d" }, changeBadge(b.change_30d)),
  );
  return tr;
}

function buildHome() {
  const mfrs = [...new Set(discs.map((d) => d.manufacturer).filter(Boolean))].sort(cmpStr);
  const types = [...new Set(discs.map((d) => d.disc_type).filter(Boolean))].sort(cmpStr);
  const hasBlankType = discs.some((d) => !d.disc_type);
  const typeValues = hasBlankType ? [...types, NONE] : types;
  const ui = { mfrs, typeValues };

  const select = (id, label, first, values, text) => {
    const sel = h("select", { id }, h("option", { value: "" }, first), values.map((v) => h("option", { value: v }, text ? text(v) : v)));
    return { sel, field: h("div", { class: "field f-" + id.slice(2) }, h("label", { class: "lbl", for: id }, label), sel) };
  };

  ui.q = h("input", { type: "search", id: "f-q", placeholder: "e.g. star destroyer, mcbeth luna", autocomplete: "off", spellcheck: "false", maxlength: "200" });
  const mfr = select("f-mfr", "Manufacturer", "All", mfrs);
  const type = select("f-type", "Disc type", "All", typeValues, (v) => (v === NONE ? "Unknown type" : v));
  ui.mfr = mfr.sel;
  ui.type = type.sel;
  ui.cond = segmented({
    label: "Condition", options: CONDS.map((c) => [c, COND_LABEL[c]]), value: home.cond,
    onChange: (c) => { home.cond = c; home.shown = PAGE_SIZE; updateHome(); },
  });
  ui.cond.el.classList.add("f-cond");
  ui.stock = h("input", { type: "checkbox", id: "f-stock" });
  ui.mp = h("input", { type: "checkbox", id: "f-mp" });
  ui.reset = h("button", { type: "button", class: "btn link", hidden: true }, "Reset filters");
  ui.count = h("span", { "aria-live": "polite", role: "status" });
  ui.sortSel = h("select", { id: "f-sort", "aria-label": "Sort by" },
    Object.keys(SORTS).map((k) => h("option", { value: k }, SORT_LABEL[k])));
  ui.dirBtn = h("button", { type: "button", class: "btn" });

  const form = h("form", { class: "filters", role: "search", "aria-label": "Find a disc" },
    h("div", { class: "field f-search" }, h("label", { class: "lbl", for: "f-q" }, "Search"), ui.q),
    mfr.field, type.field, ui.cond.el,
    h("div", { class: "field f-checks" + (mpFilter ? " two" : "") }, h("span", { class: "lbl sr" }, "Availability and sources"),
      h("label", { class: "check" }, ui.stock, "In stock only"),
      // Only offered when the data has marketplace prices: otherwise there is nothing to include or exclude.
      mpFilter ? h("label", { class: "check" }, ui.mp, "Include marketplace prices") : null));
  form.addEventListener("submit", (e) => e.preventDefault());

  ui.ths = {};
  const headRow = h("tr", { role: "row" });
  for (const [key, label, cls, numeric] of HEADERS) {
    const arrow = h("span", { class: "arrow idle", "aria-hidden": "true" }, "↕");
    const btn = h("button", { type: "button", class: "sort", "data-sort": key, "aria-label": "Sort by " + SORT_LABEL[key] }, label, arrow);
    const th = h("th", { role: "columnheader", scope: "col", class: (numeric ? "num " : "") + cls, "aria-sort": "none" }, btn);
    ui.ths[key] = { th, arrow };
    headRow.append(th);
  }
  ui.tbody = h("tbody", { role: "rowgroup" });
  ui.table = h("table", { class: "discs", role: "table", "aria-label": "Discs and their current prices" },
    h("thead", { role: "rowgroup" }, headRow), ui.tbody);
  ui.noMatch = h("div", { class: "empty", hidden: true }, h("h2", null, "No discs match"),
    h("p", null, "Try fewer words, another condition, or clear the filters."));
  ui.more = h("div", { class: "more", hidden: true });
  ui.moreBtn = h("button", { type: "button", class: "btn" });
  ui.more.append(ui.moreBtn);

  // events (delegated where it keeps the rows cheap)
  ui.q.addEventListener("input", () => { home.q = ui.q.value; home.shown = PAGE_SIZE; updateHome(); });
  ui.mfr.addEventListener("change", () => { home.mfr = ui.mfr.value; home.shown = PAGE_SIZE; updateHome(); });
  ui.type.addEventListener("change", () => { home.type = ui.type.value; home.shown = PAGE_SIZE; updateHome(); });
  ui.stock.addEventListener("change", () => { home.stock = ui.stock.checked; home.shown = PAGE_SIZE; updateHome(); });
  ui.mp.addEventListener("change", () => { home.mp = ui.mp.checked; home.shown = PAGE_SIZE; updateHome(); });
  ui.reset.addEventListener("click", () => {
    Object.assign(home, { q: "", mfr: "", type: "", cond: "new", stock: false, mp: true, shown: PAGE_SIZE });
    updateHome();
    ui.q.focus();
  });
  ui.table.addEventListener("click", (e) => {
    const btn = e.target.closest("button.sort");
    if (btn) { setSort(btn.dataset.sort); return; }
    const row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a") && !e.metaKey && !e.ctrlKey && !e.shiftKey && !window.getSelection().toString()) {
      location.hash = row.dataset.href.slice(1);
    }
  });
  ui.sortSel.addEventListener("change", () => setSort(ui.sortSel.value, true));
  ui.dirBtn.addEventListener("click", () => { home.dir = home.dir === "asc" ? "desc" : "asc"; updateHome(); });
  ui.moreBtn.addEventListener("click", () => {
    const first = home.shown;
    home.shown += PAGE_SIZE;
    updateHome();
    // the button may disappear (last page) and keyboard focus must not fall back to <body>: move it to the first new row
    const link = ui.tbody.rows[first] && ui.tbody.rows[first].querySelector("a");
    if (link) link.focus();
  });

  const bar = h("div", { class: "resultbar" }, ui.count, ui.reset, h("span", { class: "spacer" }),
    h("div", { class: "mobile-sort" }, h("label", { class: "lbl", for: "f-sort" }, "Sort"), ui.sortSel, ui.dirBtn));

  const root = h("div", null,
    h("h1", { class: "sr" }, "All discs"),
    form, bar,
    h("div", { class: "card" }, h("div", { class: "tablewrap" }, ui.table), ui.noMatch, ui.more));
  homeUI = ui;
  return root;
}

function setSort(key, keepDir) {
  if (!Object.hasOwn(SORTS, key)) return;
  if (home.sort === key && !keepDir) home.dir = home.dir === "asc" ? "desc" : "asc";
  else { home.sort = key; home.dir = SORTS[key]; }
  updateHome();
}

function updateHome() {
  const ui = homeUI;
  if (!ui) return;
  const s = home;
  // reflect state into the controls (no-ops while the user is typing into them)
  if (ui.q.value !== s.q) ui.q.value = s.q;
  ui.mfr.value = s.mfr;
  ui.type.value = s.type;
  ui.stock.checked = s.stock;
  ui.mp.checked = s.mp;
  ui.cond.set(s.cond);
  ui.sortSel.value = s.sort;
  ui.dirBtn.textContent = s.dir === "asc" ? "▲ Ascending" : "▼ Descending";
  ui.dirBtn.setAttribute("aria-label", "Sort direction: " + (s.dir === "asc" ? "ascending" : "descending") + ". Activate to reverse.");
  for (const [key, { th, arrow }] of Object.entries(ui.ths)) {
    const on = key === s.sort;
    th.setAttribute("aria-sort", on ? (s.dir === "asc" ? "ascending" : "descending") : "none");
    arrow.textContent = on ? (s.dir === "asc" ? "▲" : "▼") : "↕";
    arrow.classList.toggle("idle", !on);
  }

  const tokens = tokenize(s.q);
  const list = discs.filter((d) => matches(d, s, tokens)).sort(sorter(s));
  const shown = list.slice(0, s.shown);
  ui.tbody.replaceChildren(...shown.map((d) => discRow(d, s.cond, s.mp)));

  const total = discs.length;
  let text = list.length === total ? total + (total === 1 ? " disc" : " discs") : list.length + " of " + total + " discs";
  if (list.length > shown.length) text += " · showing the first " + shown.length;
  ui.count.textContent = text;
  const filtered = !!(s.q || s.mfr || s.type || s.stock || !s.mp || s.cond !== "new");
  ui.reset.hidden = !filtered;
  ui.table.hidden = list.length === 0;
  ui.noMatch.hidden = list.length !== 0;
  ui.more.hidden = list.length <= shown.length;
  ui.moreBtn.textContent = "Show " + Math.min(PAGE_SIZE, list.length - shown.length) + " more (" + (list.length - shown.length) + " left)";

  if (currentRoute === "home") history.replaceState(null, "", homeHash());
}

function buildEmpty() {
  const st = (data && data.stats) || {};
  const seen = num(st.listings) || 0;
  return h("div", { class: "card empty" },
    h("h1", null, "No data yet"),
    h("p", null, "No discs have been identified so far, so there are no prices to show."),
    h("p", null, seen > 0
      ? seen + " listings have been scraped, but none matched a known disc yet."
      : "The daily scrape has not recorded any listings yet. This page fills in after its first successful run."),
    data && data.generated_at ? h("p", { class: "muted" }, "Data file generated " + fmtDate(data.generated_at) + ".") : null);
}

/* ---------------------------------------------------------------- disc view */

const discTitle = (d) => [d.mold || "(unnamed)", d.plastic].filter(Boolean).join(" ");

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const g = (n) => cs.getPropertyValue(n).trim();
  return { s1: g("--series-1"), s2: g("--series-2"), ink: g("--ink"), ink2: g("--ink-2"), grid: g("--grid"), axis: g("--axis"), surface: g("--surface"), border: g("--control-border") };
}

/** Draws the hover crosshair under the lines and a labelled end-dot (2px surface ring) on top. */
const marksPlugin = (colors) => ({
  id: "discMarks",
  beforeDatasetsDraw(chart) {
    const active = chart.getActiveElements();
    if (!active.length) return;
    const { ctx, chartArea: a } = chart;
    const x = active[0].element.x;
    ctx.save();
    ctx.strokeStyle = colors.axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, a.top);
    ctx.lineTo(x, a.bottom);
    ctx.stroke();
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const { ctx, chartArea: a } = chart;
    ctx.save();
    ctx.font = "600 12px system-ui, -apple-system, 'Segoe UI', sans-serif";
    ctx.fillStyle = colors.ink;
    const ends = [];
    chart.data.datasets.forEach((ds, i) => {
      if (!chart.isDatasetVisible(i) || !ds.data.length) return;
      const el = chart.getDatasetMeta(i).data[ds.data.length - 1];
      ends.push({ i, x: el.x, y: el.y, v: ds.data[ds.data.length - 1].y, color: i === 0 ? colors.s1 : colors.s2 });
    });
    // draw the lower series last so a shared end point shows the lowest colour
    for (const e of [...ends].reverse()) {
      ctx.beginPath(); ctx.arc(e.x, e.y, 6, 0, Math.PI * 2); ctx.fillStyle = colors.surface; ctx.fill();
      ctx.beginPath(); ctx.arc(e.x, e.y, 4, 0, Math.PI * 2); ctx.fillStyle = e.color; ctx.fill();
    }
    // labels: median above its dot, lowest below its dot (min <= median, so they never collide);
    // a single visible series, or identical end values, gets one label above.
    ctx.fillStyle = colors.ink;
    ctx.textAlign = "right";
    const same = ends.length === 2 && Math.abs(ends[0].y - ends[1].y) < 1;
    for (const e of ends) {
      const below = ends.length === 2 && e.i === 0 && !same;
      if (ends.length === 2 && e.i === 0 && same) continue;
      const text = money(e.v);
      ctx.strokeStyle = colors.surface;   // halo keeps the value legible where it crosses a line
      ctx.lineWidth = 4;
      ctx.lineJoin = "round";
      const put = (t, x, y) => { ctx.strokeText(t, x, y); ctx.fillText(t, x, y); };
      if (below && e.y + 9 + 12 <= a.bottom + 6) { ctx.textBaseline = "top"; put(text, e.x + 4, e.y + 9); }
      else { ctx.textBaseline = "bottom"; put(text, e.x + 4, e.y - 9); }
    }
    ctx.restore();
  },
});

function xTicks(scale) {
  const span = scale.max - scale.min;
  const want = Math.max(2, Math.min(7, Math.floor(scale.width / 84)));
  const step = [1, 2, 3, 7, 14, 30, 60, 90, 180, 365].find((s) => s >= span / want) || 365;
  const ticks = [];
  for (let v = scale.max; v >= scale.min - 1e-9; v -= step) ticks.unshift({ value: v });   // newest date always labelled
  scale.ticks = ticks;
}

function drawChart(Chart, canvas, pts, view) {
  const colors = themeColors();
  const first = pts[0].x, last = pts[pts.length - 1].x;
  const multiYear = dayDate(first).getUTCFullYear() !== dayDate(last).getUTCFullYear();
  const isolated = (ctx) => {
    const d = ctx.dataset.data, i = ctx.dataIndex;
    return (i === 0 || d[i].x - d[i - 1].x > GAP_DAYS) && (i === d.length - 1 || d[i + 1].x - d[i].x > GAP_DAYS);
  };
  const dataset = (label, key, color) => ({
    label, data: pts.map((p) => ({ x: p.x, y: p[key] })),
    borderColor: color, backgroundColor: color, borderWidth: 2, tension: 0,
    borderCapStyle: "round", borderJoinStyle: "round",
    pointRadius: (ctx) => (isolated(ctx) ? 4 : 0), pointHoverRadius: 4,
    pointBackgroundColor: color, pointBorderColor: colors.surface, pointBorderWidth: 2,
    pointHoverBackgroundColor: color, pointHoverBorderColor: colors.surface, pointHoverBorderWidth: 2,
    segment: { borderColor: (ctx) => (ctx.p1.parsed.x - ctx.p0.parsed.x > GAP_DAYS ? "transparent" : color) },   // honest gap, not a bridge
  });
  const chart = new Chart(canvas, {
    type: "line",
    data: { datasets: [dataset("Lowest", "min", colors.s1), dataset("Median", "median", colors.s2)] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", axis: "x", intersect: false },
      layout: { padding: { top: 22, right: 8, bottom: 2, left: 0 } },
      plugins: {
        legend: { display: false },
        // HTML tooltip (see tipHandler): value first, 2px line key, theme tokens, no canvas text styling
        tooltip: { enabled: false, position: "nearest", external: view.tipHandler },
      },
      scales: {
        x: {
          type: "linear", min: pts.length === 1 ? first - 3 : first, max: pts.length === 1 ? last + 3 : last,
          grid: { display: false }, border: { color: colors.axis },
          ticks: { color: colors.ink2, font: { size: 12 }, maxRotation: 0, padding: 8, callback: (v) => (multiYear ? TICK_FMT_Y : TICK_FMT).format(dayDate(v)) },
          afterBuildTicks: xTicks,
        },
        y: {
          grace: "18%", grid: { color: colors.grid, lineWidth: 1 }, border: { display: false },
          afterDataLimits: (scale) => { if (scale.min < 0) scale.min = 0; },
          ticks: { color: colors.ink2, font: { size: 12 }, padding: 8, maxTicksLimit: 6,
            callback: (v, i, ticks) => money(v, ticks.length > 1 && Math.abs(ticks[1].value - ticks[0].value) < 1 ? 2 : 0) },
        },
      },
    },
    plugins: [marksPlugin(colors)],
  });
  for (const i of view.hidden) chart.setDatasetVisibility(i, false);
  chart.update("none");
  return chart;
}

function buildDisc(entry, hist, condParam) {
  const hasData = (c) => !!entry[c] || hist.series[c].length > 0 || hist.listings.some((l) => l.condition === c);
  const pick = () => {
    if (CONDS.includes(condParam) && hasData(condParam)) return condParam;
    if (hasData(home.cond)) return home.cond;
    return CONDS.find(hasData) || "new";
  };
  const view = { cond: pick(), range: "all", kind: "all", hidden: new Set(), chart: null, gen: 0, dead: false, pts: [] };
  const byKind = hist.seriesByKind;                       // null unless the disc has marketplace data
  const todayDay = toDay(data.generated_at);

  const title = discTitle(entry);
  const syncUrl = () => history.replaceState(null, "", discHref(entry.slug, view.cond));

  // ---- head
  const condSeg = segmented({
    label: "Condition", options: CONDS.map((c) => [c, COND_LABEL[c]]), value: view.cond,
    onChange: (c) => { view.cond = c; syncUrl(); render(); },
  });
  condSeg.el.classList.add("f-cond");
  for (const c of CONDS) condSeg.disable(c, !hasData(c), "No " + c + " data for this disc yet");
  const chips = [["Plastic", entry.plastic], ["Edition", entry.edition], ["Player", entry.player], ["Type", entry.disc_type]]
    .filter(([, v]) => v)
    .map(([k, v]) => h("li", null, h("span", { class: "muted" }, k + " "), h("strong", null, v)));
  const head = h("div", { class: "disc-head" },
    h("div", null,
      h("div", { class: "eyebrow" }, entry.manufacturer || "Unknown manufacturer"),
      h("h1", null, entry.mold || "(unnamed)"),
      chips.length ? h("ul", { class: "chips", "aria-label": "Disc details" }, chips) : null),
    condSeg.el);

  // ---- containers
  const kpis = h("section", { class: "kpis", "aria-label": "Current prices" });
  const chartSub = h("p", { class: "card-sub" });
  const rangeSeg = segmented({
    label: "Date range", hideLabel: !byKind, small: true, options: RANGES, value: view.range,
    onChange: (r) => { view.range = r; renderChart(); },
  });
  // The source switch only exists for discs with marketplace data. It changes which stores the lines are
  // computed from, not what the lines mean, so Lowest and Median keep their colours in every view.
  const kindSeg = byKind ? segmented({
    label: "Prices from", small: true, options: KINDS, value: view.kind,
    onChange: (k) => { view.kind = k; renderChart(); },
  }) : null;
  rangeSeg.el.classList.add("f-range");
  if (kindSeg) {
    kindSeg.el.classList.add("f-kind");
    for (const [k] of KINDS) {
      if (k !== "all") kindSeg.disable(k, CONDS.every((c) => byKind[k][c].length === 0), "No " + KIND_NOUN[k] + " price history for this disc");
    }
  }
  const kpiNote = h("p", { class: "kpi-note", hidden: !byKind },
    "Prices above combine retail stores and marketplace listings. Marketplace prices are individual sellers' asking prices (shipping not included), not retail prices; the chart below can show either on its own.");
  const legend = h("ul", { class: "legend", "aria-label": "Series" });
  const legendBtns = [["Lowest", "key-1"], ["Median", "key-2"]].map(([label, cls], i) => {
    const btn = h("button", { type: "button", "aria-pressed": "true" }, h("span", { class: "key " + cls, "aria-hidden": "true" }), label);
    btn.addEventListener("click", () => {
      const hiding = !view.hidden.has(i);
      if (hiding && view.hidden.size >= 1) return;        // keep at least one line on screen
      if (hiding) view.hidden.add(i); else view.hidden.delete(i);
      btn.setAttribute("aria-pressed", String(!hiding));
      if (view.chart) { view.chart.setDatasetVisibility(i, !hiding); view.chart.update("none"); }
    });
    legend.append(h("li", null, btn));
    return btn;
  });
  const canvas = h("canvas", { role: "img", tabindex: "0", "aria-describedby": "chart-hint" });
  const live = h("p", { class: "sr", "aria-live": "polite" });
  const tip = h("div", { class: "tip", hidden: true, "aria-hidden": "true" });
  const chartBox = h("div", { class: "chart-box" }, canvas, tip);
  const chartMsg = h("p", { class: "chart-msg", hidden: true });
  const chartNote = h("p", { class: "chart-hint" });
  const chartHint = h("p", { class: "chart-hint", id: "chart-hint" }, "Hover, tap, or use the left and right arrow keys on the chart to read a day's prices.");
  const dataBody = h("tbody", { role: "rowgroup" });
  const dataCaption = h("caption", { class: "sr" }, "Daily lowest and median price");
  const dataDetails = h("details", { class: "data" },
    h("summary", null, "View data as a table"),
    h("div", { class: "scroll", tabindex: "0", role: "region", "aria-label": "Price history table" },
      h("table", { class: "plain", role: "table" },
        dataCaption,
        h("thead", { role: "rowgroup" }, h("tr", { role: "row" },
          ["Date", "Lowest", "Median", "Stores in stock"].map((t, i) => h("th", { role: "columnheader", scope: "col", class: i ? "num" : null }, t)))),
        dataBody)));
  const chartCard = h("section", { class: "card chart-card", "aria-labelledby": "chart-title" },
    h("div", { class: "card-head" }, h("h2", { id: "chart-title" }, "Price history"),
      h("div", { class: "chart-controls" }, kindSeg ? kindSeg.el : null, rangeSeg.el)),
    chartSub, legend, chartBox, chartMsg, live, chartHint, chartNote, dataDetails);
  const listBody = h("tbody", { role: "rowgroup" });
  const listSub = h("p", { class: "card-sub" });
  const listEmpty = h("p", { class: "empty", hidden: true });
  const listTable = h("table", { class: "plain stack", role: "table", "aria-label": "Current listings" },
    h("thead", { role: "rowgroup" }, h("tr", { role: "row" },
      [["Store", ""], ["Listing", ""], ["Weight", "num"], ["Price", "num"], ["Availability", ""]]
        .map(([t, c]) => h("th", { role: "columnheader", scope: "col", class: c || null }, t)))),
    listBody);
  const listNote = h("p", { class: "card-sub list-note", hidden: true },
    "Marketplace listings are individual sellers' asking prices for one specific copy (shipping not included), not retail prices.");
  const listCard = h("section", { class: "card list-card", "aria-labelledby": "list-title" },
    h("div", { class: "card-head" }, h("h2", { id: "list-title" }, "Current listings")), listSub, h("div", { class: "tablewrap" }, listTable), listEmpty, listNote);

  // Recent sales. Only marketplaces produce them, so the card exists for discs with marketplace data or any recorded sale.
  const salesBody = h("tbody", { role: "rowgroup" });
  const salesSub = h("p", { class: "card-sub" });
  const salesEmpty = h("p", { class: "empty", hidden: true });
  const salesTable = h("table", { class: "plain stack sales", role: "table", "aria-label": "Recent sales" },
    h("thead", { role: "rowgroup" }, h("tr", { role: "row" },
      [["Date", ""], ["Price", "num"], ["Result", ""], ["Store", ""], ["Listing", ""]]
        .map(([t, c]) => h("th", { role: "columnheader", scope: "col", class: c || null }, t)))),
    salesBody);
  const salesCard = h("section", { class: "card list-card sales-card", "aria-labelledby": "sales-title", hidden: !(byKind || hist.sales.length) },
    h("div", { class: "card-head" }, h("h2", { id: "sales-title" }, "Recent sales")), salesSub,
    h("div", { class: "tablewrap" }, salesTable), salesEmpty);

  const el = h("div", { class: "disc" },
    h("a", { class: "back", href: homeHash() }, "← All discs"), head, kpis, kpiNote, chartCard, listCard, salesCard);
  view.el = el;

  // ---- renderers
  const allSeries = () => hist.series[view.cond];                                  // the tiles: all stores, always
  const series = () => (byKind && view.kind !== "all" ? byKind[view.kind][view.cond] : allSeries());   // the chart: per the switch
  const cname = () => COND_LABEL[view.cond].toLowerCase();

  function tile(label, valueNode, sub, cls) {
    return h("div", { class: "card tile " + (cls || "") },
      h("span", { class: "lbl" }, label), h("span", { class: "val" }, valueNode), sub ? h("span", { class: "sub" }, sub) : null);
  }

  function renderTiles() {
    const c = view.cond, blk = entry[c], s = allSeries();
    const last = s[s.length - 1];
    const lst = hist.listings.filter((l) => l.condition === c);
    const inStock = lst.filter((l) => l.available && l.price != null);
    let hero;
    if (blk && blk.stores_in_stock > 0) {
      const where = blk.stores_in_stock + " of " + blk.stores_listing + (blk.stores_listing === 1 ? " store" : " stores") + " in stock";
      // The source of the headline price must be obvious: a marketplace asking price is not a retail price.
      // At the same price the retail store is named (the home list does the same), never the marketplace.
      const cheapest = inStock.find((l) => l.price === inStock[0].price && l.storeKind !== "marketplace") || inStock[0];
      hero = tile("Lowest " + cname() + " price now", money(blk.min),
        cheapest ? [cheapest.storeKind === "marketplace" ? [mpBadge(), " "] : null, cheapest.store + " · " + where] : where, "hero");
    } else if (blk) {
      hero = tile("Lowest " + cname() + " price now", money(blk.min), "Out of stock at all " + blk.stores_listing + (blk.stores_listing === 1 ? " store" : " stores") + ". This is the last known price.", "hero");
      hero.querySelector(".val").classList.add("dim");
    } else {
      hero = tile("Lowest " + cname() + " price now", "–",
        last ? "No live listings. Last in stock " + fmtDate(last.date) + " at " + money(last.min) + "." : "No " + cname() + " listings or history for this disc yet.", "hero");
      hero.querySelector(".val").classList.add("dim");
    }
    const lo = s.reduce((m, p) => (!m || p.min < m.min ? p : m), null);
    const hi = s.reduce((m, p) => (!m || p.min > m.min ? p : m), null);
    const med = blk ? money(blk.median) : last ? money(last.median) : "–";
    const tiles = h("div", { class: "tiles" },
      tile("Median price", med, blk ? (blk.stores_in_stock > 0 ? "across stores in stock" : "last known") : last ? "last known" : ""),
      tile("7-day change", changeBadge(blk && blk.change_7d), blk && blk.change_7d != null ? "vs 7 days ago" : "Not enough history"),
      tile("30-day change", changeBadge(blk && blk.change_30d), blk && blk.change_30d != null ? "vs 30 days ago" : "Not enough history"),
      tile("Lowest seen", lo ? money(lo.min) : "–", lo ? fmtDate(lo.date) : ""),
      tile("Highest seen", hi ? money(hi.min) : "–", hi ? fmtDate(hi.date) : ""));
    kpis.replaceChildren(hero, tiles);
  }

  function rangePoints() {
    const s = series();
    if (view.range === "all") return s;
    const base = Number.isNaN(todayDay) ? (s.length ? s[s.length - 1].x : 0) : todayDay;
    return s.filter((p) => p.x >= base - Number(view.range));
  }

  function destroyChart() {
    if (view.chart) { view.chart.destroy(); view.chart = null; }
    tip.hidden = true;
  }

  /** Chart.js external tooltip: every series at the hovered/focused day. Same data as the live region and table. */
  view.tipHandler = ({ tooltip }) => {
    const dps = tooltip.dataPoints || [];
    if (tooltip.opacity === 0 || !dps.length) { tip.hidden = true; return; }
    const p = view.pts[dps[0].dataIndex];
    if (!p) { tip.hidden = true; return; }
    tip.replaceChildren(...[
      h("div", { class: "tip-title" }, DATE_LONG.format(dayDate(p.x))),
      dps.map((dp) => h("div", { class: "tip-row" },
        h("span", { class: "key key-" + (dp.datasetIndex + 1) }), h("strong", null, money(dp.parsed.y)), h("span", null, dp.dataset.label))),
      p.stores == null ? null : h("div", { class: "tip-foot" }, p.stores + (p.stores === 1 ? " store" : " stores") + " in stock"),
    ].flat().filter(Boolean));
    tip.hidden = false;
    const w = tip.offsetWidth, hgt = tip.offsetHeight;
    let x = tooltip.caretX + 14;
    if (x + w > chartBox.clientWidth) x = tooltip.caretX - w - 14;
    const y = Math.min(Math.max(0, tooltip.caretY - hgt / 2), Math.max(0, chartBox.clientHeight - hgt));
    tip.style.transform = "translate(" + Math.round(Math.max(0, x)) + "px, " + Math.round(y) + "px)";
  };

  function renderChart() {
    const gen = ++view.gen;
    destroyChart();
    const all = series();
    const pts = rangePoints();
    view.pts = pts;
    rangeSeg.set(view.range);
    if (view.kind === "marketplace") {
      chartSub.textContent = "Daily lowest and median asking price of " + cname() + " discs listed on marketplaces. These are sellers' asking prices, not sold prices.";
    } else if (view.kind === "retail") {
      chartSub.textContent = "Daily lowest and median price of " + cname() + " discs across the tracked retail stores only.";
    } else {
      chartSub.textContent = "Daily lowest and median asking price across the tracked stores" + (byKind ? " and marketplace listings" : "") + ", " + cname() + " discs.";
    }
    const src = byKind ? " (" + KIND_NOUN[view.kind] + ")" : "";
    dataCaption.textContent = "Daily lowest and median price" + src;
    for (const b of legendBtns) b.parentElement.hidden = pts.length === 0;
    chartNote.textContent = "";
    live.textContent = "";

    if (pts.length === 0) {
      chartBox.hidden = true;
      chartHint.hidden = true;
      dataDetails.hidden = true;
      chartMsg.hidden = false;
      const from = view.kind === "all" ? "" : " from " + KIND_NOUN[view.kind];
      chartMsg.textContent = all.length
        ? "No " + cname() + " prices" + from + " were recorded in this period. Pick a longer range."
        : "No " + cname() + " price history" + from + " yet. Points appear after a " + (view.kind === "marketplace" ? "marketplace" : "store") + " has the disc in stock on a scrape day.";
      return;
    }
    chartMsg.hidden = true;
    chartBox.hidden = false;
    chartHint.hidden = false;
    dataDetails.hidden = false;

    // table twin (always filled, so values never depend on the chart or the tooltip)
    dataBody.replaceChildren(...[...pts].reverse().map((p) => h("tr", { role: "row" },
      h("td", { role: "cell" }, fmtDate(p.date)), h("td", { role: "cell", class: "num" }, money(p.min)),
      h("td", { role: "cell", class: "num" }, money(p.median)), h("td", { role: "cell", class: "num" }, p.stores == null ? "–" : p.stores))));

    const notes = [];
    if (pts.some((p, i) => i && p.x - pts[i - 1].x > GAP_DAYS)) {
      notes.push(view.kind === "marketplace"
        ? "Breaks in the line mean no marketplace listing was live, or no scrape succeeded, for several days."
        : "Breaks in the line mean no store had the disc in stock, or no scrape succeeded, for several days.");
    }
    if (pts.every((p) => p.min === p.median)) {
      // a marketplace counts as one "store" however many listings it has, so "only one store" would be a false claim there
      notes.push(view.kind !== "marketplace" && pts.every((p) => p.stores === 1)
        ? "Lowest and median are identical here because only one store had it in stock at a time."
        : "Lowest and median are the same on every day shown, so the two lines overlap.");
    }
    chartNote.textContent = notes.join(" ");
    canvas.setAttribute("aria-label", "Line chart of lowest and median " + cname() + " price" + (byKind ? " from " + KIND_NOUN[view.kind] : "") + " from " + fmtDate(pts[0].date) + " to " + fmtDate(pts[pts.length - 1].date) +
      ". Latest lowest " + money(pts[pts.length - 1].min) + ", median " + money(pts[pts.length - 1].median) + ". The same numbers are in the table below.");

    loadChart().then((Chart) => {
      if (view.dead || gen !== view.gen) return;
      if (!Chart) {
        chartBox.hidden = true;
        chartHint.hidden = true;
        for (const b of legendBtns) b.parentElement.hidden = true;
        chartMsg.hidden = false;
        chartMsg.textContent = "The chart could not be loaded (the chart library is blocked or offline), so here is the same data as a table.";
        dataDetails.open = true;
        return;
      }
      view.chart = drawChart(Chart, canvas, pts, view);
    });
  }

  function inspect(i) {
    const chart = view.chart;
    if (!chart) return;
    const els = chart.data.datasets.map((_, di) => ({ datasetIndex: di, index: i })).filter((a) => chart.isDatasetVisible(a.datasetIndex));
    if (!els.length) return;
    const pt = chart.getDatasetMeta(els[0].datasetIndex).data[i];
    chart.setActiveElements(els);
    chart.tooltip.setActiveElements(els, { x: pt.x, y: pt.y });
    chart.update("none");
    const p = view.pts[i];
    live.textContent = DATE_LONG.format(dayDate(p.x)) + ": lowest " + money(p.min) + ", median " + money(p.median) +
      (p.stores == null ? "" : ", " + p.stores + (p.stores === 1 ? " store" : " stores") + " in stock");
    view.kb = i;
  }
  function clearInspect() {
    const chart = view.chart;
    view.kb = null;
    if (!chart) return;
    chart.setActiveElements([]);
    chart.tooltip.setActiveElements([], { x: 0, y: 0 });
    chart.update("none");
  }
  canvas.addEventListener("keydown", (e) => {
    const n = view.pts.length;
    if (!view.chart || !n) return;
    const cur = view.kb == null ? n : view.kb;
    let next = null;
    if (e.key === "ArrowLeft") next = Math.max(0, cur - 1);
    else if (e.key === "ArrowRight") next = Math.min(n - 1, view.kb == null ? n - 1 : cur + 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = n - 1;
    else if (e.key === "Escape") { clearInspect(); return; }
    if (next == null) return;
    e.preventDefault();
    inspect(next);
  });
  canvas.addEventListener("focus", () => { if (canvas.matches(":focus-visible") && view.kb == null && view.pts.length) inspect(view.pts.length - 1); });
  canvas.addEventListener("blur", clearInspect);

  function renderListings() {
    const c = view.cond;
    const lst = hist.listings.filter((l) => l.condition === c);
    const inStock = lst.filter((l) => l.available).length;
    const market = lst.filter((l) => l.storeKind === "marketplace").length;
    listSub.textContent = lst.length
      ? lst.length + " " + cname() + " listing" + (lst.length === 1 ? "" : "s") + (market ? " (" + market + " on a marketplace)" : "") + ", " + inStock + " in stock. Cheapest first."
      : "";
    listNote.hidden = market === 0;
    listTable.hidden = lst.length === 0;
    listEmpty.hidden = lst.length !== 0;
    listEmpty.textContent = "No live " + cname() + " listings right now. The chart shows the last known prices.";
    listBody.replaceChildren(...lst.map((l) => {
      const title = l.url ? extLink(l.url, l.title || "View listing") : (l.title || "(untitled)");
      const showWas = l.compareAt != null && l.price != null && l.compareAt > l.price;
      const market = l.storeKind === "marketplace";
      return h("tr", { role: "row", class: l.available ? null : "is-out", "data-kind": l.storeKind },
        h("td", { role: "cell", class: "s-store" }, l.store || "Unknown store", market ? [" ", mpBadge()] : null),
        h("td", { role: "cell", class: "s-title" }, title),
        h("td", { role: "cell", class: "num s-weight" }, l.weight != null ? l.weight + " g" : h("span", { class: "muted" }, "–")),
        h("td", { role: "cell", class: "num price s-price" }, money(l.price),
          showWas ? h("span", { class: "note" }, h("span", { class: "strike" }, h("span", { class: "sr" }, "was "), money(l.compareAt))) : null),
        h("td", { role: "cell", class: "c-status" },
          h("span", { class: "avail " + (l.available ? "in" : "out") },
            h("span", { class: "dot", "aria-hidden": "true" }, l.available ? "●" : "○"),
            market ? (l.available ? "Listed" : "Unavailable") : (l.available ? "In stock" : "Out of stock")),
          l.lastSeen && toDay(l.lastSeen) < todayDay ? h("span", { class: "note" }, "seen " + fmtShort(l.lastSeen)) : null));
    }));
  }

  function saleRow(x) {
    const tag = x.confirmed
      ? h("span", { class: "sale-tag sale-confirmed" }, h("span", { class: "ico", "aria-hidden": "true" }, "✔"), CONFIRMED_LABEL)
      : h("span", { class: "sale-tag sale-inferred" }, h("span", { class: "ico", "aria-hidden": "true" }, "?"), INFERRED_LABEL);
    return h("tr", { role: "row", class: x.confirmed ? "sale-row confirmed" : "sale-row inferred" },
      h("td", { role: "cell", class: "sale-date" }, fmtDate(x.date)),
      h("td", { role: "cell", class: "num price sale-price" }, money(x.price),
        h("span", { class: "note" }, x.confirmed ? "sold price" : "last asking price")),
      h("td", { role: "cell", class: "sale-what" }, tag),
      h("td", { role: "cell", class: "sale-store" }, x.store || "Unknown store"),
      h("td", { role: "cell", class: "sale-link" }, x.url ? extLink(x.url, "View listing") : h("span", { class: "muted" }, "–")));
  }

  function renderSales() {
    const c = view.cond;
    const rows = hist.sales.filter((x) => x.condition === c);
    const other = hist.sales.length - rows.length;
    const confirmed = rows.filter((x) => x.confirmed).length;
    salesTable.hidden = rows.length === 0;
    salesEmpty.hidden = rows.length !== 0;
    salesEmpty.textContent = "No sales detected for " + cname() + " discs yet." +
      (other ? " " + other + (other === 1 ? " sale is" : " sales are") + " listed under " + COND_LABEL[c === "new" ? "used" : "new"] + "." : "");
    const parts = [];
    if (rows.length - confirmed) {
      parts.push("Rows marked “Likely sold” are listings that disappeared before their end date. The seller may simply have delisted the item, so they are guesses, not confirmed sales, and the price is the last asking price.");
    }
    if (confirmed) parts.push("Rows marked “Sold” are confirmed sales.");
    salesSub.textContent = parts.join(" ");
    salesSub.hidden = parts.length === 0;
    salesBody.replaceChildren(...rows.map(saleRow));
  }

  function render() {
    renderTiles();
    renderChart();
    renderListings();
    renderSales();
  }

  view.redraw = () => { if (!view.dead) renderChart(); };
  view.destroy = () => { view.dead = true; view.gen++; destroyChart(); };
  view.start = () => { syncUrl(); render(); };
  view.title = title;
  return view;
}

async function renderDisc(r, token, isNav) {
  const entry = SLUG_RE.test(r.slug) ? bySlug.get(r.slug) : null;
  const back = h("a", { class: "back", href: homeHash() }, "← All discs");
  if (!entry) {
    mount(h("div", null, back, messageView("Disc not found", "There is no disc with that address in the current data. It may have been renamed or removed.")), "Disc not found");
    afterMount(isNav, "disc");
    return;
  }
  const title = discTitle(entry);
  mount(h("div", null, back, h("h1", { class: "sr" }, title), h("p", { class: "loading" }, "Loading price history…")), title);
  let hist;
  try {
    hist = await loadHistory(entry.slug);
  } catch (err) {
    if (token !== routeToken) return;
    const retry = h("a", { href: location.hash || "#/", class: "btn" }, "Try again");
    retry.addEventListener("click", (e) => { e.preventDefault(); route(false); });
    mount(h("div", null, back, messageView("Could not load this disc", "The price history file failed to load (" + str(err && err.message) + ").", retry)), title);
    afterMount(isNav, "disc");
    return;
  }
  if (token !== routeToken) return;
  activeView = buildDisc(entry, hist, r.params.get("c"));
  mount(activeView.el, title);
  activeView.start();
  afterMount(isNav, "disc");
}

/* ------------------------------------------------------------------ routing */

function afterMount(isNav, name) {
  if (isNav) focusHeading();
  if (name === "home" && isNav && homeScroll) window.scrollTo(0, homeScroll);
  else if (isNav) window.scrollTo(0, 0);
}

function route(isNav) {
  const r = parseRoute();
  const token = ++routeToken;
  if (currentRoute === "home") { homeScroll = window.scrollY; homeShown = home.shown; }
  if (activeView) { activeView.destroy(); activeView = null; }
  const back = currentRoute === "disc" && r.name === "home";
  currentRoute = r.name;
  if (r.name === "home") {
    if (!back && isNav) homeScroll = 0;
    if (discs.length === 0) {
      mount(buildEmpty(), "No data yet");
      afterMount(isNav, "home");
      return;
    }
    const mfrs = [...new Set(discs.map((d) => d.manufacturer).filter(Boolean))];
    const types = [...new Set(discs.map((d) => d.disc_type || NONE))];
    home = homeFromParams(r.params, mfrs, types);
    if (back) home.shown = Math.max(home.shown, homeShown);     // else "Back" lands below the end of a shortened list
    mount(buildHome(), null);
    updateHome();
    afterMount(isNav, "home");
  } else {
    renderDisc(r, token, isNav);
  }
}

/* ------------------------------------------------------------- page chrome */

function renderChrome() {
  const st = data.stats || {};
  const items = [];
  const stat = (n, label) => h("li", null, h("strong", null, String(n)), " " + label);
  const gen = str(data.generated_at);
  items.push(h("li", null, "Updated ", h("strong", null, h("time", { datetime: gen }, fmtDate(gen)))));
  items.push(stat(num(st.discs) ?? discs.length, "discs"));
  if (num(st.matched) != null) items.push(stat(st.matched, "matched listings"));
  if (num(st.review) != null) items.push(stat(st.review, "need review"));
  $("#site-meta").replaceChildren(...items);

  const stores = Array.isArray(data.stores) ? data.stores.filter((s) => s && typeof s === "object") : [];
  const foot = $("#site-foot");
  const list = h("ul");
  for (const s of stores) {
    const url = safeHttpUrl(s.base_url);
    const name = str(s.name) || str(s.id) || "Unnamed store";
    const ok = str(s.last_ok);
    let status;
    if (!ok) status = h("span", { class: "stale" }, "no successful scrape yet");
    else if (ok < str(data.generated_at)) status = h("span", { class: "stale" }, "last scraped " + fmtDate(ok) + " (stale)");
    else status = "scraped " + fmtDate(ok);
    list.append(h("li", null, url ? extLink(url, name) : name, s.kind === "marketplace" ? [" ", mpBadge()] : null, " · ", status));
  }
  const hasMarket = stores.some((s) => s.kind === "marketplace");
  foot.replaceChildren(...[
    stores.length ? [h("h2", null, "Sources"), list] : null,
    h("p", null, "Retail asking prices from public storefronts, not sold prices. Prices and stock may have changed since the last scrape. Change badges compare the daily lowest in-stock price with the nearest earlier scrape."),
    hasMarket ? h("p", null, "Marketplace prices are individual sellers' asking prices for one specific copy, not retail prices, and are marked as such. “Likely sold” entries are inferred from listings that disappeared, so they may be delistings rather than sales.") : null].flat().filter(Boolean));
}

function showFatal(err) {
  mount(messageView("Could not load the price data", "data/index.json failed to load (" + str(err && err.message) + ")."), "Error");
}

/** The skip link is a plain #main anchor, but any hash change is a route change here: following it would reset the
 *  filters (or leave the disc page). Move focus in script instead; without JS the native anchor still works. */
document.addEventListener("click", (e) => {
  const a = e.target instanceof Element && e.target.closest('a[href="#main"]');
  if (!a || e.defaultPrevented) return;
  e.preventDefault();
  $("#main").focus();
});

async function boot() {
  try {
    const index = await getJSON("data/index.json");
    if (!index || typeof index !== "object" || !Array.isArray(index.discs)) throw new Error("unexpected index format");
    data = index;
    prepare(index);
  } catch (err) {
    showFatal(err);
    return;
  }
  renderChrome();
  window.addEventListener("hashchange", () => route(true));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (activeView) activeView.redraw(); });
  route(false);
}

boot();
})();
