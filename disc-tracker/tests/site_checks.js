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

async function openPage(base, { width = 1280, height = 900, route = "" } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: "light" });
  const page = await ctx.newPage();
  page.setDefaultTimeout(8000);
  page.on("pageerror", (e) => problems.push("pageerror: " + e));
  page.on("console", (m) => {
    // expected noise: the CDN answers 404 on purpose, and the generated discs have no history file
    const noise = /cdnjs\.cloudflare\.com|\/data\/history\/d\d+\.json/.test(m.location().url + m.text());
    if (m.type() === "error" && !noise) problems.push("console: " + m.text());
  });
  page.on("dialog", (d) => { problems.push("dialog: " + d.message()); d.dismiss(); });
  await page.route("https://cdnjs.cloudflare.com/**", (r) => r.fulfill({ status: 404, body: "" }));
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
    for (const route of ["", "#/disc/innova-destroyer-star", "#/disc/innova-boss-champion"]) {
      const page = await openPage(base, { width: 360, route });
      await page.waitForSelector(route ? ".chart-card" : "table.discs tbody tr");
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
  if (REAL) await check("every disc written by the real exporter is listed and opens", checkExporterOutput);
  await check("no script errors, console errors or dialogs during the run", async () => ({ ok: problems.length === 0, detail: problems.slice(0, 5) }));
  await browser.close();
  console.log(JSON.stringify({ results }));
  finish(0);
})().catch((e) => { console.error(e); finish(2); });
