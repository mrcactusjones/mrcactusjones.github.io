// The dashboard recomputes the whole cost model in JavaScript so the sliders
// respond instantly. That duplication is the project's biggest correctness
// risk: the page and the Python model can drift apart and nothing would say
// so. This runs the page's own script against the current rankings and
// asserts they agree on every number that matters.
//
// It has already caught five real bugs: probabilities rounded in the payload
// but not in the maths, the historical floor priced at a cheaper PSA tier
// than the present, a downsampled series hiding the dip the model saw,
// capital months stored rounded but divided at full precision, and a fee
// slider that silently did nothing.
//
//   node tests/js/check_page_matches_model.js      (needs data/rankings.json)

// Evaluate the dashboard's script in a stubbed DOM and cross-check its money
// math against the numbers rank.py already wrote.
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const path = require("path");
const ROOT = path.join(__dirname, "..", "..");
const RANKINGS = path.join(ROOT, "data", "rankings.json");
const PAGE = path.join(ROOT, "index.html");

if (!fs.existsSync(RANKINGS)) {
  console.error("No data/rankings.json yet. Generate some first:\n" +
                "  python3 run.py demo   (fake data)\n" +
                "  python3 run.py rank   (real data)");
  process.exit(2);
}

const html = fs.readFileSync(PAGE, "utf8");
const script = html.split("<script>")[1].split("</script>")[0];

const el = () => ({ value:"0", textContent:"", innerHTML:"", hidden:false,
                    dataset:{}, onclick:null, addEventListener(){}, setAttribute(){},
                    querySelectorAll:()=>[], querySelector:()=>el() });
const ctx = {
  console,
  document: { getElementById: el, querySelector: el, querySelectorAll: () => [],
              documentElement: { style: {} } },
  getComputedStyle: () => ({ fontSize: "17px" }),
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: () => new Promise(() => {}),           // never resolves: boot() won't run
};
for (const id of ["fee","ship","mkt","out","prem","comps","q","set","head",
                  "vfee","vship","vmkt","vout","vprem","vcomps"]) ctx[id] = el();
vm.createContext(ctx);
vm.runInContext(script, ctx);
// SCORING is a `let` inside the script, so assign it inside the VM the way
// boot() does; setting it on the context object would not rebind it.
const cfgBlob = JSON.parse(fs.readFileSync(RANKINGS, "utf8"));
vm.runInContext("SCORING = " + JSON.stringify(cfgBlob.scoring), ctx);
// Thresholds too: boot() mirrors them into T, and without that the check
// compares the page's defaults against Python's configured values.
vm.runInContext("T = Object.assign({}, T, " +
                JSON.stringify(cfgBlob.config.thresholds) + ")", ctx);

const data = JSON.parse(fs.readFileSync(RANKINGS, "utf8"));
const e = data.config.econ, t = data.config.thresholds;
const s = { fee:e.grading_fee, ship:e.sub_ship_per_card, mkt:e.sale_fee_pct*100,
            out:e.ship_out, prem:e.raw_premium_pct*100, comps:t.min_sales_9,
            maxAge:t.max_sale_age_days, lowRecovery:t.low_grade_recovery,
            // Mirror boot(): the page takes the fee tiers from the config.
            tiers: e.use_fee_tiers ? e.fee_tiers : null,
            insThreshold: e.insurance_threshold, insPct: e.insurance_pct };

let checked = 0, verdictMatches = 0, evChecked = 0, worstChecked = 0, convChecked = 0;
for (const row of data.rows) {
  const js = ctx.score(row, s);
  assert.ok(Math.abs(js.floor - row.floor_profit) < 0.01,
    `floor mismatch ${row.id}: js=${js.floor} py=${row.floor_profit}`);
  assert.ok(Math.abs(js.upside - row.upside_profit) < 0.01,
    `upside mismatch ${row.id}: js=${js.upside} py=${row.upside_profit}`);
  // With no PSA 10 comps both sides leave upside equal to the floor, so the
  // numbers agree while the pages disagree about what they mean. The flag is
  // what the display keys off, so it has to match too.
  if (row.upside_known != null) {
    assert.strictEqual(js.upsideKnown, row.upside_known,
      `upside_known mismatch ${row.id}: js=${js.upsideKnown} py=${row.upside_known}`);
  }
  assert.ok(Math.abs(js.cost - row.all_in) < 0.01, `all-in mismatch ${row.id}`);
  if (row.breakeven_p10 == null) assert.strictEqual(js.be, null, `be mismatch ${row.id}`);
  else assert.ok(Math.abs(js.be - row.breakeven_p10) < 1e-6, `be mismatch ${row.id}`);
  if (row.ev_profit != null) {
    assert.ok(Math.abs(js.ev - row.ev_profit) < 0.01,
      "EV mismatch " + row.id + ": js=" + js.ev + " py=" + row.ev_profit);
    evChecked++;
  } else {
    assert.strictEqual(js.ev, null, "EV should be absent for " + row.id);
  }
  if (row.floor_worst_90d != null && js.worst != null) {
    assert.ok(Math.abs(js.worst - row.floor_worst_90d) < 0.02,
      "worst mismatch " + row.id + ": js=" + js.worst + " py=" + row.floor_worst_90d);
    worstChecked++;
  }
  if (row.floor_per_month != null) {
    assert.ok(Math.abs(js.perMonth - row.floor_per_month) < 0.02,
      "per-month mismatch " + row.id + ": js=" + js.perMonth + " py=" + row.floor_per_month);
  }
  if (row.conviction != null) {
    assert.ok(Math.abs(js.conv.value - row.conviction) < 0.05,
      "conviction mismatch " + row.id + ": js=" + js.conv.value + " py=" + row.conviction);
    convChecked++;
  }
  if (js.verdict === row.verdict) verdictMatches++;
  checked++;
}
assert.strictEqual(verdictMatches, checked,
  `${checked - verdictMatches}/${checked} verdicts disagree between JS and Python`);

// Sparkline must survive short/empty/null-laden series.
for (const series of [null, [], [1], [1,2,3], [null,null], [5,null,7]])
  assert.ok(typeof ctx.spark(series) === "string");

console.log("OK: " + checked + " rows agree on all-in, floor, upside, break-even and verdict; " + evChecked + " also agree on EV; " + worstChecked + " on the worst-case floor; " + convChecked + " on conviction");
