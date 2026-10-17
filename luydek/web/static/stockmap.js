// The stock map (/map): each shelf face on, the way the owner sees it, one at a time like slides.
//
// A shelf is its levels, top to bottom, with each product in its spot left to right (as wide as its
// facings), showing how many are left and the first date. The colours show one layer at a time:
// expiry (red: expired, amber: expires soon or won't sell in time, green: fine), dead stock (darker:
// more money sleeping), or stock (a dashed gap: out of stock). The arrows, the strip of shelf names, the
// arrow keys and a swipe go from shelf to shelf. Searching for a product or a lot, or a filter, lights up
// its spots, says where each one is ("Shelf A, level 2 of 4, 3rd from the left") and goes to the first
// shelf with one. Today's walk goes through the shelves with something to do. "Edit this shelf" moves
// products by dragging, adds them with +, and changes the levels.
(() => {
  const $ = (id) => document.getElementById(id);
  const T = window.mapT || ((k, v = {}) => k.split("|")[0].replace(/\{(\w+)\}/g, (m, x) => (x in v ? v[x] : m)));
  const phone = () => window.matchMedia("(max-width: 991.98px)").matches;
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
  };
  const boot = JSON.parse($("map-data").textContent);
  let M = boot.map;
  let layer = store.get("luydek-map-layer") || "expiry";
  let filter = boot.show || "all";
  let query = $("q").value.trim();
  let mode = "view";            // view | walk | edit
  let cur = null;               // the shelf shown: a place id, or "none" (stock with no place yet)
  let sel = null;               // the product picked on this shelf
  let draft = null;             // edit mode: {name, kind, levels, items: [{product, level, width}]}
  let adding = null;            // edit mode: the level a product is being added to
  let enter = "";               // the way the last slide came in

  const money = (v) => "$" + (Math.round(v * 100) / 100).toFixed(2);
  // a date the way people say it: "5 Oct", or "5 Oct 2027" when it is not this year
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const short = (iso) => {
    if (!iso) return "";
    const day = `${+iso.slice(8, 10)} ${MONTHS[+iso.slice(5, 7) - 1]}`;
    return +iso.slice(0, 4) === new Date().getFullYear() ? day : `${day} ${iso.slice(0, 4)}`;
  };
  const nth = (n) => (document.documentElement.lang === "km" ? String(n) : n + (["th", "st", "nd", "rd"][(n % 100 >= 11 && n % 100 <= 13) ? 0 : n % 10] || "th"));
  const prod = (id) => M.products[id] || { name: (M.batches.find((b) => b.product === id) || {}).name || "?", photo: null };
  const place = (id) => M.places.find((p) => p.id === id);
  const cap = (s) => T(s[0].toUpperCase() + s.slice(1));

  // --- what to show -------------------------------------------------------------------------------
  const FILTERS = [
    ["all", T("Everything"), () => true],
    ["expired", T("Past their date"), (b) => b.status.includes("expired")],
    ["expiring", T("Expires within {n} days", { n: M.warning_days }), (b) => b.status.includes("expiring")],
    ["clear", T("Won't sell in time"), (b) => b.status.includes("clear")],
    ["sleeping", T("Sleeping money"), (b) => b.status.includes("sleeping")],
    ["out", T("Out of stock"), null],
    ["none", T("No place yet"), (b) => b.place == null],
  ];
  const testOf = () => (FILTERS.find((f) => f[0] === filter) || FILTERS[0])[2];
  const matchText = (r) => !query || [r.name, r.name_km, r.sku, r.lot].some((t) => t && t.toLowerCase().includes(query.toLowerCase()));
  const looking = () => (!!query || filter !== "all") && mode !== "edit";
  const hitBatch = (b) => filter !== "out" && testOf()(b) && matchText(b);
  const hitOut = (o) => filter === "out" || (!!query && filter === "all" && matchText(o));

  // --- the slides -----------------------------------------------------------------------------------
  const unplacedOut = () => M.out.filter((o) => !o.places.length);
  function slides() {
    const all = M.places.map((p) => p.id);
    if (M.unplaced.units || (filter === "out" && unplacedOut().length)) all.push("none");
    if (mode === "walk") {
      const stops = M.walk.map((s) => s.place ?? "none");
      return all.filter((id) => stops.includes(id)).sort((a, b) => stops.indexOf(a) - stops.indexOf(b));
    }
    return all;
  }
  const slideName = (id) => (id === "none" ? T("No place yet") : (place(id) || {}).name || "?");

  // Each shelf as the page draws it: levels of spots, and what is on it but on no level.
  function shelf(id) {
    const p = id === "none" ? null : place(id);
    const items = p ? (mode === "edit" && draft && draft.id === id ? draft.items : p.facings) : [];
    const n = p ? (mode === "edit" && draft && draft.id === id ? draft.levels : p.levels) : 0;
    const here = M.batches.filter((b) => (id === "none" ? b.place == null : b.place === id));
    const byProd = {};
    for (const b of here) (byProd[b.product] = byProd[b.product] || []).push(b);
    const outs = new Set(M.out.filter((o) => (id === "none" ? !o.places.length : o.places.includes(id))).map((o) => o.product));
    const spot = (product, extra) => ({ product, batches: byProd[product] || [], out: outs.has(product) && !(byProd[product] || []).length, ...extra });
    const levels = Array.from({ length: n }, () => []);
    const placed = new Set();
    items.forEach((it, i) => {
      placed.add(it.product);
      levels[Math.min(n, Math.max(1, it.level)) - 1].push(spot(it.product, { level: it.level, width: it.width, i }));
    });
    const loose = [...new Set([...Object.keys(byProd).map(Number), ...outs])].filter((pid) => !placed.has(pid))
      .map((pid) => spot(pid, { level: null, width: 1, i: null }));
    return { p, levels, loose };
  }

  // Where a product is on a shelf, in words.
  function where(id, product) {
    if (id === "none" || id == null) return T("No place yet");
    const p = place(id);
    if (!p) return "?";
    const at = p.facings.findIndex((f) => f.product === product);
    if (at < 0) return `${p.name}, ${T("not on a level yet")}`;
    const f = p.facings[at];
    const pos = p.facings.slice(0, at).filter((g) => g.level === f.level).length + 1;
    const lv = p.levels === 1 ? "" : f.level === 1 ? `, ${T("top level")}` : f.level === p.levels ? `, ${T("bottom level")}`
      : `, ${T("level {n} of {m}", { n: f.level, m: p.levels })}`;
    return `${p.name}${lv}, ${T("{n} from the left", { n: nth(pos) })}`;
  }

  // --- colours: one layer at a time -------------------------------------------------------------------
  const sleepValue = (s) => s.batches.filter((b) => b.status.includes("sleeping")).reduce((t, b) => t + b.value, 0);
  let maxSleep = 0;
  function tone(s) {
    if (s.out) return "t-out";
    if (!s.batches.length) return "t-empty";
    if (layer === "expiry") {
      if (s.batches.some((b) => b.status.includes("expired"))) return "t-bad";
      if (s.batches.some((b) => b.status.includes("expiring") || b.status.includes("clear"))) return "t-warn";
      return "t-ok";
    }
    if (layer === "dead") {
      const v = sleepValue(s);
      return v ? "t-d" + Math.max(1, Math.ceil((v / (maxSleep || 1)) * 4)) : "t-d0";
    }
    return "t-stock";
  }
  function firstDate(s) {
    const dated = s.batches.filter((b) => b.expiry).sort((a, b) => a.expiry.localeCompare(b.expiry));
    return dated.length ? dated[0] : null;
  }
  const units = (s) => s.batches.reduce((t, b) => t + b.left, 0);
  const isHit = (s) => s.batches.some(hitBatch) || (s.out && hitOut({ ...prod(s.product), product: s.product, name: prod(s.product).name }));
  function hitsOn(id) {
    const sh = shelf(id);
    return [...sh.levels.flat(), ...sh.loose].filter(isHit).length;
  }

  // --- drawing the shelf -------------------------------------------------------------------------------
  const DO = { remove: T("Take off"), front: T("Put in front"), clear: T("Won't sell in time") };
  function taskFor(s) {
    if (mode !== "walk") return null;
    const stop = M.walk.find((w) => (w.place ?? "none") === cur);
    const ts = stop ? stop.tasks.filter((t) => t.product === s.product) : [];
    if (!ts.length) return null;
    const order = ["remove", "front", "clear"];
    ts.sort((a, b) => order.indexOf(a.action) - order.indexOf(b.action));
    return { action: ts[0].action, left: ts.filter((t) => t.action === ts[0].action).reduce((n, t) => n + t.left, 0) };
  }
  // A spot's batches, soonest date first: one product often has several dates on the shelf at once.
  const dated = (s) => s.batches.slice().sort((a, b) => (a.expiry || "9999").localeCompare(b.expiry || "9999"));
  const lotState = (b) => (b.status.includes("expired") ? "bad" : b.status.includes("expiring") || b.status.includes("clear") ? "warn"
    : b.expiry ? "ok" : "none");
  function lotWords(s) {   // what the strip says in words: the part that needs doing, not the whole spot
    const bs = dated(s);
    const expired = bs.filter((b) => b.status.includes("expired")).reduce((t, b) => t + b.left, 0);
    if (expired) return { text: T("{n} expired", { n: expired }), tone: "bad" };
    const soon = bs.filter((b) => b.status.includes("expiring")).reduce((t, b) => t + b.left, 0);
    const first = bs.find((b) => b.expiry);
    if (soon) return { text: T("exp {date}", { date: short(first.expiry) }), tone: "warn" };
    const dates = new Set(bs.map((b) => b.expiry).filter(Boolean)).size;
    if (first) return { text: `${T("exp {date}", { date: short(first.expiry) })}${dates > 1 ? ` +${dates - 1}` : ""}`, tone: "" };
    return { text: T("no date"), tone: "" };
  }
  function strip(s) {
    const bs = dated(s), total = bs.reduce((t, b) => t + b.left, 0);
    if (!total || bs.length < 1) return "";
    return `<span class="spot-lots" aria-hidden="true">${bs.map((b) => `<i class="seg seg-${lotState(b)}" style="flex:${b.left}"></i>`).join("")}</span>`;
  }
  const lotList = (s) => dated(s).map((b) => `${b.left} ${b.expiry ? (b.days_left < 0 ? T("expired {date}", { date: short(b.expiry) }) : T("exp {date}", { date: short(b.expiry) })) : T("no date")}`).join(", ");
  function spotHtml(s, small = false) {
    const pr = prod(s.product);
    const n = units(s);
    const searching = looking();
    const hit = searching && isHit(s);
    const task = taskFor(s);
    const words = s.batches.length ? lotWords(s) : { text: s.out ? T("out of stock") : M.batches.some((b) => b.product === s.product) ? T("none here") : T("not counted"), tone: "" };
    const date = words.text;
    const cls = ["spot", tone(s), hit ? "hit" : "", searching && !hit ? "dim" : "", sel === s.product ? "sel" : "",
                 s.out ? "gap" : "", task ? `has-task task-${task.action}` : "", small ? "small-spot" : ""].filter(Boolean).join(" ");
    const label = `${pr.name}: ${s.out ? T("out of stock") : n ? `${T("{n} left", { n })}: ${lotList(s)}` : T("not counted")}`;
    return `<button type="button" class="${cls}" style="--w:${s.width || 1}" data-product="${s.product}" data-i="${s.i ?? ""}"
        aria-label="${esc(label)}" aria-pressed="${sel === s.product}">
      ${task ? `<span class="spot-task">${esc(DO[task.action])} ×${task.left}</span>` : ""}
      <span class="spot-img">${pr.photo ? `<img src="/thumb/${pr.photo}.jpg" alt="" loading="lazy" draggable="false">`
        : '<i class="bi bi-box-seam" aria-hidden="true"></i>'}</span>
      <span class="spot-name">${esc(pr.name)}</span>
      ${strip(s)}
      <span class="spot-meta"><b>${s.out ? "0" : `×${n}`}</b> · <span class="${words.tone ? `t-${words.tone}` : ""}">${esc(date)}</span></span>
    </button>`;
  }

  function drawStage() {
    const stage = $("stage");
    const all = slides();
    if (!all.length) {
      stage.innerHTML = `<div class="empty-state"><div class="ico"><i class="bi bi-bookshelf" aria-hidden="true"></i></div>
        <div class="t">${esc(T("No shelves yet"))}</div><div>${esc(T("Add your first with New shelf: from a photo of it, from your spreadsheet, or empty."))}</div></div>`;
      return;
    }
    const sh = shelf(cur);
    const editing = mode === "edit" && sh.p;
    maxSleep = Math.max(0, ...M.places.flatMap((p) => { const x = shelf(p.id); return [...x.levels.flat(), ...x.loose].map(sleepValue); }));
    const widths = sh.levels.map((row) => row.reduce((t, s) => t + (s.width || 1), 0) + (editing ? 1 : 0));
    const cols = Math.max(5, ...widths);
    const kind = sh.p ? sh.p.kind : "none";
    let html = "";
    if (sh.p) {
      const bare = !editing && !sh.levels.some((row) => row.length);   // nothing placed yet: short levels, the stock in view
      html += `<div class="shelf k-${kind}${editing ? " editing" : ""}${bare ? " bare" : ""} ${enter}" style="--cols:${cols}">
        ${sh.levels.map((row, li) => `<div class="level" data-level="${li + 1}">
          <span class="level-no" aria-hidden="true">${li + 1}</span>
          <div class="level-row" data-level="${li + 1}" role="group" aria-label="${esc(T("level {n} of {m}", { n: li + 1, m: sh.levels.length }))}">
            ${row.map((s) => spotHtml(s)).join("")}
            ${editing ? `<button type="button" class="add-spot${adding === li + 1 ? " on" : ""}" data-add="${li + 1}" aria-label="Add a product to level ${li + 1}">
              <i class="bi bi-plus-lg" aria-hidden="true"></i></button>` : ""}
            ${!row.length && !editing ? `<span class="level-empty">${esc(bare && li === 0 ? T("Nothing placed on its levels yet: Edit this shelf, or New shelf from a photo or a spreadsheet") : T("empty"))}</span>` : ""}
          </div></div>`).join("")}
      </div>`;
    }
    if (sh.loose.length || !sh.p) {
      html += `<div class="shelf-loose ${!sh.p ? enter : ""}">
        <div class="loose-title">${esc(sh.p ? T("On this shelf, not on a level yet") : T("Stock that isn't anywhere yet: tap one to put it on a shelf"))}</div>
        <div class="loose-row">${sh.loose.map((s) => spotHtml(s, true)).join("") || `<span class="muted small">${esc(T("Nothing."))}</span>`}</div></div>`;
    }
    stage.innerHTML = html;
    enter = "";
  }

  function drawStrip() {
    const all = slides();
    $("strip").innerHTML = all.map((id) => {
      const p = id === "none" ? null : place(id);
      let badge = "";
      if (looking()) {
        const n = hitsOn(id);
        badge = n ? `<b class="sb sb-hit">${n}</b>` : "";
      } else if (p && layer === "expiry" && (p.expired || p.expiring)) {
        badge = `<b class="sb ${p.expired ? "sb-bad" : "sb-warn"}">${p.expired || p.expiring}</b>`;
      } else if (p && layer === "dead" && p.sleeping_value) {
        badge = `<b class="sb sb-sleep">${money(p.sleeping_value)}</b>`;
      } else if (p && layer === "stock" && p.out.length) {
        badge = `<b class="sb sb-bad">${p.out.length} ${esc(T("out"))}</b>`;
      }
      const on = id === cur;
      const dim = looking() && !hitsOn(id);
      return `<button type="button" role="tab" class="strip-tab${on ? " on" : ""}${dim ? " dim" : ""}" data-slide="${id}"
          aria-selected="${on}">${esc(slideName(id))}${badge}</button>`;
    }).join("");
    const on = $("strip").querySelector(".on");
    if (on) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function drawHead() {
    const all = slides();
    const i = all.indexOf(cur);
    const p = cur === "none" ? null : place(cur);
    $("shelf-name").textContent = all.length ? (mode === "edit" && draft ? draft.name : slideName(cur)) : T("No shelves yet");
    const bits = [];
    const lv = mode === "edit" && draft ? draft.levels : p ? p.levels : 0;
    if (p) bits.push(cap(mode === "edit" && draft ? draft.kind : p.kind), T("{n} level|{n} levels", { n: lv }));
    if (all.length) bits.push(mode === "walk" ? T("stop {i} of {n}", { i: i + 1, n: all.length }) : T("{i} of {n}", { i: i + 1, n: all.length }));
    $("shelf-sub").textContent = bits.join(" · ");
    $("prev").disabled = i <= 0 || mode === "edit";
    $("next").disabled = i < 0 || i >= all.length - 1 || mode === "edit";
    $("strip").classList.toggle("d-none", mode === "edit");
  }

  // --- the panel beside the shelf ------------------------------------------------------------------------
  function row(b, actions = true) {
    const when = b.expiry
      ? (b.days_left < 0 ? T("expired {date}", { date: short(b.expiry) }) : b.days_left === 0 ? T("expires today")
        : `${T("exp {date}", { date: short(b.expiry) })} · ${T("{n} days", { n: b.days_left })}`)
      : b.estimated ? T("no date on it") : T("no date");
    const pills = [];
    if (b.status.includes("expired")) pills.push(`<span class="tag tag-expired">${esc(T("expired"))}</span>`);
    else if (b.status.includes("expiring")) pills.push(`<span class="tag tag-expiring">${esc(T("expires soon"))}</span>`);
    if (b.status.includes("clear")) pills.push(`<span class="tag tag-expiring">${esc(T("won't sell in time"))}</span>`);
    if (b.status.includes("sleeping")) pills.push(`<span class="tag tag-info">${esc(T("sleeping"))}${b.since_sale != null ? ` · ${esc(T("no sale in {n} days", { n: b.since_sale }))}` : ""}</span>`);
    const opts = M.places.map((p) => `<option value="${p.id}" ${p.id === b.place ? "selected" : ""}>${esc(p.name)}</option>`).join("");
    return `<li class="list-group-item map-row" data-batch="${b.batch}">
        <div class="fw-semibold">×${b.left} <span class="muted fw-normal small">${esc(when)}${b.lot ? ` · ${esc(T("lot {lot}", { lot: b.lot }))}` : ""}${b.value ? ` · ${money(b.value)}` : ""}</span></div>
        <div class="d-flex gap-1 flex-wrap mt-1">${pills.join("")}</div>
        ${actions ? `<div class="d-flex gap-1 flex-wrap mt-2">
          <button type="button" class="btn btn-sm ${b.status.includes("expired") ? "btn-danger" : "btn-outline-danger"}" data-act="off">${esc(T("Taken off"))}</button>
          <select class="form-select form-select-sm" data-act="move" aria-label="${esc(T("Moved to"))}" style="max-width:12rem">
            <option value="" ${b.place == null ? "selected" : ""}>${esc(T("No place"))}</option>${opts}</select>
        </div>` : ""}
      </li>`;
  }
  const sortByDate = (a, b) => (a.days_left ?? 1e9) - (b.days_left ?? 1e9) || a.name.localeCompare(b.name);
  const thumb = (pid) => { const ph = prod(pid).photo; return ph ? `<img src="/thumb/${ph}.jpg" alt="" class="map-thumb">` : '<span class="map-thumb"></span>'; };

  function panel() {
    const box = $("panel"), sheet = $("spot-sheet");
    if (sel != null && mode !== "edit" && phone()) {          // phones: the product in a sheet over the shelf
      spotPanel($("spot-body"));
      if (sheet && !sheet.open) { sheet.showModal(); sheet.focus(); }
      sel = sel;                                             // the list below stays the shelf's (or the walk's)
      return mode === "walk" ? walkPanel(box) : looking() ? resultsPanel(box) : shelfPanel(box);
    }
    if (sheet && sheet.open) sheet.close();
    if (mode === "edit") return editPanel(box);
    if (mode === "walk") return walkPanel(box);
    if (sel != null) return spotPanel(box);
    if (looking()) return resultsPanel(box);
    return shelfPanel(box);
  }

  function shelfPanel(box) {
    if (!slides().length) { box.innerHTML = `<div class="panel-head"><b>${esc(T("Shelves"))}</b></div><div class="panel-empty">${esc(T("No shelves yet"))}</div>`; return; }
    const sh = shelf(cur);
    const p = sh.p;
    const all = [...sh.levels.flat(), ...sh.loose];
    const sub = p ? [T("{n} product|{n} products", { n: p.products }), T("{n} unit|{n} units", { n: p.units }), T("{money} at cost", { money: money(p.value) })]
        .concat(p.sleeping_value ? [T("{money} sleeping", { money: money(p.sleeping_value) })] : []).join(" · ")
      : T("Stock that isn't anywhere yet: tap one to put it on a shelf");
    const levelName = (li) => (sh.levels.length === 1 ? "" : li === 0 ? T("Top") : li === sh.levels.length - 1 ? T("Bottom") : T("Level {n}", { n: li + 1 }));
    const lotChips = (s) => dated(s).map((b) => `<span class="lot lot-${{ bad: "expired", warn: "soon", ok: "ok", none: "undated" }[lotState(b)]}"><b>${b.left}</b> ${b.expiry ? short(b.expiry) : esc(T("no date"))}</span>`).join("");
    const rowOf = (s) => `<li><button type="button" class="map-row-btn" data-pick="${s.product}">
        ${thumb(s.product)}
        <span class="map-row-text"><b>${esc(prod(s.product).name)}</b>
          <span class="lots">${s.out ? `<span class="tag tag-out">${esc(T("out of stock"))}</span>` : lotChips(s) || `<span class="small muted">${esc(T("none here"))}</span>`}</span></span>
        <span class="map-row-n">${s.out ? "0" : units(s) || "–"}</span></button></li>`;
    const body = sh.levels.map((row, li) => row.length ? (levelName(li) ? `<li class="lvl">${esc(levelName(li))}</li>` : "") + row.map(rowOf).join("") : "").join("")
      + (sh.loose.length ? `<li class="lvl">${esc(p ? T("not on a level yet") : T("Here"))}</li>${sh.loose.map(rowOf).join("")}` : "");
    box.innerHTML = `<div class="panel-head"><b>${esc(slideName(cur))}</b><span>${esc(sub)}</span></div>
      ${all.length ? `<ul class="map-list">${body}</ul>` : `<div class="panel-empty">${esc(T("Nothing here yet."))}</div>`}`;
  }

  function spotPanel(box) {
    const sh = shelf(cur);
    const s = [...sh.levels.flat(), ...sh.loose].find((x) => x.product === sel);
    if (!s) { sel = null; return panel(); }
    const pr = prod(s.product);
    const list = s.batches.slice().sort(sortByDate);
    const elsewhere = M.batches.filter((b) => b.product === s.product && b.place !== (cur === "none" ? null : cur));
    const places = [...new Set(elsewhere.map((b) => b.place ?? "none"))];
    box.innerHTML = `<div class="panel-head spot-head">
        <span class="d-flex gap-3 align-items-center">${thumb(s.product)}<span><b>${esc(pr.name)}</b>
          <span><i class="bi bi-geo-alt-fill" aria-hidden="true"></i> ${esc(where(cur, s.product))}</span></span></span>
        ${box.id === "panel" ? `<button type="button" class="btn-close" data-act="close" aria-label="${esc(T("Shelves"))}"></button>` : ""}</div>
      <ul class="list-group list-group-flush">
        ${s.out ? `<li class="list-group-item"><span class="tag tag-out">${esc(T("out of stock"))}</span> <span class="small muted">${esc(T("a gap on the shelf"))}</span></li>` : ""}
        ${list.map((b) => row(b)).join("")}
        ${!list.length && !s.out ? `<li class="list-group-item small muted">${esc(T("None counted here."))}</li>` : ""}
        ${places.length ? `<li class="list-group-item small">${esc(T("Also on"))} ${places.map((id) => `<a href="#" data-goto="${id}" data-pick="${s.product}">${esc(slideName(id === "none" ? "none" : +id))}</a>`).join(", ")}</li>` : ""}
      </ul>
      <div class="p-3"><a class="link-row" href="/catalog/${s.product}"><i class="bi bi-box-seam" aria-hidden="true"></i> ${esc(T("Open the product"))} <i class="bi bi-chevron-right ms-auto" aria-hidden="true"></i></a></div>`;
  }

  function resultsPanel(box) {
    const groups = [];
    for (const id of slides()) {
      const sh = shelf(id);
      const found = [...sh.levels.flat(), ...sh.loose].filter(isHit);
      if (found.length) groups.push({ id, found });
    }
    const what = query ? `“${esc(query)}”` : esc((FILTERS.find((f) => f[0] === filter) || FILTERS[0])[1]);
    const n = groups.reduce((t, g) => t + g.found.length, 0);
    box.innerHTML = `<div class="panel-head"><b>${what}</b><span>${esc(T("{n} spot on {m} shelves|{n} spots on {m} shelves", { n, m: groups.length }))}</span></div>
      <ul class="list-group list-group-flush">
        ${groups.map((g) => g.found.map((s) => {
          const first = firstDate(s);
          const hitB = s.batches.filter(hitBatch);
          return `<li class="list-group-item map-hit${g.id === cur ? " here" : ""}" data-goto="${g.id}" data-pick="${s.product}" role="button" tabindex="0">
            <div class="d-flex gap-2 align-items-center">${thumb(s.product)}<div class="flex-grow-1" style="min-width:0">
              <div class="fw-semibold">${esc(prod(s.product).name)} <span class="muted fw-normal">${s.out ? `· ${esc(T("out"))}` : `×${units(s)}`}</span></div>
              <div class="small"><i class="bi bi-geo-alt-fill" aria-hidden="true"></i> ${esc(where(g.id, s.product))}</div>
              <div class="small muted">${first ? esc(T("first date {date}", { date: short(first.expiry) })) : ""}</div>
            </div></div></li>`;
        }).join("")).join("")}
        ${!n ? `<li class="list-group-item muted">${esc(T("Nothing here."))}</li>` : ""}
      </ul>`;
  }

  function merged(tasks) {   // batches of one product with the same date and job are one line
    const out = [];
    for (const t of tasks) {
      const same = out.find((o) => o.product === t.product && o.action === t.action && o.expiry === t.expiry);
      if (same) same.left += t.left; else out.push({ ...t });
    }
    return out;
  }
  function walkPanel(box) {
    if (!M.walk.length) {
      box.innerHTML = `<div class="panel-head"><b>${esc(T("Today's walk"))}</b></div><div class="panel-empty">${esc(T("Nothing to do: nothing has expired, and nothing expires in the next {n} days.", { n: M.warning_days }))}</div>`;
      return;
    }
    box.innerHTML = `<div class="panel-head"><b>${esc(T("Today's walk"))}</b><span>${esc(T("Take off what has expired, put what expires soon in front."))}</span></div>
      <ol class="list-group list-group-flush list-group-numbered">${M.walk.map((st) => {
        const id = st.place ?? "none";
        return `<li class="list-group-item map-stop${id === cur ? " here" : ""}" data-goto="${id}" role="button" tabindex="0">
          <span class="fw-semibold">${esc(slideName(id))}</span>
          <ul class="small mb-0 ps-3">${merged(st.tasks).map((t) => `<li><span class="tag ${t.action === "remove" ? "tag-expired" : "tag-expiring"}">${esc(DO[t.action])}</span>
            ${esc(t.name)} ×${t.left} <span class="muted">${t.expiry ? (t.days_left < 0 ? esc(T("expired {date}", { date: short(t.expiry) })) : esc(T("exp {date}", { date: short(t.expiry) }))) : ""}</span></li>`).join("")}</ul></li>`;
      }).join("")}</ol>`;
  }

  // Today's walk as a card above the shelves: start it, then stop by stop.
  function walkCard() {
    const box = $("walk-card");
    if (!box) return;
    const stops = M.walk.length;
    if (!stops || mode === "edit") { box.innerHTML = ""; return; }
    if (mode !== "walk") {
      box.innerHTML = `<div class="walk-card"><span class="task-tile tone-warn"><i class="bi bi-signpost-split" aria-hidden="true"></i></span>
        <span class="row-text"><b>${esc(T("Today's walk"))}</b><span>${esc(T("{n} shelf has something to do|{n} shelves have something to do", { n: stops }))}</span></span>
        <button type="button" class="btn btn-dark" data-walk="start">${esc(T("Start"))}</button></div>`;
      return;
    }
    const all = slides(), i = all.indexOf(cur);
    box.innerHTML = `<div class="walk-card on"><span class="task-tile tone-warn"><i class="bi bi-signpost-split" aria-hidden="true"></i></span>
      <span class="row-text"><b>${esc(T("stop {i} of {n}", { i: i + 1, n: all.length }))}: ${esc(slideName(cur))}</b>
        <span>${esc(i < all.length - 1 ? T("Take off what has expired, put what expires soon in front.") : T("That was the last stop."))}</span></span>
      ${i < all.length - 1 ? `<button type="button" class="btn btn-dark" data-walk="next">${esc(T("Next stop"))}</button>` : ""}
      <button type="button" class="btn btn-outline-dark" data-walk="stop">${esc(T("Stop"))}</button></div>`;
  }

  function editPanel(box) {
    if (!draft) return;
    if (adding != null) return pickerPanel(box);
    const s = sel != null ? draft.items.findIndex((it) => it.product === sel) : -1;
    if (s >= 0) {
      const it = draft.items[s];
      const sameLevel = draft.items.filter((x) => x.level === it.level);
      const at = sameLevel.indexOf(it);
      box.innerHTML = `<div class="card-header d-flex justify-content-between align-items-center gap-2">
          <div class="d-flex gap-2 align-items-center">${thumb(it.product)}<span>${esc(prod(it.product).name)}</span></div>
          <button type="button" class="btn-close" data-act="close" aria-label="Done with this product"></button></div>
        <div class="card-body d-flex flex-column gap-2">
          <div class="small muted">Level ${it.level} of ${draft.levels}, ${nth(at + 1)} from the left, ${it.width} wide.</div>
          <div class="edit-pad">
            <button type="button" class="btn btn-outline-dark" data-move="up" ${it.level <= 1 ? "disabled" : ""}><i class="bi bi-arrow-up" aria-hidden="true"></i> Level up</button>
            <button type="button" class="btn btn-outline-dark" data-move="down" ${it.level >= draft.levels ? "disabled" : ""}><i class="bi bi-arrow-down" aria-hidden="true"></i> Level down</button>
            <button type="button" class="btn btn-outline-dark" data-move="left" ${at <= 0 ? "disabled" : ""}><i class="bi bi-arrow-left" aria-hidden="true"></i> Left</button>
            <button type="button" class="btn btn-outline-dark" data-move="right" ${at >= sameLevel.length - 1 ? "disabled" : ""}><i class="bi bi-arrow-right" aria-hidden="true"></i> Right</button>
          </div>
          <div class="d-flex gap-2 align-items-center"><span class="small">How many wide</span>
            <button type="button" class="btn btn-sm btn-outline-dark" data-move="narrower" aria-label="Narrower" ${it.width <= 1 ? "disabled" : ""}>−</button>
            <b>${it.width}</b>
            <button type="button" class="btn btn-sm btn-outline-dark" data-move="wider" aria-label="Wider" ${it.width >= 12 ? "disabled" : ""}>+</button></div>
          <button type="button" class="btn btn-sm btn-outline-danger align-self-start" data-move="off">Take it off this shelf</button>
          <div class="small muted">Its stock stays put: this only changes where the map shows it.</div>
        </div>`;
      return;
    }
    const loose = shelf(cur).loose.filter((x) => !x.out || true);
    const looseSel = sel != null && loose.find((x) => x.product === sel);
    const all = slides();
    const i = all.indexOf(cur);
    box.innerHTML = `<div class="card-header">Editing ${esc(slideName(cur))}</div>
      <div class="card-body d-flex flex-column gap-2">
        ${looseSel ? `<div class="edit-loose"><div class="fw-semibold">${esc(prod(sel).name)}</div><div class="small mb-1">Put it on level:</div>
          <div class="d-flex gap-1 flex-wrap">${Array.from({ length: draft.levels }, (_, k) => `<button type="button" class="btn btn-sm btn-outline-dark" data-put="${k + 1}">${k + 1}</button>`).join("")}</div></div>` : ""}
        <label class="form-label mb-0" for="ed-name">Name</label>
        <input id="ed-name" class="form-control form-control-sm" value="${esc(draft.name)}">
        <div class="d-flex gap-2">
          <div class="flex-grow-1"><label class="form-label mb-0" for="ed-kind">What it is</label>
            <select id="ed-kind" class="form-select form-select-sm">${M.kinds.map((k) => `<option value="${k}" ${k === draft.kind ? "selected" : ""}>${cap(k)}</option>`).join("")}</select></div>
          <div><span class="form-label d-block mb-0">Levels</span>
            <div class="d-flex gap-1 align-items-center">
              <button type="button" class="btn btn-sm btn-outline-dark" data-levels="-1" aria-label="One level fewer" ${draft.levels <= 1 ? "disabled" : ""}>−</button>
              <b class="px-1">${draft.levels}</b>
              <button type="button" class="btn btn-sm btn-outline-dark" data-levels="1" aria-label="One more level" ${draft.levels >= 12 ? "disabled" : ""}>+</button></div></div>
        </div>
        <div class="small muted">Level 1 is the top. Fewer levels: what stood on the bottom ones moves up onto the last.</div>
        <div class="d-flex gap-1 flex-wrap mt-1">
          <button type="button" class="btn btn-sm btn-outline-dark" data-order="-1" ${i <= 0 ? "disabled" : ""}><i class="bi bi-chevron-left" aria-hidden="true"></i> Earlier in the walk</button>
          <button type="button" class="btn btn-sm btn-outline-dark" data-order="1" ${i >= all.length - 1 || all[i + 1] === "none" ? "disabled" : ""}>Later <i class="bi bi-chevron-right" aria-hidden="true"></i></button>
        </div>
        <button type="button" class="btn btn-sm btn-outline-danger align-self-start mt-2" data-act="remove-place">Remove this shelf</button>
        <div class="small muted">Removing it keeps its stock, with no place, until you put it somewhere.</div>
      </div>`;
  }

  function pickerPanel(box) {
    const onShelf = new Set(draft.items.map((it) => it.product));
    box.innerHTML = `<div class="card-header d-flex justify-content-between align-items-center">Add to level ${adding}
        <button type="button" class="btn-close" data-act="close" aria-label="Stop adding"></button></div>
      <div class="card-body pb-2"><input type="search" id="pick-q" class="form-control" placeholder="Find a product" autocomplete="off" aria-label="Find a product"></div>
      <ul class="list-group list-group-flush pick-list" id="pick-list"></ul>`;
    const list = () => {
      const q = ($("pick-q").value || "").trim().toLowerCase();
      const ids = Object.keys(M.products).map(Number)
        .filter((id) => !q || [M.products[id].name, M.products[id].name_km, M.products[id].sku].some((t) => t && t.toLowerCase().includes(q)))
        .sort((a, b) => (onShelf.has(a) - onShelf.has(b)) || ((M.products[b].home === cur) - (M.products[a].home === cur)) || M.products[a].name.localeCompare(M.products[b].name))
        .slice(0, 60);
      $("pick-list").innerHTML = ids.map((id) => `<li class="list-group-item map-pick" data-add-product="${id}" role="button" tabindex="0">
          ${thumb(id)}<span class="flex-grow-1">${esc(M.products[id].name)}</span>${onShelf.has(id) ? '<span class="small muted">on this shelf</span>' : ""}</li>`).join("")
        || '<li class="list-group-item small muted">No product by that name. New products come in on Products, Stock in or a delivery.</li>';
    };
    $("pick-q").addEventListener("input", list);
    list();
    $("pick-q").focus({ preventScroll: true });
  }

  // --- legend and controls ---------------------------------------------------------------------------------
  function legend() {
    const L = {
      expiry: [["t-bad", T("Expired (the date on the pack)")], ["t-warn", T("Expires within {n} days, or won't sell in time", { n: M.warning_days })],
               ["t-ok", T("Fine")], ["t-out", T("Out of stock: a gap")]],
      dead: [["t-d4", T("Most money sleeping")], ["t-d2", T("Some")], ["t-d0", T("Selling")], ["t-out", T("Out of stock")]],
      stock: [["t-stock", T("In stock: how many left")], ["t-out", T("Out of stock: a gap")], ["t-empty", T("Not counted yet")]],
    }[layer];
    const note = layer === "dead" ? T("Sleeping: no sale in {n} days, or months of stock at today's pace; valued at cost.", { n: M.sleeping_days })
      : T("×12: units on this shelf. The date is the first one to expire.");
    $("legend").innerHTML = L.map(([cls, text]) => `<span class="leg"><span class="swatch ${cls}"></span>${esc(text)}</span>`).join("")
      + `<span class="small muted">${note}</span>`;
  }

  function controls() {
    document.querySelectorAll("#layers [data-layer]").forEach((b) => {
      const on = b.dataset.layer === layer;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on);
    });
    const counts = {
      all: null,
      expired: M.batches.filter((b) => b.status.includes("expired")).length,
      expiring: M.batches.filter((b) => b.status.includes("expiring")).length,
      clear: M.batches.filter((b) => b.status.includes("clear")).length,
      sleeping: money(M.batches.filter((b) => b.status.includes("sleeping")).reduce((s, b) => s + b.value, 0)),
      out: M.out.length,
      none: M.batches.filter((b) => b.place == null).length,
    };
    $("filters").innerHTML = FILTERS.filter(([k]) => k === "all" || (counts[k] && counts[k] !== "$0.00") || k === filter)
      .map(([k, label]) => `<button type="button" class="chip ${k === filter ? "active" : ""}" data-filter="${k}"
          aria-pressed="${k === filter}">${esc(label)}${counts[k] != null ? ` <b>${counts[k]}</b>` : ""}</button>`).join("");
    $("btn-edit").setAttribute("aria-pressed", mode === "edit");
    $("btn-edit").disabled = cur === "none" || !slides().length;
    $("bar").classList.toggle("d-none", mode === "edit");
    $("edit-bar").classList.toggle("d-none", mode !== "edit");
  }

  const listeners = [];   // the 3D view (shop3d.js), told whenever the data, the search or a filter change
  function render() {
    const all = slides();
    if (!all.includes(cur)) cur = all.length ? all[0] : null;
    controls(); walkCard(); drawHead(); drawStrip(); drawStage(); panel(); legend(); remember();
    for (const fn of listeners) fn();
  }
  function remember() {
    const u = new URL(location.href);
    if (query) u.searchParams.set("q", query); else u.searchParams.delete("q");
    if (filter !== "all") u.searchParams.set("show", filter); else u.searchParams.delete("show");
    if (cur != null && cur !== "none") u.searchParams.set("shelf", cur); else u.searchParams.delete("shelf");
    history.replaceState(null, "", u);
    if (cur != null) store.set("luydek-map-shelf", String(cur));
  }

  // --- going from shelf to shelf ------------------------------------------------------------------------------
  function go(id, way = "") {
    if (id == null || id === cur) return;
    const all = slides();
    enter = way || (all.indexOf(id) > all.indexOf(cur) ? "from-right" : "from-left");
    cur = id;
    sel = null;
    render();
  }
  function step(d) {
    if (mode === "edit") return;
    const all = slides();
    const i = all.indexOf(cur) + d;
    if (i >= 0 && i < all.length) go(all[i], d > 0 ? "from-right" : "from-left");
  }
  function firstWithHits() {
    if (!looking()) return;
    const all = slides();
    if (cur != null && hitsOn(cur)) return;
    const id = all.find((x) => hitsOn(x));
    if (id != null) { enter = "from-right"; cur = id; }
  }
  $("prev").onclick = () => step(-1);
  $("next").onclick = () => step(1);
  $("strip").addEventListener("click", (e) => {
    const b = e.target.closest("[data-slide]");
    if (b) go(b.dataset.slide === "none" ? "none" : +b.dataset.slide);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea, dialog[open]") || e.altKey || e.ctrlKey || e.metaKey) return;
    if (document.body.dataset.view === "3d") return;
    if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
    if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
    if (e.key === "Escape" && (sel != null || adding != null)) { sel = null; adding = null; render(); }
  });
  // A swipe across the shelf goes to the next one, unless the shelf is wider than the screen and still scrolls that way.
  let swipe = null;
  $("stage").addEventListener("pointerdown", (e) => {
    if (mode !== "edit" && e.pointerType !== "mouse") swipe = { x: e.clientX, y: e.clientY, scroll: $("stage").scrollLeft };
  });
  $("stage").addEventListener("pointerup", (e) => {
    if (!swipe) return;
    const dx = e.clientX - swipe.x, dy = e.clientY - swipe.y, st = $("stage");
    const scrolled = Math.abs(st.scrollLeft - swipe.scroll) > 4;
    swipe = null;
    if (scrolled || Math.abs(dx) < 60 || Math.abs(dy) > 45) return;
    const atEnd = dx < 0 ? st.scrollLeft + st.clientWidth >= st.scrollWidth - 2 : st.scrollLeft <= 2;
    if (atEnd) step(dx < 0 ? 1 : -1);
  });
  $("stage").addEventListener("pointercancel", () => { swipe = null; });

  // --- tapping a spot ---------------------------------------------------------------------------------------
  $("stage").addEventListener("click", (e) => {
    if (dragged) { dragged = false; return; }
    const add = e.target.closest("[data-add]");
    if (add) { adding = +add.dataset.add; sel = null; render(); return; }
    const s = e.target.closest(".spot");
    if (!s) return;
    const pid = +s.dataset.product;
    sel = sel === pid && mode !== "edit" ? null : pid;
    adding = null;
    render();
    if (sel != null && mode === "edit" && phone()) $("panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // --- editing a shelf ---------------------------------------------------------------------------------------
  function startEdit() {
    const p = place(cur);
    if (!p) return;
    mode = "edit";
    draft = { id: p.id, name: p.name, kind: p.kind, levels: p.levels, items: p.facings.map((f) => ({ ...f })) };
    sel = null; adding = null;
    render();
  }
  function stopEdit() { mode = "view"; draft = null; sel = null; adding = null; render(); }
  function levelOrder() {   // keep the items level by level, in their order on each level
    draft.items = draft.items.map((it, k) => ({ it, k })).sort((a, b) => a.it.level - b.it.level || a.k - b.k).map((x) => x.it);
  }
  function moveTo(item, level, before) {   // put `item` on `level`, before the item `before` (or at the end)
    draft.items.splice(draft.items.indexOf(item), 1);
    item.level = level;
    let at = before ? draft.items.indexOf(before) : -1;
    if (at < 0) {
      at = draft.items.length;
      for (let k = draft.items.length - 1; k >= 0; k--) {
        if (draft.items[k].level <= level) { at = k + 1; break; }
        at = k;
      }
    }
    draft.items.splice(at, 0, item);
    levelOrder();
  }
  $("btn-edit").onclick = () => (mode === "edit" ? stopEdit() : startEdit());
  $("edit-cancel").onclick = stopEdit;
  $("edit-save").onclick = async () => {
    try {
      await call(`/api/map/places/${draft.id}/shelf`, { name: ($("ed-name") || {}).value || draft.name, kind: draft.kind,
        levels: draft.levels, items: draft.items.map(({ product, level, width }) => ({ product, level, width })) });
      stopEdit();
    } catch (err) { alert(err.message); }
  };

  // Dragging a product along a level, or to another level (edit mode).
  let drag = null, dragged = false;
  $("stage").addEventListener("pointerdown", (e) => {
    if (mode !== "edit" || !draft) return;
    const s = e.target.closest(".spot");
    if (!s) return;
    drag = { el: s, pid: +s.dataset.product, x: e.clientX, y: e.clientY, ghost: null, target: null };
  });
  window.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (!drag.ghost) {
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 8) return;
      const r = drag.el.getBoundingClientRect();
      drag.ghost = drag.el.cloneNode(true);
      drag.ghost.classList.add("ghost");
      drag.ghost.style.width = `${r.width}px`;
      drag.ghost.style.height = `${r.height}px`;
      document.body.append(drag.ghost);
      drag.el.classList.add("lifted");
      drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
    }
    e.preventDefault();
    drag.ghost.style.transform = `translate(${e.clientX - drag.dx}px, ${e.clientY - drag.dy}px)`;
    document.querySelectorAll(".drop-before, .drop-end").forEach((n) => n.classList.remove("drop-before", "drop-end"));
    drag.ghost.style.display = "none";
    const under = document.elementFromPoint(e.clientX, e.clientY);
    drag.ghost.style.display = "";
    const rowEl = under && under.closest(".level-row");
    if (!rowEl) { drag.target = null; return; }
    const spots = [...rowEl.querySelectorAll(".spot:not(.lifted)")];
    const before = spots.find((sp) => { const r = sp.getBoundingClientRect(); return e.clientX < r.left + r.width / 2; });
    drag.target = { level: +rowEl.dataset.level, before: before ? +before.dataset.product : null };
    if (before) before.classList.add("drop-before"); else rowEl.classList.add("drop-end");
  }, { passive: false });
  window.addEventListener("pointerup", () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (!d.ghost) return;
    d.ghost.remove();
    dragged = true;
    setTimeout(() => { dragged = false; }, 0);
    if (!d.target) { render(); return; }
    let item = draft.items.find((it) => it.product === d.pid);
    if (!item) { item = { product: d.pid, level: d.target.level, width: 1 }; draft.items.push(item); }   // from "not on a level"
    moveTo(item, d.target.level, d.target.before != null ? draft.items.find((it) => it.product === d.target.before) : null);
    sel = d.pid;
    render();
  });

  // --- talking to the server --------------------------------------------------------------------------------------
  async function call(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : r.statusText);
    M = data;
    return data;
  }
  async function onPanelClick(e) {
    const t = e.target.closest("[data-act], [data-goto], [data-pick], [data-move], [data-levels], [data-order], [data-put], [data-add-product]");
    if (!t) return;
    e.preventDefault();
    try {
      const act = t.dataset.act;
      if (act === "close") { sel = null; adding = null; render(); return; }
      if (act === "off") {
        const li = t.closest("[data-batch]"), b = M.batches.find((x) => x.batch === +li.dataset.batch);
        const qty = prompt(`${T("How many were taken off the shelf?")} (${b.name})`, b.left);
        if (qty == null) return;
        await call(`/api/map/batches/${b.batch}/remove`, { qty: parseInt(qty, 10) || b.left,
                                                           reason: b.status.includes("expired") ? "expired" : "damaged" });
        render();
        return;
      }
      if (act === "remove-place") {
        if (!confirm(`Remove ${draft.name}? Its stock is kept, with no place.`)) return;
        await call(`/api/map/places/${draft.id}/remove`);
        stopEdit();
        return;
      }
      if (t.dataset.goto != null) {
        const id = t.dataset.goto === "none" ? "none" : +t.dataset.goto;
        if (id !== cur) go(id);
        if (t.dataset.pick) { sel = +t.dataset.pick; render(); }
        return;
      }
      if (t.dataset.pick) { sel = +t.dataset.pick; render(); return; }
      if (t.dataset.addProduct) {
        const pid = +t.dataset.addProduct;
        const have = draft.items.find((it) => it.product === pid);
        if (have) moveTo(have, adding, null);
        else { draft.items.push({ product: pid, level: adding, width: 1 }); levelOrder(); }
        sel = pid; adding = null; render();
        return;
      }
      if (t.dataset.put) {
        draft.items.push({ product: sel, level: +t.dataset.put, width: 1 });
        levelOrder(); render();
        return;
      }
      if (t.dataset.levels) {
        draft.name = $("ed-name").value || draft.name;
        draft.levels = Math.max(1, Math.min(12, draft.levels + +t.dataset.levels));
        for (const it of draft.items) it.level = Math.min(it.level, draft.levels);
        levelOrder(); render();
        return;
      }
      if (t.dataset.order) {
        const ids = M.places.map((p) => p.id);
        const i = ids.indexOf(draft.id), j = i + +t.dataset.order;
        if (j < 0 || j >= ids.length) return;
        [ids[i], ids[j]] = [ids[j], ids[i]];
        await call("/api/map/order", { ids });
        render();
        return;
      }
      if (t.dataset.move) {
        const item = draft.items.find((it) => it.product === sel);
        if (!item) return;
        const same = draft.items.filter((x) => x.level === item.level);
        const at = same.indexOf(item);
        const m = t.dataset.move;
        if (m === "up" || m === "down") moveTo(item, item.level + (m === "up" ? -1 : 1), null);
        else if (m === "left" && at > 0) moveTo(item, item.level, same[at - 1]);
        else if (m === "right" && at < same.length - 1) moveTo(item, item.level, same[at + 2] || null);
        else if (m === "wider") item.width = Math.min(12, item.width + 1);
        else if (m === "narrower") item.width = Math.max(1, item.width - 1);
        else if (m === "off") { draft.items.splice(draft.items.indexOf(item), 1); sel = null; }
        render();
      }
    } catch (err) { alert(err.message); }
  }
  async function onPanelChange(e) {
    const t = e.target;
    try {
      if (t.dataset.act === "move") {
        const li = t.closest("[data-batch]");
        await call(`/api/map/batches/${li.dataset.batch}/move`, { location_id: t.value ? +t.value : null });
        render();
      } else if (t.id === "ed-kind") {
        draft.kind = t.value; drawHead(); drawStage();
      } else if (t.id === "ed-name") {
        draft.name = t.value; drawHead();
      }
    } catch (err) { alert(err.message); }
  }
  for (const box of [$("panel"), $("spot-body")].filter(Boolean)) {
    box.addEventListener("click", onPanelClick);
    box.addEventListener("change", onPanelChange);
  }
  // Closing the sheet (Close, Escape, a tap outside) lets go of the product.
  const sheet = $("spot-sheet");
  if (sheet) {
    sheet.addEventListener("close", () => { if (sel != null && mode !== "edit") { sel = null; render(); } });
    sheet.addEventListener("click", (e) => {
      if (e.target !== sheet) return;
      const r = sheet.getBoundingClientRect();
      if (e.clientY < r.top || e.clientY > r.bottom || e.clientX < r.left || e.clientX > r.right) sheet.close();
    });
  }
  $("walk-card").addEventListener("click", (e) => {
    const b = e.target.closest("[data-walk]");
    if (!b) return;
    if (b.dataset.walk === "start") { mode = "walk"; sel = null; if (M.walk.length) cur = M.walk[0].place ?? "none"; render(); }
    else if (b.dataset.walk === "next") step(1);
    else { mode = "view"; sel = null; render(); }
  });
  $("panel").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[role=button]")) { e.preventDefault(); e.target.click(); }
  });

  // --- toolbar --------------------------------------------------------------------------------------------------
  let typing = null;
  $("q").addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => { query = $("q").value.trim(); sel = null; firstWithHits(); render(); }, 150);
  });
  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    filter = b.dataset.filter === filter ? "all" : b.dataset.filter;
    sel = null;
    if (filter === "none") cur = "none";
    firstWithHits(); render();
  });
  $("layers").addEventListener("click", (e) => {
    const b = e.target.closest("[data-layer]");
    if (!b) return;
    layer = b.dataset.layer;
    store.set("luydek-map-layer", layer);
    render();
  });

  // For the new-shelf maker (shelfmaker.js) and the 3D view (shop3d.js): the data, the shelves as this page
  // sees them, the search, and ways to show something.
  window.LuyMap = {
    data: () => M,
    current: () => cur,
    shelf: (id) => shelf(id),
    isHit: (s) => isHit(s),
    looking: () => looking(),
    firstDate: (s) => firstDate(s),
    onChange(fn) { listeners.push(fn); },
    set(data) { M = data; render(); },
    open(id, product = null) {      // a shelf face on, with a product picked
      if (mode !== "view") { mode = "view"; draft = null; }
      enter = "from-right"; cur = id; sel = product; adding = null;
      render();
    },
    update(data, { goTo = null, edit = false } = {}) {
      M = data;
      if (mode === "edit") { mode = "view"; draft = null; }
      if (goTo != null) { enter = "from-right"; cur = goTo; sel = null; }
      if (edit) startEdit(); else render();
    },
  };

  const all = slides();
  const want = boot.shelf != null ? boot.shelf : +store.get("luydek-map-shelf");
  cur = all.includes(want) ? want : all.length ? all[0] : null;
  firstWithHits();
  render();
})();
