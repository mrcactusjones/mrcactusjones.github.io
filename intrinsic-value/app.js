/* ============================================================
   Intrinsic Value Portfolio — app logic
   Reads window.PORTFOLIO_DATA (from data.js). Works from file://
   ============================================================ */
(function () {
  const D = window.PORTFOLIO_DATA;
  if (!D) { console.error("data.js not loaded"); return; }

  /* ---- categorical palette (dark-bg friendly, brand-leaning) ---- */
  const PALETTE = [
    "#f4c65a", "#3ad29f", "#5b8def", "#e8894a", "#b073e6",
    "#35bcc9", "#f0616d", "#8ad35a", "#e06ba0", "#5ad0c0",
    "#c9a034", "#7f8cf0", "#e2a94a", "#4fb0e0", "#d07be6",
    "#9aa4b2" /* cash = grey */
  ];

  /* ---------------- formatting helpers ---------------- */
  const fmtMoney = (v, dec = 2) =>
    (v == null || isNaN(v)) ? "—"
      : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });

  const fmtBig = (v) => {
    if (v == null || isNaN(v)) return "—";
    if (Math.abs(v) >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
    if (Math.abs(v) >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
    return "$" + v.toFixed(0);
  };

  const fmtPct = (v, withSign = true) => {
    if (v == null || isNaN(v)) return "—";
    const p = v * 100;
    const s = (withSign && p > 0 ? "+" : "") + p.toFixed(1) + "%";
    return s;
  };

  const fmtShares = (v) => (v == null || isNaN(v)) ? "—"
    : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });

  const signClass = (v) => (v == null || isNaN(v)) ? "" : (v >= 0 ? "pos" : "neg");
  // Margin of safety: positive = undervalued (good = green)
  const mosClass = (v) => (v == null || isNaN(v)) ? "" : (v > 0 ? "pos" : "neg");

  const posValue = (s) => (s.shares != null && s.currentPrice != null)
    ? s.shares * s.currentPrice : null;

  /* ---------------- shared modal ---------------- */
  function ensureOverlay() {
    let ov = document.getElementById("detailOverlay");
    if (ov) return ov;
    ov = document.createElement("div");
    ov.id = "detailOverlay";
    ov.className = "overlay";
    ov.innerHTML = '<div class="modal" id="detailModal"></div>';
    document.body.appendChild(ov);
    ov.addEventListener("click", (e) => { if (e.target === ov) closeModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
    return ov;
  }
  function closeModal() {
    const ov = document.getElementById("detailOverlay");
    if (ov) ov.classList.remove("open");
  }
  window.__ivCloseModal = closeModal;

  function linkBtn(label, href, icon) {
    const off = !href ? " disabled" : "";
    const h = href ? ` href="${href}" target="_blank" rel="noopener"` : "";
    return `<a class="link-btn${off}"${h}><span class="ic">${icon}</span>${label}</a>`;
  }

  function openDetail(s, opts = {}) {
    const ov = ensureOverlay();
    const modal = document.getElementById("detailModal");
    const held = ("weight" in s) || ("shares" in s);

    // verdict badge from margin of safety
    let verdict = '<span class="verdict na">No target set</span>';
    if (s.marginOfSafety != null && !isNaN(s.marginOfSafety)) {
      if (s.marginOfSafety > 0)
        verdict = `<span class="verdict under">▲ Undervalued · ${fmtPct(s.marginOfSafety)} margin of safety</span>`;
      else
        verdict = `<span class="verdict over">▼ Above fair value · ${fmtPct(s.marginOfSafety)}</span>`;
    }

    const cells = [];
    const cell = (k, v, cls = "") => cells.push(
      `<div class="detail-cell"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`);

    cell("Fair Value Price", fmtMoney(s.fairValue));
    cell("Current Price", fmtMoney(s.currentPrice));
    cell("Margin of Safety", fmtPct(s.marginOfSafety), mosClass(s.marginOfSafety));
    cell("Target Last Updated", s.targetLastUpdated || "—", "muted");
    if (held) {
      cell("Portfolio Avg Price", fmtMoney(s.avgPrice));
      cell("Shares Owned", fmtShares(s.shares));
      cell("Gain / Loss", fmtPct(s.gain), signClass(s.gain));
      cell("Portfolio Weighting", fmtPct(s.weight, false));
      cell("Position Value", fmtBig(posValue(s)), "");
      if (opts.rank) cell("Watchlist Rank", "#" + opts.rank, "");
    } else if (opts.rank) {
      cell("Watchlist Rank", "#" + opts.rank, "");
    }

    modal.innerHTML = `
      <div class="modal-head">
        <button class="close" onclick="window.__ivCloseModal()" aria-label="Close">×</button>
        ${s.ticker ? `<span class="tk-badge">$${s.ticker}</span>` : ""}
        <h3>${s.name}</h3>
        ${verdict}
      </div>
      <div class="detail-grid">${cells.join("")}</div>
      <div class="modal-links">
        ${linkBtn("Valuation Model", s.modelLink, "▦")}
        ${linkBtn("Podcast Episode", s.podcastLink, "▶")}
        ${linkBtn("Newsletter", s.newsletterLink, "✉")}
      </div>`;
    ov.classList.add("open");
  }
  window.__ivOpenDetail = openDetail;

  /* ---------------- HOME PAGE ---------------- */
  function initHome() {
    // stat strip
    const t = D.totals;
    const statEl = document.getElementById("stats");
    if (statEl) {
      const stats = [
        ["Total Portfolio Value", fmtBig(t.totalValue), false],
        ["2025 Performance", t.perf2025 || "—", (t.perf2025 || "").startsWith("+") ? "pos" : "neg"],
        ["2026 YTD", fmtPct(t.ytd2026), signClass(t.ytd2026)],
        ["Cash Weighting", fmtPct(t.cashWeight, false), false],
        ["Holdings", String(D.holdings.length), false],
      ];
      statEl.innerHTML = stats.map(([k, v, cls]) =>
        `<div class="stat"><div class="k">${k}</div><div class="v small ${cls || ""}">${v}</div></div>`).join("");
    }

    // build slice list: holdings + cash
    const slices = D.holdings.map((h, i) => ({
      s: h, label: h.name, ticker: h.ticker,
      weight: h.weight || 0, color: PALETTE[i % PALETTE.length], isCash: false,
    }));
    slices.push({ s: null, label: "Cash", ticker: null, weight: t.cashWeight || 0, color: "#9aa4b2", isCash: true });

    const total = slices.reduce((a, b) => a + b.weight, 0) || 1;

    // ---- donut ----
    const svg = document.getElementById("donut");
    const R = 70, CX = 100, CY = 100, C = 2 * Math.PI * R, SW = 30;
    let acc = 0;
    const segEls = [];
    slices.forEach((sl, i) => {
      const frac = sl.weight / total;
      const seg = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      seg.setAttribute("class", "seg");
      seg.setAttribute("cx", CX); seg.setAttribute("cy", CY); seg.setAttribute("r", R);
      seg.setAttribute("stroke", sl.color);
      seg.setAttribute("stroke-width", SW);
      seg.setAttribute("stroke-dasharray", `${frac * C} ${C - frac * C}`);
      seg.setAttribute("stroke-dashoffset", `${-acc * C}`);
      seg.setAttribute("transform", `rotate(-90 ${CX} ${CY})`);
      seg.dataset.i = i;
      seg.addEventListener("mouseenter", () => hover(i, true));
      seg.addEventListener("mouseleave", () => hover(i, false));
      seg.addEventListener("click", () => { if (!sl.isCash) openDetail(sl.s); });
      svg.appendChild(seg);
      segEls.push(seg);
      acc += frac;
    });

    // center text
    const ctr = document.createElementNS("http://www.w3.org/2000/svg", "g");
    ctr.setAttribute("class", "donut-center");
    ctr.innerHTML = `
      <text x="100" y="86" text-anchor="middle" class="lab">Portfolio</text>
      <text x="100" y="106" text-anchor="middle" class="big" id="ctrBig">${fmtBig(t.totalValue)}</text>
      <text x="100" y="122" text-anchor="middle" class="sub" id="ctrSub">${D.holdings.length} holdings + cash</text>`;
    svg.appendChild(ctr);

    const legend = document.getElementById("legend");
    const rows = slices.map((sl, i) => {
      if (sl.isCash) {
        return `<div class="legend-row" data-i="${i}" data-cash="1">
          <span class="dot" style="background:${sl.color}"></span>
          <span class="nm"><b>Cash</b></span>
          <span class="wt num">${fmtPct(sl.weight, false)}</span>
          <span class="gn"></span></div>`;
      }
      return `<div class="legend-row" data-i="${i}">
        <span class="dot" style="background:${sl.color}"></span>
        <span class="nm"><b>${sl.label}</b><span class="tk">$${sl.ticker || ""}</span></span>
        <span class="wt num">${fmtPct(sl.weight, false)}</span>
        <span class="gn num ${signClass(sl.s.gain)}">${fmtPct(sl.s.gain)}</span></div>`;
    }).join("");
    legend.innerHTML = rows;

    const legendRows = Array.from(legend.querySelectorAll(".legend-row"));
    legendRows.forEach((row) => {
      const i = +row.dataset.i;
      row.addEventListener("mouseenter", () => hover(i, true));
      row.addEventListener("mouseleave", () => hover(i, false));
      row.addEventListener("click", () => { if (!row.dataset.cash) openDetail(slices[i].s); });
    });

    const ctrBig = document.getElementById("ctrBig");
    const ctrSub = document.getElementById("ctrSub");
    function hover(i, on) {
      segEls.forEach((el, j) => {
        if (on && j !== i) { el.style.opacity = "0.32"; }
        else { el.style.opacity = "1"; }
        el.setAttribute("stroke-width", (on && j === i) ? SW + 8 : SW);
      });
      legendRows.forEach((r) => r.classList.toggle("active", on && +r.dataset.i === i));
      if (on) {
        const sl = slices[i];
        ctrBig.textContent = fmtPct(sl.weight, false);
        ctrSub.textContent = sl.isCash ? "Cash" : sl.label;
      } else {
        ctrBig.textContent = fmtBig(D.totals.totalValue);
        ctrSub.textContent = D.holdings.length + " holdings + cash";
      }
    }
  }

  /* ---------------- WATCHLIST PAGE ---------------- */
  // find a coverage/holding record matching a watch-rank name
  function matchStock(name) {
    const norm = (x) => (x || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    const aliases = {
      "lulu": "lululemon", "meta": "meta", "constellationsoftware": "constellationsoftware",
    };
    let n = norm(name);
    n = aliases[n] || n;
    const pool = D.coverage.concat(D.holdings);
    // try name contains, then ticker
    let hit = pool.find((s) => norm(s.name) === n)
      || pool.find((s) => norm(s.name).includes(n) || n.includes(norm(s.name)))
      || pool.find((s) => norm(s.ticker) === norm(name));
    return hit || null;
  }

  function initWatchlist() {
    // ranked cards
    const rg = document.getElementById("rankGrid");
    if (rg) {
      rg.innerHTML = D.watchRank.map((w) => {
        const s = matchStock(w.name);
        const tk = s && s.ticker ? `$${s.ticker}` : "";
        let mos = `<div class="mos"><small>Margin of Safety</small><span class="muted-2">—</span></div>`;
        if (s && s.marginOfSafety != null && !isNaN(s.marginOfSafety)) {
          mos = `<div class="mos"><small>Margin of Safety</small>
            <span class="${mosClass(s.marginOfSafety)}">${fmtPct(s.marginOfSafety)}</span></div>`;
        }
        const top3 = w.rank <= 3 ? " top3" : "";
        return `<div class="rank-card" data-name="${w.name}">
          <div class="rk${top3}">${w.rank}</div>
          <h3>${s ? s.name : w.name}</h3>
          <div class="tk">${tk}</div>
          ${mos}</div>`;
      }).join("");
      rg.querySelectorAll(".rank-card").forEach((c) => {
        c.addEventListener("click", () => {
          const w = D.watchRank.find((x) => x.name === c.dataset.name);
          const s = matchStock(c.dataset.name);
          if (s) openDetail(s, { rank: w.rank });
        });
      });
    }

    // coverage table
    const cov = D.coverage.slice();
    let sortKey = "marginOfSafety", sortDir = -1;
    const tbody = document.getElementById("covBody");
    const searchInput = document.getElementById("covSearch");
    const countTag = document.getElementById("covCount");
    let filter = "";

    const cols = [
      { key: "name", label: "Company", num: false },
      { key: "fairValue", label: "Fair Value", num: true },
      { key: "currentPrice", label: "Current", num: true },
      { key: "marginOfSafety", label: "Margin of Safety", num: true },
      { key: "targetLastUpdated", label: "Target Updated", num: false },
      { key: "links", label: "Links", num: false, noSort: true },
    ];

    const thead = document.getElementById("covHead");
    function renderHead() {
      thead.innerHTML = "<tr>" + cols.map((c) => {
        if (c.noSort) return `<th data-k="${c.key}" class="nosort">${c.label}</th>`;
        const arw = sortKey === c.key ? `<span class="arw">${sortDir < 0 ? "▼" : "▲"}</span>` : "";
        return `<th data-k="${c.key}">${c.label}${arw}</th>`;
      }).join("") + "</tr>";
      thead.querySelectorAll("th").forEach((th) => {
        if (th.classList.contains("nosort")) return;
        th.addEventListener("click", () => {
          const k = th.dataset.k;
          if (sortKey === k) sortDir *= -1;
          else { sortKey = k; sortDir = (k === "name" || k === "targetLastUpdated") ? 1 : -1; }
          render();
        });
      });
    }

    function sortVal(s, k) {
      const v = s[k];
      if (k === "name") return (v || "").toLowerCase();
      if (k === "targetLastUpdated") { const d = Date.parse(v); return isNaN(d) ? -Infinity : d; }
      return (v == null || isNaN(v)) ? -Infinity : v;
    }

    function render() {
      renderHead();
      let rows = cov.filter((s) => {
        if (!filter) return true;
        const hay = (s.name + " " + (s.ticker || "")).toLowerCase();
        return hay.includes(filter);
      });
      rows.sort((a, b) => {
        const av = sortVal(a, sortKey), bv = sortVal(b, sortKey);
        if (av < bv) return -1 * sortDir;
        if (av > bv) return 1 * sortDir;
        return 0;
      });
      tbody.innerHTML = rows.map((s, idx) => {
        const ml = (href, txt) => href
          ? `<a href="${href}" target="_blank" rel="noopener" title="${txt}">${txt}</a>`
          : `<a class="off" title="Not available">${txt}</a>`;
        return `<tr class="row" data-idx="${cov.indexOf(s)}">
          <td><span class="nm"><b>${s.name}</b><span class="tk">$${s.ticker || "—"}</span></span></td>
          <td class="n">${fmtMoney(s.fairValue)}</td>
          <td class="n">${fmtMoney(s.currentPrice)}</td>
          <td class="n ${mosClass(s.marginOfSafety)}">${fmtPct(s.marginOfSafety)}</td>
          <td class="n" style="color:var(--muted)">${s.targetLastUpdated || "—"}</td>
          <td><span class="mini-links">${ml(s.modelLink, "Model")}${ml(s.podcastLink, "Pod")}${ml(s.newsletterLink, "News")}</span></td>
        </tr>`;
      }).join("");
      countTag.textContent = rows.length + " of " + cov.length + " companies";
      tbody.querySelectorAll("tr.row").forEach((tr) => {
        tr.addEventListener("click", (e) => {
          if (e.target.closest("a")) return; // let link clicks through
          openDetail(cov[+tr.dataset.idx]);
        });
      });
    }

    if (searchInput) {
      searchInput.addEventListener("input", () => { filter = searchInput.value.trim().toLowerCase(); render(); });
    }
    render();
  }

  /* ---------------- boot ---------------- */
  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page;
    if (page === "home") initHome();
    else if (page === "watchlist") initWatchlist();
  });
})();
