"use strict";
/* Browser regression checks for site/ (DESIGN.md section 6). Run by tests/test_site.py, or by hand:
 *
 *     node tests/site_checks.js            # needs playwright(-core) and a Chromium; prints one JSON line per run
 *
 * Environment (all optional):
 *   SITE_DIR           site files to test (default ../site)
 *   SAMPLE_DIR         exporter-shaped sample data (default fixtures/site_sample)
 *   REAL_DIR           a directory written by the real exporter; adds the "exporter output" check
 *   PLAYWRIGHT_MODULE  path of playwright-core / playwright
 *   CHROMIUM_PATH      Chromium executable
 *   WORK_DIR           scratch directory for generated data
 *   CHART_JS_FILE      a local copy of Chart.js 4.4.x: adds a check that the drawn chart follows the source switch
 *
 * Offline: every request goes to a localhost server started here; the Chart.js CDN request is answered with a 404 so
 * the chart-less fallback is what gets exercised. Exit code 0 = ran (look at the JSON for failures), 3 = cannot run.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const { execSync } = require("child_process");

const SITE = path.resolve(process.env.SITE_DIR || path.join(__dirname, "..", "site"));
const SAMPLE = path.resolve(process.env.SAMPLE_DIR || path.join(__dirname, "fixtures", "site_sample"));
const REAL = process.env.REAL_DIR ? path.resolve(process.env.REAL_DIR) : "";
const CHART_JS_FILE = process.env.CHART_JS_FILE ? path.resolve(process.env.CHART_JS_FILE) : "";
const WORK = process.env.WORK_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "site-checks-"));
fs.mkdirSync(WORK, { recursive: true });
const finish = (code) => {
  if (!process.env.WORK_DIR) fs.rmSync(WORK, { recursive: true, force: true });
  process.exit(code);
};

const MIME = { ".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css", ".json": "application/json" };

function loadPlaywright() {
  const tries = [process.env.PLAYWRIGHT_MODULE, "playwright-core", "playwright"].filter(Boolean);
  try {
    const root = execSync("npm root -g", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    tries.push(path.join(root, "playwright-core"), path.join(root, "playwright"));
  } catch { /* no npm */ }
  for (const t of tries) {
    try { return require(t); } catch { /* try the next one */ }
  }
  return null;
}

/* ------------------------------------------------------------------ data helpers */

const readJSON = (f) => JSON.parse(fs.readFileSync(f, "utf8"));
let dirCount = 0;

/** A fresh data directory (index.json + history/) built from the sample and changed by `mutate(index, history)`. */
function makeData(mutate, { empty = false } = {}) {
  const dir = path.join(WORK, "data" + ++dirCount);
  fs.mkdirSync(path.join(dir, "history"), { recursive: true });
  const index = readJSON(path.join(SAMPLE, "index.json"));
  const hist = {};
  if (!empty) for (const d of index.discs) hist[d.slug] = readJSON(path.join(SAMPLE, "history", d.slug + ".json"));
  if (empty) index.discs = [];
  if (mutate) mutate(index, hist);
  fs.writeFileSync(path.join(dir, "index.json"), JSON.stringify(index));
  for (const [slug, h] of Object.entries(hist)) fs.writeFileSync(path.join(dir, "history", slug + ".json"), JSON.stringify(h));
  return dir;
}

const BLOCK = { min: 10, median: 11, stores_in_stock: 1, stores_listing: 1, change_7d: null, change_30d: null };
const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
function synth(manufacturer, mold, plastic, extra) {
  return {
    key: [manufacturer, mold, plastic, "", ""].join("|").toLowerCase(), slug: slugify(`${manufacturer} ${mold} ${plastic}`) || "disc",
    manufacturer, mold, plastic, edition: "", player: "", disc_type: "Putter", new: { ...BLOCK }, used: null, last_seen: "2026-10-08", ...extra,
  };
}

function serve(dataDir) {
  const server = http.createServer((req, res) => {
    let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
    if (p === "/") p = "/index.html";
    const isData = p.startsWith("/data/");
    const root = isData ? dataDir : SITE;
    const file = path.join(root, isData ? p.slice(6) : p.slice(1));
    if (!file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end("not found"); return; }
    res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
    res.end(fs.readFileSync(file));
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, base: `http://127.0.0.1:${server.address().port}/` })));
}

/* ------------------------------------------------------------------ harness */

const results = [];
const problems = [];          // page errors and console errors seen anywhere during the run
let browser;

async function openPage(base, { width = 1280, height = 900, route = "", scheme = "light", chart = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
  const page = await ctx.newPage();
  page.setDefaultTimeout(8000);
  page.on("pageerror", (e) => problems.push("pageerror: " + e));
  page.on("console", (m) => {
    // expected noise: the CDN answers 404 on purpose, and the generated discs have no history file
    const noise = /cdnjs\.cloudflare\.com|\/data\/history\/d\d+\.json/.test(m.location().url + m.text());
    if (m.type() === "error" && !noise) problems.push("console: " + m.text());
  });
  page.on("dialog", (d) => { problems.push("dialog: " + d.message()); d.dismiss(); });
  await page.route("https://cdnjs.cloudflare.com/**", (r) => (chart && CHART_JS_FILE
    ? r.fulfill({ status: 200, contentType: "application/javascript", headers: { "access-control-allow-origin": "*" }, body: fs.readFileSync(CHART_JS_FILE) })
    : r.fulfill({ status: 404, body: "" })));
  await page.goto(base + route);
  return page;
}

async function withSite(dataDir, fn) {
  const { server, base } = await serve(dataDir);
  try { return await fn(base); } finally { server.close(); }
}

async function check(name, fn) {
  try {
    const r = await fn();
    const ok = r === true || (r && r.ok === true);
    results.push({ name, ok, detail: ok ? "" : JSON.stringify(r && r.detail !== undefined ? r.detail : r) });
  } catch (e) {
    results.push({ name, ok: false, detail: String((e && e.stack) || e).slice(0, 600) });
  }
}

const rowsReady = (page) => page.waitForSelector("table.discs tbody tr");
const rowSlugs = (page) => page.$$eval("table.discs tbody tr", (trs) => trs.map((tr) => tr.dataset.slug || null));
const hOverflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const setCond = (page, c) => page.check(`.seg input[value=${c}]`, { force: true });

/* ------------------------------------------------------------------ checks */

async function checkNameOrder() {
  // Collation ignores control characters, so a name joined with a separator loses its field boundaries:
  // "Aviar3" used to sort before "Aviar" and "Zones" between "Zone" and "Zone OS".
  const mk = (mold, plastic) => synth("Innova", mold, plastic);
  const molds = [["Zones", ""], ["Roc3", "Champion"], ["Zone OS", "ESP"], ["Aviar3", "Star"], ["Zone", "Z"], ["Roc", "Champion"], ["Aviar", "Pro"]];
  const dir = makeData((idx) => { idx.discs = molds.map(([m, p]) => mk(m, p)); }, { empty: true });
  const want = ["Aviar Pro", "Aviar3 Star", "Roc Champion", "Roc3 Champion", "Zone Z", "Zone OS ESP", "Zones"];
  return withSite(dir, async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    const names = () => page.$$eval("table.discs tbody tr td.c-name", (tds) => tds.map((td) => [td.querySelector("a").textContent, (td.querySelector(".plastic") || {}).textContent].filter(Boolean).join(" ")));
    const asc = await names();
    await page.click("button.sort[data-sort=name]");
    const desc = await names();
    await page.context().close();
    return { ok: JSON.stringify(asc) === JSON.stringify(want) && JSON.stringify(desc) === JSON.stringify([...want].reverse()), detail: { asc, desc } };
  });
}

async function checkSkipLink() {
  return withSite(makeData(), async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    await page.fill("#f-q", "star");
    const before = { hash: await page.evaluate(() => location.hash), rows: (await rowSlugs(page)).length };
    await page.focus(".skip");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);
    const after = {
      hash: await page.evaluate(() => location.hash), rows: (await rowSlugs(page)).length, q: await page.inputValue("#f-q"),
      focus: await page.evaluate(() => document.activeElement && document.activeElement.id),
    };
    await page.goto(base + "#/disc/innova-destroyer-star");
    await page.reload();
    await page.waitForSelector(".disc");
    await page.focus(".skip");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);
    const onDisc = { hash: await page.evaluate(() => location.hash), stillDisc: !!(await page.$(".disc")) };
    await page.context().close();
    return {
      ok: after.hash === before.hash && after.rows === before.rows && before.rows > 0 && after.q === "star" && after.focus === "main" &&
        onDisc.stillDisc && onDisc.hash === "#/disc/innova-destroyer-star",
      detail: { before, after, onDisc },
    };
  });
}

async function checkDelisted() {
  // A disc every store has delisted has new = used = null but keeps its history. It must stay findable.
  const index = readJSON(path.join(SAMPLE, "index.json"));
  const delisted = index.discs.filter((d) => !d.new && !d.used);
  if (!delisted.length) return { ok: false, detail: "the sample has no fully delisted disc to test with" };
  const liveNew = index.discs.filter((d) => d.new).length, liveUsed = index.discs.filter((d) => d.used).length;
  return withSite(makeData(), async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    const out = {};
    const isDelisted = (s) => delisted.some((d) => d.slug === s);
    const tailIsDelisted = async () => {
      const s = await rowSlugs(page);
      return s.slice(s.length - delisted.length).every(isDelisted);
    };
    const slugsNew = await rowSlugs(page);
    out.newRows = slugsNew.length;
    out.inNew = delisted.every((d) => slugsNew.includes(d.slug));
    const row = page.locator(`tr[data-slug="${delisted[0].slug}"]`);
    out.price = (await row.locator(".c-min").textContent()).trim();
    out.stores = (await row.locator(".c-stores").textContent()).trim();
    await setCond(page, "used");
    const slugsUsed = await rowSlugs(page);
    out.usedRows = slugsUsed.length;
    out.inUsed = delisted.every((d) => slugsUsed.includes(d.slug));
    await page.check("#f-stock");
    out.withStockFilter = (await rowSlugs(page)).filter(isDelisted).length;
    await page.uncheck("#f-stock");
    await page.click("button.sort[data-sort=min]");
    out.lastAscending = (await page.getAttribute("th.c-min", "aria-sort")) === "ascending" && (await tailIsDelisted());
    await page.click("button.sort[data-sort=min]");
    out.lastDescending = (await page.getAttribute("th.c-min", "aria-sort")) === "descending" && (await tailIsDelisted());
    await page.click(`tr[data-slug="${delisted[0].slug}"] .c-name a`);
    await page.waitForSelector(".disc");
    out.page = (await page.textContent("h1")).trim();
    await page.context().close();
    const ok = out.newRows === liveNew + delisted.length && out.usedRows === liveUsed + delisted.length && out.inNew && out.inUsed &&
      out.price === "\u2013" && /no live listings/i.test(out.stores) && out.withStockFilter === 0 && out.lastAscending && out.lastDescending &&
      out.page === delisted[0].mold;
    return { ok, detail: { ...out, liveNew, liveUsed, delisted: delisted.length } };
  });
}

async function checkRowWithoutValidSlug() {
  // A row whose slug is unusable has no link, but must still get the card layout on a phone.
  const dir = makeData((idx) => { idx.discs[2].slug = "Not A Slug!"; });
  return withSite(dir, async (base) => {
    const page = await openPage(base, { width: 360 });
    await rowsReady(page);
    const widths = await page.$$eval("table.discs tbody tr", (trs) => trs.slice(0, 5).map((tr) => Math.round(tr.getBoundingClientRect().width)));
    await page.context().close();
    return { ok: widths.every((w) => w === widths[0]) && widths[0] > 300, detail: widths };
  });
}

async function checkNoHorizontalScroll() {
  return withSite(makeData(), async (base) => {
    const bad = [];
    for (const route of ["", "#/?mp=0", "#/disc/innova-destroyer-star", "#/disc/innova-boss-champion", `#/disc/${TB}`,
      `#/disc/${TB}?c=used`, `#/disc/${BERG}`, `#/disc/${COMPASS}`, `#/disc/${WARSHIP}`]) {
      const page = await openPage(base, { width: 360, route });
      await page.waitForSelector(route.includes("/disc/") ? ".chart-card" : "table.discs tbody tr");
      for (const w of [320, 360, 414, 600, 700, 701, 720, 768, 800, 840, 841, 900, 1024, 1280, 1600]) {
        await page.setViewportSize({ width: w, height: 900 });
        await page.waitForTimeout(40);
        const over = await hOverflow(page);
        if (over > 0) bad.push(`${route || "home"} @${w}px: ${over}px too wide`);
      }
      await page.context().close();
    }
    return { ok: bad.length === 0, detail: bad };
  });
}

async function checkLongStrings() {
  // Scraped names without spaces must wrap instead of widening the page.
  const long = "Supercalifragilisticexpialidocious".repeat(6);
  const cases = {
    "manufacturer": (d) => { d.manufacturer = long; },
    "mold": (d) => { d.mold = long; },
    "plastic": (d) => { d.plastic = long; },
    "edition": (d) => { d.edition = long; },
    "player": (d) => { d.player = long; },
    "disc_type": (d) => { d.disc_type = long; },
    "store name": (d, idx) => { idx.stores[0].name = long; },
  };
  const bad = [];
  for (const [label, apply] of Object.entries(cases)) {
    const dir = makeData((idx, hist) => {
      const d = idx.discs.find((x) => x.slug === "innova-destroyer-star");
      apply(d, idx);
      const h = hist[d.slug];
      h.listings[0].title = long;
      h.listings[0].store = long;
    });
    await withSite(dir, async (base) => {
      for (const width of [360, 1280]) {
        for (const route of ["", "#/disc/innova-destroyer-star"]) {
          const page = await openPage(base, { width, route });
          await page.waitForSelector(route ? ".chart-card" : "table.discs tbody tr");
          const over = await hOverflow(page);
          if (over > 0) bad.push(`${label} ${route || "home"} @${width}px: ${over}px too wide`);
          await page.context().close();
        }
      }
    });
  }
  return { ok: bad.length === 0, detail: bad };
}

async function checkPerformance() {
  // Every keystroke re-filters and re-sorts the whole list: it has to stay interactive with thousands of discs.
  const mfrs = ["Innova", "Discraft", "Dynamic Discs", "Latitude 64", "Westside Discs", "MVP", "Axiom", "Prodigy", "Discmania", "Kastaplast"];
  const dir = makeData((idx) => {
    idx.discs = [];
    for (let i = 0; i < 6000; i++) {
      const d = synth(mfrs[i % mfrs.length], "Mold" + ((i * 7) % 700), ["Star", "Champion", "DX", "ESP", "Z", "Opto"][i % 6], { slug: "d" + i });
      d.new = { min: 10 + (i % 300) / 10, median: 12 + (i % 300) / 10, stores_in_stock: i % 4, stores_listing: 3, change_7d: ((i % 21) - 10) / 100, change_30d: i % 7 ? ((i % 31) - 15) / 100 : null };
      idx.discs.push(d);
    }
  }, { empty: true });
  return withSite(dir, async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    const t = await page.evaluate(() => {
      const q = document.querySelector("#f-q");
      const time = (fn) => { const t0 = performance.now(); fn(); return Math.round(performance.now() - t0); };
      const type = (v) => time(() => { q.value = v; q.dispatchEvent(new Event("input")); });
      const sort = (k) => time(() => document.querySelector(`button.sort[data-sort=${k}]`).click());
      return { m: type("m"), mo: type("mo"), mold1: type("mold1"), clear: type(""), name: sort("name"), nameDesc: sort("name"), min: sort("min"), type: sort("type"), c7: sort("c7") };
    });
    await page.context().close();
    const slowest = Math.max(...Object.values(t));
    return { ok: slowest < 300, detail: t };
  });
}

async function checkShowMoreFocus() {
  const dir = makeData((idx) => { idx.discs = []; for (let i = 0; i < 250; i++) idx.discs.push(synth("Innova", "Mold" + String(i).padStart(3, "0"), "Star", { slug: "d" + i })); }, { empty: true });
  return withSite(dir, async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    const where = () => page.evaluate(() => {
      const a = document.activeElement;
      return { tag: a.tagName, inRows: !!a.closest("table.discs tbody"), rows: document.querySelectorAll("table.discs tbody tr").length, more: !document.querySelector(".more").hidden };
    });
    await page.focus(".more button");
    await page.keyboard.press("Enter");
    const first = await where();
    await page.focus(".more button");
    await page.keyboard.press("Enter");
    const last = await where();
    await page.context().close();
    return { ok: first.rows === 200 && first.tag === "A" && first.inRows && last.rows === 250 && !last.more && last.tag === "A" && last.inRows, detail: { first, last } };
  });
}

async function checkBackKeepsRows() {
  const dir = makeData((idx) => { idx.discs = []; for (let i = 0; i < 250; i++) idx.discs.push(synth("Innova", "Mold" + String(i).padStart(3, "0"), "Star", { slug: "d" + i })); }, { empty: true });
  return withSite(dir, async (base) => {
    const page = await openPage(base, { height: 700 });
    await rowsReady(page);
    for (let i = 0; i < 2; i++) await page.click(".more button");
    const link = page.locator("table.discs tbody tr:nth-child(221) .c-name a");
    await link.scrollIntoViewIfNeeded();
    const slug = await link.evaluate((a) => a.closest("tr").dataset.slug);
    await link.click();
    await page.waitForSelector("h1:not(.sr), .empty h1");
    await page.evaluate(() => history.back());
    await page.waitForSelector("table.discs tbody tr");
    await page.waitForTimeout(100);
    const after = await page.evaluate((s) => {
      const tr = document.querySelector(`tr[data-slug="${s}"]`);
      return { rows: document.querySelectorAll("table.discs tbody tr").length, top: tr ? Math.round(tr.getBoundingClientRect().top) : null, vh: innerHeight };
    }, slug);
    await page.context().close();
    return { ok: after.top !== null && after.top >= 0 && after.top < after.vh, detail: after };
  });
}

async function checkEditionCase() {
  // The parser's edition vocabulary is lower case ("tour series"); the page shows it like the other name parts.
  const dir = makeData((idx) => { for (const d of idx.discs) d.edition = d.edition.toLowerCase(); });
  const index = readJSON(path.join(dir, "index.json"));
  const withEdition = index.discs.find((d) => d.edition && d.new && !/[<>]/.test(d.mold));
  if (!withEdition) return { ok: false, detail: "the sample has no disc with an edition" };
  return withSite(dir, async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    const sub = await page.locator(`tr[data-slug="${withEdition.slug}"] .sub`).textContent();
    await page.goto(base + "#/disc/" + withEdition.slug);
    await page.waitForSelector(".chips");
    const chips = await page.locator(".chips").textContent();
    await page.context().close();
    const pretty = withEdition.edition.replace(/(^|[\s-])(\p{L})/gu, (m, s, c) => s + c.toUpperCase());
    return { ok: sub.includes(pretty) && chips.includes(pretty), detail: { sub, chips, pretty } };
  });
}

async function checkIdenticalNote() {
  // min == median can also mean several stores charge the same price: do not claim "only one store".
  const run = (stores) => withSite(makeData((idx, hist) => {
    for (const p of hist["innova-destroyer-star"].series.new) { p.min = 17.99; p.median = 17.99; p.stores_in_stock = stores; }
  }), async (base) => {
    const page = await openPage(base, { route: "#/disc/innova-destroyer-star" });
    await page.waitForSelector(".chart-card");
    await page.waitForFunction(() => document.querySelector(".chart-hint:not(#chart-hint)").textContent.length > 0);
    const note = await page.locator(".chart-hint:not(#chart-hint)").textContent();
    await page.context().close();
    return note;
  });
  const many = await run(3), one = await run(1);
  return { ok: !/only one store/i.test(many) && /identical|same/i.test(many) && /only one store/i.test(one), detail: { many, one } };
}

async function checkExporterOutput() {
  // Real exporter output: every exported disc is listed on the home page and opens.
  const index = readJSON(path.join(REAL, "index.json"));
  return withSite(REAL, async (base) => {
    const page = await openPage(base);
    await rowsReady(page);
    const seen = new Set();
    for (const c of ["new", "used"]) {
      await setCond(page, c);
      for (const s of await rowSlugs(page)) seen.add(s);
    }
    const missing = index.discs.map((d) => d.slug).filter((s) => !seen.has(s));
    const broken = [];
    for (const d of index.discs) {
      await page.goto(base + "#/disc/" + d.slug);
      await page.waitForSelector(".disc, .empty");
      if (!(await page.$(".disc"))) broken.push(d.slug);
    }
    await page.context().close();
    return { ok: missing.length === 0 && broken.length === 0 && index.discs.length > 0, detail: { missing, broken, discs: index.discs.length } };
  });
}


/* ------------------------------------------------------------------ marketplace (DESIGN.md section 10.4) */

const TB = "innova-thunderbird-star", BERG = "kastaplast-berg-k1", COMPASS = "latitude-64-compass-opto", WARSHIP = "westside-discs-warship-vip";
const INFERRED_LABEL = "Likely sold (inferred, low confidence - the seller may have delisted it)";
const CONFIRMED_LABEL = "Sold (confirmed)";
const histOf = (slug, dir = SAMPLE) => readJSON(path.join(dir, "history", slug + ".json"));
const norm = (t) => String(t).replace(/\s+/g, " ").trim();
const usd = (v) => "$" + v.toFixed(2);
const DATE_US = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
const fmtDay = (iso) => DATE_US.format(new Date(iso + "T00:00:00Z"));
const pointRow = (p) => [fmtDay(p.date), usd(p.min), usd(p.median), String(p.stores_in_stock)];
/** Rows of the chart's data table twin (newest first): [date, lowest, median, stores]. */
const dataRows = (page) => page.$$eval("details.data tbody tr", (trs) => trs.map((tr) => [...tr.cells].map((c) => c.textContent.trim())));
const seg = (page, which, value) => page.check(`.${which} input[value="${value}"]`, { force: true });
const discPage = async (base, slug, opts = {}) => {
  const page = await openPage(base, { ...opts, route: "#/disc/" + slug + (opts.cond ? "?c=" + opts.cond : "") });
  await page.waitForSelector(".chart-card");
  return page;
};
const salesRows = (page) => page.$$eval(".sales-card tbody tr", (trs) => trs.map((tr) => ({
  cls: tr.className, text: tr.textContent.replace(/\s+/g, " ").trim(),
  date: tr.querySelector(".sale-date").textContent, price: tr.querySelector(".sale-price").textContent.replace(/\s+/g, " ").trim(),
  tag: [...tr.querySelector(".sale-tag").childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim(), tagCls: tr.querySelector(".sale-tag").className,
  store: tr.querySelector(".sale-store").textContent, href: (tr.querySelector(".sale-link a") || {}).href || null,
  rel: (tr.querySelector(".sale-link a") || {}).rel || null, target: (tr.querySelector(".sale-link a") || {}).target || null,
})));

async function checkMarketplaceBadges() {
  const index = readJSON(path.join(SAMPLE, "index.json"));
  const h = histOf(TB);
  return withSite(makeData(), async (base) => {
    const out = {};
    let page = await discPage(base, TB);
    const rows = await page.$$eval(".list-card:not(.sales-card) tbody tr", (trs) => trs.map((tr) => ({
      kind: tr.dataset.kind, badge: [...tr.querySelectorAll(".badge-mp")].map((b) => b.textContent), avail: tr.querySelector(".avail").textContent.replace(/^[●○]\s*/, "").trim(),
    })));
    const want = h.listings.filter((l) => l.condition === "new");
    out.rows = rows.length === want.length && rows.every((r, i) => r.kind === want[i].store_kind);
    out.badgeIffMarketplace = rows.every((r) => (r.kind === "marketplace") === (r.badge.length === 1 && r.badge[0] === "Marketplace"));
    out.mixed = rows.some((r) => r.kind === "marketplace") && rows.some((r) => r.kind === "retail");
    out.labels = rows.every((r) => (r.kind === "marketplace" ? /^Listed$/ : /^In stock$/).test(r.avail));
    out.hero = (await page.locator(".tile.hero .badge-mp").count()) === 1;           // the cheapest copy is an eBay one
    out.note = await page.locator(".list-note").isVisible();
    out.kpiNote = /asking prices/.test(await page.locator(".kpi-note").textContent());
    await page.context().close();

    page = await discPage(base, "innova-destroyer-star");                              // retail only: nothing marketplace-ish at all
    out.retailOnly = {
      badges: await page.locator("#app .badge-mp").count(), salesCard: await page.locator(".sales-card").isVisible(),
      note: await page.locator(".list-note").isVisible(), kpiNote: await page.locator(".kpi-note").isVisible(), switch: await page.locator(".f-kind").count(),
    };
    await page.context().close();

    page = await openPage(base);                                                       // home: marketplace price marker + footer
    await rowsReady(page);
    const marked = await page.$$eval("table.discs tbody tr", (trs) => trs.filter((tr) => tr.querySelector(".c-min .badge-mp")).map((tr) => tr.dataset.slug));
    const expected = index.discs.filter((d) => {
      const b = d.new, r = d.retail && d.retail.new;
      return d.retail && b && b.stores_in_stock > 0 && (!r || r.stores_in_stock === 0 || b.min < r.min);
    }).map((d) => d.slug);
    out.marked = { marked, expected };
    const foot = await page.$$eval("#site-foot li", (lis) => lis.map((li) => ({ text: li.textContent.replace(/\s+/g, " "), badge: !!li.querySelector(".badge-mp") })));
    out.footer = foot.filter((f) => f.badge).length === index.stores.filter((s) => s.kind === "marketplace").length && foot.some((f) => /^eBay/.test(f.text) && f.badge);
    await page.context().close();
    const ok = out.rows && out.badgeIffMarketplace && out.mixed && out.labels && out.hero && out.note && out.kpiNote &&
      out.retailOnly.badges === 0 && !out.retailOnly.salesCard && !out.retailOnly.note && !out.retailOnly.kpiNote && out.retailOnly.switch === 0 &&
      JSON.stringify(marked) === JSON.stringify(expected) && marked.includes(TB) && marked.includes(BERG) && !marked.includes(COMPASS) && out.footer;
    return { ok, detail: out };
  });
}

async function checkSourceSwitch() {
  const h = histOf(TB);
  return withSite(makeData(), async (base) => {
    const out = { steps: [] };
    const page = await discPage(base, TB);
    const radios = await page.$$eval(".f-kind input", (is) => is.map((i) => [i.value, i.checked, i.parentElement.textContent.trim()]));
    out.radios = JSON.stringify(radios) === JSON.stringify([["all", true, "All"], ["retail", false, "Retail"], ["marketplace", false, "Marketplace"]]);
    out.label = norm(await page.locator(".f-kind .lbl").textContent()) === "Prices from";
    const hero = norm(await page.locator(".tile.hero").textContent());
    const tiles = norm(await page.locator(".tiles").textContent());
    const expectFor = (kind, cond) => (kind === "all" ? h.series[cond] : h.series_by_kind[kind][cond]);
    const rangeStart = (days) => { const t = new Date("2026-10-08T00:00:00Z").getTime() - days * 864e5; return new Date(t).toISOString().slice(0, 10); };
    for (const kind of ["retail", "marketplace", "all"]) {
      await seg(page, "f-kind", kind);
      const rows = await dataRows(page);
      const want = [...expectFor(kind, "new")].reverse().map(pointRow);
      const sub = norm(await page.locator(".chart-card .card-sub").textContent());
      const caption = norm(await page.locator("details.data caption").textContent());
      out.steps.push({
        kind, rows: JSON.stringify(rows) === JSON.stringify(want),
        sub: kind === "retail" ? /retail stores only/.test(sub) : kind === "marketplace" ? /marketplaces/.test(sub) && /asking price/.test(sub) && /not sold prices/.test(sub) : /stores and marketplace listings/.test(sub),
        caption: caption.includes(kind === "all" ? "all sources" : kind === "retail" ? "retail stores" : "marketplace listings"),
        // the tiles describe all sources and must not move with the chart
        tiles: norm(await page.locator(".tile.hero").textContent()) === hero && norm(await page.locator(".tiles").textContent()) === tiles,
        aria: /from (all sources|retail stores|marketplace listings)/.test(await page.getAttribute("canvas", "aria-label")),
      });
    }
    // the choice survives a condition change, and combines with the date range
    await seg(page, "f-kind", "marketplace");
    await seg(page, "f-cond", "used");
    const usedRows = await dataRows(page);
    out.usedKeepsKind = JSON.stringify(usedRows) === JSON.stringify([...expectFor("marketplace", "used")].reverse().map(pointRow)) && usedRows.length > 0;
    out.checkedAfterCond = await page.$eval('.f-kind input[value="marketplace"]', (i) => i.checked);
    await seg(page, "f-cond", "new");
    await seg(page, "f-range", "30");
    const r30 = await dataRows(page);
    const want30 = expectFor("marketplace", "new").filter((p) => p.date >= rangeStart(30)).reverse().map(pointRow);
    out.range = JSON.stringify(r30) === JSON.stringify(want30) && r30.length > 0 && r30.length < expectFor("marketplace", "new").length;
    await seg(page, "f-range", "all");
    // keyboard: a real radio group, arrow keys move the selection and the chart data follows
    await seg(page, "f-kind", "all");
    await page.focus('.f-kind input[value="all"]');
    await page.keyboard.press("ArrowRight");
    out.keyboard = (await page.$eval('.f-kind input[value="retail"]', (i) => i.checked)) &&
      JSON.stringify(await dataRows(page)) === JSON.stringify([...expectFor("retail", "new")].reverse().map(pointRow));
    await page.context().close();

    // an eBay-only disc has no retail history: that option is disabled (and says why); a mixed one without
    // marketplace used data can still be switched, and shows an honest message instead of an empty chart
    let p2 = await discPage(base, BERG);
    out.berg = {
      retailDisabled: await p2.$eval('.f-kind input[value="retail"]', (i) => i.disabled), marketDisabled: await p2.$eval('.f-kind input[value="marketplace"]', (i) => i.disabled),
      title: await p2.$eval('.f-kind input[value="retail"]', (i) => i.parentElement.title),
    };
    await p2.context().close();
    p2 = await discPage(base, COMPASS);
    await seg(p2, "f-kind", "marketplace");
    const msg = norm(await p2.locator(".chart-msg").textContent());
    out.compass = { visible: await p2.locator(".chart-msg").isVisible(), msg, boxHidden: !(await p2.locator(".chart-box").isVisible()) };
    await p2.context().close();
    const ok = out.radios && out.label && out.steps.every((x) => x.rows && x.sub && x.caption && x.tiles && x.aria) && out.usedKeepsKind &&
      out.checkedAfterCond && out.range && out.keyboard && out.berg.retailDisabled && !out.berg.marketDisabled && /retail/i.test(out.berg.title) &&
      out.compass.visible && /marketplace/.test(out.compass.msg) && out.compass.boxHidden;
    return { ok, detail: out };
  });
}

async function checkRecentSales() {
  const h = histOf(TB), berg = histOf(BERG), warship = histOf(WARSHIP);
  return withSite(makeData(), async (base) => {
    const out = {};
    let page = await discPage(base, TB);
    const rows = await salesRows(page);
    const want = h.sales.filter((x) => x.condition === "new");
    out.count = rows.length === want.length && rows.length >= 5;
    out.newestFirst = rows.map((r) => r.date).join("|") === want.map((x) => fmtDay(x.date)).join("|");
    out.prices = rows.every((r, i) => r.price.startsWith(usd(want[i].price)));
    out.inferred = rows.filter((r, i) => want[i].confidence !== "confirmed").every((r) => r.tag === INFERRED_LABEL && r.cls.includes("inferred") && !r.cls.includes("confirmed") &&
      r.tagCls.includes("sale-inferred") && !/confirmed|Sold \(/.test(r.text.replace(INFERRED_LABEL, "")) && /last asking price/.test(r.price));
    out.confirmed = rows.filter((r, i) => want[i].confidence === "confirmed").every((r) => r.tag === CONFIRMED_LABEL && r.cls.includes("confirmed") && !r.cls.includes("inferred") &&
      r.tagCls.includes("sale-confirmed") && !/inferred|Likely|low confidence|delisted/i.test(r.text) && /sold price/.test(r.price));
    out.mix = rows.some((r) => r.tagCls.includes("sale-confirmed")) && rows.some((r) => r.tagCls.includes("sale-inferred"));
    // an inferred sale is never styled like a confirmed one
    const style = await page.evaluate(() => {
      const cs = (sel) => { const e = document.querySelector(sel); const s = getComputedStyle(e); return { w: s.fontWeight, i: s.fontStyle, c: s.color }; };
      const ico = (sel) => { const s = getComputedStyle(document.querySelector(sel + " .ico")); return { bg: s.backgroundColor, b: s.borderStyle }; };
      return { inf: cs(".sale-inferred"), con: cs(".sale-confirmed"), infIco: ico(".sale-inferred"), conIco: ico(".sale-confirmed") };
    });
    out.style = style;
    out.distinct = style.inf.i === "italic" && style.con.i !== "italic" && Number(style.con.w) > Number(style.inf.w) && style.infIco.b === "dashed" && style.conIco.b === "solid" && style.infIco.bg !== style.conIco.bg;
    // hostile store name: shown literally; javascript: URLs never become links; real URLs open safely
    const hostile = rows.filter((r) => r.store.includes("<"));
    out.hostile = hostile.length >= 2 && hostile.every((r) => r.store === want.find((x) => x.store === r.store).store);
    out.noInjection = await page.evaluate(() => document.querySelectorAll(".sales-card img, .sales-card script, .sales-card svg").length === 0 && ![...document.scripts].some((s) => !s.src));
    out.links = rows.every((r, i) => (want[i].url ? r.href === new URL(want[i].url).href && r.rel === "noopener noreferrer" && r.target === "_blank" : r.href === null)) && rows.some((r) => r.href === null);
    out.hrefs = await page.$$eval("a[href]", (as) => as.filter((a) => !/^(https?:|#)/.test(a.getAttribute("href"))).map((a) => a.getAttribute("href")));
    out.sub = norm(await page.locator(".sales-card .card-sub").textContent());
    // used discs: the one used sale, filtered by condition like the listings; the other condition is mentioned when empty
    await seg(page, "f-cond", "used");
    const usedRows = await salesRows(page);
    out.used = usedRows.length === h.sales.filter((x) => x.condition === "used").length && usedRows.length > 0;
    await page.context().close();

    page = await discPage(base, BERG);                                                  // only inferred sales exist here
    const bRows = await salesRows(page);
    out.bergOnlyInferred = bRows.length === berg.sales.filter((x) => x.condition === "new").length && bRows.length > 0 &&
      bRows.every((r) => r.tag === INFERRED_LABEL && !r.tagCls.includes("confirmed")) && !/Sold \(confirmed\)/.test(await page.locator(".sales-card").textContent()) &&
      !/confirmed sales\./.test((await page.locator(".sales-card .card-sub").textContent()).replace(/not confirmed sales\./, ""));
    await page.context().close();

    page = await discPage(base, COMPASS);                                               // marketplace data, no sales
    out.compass = { card: await page.locator(".sales-card").isVisible(), table: await page.locator(".sales-card table").isVisible(), empty: norm(await page.locator(".sales-card .empty").textContent()) };
    await page.context().close();

    page = await discPage(base, WARSHIP);                                               // gone everywhere, the sale remains
    out.warship = { rows: (await salesRows(page)).length, expected: warship.sales.length };
    await page.context().close();

    // the page puts them in order itself, whatever order the file has
    const shuffled = makeData((idx, hist) => { hist[TB].sales.reverse(); });
    await withSite(shuffled, async (base2) => {
      const p2 = await discPage(base2, TB);
      out.sortedByPage = (await salesRows(p2)).map((r) => r.date).join("|") === want.map((x) => fmtDay(x.date)).join("|");
      await p2.context().close();
    });

    const ok = out.sortedByPage && out.count && out.newestFirst && out.prices && out.inferred && out.confirmed && out.mix && out.distinct && out.hostile && out.noInjection && out.links &&
      out.hrefs.length === 0 && /guesses, not confirmed sales/.test(out.sub) && out.used && out.bergOnlyInferred &&
      out.compass.card && !out.compass.table && /No sales detected for new discs/.test(out.compass.empty) && out.warship.rows === out.warship.expected && out.warship.rows > 0;
    return { ok, detail: out };
  });
}

async function checkUnconfirmedIsNeverConfirmed() {
  // Anything the data does not spell out as a confirmed sale is worded as the inferred guess: odd values, missing fields,
  // a contradictory row (a disappeared listing marked confirmed), placeholder prices and junk dates.
  const mk = (extra) => ({ date: "2026-10-01", price: 12.5, condition: "new", source: "inferred_disappeared", confidence: "low", store: "eBay", url: "https://www.ebay.com/itm/1", ...extra });
  const cases = [
    mk({ confidence: "high" }), mk({ confidence: "" }), mk({ confidence: undefined }), mk({ confidence: "CONFIRMED" }), mk({ confidence: "confirmed" }),
    mk({ source: "marketplace_insights", confidence: "low" }), mk({ source: undefined, confidence: undefined }), mk({ source: "marketplace_insights", confidence: true }),
  ];
  const dropped = [mk({ price: 0 }), mk({ price: -3 }), mk({ price: null }), mk({ price: "12" }), mk({ price: NaN })];
  const junk = [mk({ date: "not a date" }), mk({ date: "" })];
  const dir = makeData((idx, hist) => { hist[TB].sales = [...cases, ...dropped, ...junk, null, "x", 5]; });
  return withSite(dir, async (base) => {
    const page = await discPage(base, TB);
    const rows = await salesRows(page);
    await page.context().close();
    const kept = cases.length + junk.length;
    const confirmedRows = rows.filter((r) => r.tagCls.includes("sale-confirmed"));
    // the page cannot know better, so only an exact confirmed + non-inferred source row may be confirmed: none of the cases here
    return {
      ok: rows.length === kept && confirmedRows.length === 0 && rows.every((r) => r.tag === INFERRED_LABEL) &&
        rows.filter((r) => r.date === "–").length === junk.length,
      detail: { rows: rows.length, kept, confirmed: confirmedRows.length, tags: [...new Set(rows.map((r) => r.tag))] },
    };
  });
}

async function checkSalesTextSafety() {
  // Every scraped string of a sales row reaches the page as text only, whatever it contains, and long unbroken ones wrap.
  const evil = [`<img src=x onerror=alert('s1')>`, `"><script>alert('s2')</script>`, `<svg onload=alert('s3')>`, "&lt;b&gt;x&lt;/b&gt;", "{{7*7}}", "javascript:alert('s4')"];
  const long = "Supercalifragilisticexpialidocious".repeat(8);
  const dir = makeData((idx, hist) => {
    const urls = ["javascript:alert('u1')", "JaVaScRiPt:alert('u2')", " javascript:alert('u3')", "data:text/html,<script>alert('u4')</script>", "//evil.example/x", "ftp://x.example/", "https://user:pw@evil.example/x", "vbscript:x"];
    hist[TB].sales = [
      ...evil.map((store, i) => ({ date: `2026-10-0${i + 1}`, price: 10 + i, condition: "new", source: "inferred_disappeared", confidence: "low", store, url: null })),
      ...urls.map((url, i) => ({ date: `2026-09-0${i + 1}`, price: 20 + i, condition: "new", source: "inferred_disappeared", confidence: "low", store: "eBay", url })),
      // a browser reads a backslash as a slash, drops tabs and opens "https:host": none of these may become a link
      ...["https://evil.example\\www.ebay.com/itm/1", "https://www.ebay.com\\@evil.example/x", "https://www.ebay.com/itm/1\t.evil.example/x", "https:evil.example"]
        .map((url, i) => ({ date: `2026-07-0${i + 1}`, price: 40 + i, condition: "new", source: "inferred_disappeared", confidence: "low", store: "eBay", url })),
      { date: "2026-08-01", price: 30, condition: "new", source: "inferred_disappeared", confidence: "low", store: long, url: "https://www.ebay.com/itm/" + long },
      { date: "2026-08-02", price: 31, condition: "new", source: "marketplace_insights", confidence: "confirmed", store: long, url: "https://www.ebay.com/itm/" + long },
    ];
  });
  return withSite(dir, async (base) => {
    const bad = [];
    for (const [width, scheme] of [[360, "light"], [1280, "light"], [360, "dark"], [1280, "dark"]]) {
      const page = await discPage(base, TB, { width, scheme });
      const rows = await salesRows(page);
      const shown = rows.filter((r) => evil.includes(r.store)).map((r) => r.store);
      if (JSON.stringify(shown.sort()) !== JSON.stringify([...evil].sort())) bad.push(`${width} ${scheme}: stores not shown literally ${JSON.stringify(shown)}`);
      const links = rows.filter((r) => r.href).length;
      if (links !== 2) bad.push(`${width} ${scheme}: ${links} links, expected only the two https ones`);
      const dom = await page.evaluate(() => ({
        injected: document.querySelectorAll(".sales-card img, .sales-card script, .sales-card svg, .sales-card b").length,
        scripts: [...document.scripts].filter((s) => !s.src).length, over: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        bad: [...document.querySelectorAll("a[href]")].filter((a) => !/^(https?:|#)/.test(a.getAttribute("href"))).length,
      }));
      if (dom.injected || dom.scripts || dom.bad) bad.push(`${width} ${scheme}: ${JSON.stringify(dom)}`);
      if (dom.over > 0) bad.push(`${width} ${scheme}: ${dom.over}px too wide`);
      await page.context().close();
    }
    return { ok: bad.length === 0, detail: bad };
  });
}

async function checkMarketplaceFilter() {
  const index = readJSON(path.join(SAMPLE, "index.json"));
  const mkt = index.discs.filter((d) => d.retail && !d.retail.new && !d.retail.used);        // marketplace-only discs
  if (mkt.length < 2) return { ok: false, detail: "the sample needs marketplace-only discs" };
  return withSite(makeData(), async (base) => {
    const out = {};
    let page = await openPage(base);
    await rowsReady(page);
    const price = (slug) => page.locator(`tr[data-slug="${slug}"] .c-min`).first().textContent().then((t) => norm(t));
    const slugs = () => rowSlugs(page);
    out.defaultOn = await page.isChecked("#f-mp");
    out.label = norm(await page.locator("label.check", { has: page.locator("#f-mp") }).textContent()) === "Include marketplace prices";
    const on = await slugs();
    out.onHasMarketplaceOnly = mkt.every((d) => on.includes(d.slug));        // a fully delisted one stays findable, as before
    out.onPrice = await price(TB);                                                      // blended: the eBay copy is the lowest
    out.reset0 = await page.locator(".resultbar .btn.link").isHidden();
    await page.uncheck("#f-mp");
    const off = await slugs();
    out.offHides = mkt.every((d) => !off.includes(d.slug)) && off.length === on.length - mkt.length;
    out.offPrice = await price(TB);
    out.offNoBadge = (await page.locator("table.discs .badge-mp").count()) === 0;
    out.hash = await page.evaluate(() => location.hash);
    out.reset1 = await page.locator(".resultbar .btn.link").isVisible();
    out.count = norm(await page.locator(".resultbar [role=status]").textContent());
    const tbOff = index.discs.find((d) => d.slug === TB).retail.new;
    // the retail-only block drives the row: price, in-stock figures, change badges and sorting
    out.row = norm(await page.locator(`tr[data-slug="${TB}"]`).textContent());
    await page.click("button.sort[data-sort=min]");
    const asc = await page.$$eval("table.discs tbody tr .c-min", (tds) => tds.map((t) => t.textContent.replace(/[^0-9.]/g, "")).filter(Boolean).map(Number));
    out.sorted = asc.every((v, i) => !i || asc[i - 1] <= v);
    await page.click("button.sort[data-sort=name]");
    await setCond(page, "used");                                                        // used: eBay-only used copies of mixed discs go too
    const usedOff = await slugs();
    await page.check("#f-mp");
    const usedOn = await slugs();
    out.usedOffSubset = usedOff.every((s) => usedOn.includes(s)) && usedOn.length > usedOff.length;
    await setCond(page, "new");
    // state lives in the URL: reload keeps it, the disc page's back link keeps it, Reset restores the default
    await page.uncheck("#f-mp");
    await page.reload();
    await rowsReady(page);
    out.reload = !(await page.isChecked("#f-mp")) && (await slugs()).length === off.length;
    await page.click(`tr[data-slug="${TB}"] .c-name a`);
    await page.waitForSelector(".disc");
    out.back = await page.getAttribute("a.back", "href");
    await page.click("a.back");
    await rowsReady(page);
    out.backState = !(await page.isChecked("#f-mp")) && (await slugs()).length === off.length;
    await page.click(".resultbar .btn.link");
    out.resetOn = (await page.isChecked("#f-mp")) && (await slugs()).length === on.length && !out.hash.includes("undefined");
    await page.context().close();
    // a link with mp=0 straight from the address bar
    page = await openPage(base, { route: "#/?mp=0" });
    await rowsReady(page);
    out.deepLink = !(await page.isChecked("#f-mp")) && (await slugs()).length === off.length;
    await page.context().close();
    const ok = out.defaultOn && out.label && out.onHasMarketplaceOnly && out.offHides && /\$15\.50/.test(out.onPrice) && /Marketplace/.test(out.onPrice) &&
      out.offPrice === usd(tbOff.min) && out.offNoBadge && out.hash.includes("mp=0") && out.reset0 && out.reset1 && out.sorted && out.usedOffSubset &&
      out.reload && /mp=0/.test(out.back) && out.backState && out.resetOn && out.deepLink && out.row.includes(`${tbOff.stores_in_stock} of ${tbOff.stores_listing}`);
    return { ok, detail: out };
  });
}

async function checkLegacyData() {
  // Data written before the marketplace work (or by an exporter without it) must still render everything it used to,
  // with no filter, no switch, no sales card, no marketplace markers and no errors.
  const dir = makeData((idx, hist) => {
    const drop = new Set([TB, BERG, COMPASS, WARSHIP]);
    idx.discs = idx.discs.filter((d) => !drop.has(d.slug));
    for (const slug of drop) delete hist[slug];
    for (const d of idx.discs) { delete d.sales_30d; delete d.retail; }
    for (const h of Object.values(hist)) { delete h.sales; delete h.series_by_kind; for (const l of h.listings) delete l.store_kind; }
    for (const s of idx.stores) delete s.kind;
    idx.stores = idx.stores.filter((s) => s.id !== "ebay" && s.id !== "bazaar");
    delete idx.stats.sales_confirmed; delete idx.stats.sales_inferred;
  });
  return withSite(dir, async (base) => {
    const out = {};
    let page = await openPage(base);
    await rowsReady(page);
    out.mpControl = await page.locator("#f-mp").count();
    out.rows = (await rowSlugs(page)).length > 20;
    out.badges = await page.locator("#app .badge-mp").count();
    out.foot = !/Marketplace prices/.test(await page.locator("#site-foot").textContent());
    await page.context().close();
    page = await discPage(base, "innova-destroyer-star");
    out.disc = {
      switch: await page.locator(".f-kind").count(), sales: await page.locator(".sales-card").isVisible(), badges: await page.locator("#app .badge-mp").count(),
      listings: await page.locator(".list-card tbody tr").count(), note: await page.locator(".kpi-note").isVisible(),
      label: norm(await page.locator(".avail").first().textContent()).replace(/^[●○]\s*/, ""),
    };
    await page.context().close();
    const ok = out.mpControl === 0 && out.rows && out.badges === 0 && out.foot && out.disc.switch === 0 && !out.disc.sales && out.disc.badges === 0 &&
      out.disc.listings > 0 && !out.disc.note && /^(In stock|Out of stock)$/.test(out.disc.label);
    return { ok, detail: out };
  });
}

async function checkMarketplaceLayoutAndContrast() {
  // Both widths, both colour schemes: nothing overflows, the markers have a box, and their text clears 4.5:1 on its surface.
  const probe = () => {
    const rgb = (c) => { const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) return null; const [r, g, b, a] = m[1].split(/[ ,\/]+/).map(Number); return { r, g, b, a: a === undefined ? 1 : a }; };
    const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
    const bgOf = (el) => { for (let e = el; e; e = e.parentElement) { const c = rgb(getComputedStyle(e).backgroundColor); if (c && c.a > 0.5) return c; } return { r: 255, g: 255, b: 255, a: 1 }; };
    const ratio = (fg, bg) => { const a = lum(fg), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
    const res = {};
    for (const [name, sel, bgSel] of [["badge", ".badge-mp", null], ["inferred", ".sale-inferred", null], ["confirmed", ".sale-confirmed", null], ["confirmedIcon", ".sale-confirmed .ico", null],
      ["salePriceNote", ".sale-price .note", null], ["listNote", ".list-note", null], ["kpiNote", ".kpi-note", null]]) {
      const el = document.querySelector(sel);
      if (!el) { res[name] = null; continue; }
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      res[name] = { w: Math.round(r.width), h: Math.round(r.height), ratio: Math.round(ratio(rgb(cs.color), bgOf(bgSel ? document.querySelector(bgSel) : el)) * 10) / 10 };
    }
    return res;
  };
  return withSite(makeData(), async (base) => {
    const bad = [], seen = [];
    for (const scheme of ["light", "dark"]) {
      for (const width of [360, 1280]) {
        for (const route of ["", `#/disc/${TB}`, `#/disc/${BERG}`, `#/disc/${COMPASS}`, `#/disc/${WARSHIP}`]) {
          const page = await openPage(base, { width, scheme, route });
          await page.waitForSelector(route ? ".chart-card" : "table.discs tbody tr");
          const over = await hOverflow(page);
          if (over > 0) bad.push(`${scheme} ${width} ${route || "home"}: ${over}px too wide`);
          const res = await page.evaluate(probe);
          if (route === `#/disc/${TB}`) {
            seen.push(`${scheme}/${width}`);
            for (const k of ["badge", "inferred", "confirmed", "confirmedIcon", "salePriceNote", "listNote", "kpiNote"]) {
              if (!res[k]) bad.push(`${scheme} ${width}: no ${k}`);
              else if (res[k].w < 8 || res[k].h < 8) bad.push(`${scheme} ${width}: ${k} has no box ${JSON.stringify(res[k])}`);
              else if (res[k].ratio < 4.5) bad.push(`${scheme} ${width}: ${k} contrast ${res[k].ratio}`);
            }
          }
          if (!route) {
            const homeBadge = res.badge;
            if (!homeBadge || homeBadge.ratio < 4.5) bad.push(`${scheme} ${width}: home badge ${JSON.stringify(homeBadge)}`);
          }
          await page.context().close();
        }
      }
    }
    return { ok: bad.length === 0 && seen.length === 4, detail: bad };
  });
}

async function checkTieGoesToRetail() {
  // The same lowest price at a retail store and on a marketplace: the disc page names the retail store (the home list
  // already gives ties to retail), so the headline price of a tie never carries the marketplace badge.
  const dir = makeData((idx, hist) => {
    const listings = hist[TB].listings;
    listings.find((l) => l.store_id === "fairway-supply" && l.condition === "new").price = 15.5;   // eBay's cheapest new copy is 15.50
    listings.sort((a, b) => a.price - b.price);                                                    // stable: the eBay copy stays first, as the exporter orders store ids
    idx.discs.find((d) => d.slug === TB).retail.new.min = 15.5;
  });
  return withSite(dir, async (base) => {
    let page = await discPage(base, TB);
    const hero = norm(await page.locator(".tile.hero").textContent());
    const badges = await page.locator(".tile.hero .badge-mp").count();
    await page.context().close();
    page = await openPage(base);
    await rowsReady(page);
    const homeBadges = await page.locator(`tr[data-slug="${TB}"] .c-min .badge-mp`).count();
    await page.context().close();
    return { ok: badges === 0 && /Fairway Supply/.test(hero) && /\$15\.50/.test(hero) && homeBadges === 0, detail: { hero, badges, homeBadges } };
  });
}

async function checkChartFollowsSource() {
  // Only with a local Chart.js (CHART_JS_FILE): the drawn datasets are the selected source's points.
  const h = histOf(TB);
  return withSite(makeData(), async (base) => {
    const page = await discPage(base, TB, { chart: true });
    await page.waitForFunction(() => window.Chart && window.Chart.getChart(document.querySelector("canvas")));
    const ys = () => page.evaluate(() => {
      const c = window.Chart.getChart(document.querySelector("canvas"));
      return { lowest: c.data.datasets[0].data.map((p) => p.y), median: c.data.datasets[1].data.map((p) => p.y), labels: c.data.datasets.map((d) => d.label), colors: c.data.datasets.map((d) => d.borderColor) };
    });
    const out = {};
    let colors = null;
    for (const kind of ["all", "retail", "marketplace"]) {
      await seg(page, "f-kind", kind);
      await page.waitForTimeout(80);
      const got = await ys();
      const want = kind === "all" ? h.series.new : h.series_by_kind[kind].new;
      out[kind] = JSON.stringify(got.lowest) === JSON.stringify(want.map((p) => p.min)) && JSON.stringify(got.median) === JSON.stringify(want.map((p) => p.median)) && got.labels.join() === "Lowest,Median";
      if (colors && JSON.stringify(colors) !== JSON.stringify(got.colors)) out[kind + "Colors"] = "repainted";   // colour follows the measure, not the source
      colors = got.colors;
    }
    await page.context().close();
    return { ok: out.all && out.retail && out.marketplace && !out.retailColors && !out.marketplaceColors, detail: out };
  });
}

async function checkExporterMarketplace() {
  // Real exporter output with eBay data: the new UI works on what the exporter really writes.
  return withSite(REAL, async (base) => {
    const out = {};
    let page = await discPage(base, "innova-destroyer-star");
    out.switch = await page.locator(".f-kind").count();
    out.badges = await page.locator(".list-card:not(.sales-card) .badge-mp").count();
    const rows = await salesRows(page);
    out.sales = rows.map((r) => r.tag);
    await page.context().close();
    page = await discPage(base, "innova-wraith-star");
    out.wraith = {
      retailDisabled: await page.$eval('.f-kind input[value="retail"]', (i) => i.disabled), sales: await page.locator(".sales-card table").isVisible(),
      badges: await page.locator(".list-card:not(.sales-card) .badge-mp").count(), hero: await page.locator(".tile.hero .badge-mp").count(),
    };
    await page.context().close();
    page = await openPage(base);
    await rowsReady(page);
    out.filter = await page.locator("#f-mp").count();
    const all = (await rowSlugs(page)).length;
    await page.uncheck("#f-mp");
    out.hidden = all - (await rowSlugs(page)).length;                                   // the eBay-only Wraith
    await page.context().close();
    // Destroyer: its eBay copy vanished (one inferred sale, no live marketplace listing); Wraith: eBay only, live, no sales
    return { ok: out.switch === 1 && out.badges === 0 && out.sales.length === 1 && out.sales[0] === INFERRED_LABEL && out.wraith.retailDisabled && !out.wraith.sales &&
      out.wraith.badges === 1 && out.wraith.hero === 1 && out.filter === 1 && out.hidden === 1, detail: out };
  });
}

/* ------------------------------------------------------------------ main */

(async () => {
  const pw = loadPlaywright();
  if (!pw) { console.log("SKIP: playwright-core is not installed"); finish(3); }
  try {
    browser = await pw.chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  } catch (e) {
    console.log("SKIP: no usable Chromium (" + String(e).split("\n")[0] + ")");
    finish(3);
  }
  await check("names sort field by field (Aviar < Aviar3, Zone < Zone OS < Zones)", checkNameOrder);
  await check("skip link keeps the filters and the disc page", checkSkipLink);
  await check("fully delisted discs stay in the list, last in price sorts", checkDelisted);
  await check("a row without a usable slug still gets the phone card layout", checkRowWithoutValidSlug);
  await check("no page-level horizontal scroll from 320px to 1600px", checkNoHorizontalScroll);
  await check("long unbroken names wrap instead of widening the page", checkLongStrings);
  await check("6000 discs: filtering and sorting stay under 300 ms", checkPerformance);
  await check("Show more keeps keyboard focus in the list", checkShowMoreFocus);
  await check("Back from a disc returns to the revealed rows", checkBackKeepsRows);
  await check("lower-case editions are shown capitalised", checkEditionCase);
  await check("identical lowest/median note does not claim a single store", checkIdenticalNote);
  await check("marketplace listings, prices and sources carry a visible badge; retail-only discs do not", checkMarketplaceBadges);
  await check("chart source switch (All / Retail / Marketplace) changes the chart data, not the tiles", checkSourceSwitch);
  await check("recent sales: inferred rows say so, confirmed rows are distinct, newest first", checkRecentSales);
  await check("anything not confirmed in the data is worded as an inferred guess", checkUnconfirmedIsNeverConfirmed);
  await check("sales rows: hostile text stays text, unsafe URLs never become links, long strings wrap", checkSalesTextSafety);
  await check("home filter: include marketplace prices (default on) hides marketplace-only discs and switches to retail prices", checkMarketplaceFilter);
  await check("data without marketplace fields renders as before (no filter, switch, sales or badges)", checkLegacyData);
  await check("marketplace UI at 360px and 1280px, light and dark: no overflow, visible markers, contrast >= 4.5", checkMarketplaceLayoutAndContrast);
  await check("a price tie between a retail store and a marketplace credits the retail store (disc page and home agree)", checkTieGoesToRetail);
  if (CHART_JS_FILE) await check("the drawn chart follows the source switch and keeps its colours", checkChartFollowsSource);
  if (REAL) await check("every disc written by the real exporter is listed and opens", checkExporterOutput);
  if (REAL) await check("real exporter marketplace output drives the new UI", checkExporterMarketplace);
  await check("no script errors, console errors or dialogs during the run", async () => ({ ok: problems.length === 0, detail: problems.slice(0, 5) }));
  await browser.close();
  console.log(JSON.stringify({ results }));
  finish(0);
})().catch((e) => { console.error(e); finish(2); });
