// Stock in (/stock-in): scan what is on the mat, name what the camera doesn't know, read the dates,
// check the numbers, and save it all at once as a delivery or as a count of the shelf.
//
// Every scan adds to the list: one line per product, its count, and its photos. Things the camera
// doesn't know come grouped by look, so ten of something new are one question; naming them teaches
// the camera at once. A line's units can be split by expiry date: each date becomes its own batch.
(() => {
  const DATA = JSON.parse(document.getElementById("stockin-data").textContent);
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LOOKS = { phone: "a phone", cash: "cash", calculator: "a calculator", plastic_bag: "a bag", hand: "a hand",
                  other: "clutter", background: "part of the mat" };
  const packs = new Map(DATA.packs.map((p) => [p.pack_id, p]));
  const video = $("video"), stage = $("stage"), overlay = $("overlay"), status = $("status"), snap = $("snap");
  const cam = new LuyCam(video, stage);

  let rows = [];          // {key, pack_id, label, sure, looks, candidates, crops, location_id, total, groups, teach, naming}
  const scans = [];       // per scan: {key: [items added]} so a scan can be undone
  const allCrops = [];    // every photo the scans made: removed from the counter photos once saved
  let liveCamera = false, busy = false;

  // --- talking to the server ------------------------------------------------------------------------
  async function call(url, opts) {
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : r.statusText);
    return body;
  }
  const postJson = (url, data) => call(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify(data) });

  // --- scanning ------------------------------------------------------------------------------------
  async function scan(blob, useRef) {
    const f = new FormData();
    f.append("image", blob, "mat.jpg");
    f.append("use_reference", useRef ? "true" : "false");
    const res = await call("/api/stockin/scan", { method: "POST", body: f });
    const added = {};
    for (const it of res.items) {
      allCrops.push(it.crop);
      let row = rows.find((r) => r.key === it.group);
      if (!row) {
        // Teach it by default while it has few photos: these are taken where it will be sold.
        row = { key: it.group, pack_id: it.pack_id, label: it.label, sure: true, looks: it.looks_like,
                candidates: it.candidates, crops: [], location_id: it.location_id, total: 0,
                groups: [{ expiry: "", lot: "" }], teach: !!it.pack_id && (packs.get(it.pack_id)?.photos || 0) < 10 };
        rows.push(row);
      }
      row.total++;
      if (!it.sure && row.sure && row.pack_id) row.teach = true;  // it wasn't sure: these photos help most
      row.sure = row.sure && it.sure;
      row.crops.push({ crop: it.crop, url: it.url });
      (added[it.group] = added[it.group] || []).push(it.crop);
    }
    scans.push(added);
    drawBoxes(res);
    render();
    return res;
  }

  function undoScan() {
    const last = scans.pop();
    if (!last) return;
    for (const [key, crops] of Object.entries(last)) {
      const row = rows.find((r) => r.key === key);
      if (!row) continue;
      row.total = Math.max(0, row.total - crops.length);
      row.crops = row.crops.filter((c) => !crops.includes(c.crop));
    }
    rows = rows.filter((r) => r.total > 0);
    status.textContent = "Took the last scan off the list.";
    render();
  }

  // Boxes on the viewfinder for a moment: green when sure, amber to check, red when unknown.
  let boxTimer = null;
  function drawBoxes(res) {
    const rect = overlay.getBoundingClientRect();
    overlay.width = rect.width; overlay.height = rect.height;
    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (!liveCamera) return;
    const s = Math.min(overlay.width / res.width, overlay.height / res.height);
    const ox = (overlay.width - res.width * s) / 2, oy = (overlay.height - res.height * s) / 2;
    ctx.lineWidth = 3;
    ctx.font = "600 13px system-ui, sans-serif";
    for (const it of res.items) {
      const [x, y, w, h] = it.bbox;
      const colour = !it.pack_id ? "#E24B4A" : it.sure ? "#3B9C4B" : "#EF9F27";
      ctx.strokeStyle = colour;
      ctx.strokeRect(ox + x * s, oy + y * s, w * s, h * s);
      const text = it.label || "?";
      const tw = ctx.measureText(text).width + 10;
      ctx.fillStyle = colour;
      ctx.fillRect(ox + x * s, Math.max(0, oy + y * s - 20), tw, 20);
      ctx.fillStyle = "#fff";
      ctx.fillText(text, ox + x * s + 5, Math.max(14, oy + y * s - 6));
    }
    clearTimeout(boxTimer);
    boxTimer = setTimeout(() => ctx.clearRect(0, 0, overlay.width, overlay.height), 3000);
  }

  async function scanNow() {
    if (busy || !video.videoWidth) return;
    busy = true; snap.disabled = true;
    stage.classList.remove("blink"); void stage.offsetWidth; stage.classList.add("blink");
    status.textContent = "Looking…";
    try {
      const res = await scan(await cam.grab(1920, 0.92), true);
      status.textContent = saidAboutScan(res);
    } catch (e) { status.textContent = "That scan didn't work: " + e.message; }
    finally { busy = false; snap.disabled = false; }
  }

  function saidAboutScan(res) {
    if (!res.items.length) return "Nothing found on the mat. Put the items a little apart, inside the mat, and scan again.";
    const unknown = res.items.filter((i) => !i.pack_id).length;
    const said = [`✓ ${res.items.length} item${res.items.length === 1 ? "" : "s"} counted.`];
    if (unknown) said.push(`${unknown} the camera doesn't know yet: say what ${unknown === 1 ? "it is" : "they are"} on the right.`);
    if (res.own_background) said.push("Not the counter camera's view: items were found against their own background.");
    return said.join(" ");
  }

  // --- naming what the camera doesn't know --------------------------------------------------------
  async function nameRow(row, how) {
    row.naming = true; render();
    try {
      const res = await postJson("/api/stockin/name", { crops: row.crops.map((c) => c.crop), ...how });
      packs.set(res.pack_id, { pack_id: res.pack_id, label: res.label, photos: (packs.get(res.pack_id)?.photos || 0) + res.taught });
      const same = rows.find((r) => r !== row && r.pack_id === res.pack_id);
      if (same) {                         // already on the list: one line for it
        same.total += row.total;
        same.crops.push(...row.crops);
        rows = rows.filter((r) => r !== row);
      } else {
        Object.assign(row, { key: `p${res.pack_id}`, pack_id: res.pack_id, label: res.label, sure: true, looks: null,
                             location_id: row.location_id || res.location_id, teach: false });
      }
      status.textContent = res.created ? `Added ${res.label} to your products. The camera knows it now.`
                                       : `✓ ${res.label}. The camera learned ${res.taught} more photo${res.taught === 1 ? "" : "s"} of it.`;
    } catch (e) { status.textContent = "Couldn't name it: " + e.message; }
    finally { row.naming = false; render(); }
  }

  async function readName(row) {
    row.naming = true; render();
    try {
      const f = new FormData();
      f.append("crop", row.crops[0].crop);
      const res = await call("/api/stockin/read-name", { method: "POST", body: f });
      if (res.known) { row.naming = false; return nameRow(row, { pack_id: res.known.pack_id }); }
      row.suggested = res.sure ? res.name_en : "";
      row.suggested_km = res.sure ? res.name_km : "";
      status.textContent = res.sure ? `It reads “${res.name_en}”. Check it and press Add.` : "Couldn't read a name on it: type it instead.";
    } catch (e) { status.textContent = e.message; }
    row.naming = false; render();
  }

  // --- reading a date --------------------------------------------------------------------------------
  let dateTarget = null;
  async function readDate(row, gi) {
    dateTarget = { row, gi };
    if (!liveCamera) { $("date-file").click(); return; }
    status.textContent = "Reading the date…";
    stage.classList.remove("blink"); void stage.offsetWidth; stage.classList.add("blink");
    await sendDate(await cam.grab(1920, 0.95));
  }
  async function sendDate(blob) {
    const { row, gi } = dateTarget || {};
    if (!row) return;
    const f = new FormData();
    f.append("image", blob, "date.jpg");
    try {
      const res = await call("/api/stockin/expiry", { method: "POST", body: f });
      if (res.expiry && res.sure) {
        row.groups[gi].expiry = res.expiry;
        if (res.lot) row.groups[gi].lot = res.lot;
        status.textContent = `✓ Expires ${res.expiry}${res.lot ? `, lot ${res.lot}` : ""} (read “${res.text_seen}”).`;
      } else {
        status.textContent = res.text_seen ? `Read “${res.text_seen}” but couldn't tell the expiry date: type it.`
                                           : "Couldn't read a date: hold the stamp closer, in good light, or type it.";
      }
    } catch (e) { status.textContent = e.message; }
    render();
  }
  $("date-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) { status.textContent = "Reading the date…"; await sendDate(file); }
  });

  // --- the list ----------------------------------------------------------------------------------------
  // A line's units: the first date takes whatever the others don't.
  const firstCount = (row) => Math.max(0, row.total - row.groups.slice(1).reduce((n, g) => n + (g.count || 0), 0));

  function placeSelect(row) {
    if (!DATA.places.length) return `<a class="small" href="/shelf#places">Name your shop's places</a>`;
    const opts = DATA.places.map((p) => `<option value="${p.id}" ${row.location_id === p.id ? "selected" : ""}>${esc(p.name)}</option>`);
    return `<select class="form-select form-select-sm" data-act="place" aria-label="Where it goes" style="max-width:11rem">
      <option value="">No place</option>${opts.join("")}</select>`;
  }

  function groupLines(row, ri) {
    return row.groups.map((g, gi) => `
      <div class="si-group" data-g="${gi}">
        ${gi === 0 ? `<span class="si-n" title="units with this date">${firstCount(row)}</span>`
                   : `<input type="number" min="0" class="form-control form-control-sm" data-act="gcount" value="${g.count || 0}" aria-label="How many have this date">`}
        <input type="date" id="exp-${ri}-${gi}" class="form-control form-control-sm" data-act="expiry" value="${esc(g.expiry)}" aria-label="Expiry date">
        <button type="button" class="btn btn-sm btn-outline-secondary" data-act="read-date" title="Read the date from the camera" aria-label="Read the date from the camera"><i class="bi bi-calendar-event" aria-hidden="true"></i></button>
        <input type="text" class="form-control form-control-sm" placeholder="lot" data-act="lot" value="${esc(g.lot)}" aria-label="Lot number">
        ${gi > 0 ? `<button type="button" class="btn btn-sm btn-link text-danger p-0" data-act="drop-group" aria-label="Remove this date"><i class="bi bi-x-lg" aria-hidden="true"></i></button>` : "<span></span>"}
      </div>`).join("");
  }

  function unknownBody(row) {
    const looks = row.looks && LOOKS[row.looks] ? `<div class="small muted">Looked like ${LOOKS[row.looks]}. Not stock? Remove the line.</div>` : "";
    const chips = row.candidates.map((c) => `<button type="button" class="btn btn-sm btn-outline-dark" data-act="pick" data-pack="${c.pack_id}">${esc(c.label)}?</button>`).join("");
    return `<div class="si-details">${looks}
      ${chips ? `<div class="si-choices mt-2">${chips}</div>` : ""}
      <div class="d-flex gap-2 mt-2 flex-wrap">
        <input type="text" class="form-control form-control-sm" style="max-width:16rem" list="pack-names" data-act="name"
               placeholder="What is it? e.g. Fanta can 330ml" value="${esc(row.suggested || "")}" aria-label="What is it?">
        <button type="button" class="btn btn-sm btn-primary" data-act="add-name" ${row.naming ? "disabled" : ""}>Add</button>
        ${DATA.can_read ? `<button type="button" class="btn btn-sm btn-outline-dark" data-act="read-name" ${row.naming ? "disabled" : ""}><i class="bi bi-eye" aria-hidden="true"></i> Read the name</button>` : ""}
      </div></div>`;
  }

  function render() {
    const list = $("rows");
    list.innerHTML = rows.map((row, ri) => {
      const thumbs = row.crops.slice(0, 3).map((c) => `<img src="${esc(c.url)}" alt="">`).join("");
      const head = row.pack_id
        ? `<div class="fw-semibold">${esc(row.label)} ${row.sure ? "" : `<span class="pill new-from">check it</span>`}</div>
           <select class="form-select form-select-sm mt-1 d-none" data-act="change" aria-label="It is">
             ${[...packs.values()].sort((a, b) => a.label.localeCompare(b.label))
               .map((p) => `<option value="${p.pack_id}" ${p.pack_id === row.pack_id ? "selected" : ""}>${esc(p.label)}</option>`).join("")}
           </select>`
        : `<div class="fw-semibold text-danger">Not known yet <span class="small muted fw-normal">×${row.total}</span></div>`;
      return `<li class="list-group-item si-row ${row.pack_id ? "" : "si-unknown"}" data-r="${ri}">
        <div class="d-flex gap-3 align-items-start">
          <div class="si-thumbs">${thumbs}</div>
          <div class="flex-grow-1" style="min-width:0">
            ${head}
            ${row.pack_id ? `<button type="button" class="btn btn-link btn-sm p-0" data-act="show-change">not this?</button>`
                          : `<div class="small muted">Is it one of these, or something new?</div>`}
          </div>
          <div class="si-qty text-nowrap">
            <button type="button" class="btn btn-sm btn-outline-secondary" data-act="minus" aria-label="One fewer">−</button>
            <b class="mx-1">${row.total}</b>
            <button type="button" class="btn btn-sm btn-outline-secondary" data-act="plus" aria-label="One more">+</button>
          </div>
          <button type="button" class="btn btn-sm btn-link text-danger p-0" data-act="remove" aria-label="Remove this line"><i class="bi bi-x-lg" aria-hidden="true"></i></button>
        </div>
        ${row.pack_id ? "" : unknownBody(row)}
        ${row.pack_id ? `<div class="si-details">
          ${groupLines(row, ri)}
          <div class="d-flex gap-3 align-items-center flex-wrap mt-2">
            <button type="button" class="btn btn-link btn-sm p-0" data-act="add-group"><i class="bi bi-plus" aria-hidden="true"></i> another date</button>
            ${placeSelect(row)}
            ${row.crops.length ? `<div class="form-check mb-0"><input class="form-check-input" type="checkbox" id="teach-${ri}" data-act="teach" ${row.teach ? "checked" : ""}>
              <label class="form-check-label small" for="teach-${ri}">learn its look from these photos</label></div>` : ""}
          </div></div>` : ""}
      </li>`;
    }).join("");
    const items = rows.reduce((n, r) => n + r.total, 0);
    const unknown = rows.filter((r) => !r.pack_id).length;
    $("n-items").textContent = items;
    $("n-scans").textContent = scans.length ? `${scans.length} scan${scans.length === 1 ? "" : "s"}` : "";
    $("rows-empty").classList.toggle("d-none", rows.length > 0);
    $("btn-undo").disabled = !scans.length;
    $("btn-save").disabled = !rows.length || unknown > 0;
    $("save-note").textContent = unknown ? `Say what the ${unknown === 1 ? "unknown line is" : `${unknown} unknown lines are`} first (or remove ${unknown === 1 ? "it" : "them"}).`
      : rows.length ? `${rows.length} product${rows.length === 1 ? "" : "s"}, ${items} item${items === 1 ? "" : "s"}${DATA.mode === "count" ? ": this replaces what LuyDek had for them." : "."}` : "";
  }

  // One listener for the whole list.
  function rowOf(el) { const li = el.closest("[data-r]"); return li ? rows[+li.dataset.r] : null; }
  function groupOf(el) { const g = el.closest("[data-g]"); return g ? +g.dataset.g : 0; }
  $("rows").addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const row = rowOf(el);
    if (!row) return;
    const act = el.dataset.act;
    if (act === "plus") row.total++;
    else if (act === "minus") row.total = Math.max(0, row.total - 1);
    else if (act === "remove") rows = rows.filter((r) => r !== row);
    else if (act === "add-group") row.groups.push({ count: 0, expiry: "", lot: "" });
    else if (act === "drop-group") row.groups.splice(groupOf(el), 1);
    else if (act === "read-date") return readDate(row, groupOf(el));
    else if (act === "pick") return nameRow(row, { pack_id: +el.dataset.pack });
    else if (act === "read-name") return readName(row);
    else if (act === "add-name") {
      const typed = el.closest("li").querySelector("[data-act=name]").value.trim();
      if (!typed) { status.textContent = "Type what it is first."; return; }
      const known = [...packs.values()].find((p) => p.label.toLowerCase() === typed.toLowerCase());
      return nameRow(row, known ? { pack_id: known.pack_id }
                                : { name: typed, name_km: typed === row.suggested ? row.suggested_km : "" });
    } else if (act === "show-change") { el.closest("li").querySelector("[data-act=change]").classList.remove("d-none"); el.remove(); return; }
    else return;
    if (row.total === 0 && act === "minus") rows = rows.filter((r) => r !== row);
    render();
  });
  $("rows").addEventListener("change", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const row = rowOf(el);
    if (!row) return;
    const act = el.dataset.act, gi = groupOf(el);
    if (act === "expiry") row.groups[gi].expiry = el.value;
    else if (act === "lot") row.groups[gi].lot = el.value.trim();
    else if (act === "gcount") { row.groups[gi].count = Math.max(0, parseInt(el.value, 10) || 0); render(); }
    else if (act === "place") row.location_id = el.value ? +el.value : null;
    else if (act === "teach") row.teach = el.checked;
    else if (act === "change") return nameRow(row, { pack_id: +el.value });  // also teaches it: the camera got it wrong
  });
  $("rows").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.dataset.act === "name") {
      e.preventDefault();
      e.target.closest("li").querySelector("[data-act=add-name]").click();
    }
  });

  // --- saving ------------------------------------------------------------------------------------------
  $("btn-save").addEventListener("click", async () => {
    const btn = $("btn-save");
    btn.disabled = true;
    const payload = {
      mode: DATA.mode,
      crops: allCrops,
      rows: rows.filter((r) => r.pack_id && r.total > 0).map((r) => ({
        pack_id: r.pack_id, location_id: r.location_id, teach: r.teach, crops: r.crops.map((c) => c.crop),
        groups: r.groups.map((g, gi) => ({ count: gi === 0 ? firstCount(r) : g.count || 0, expiry: g.expiry || "", lot: g.lot || "" })),
      })),
    };
    try {
      const res = await postJson("/api/stockin/save", payload);
      $("list-card").classList.add("d-none");
      $("done-card").classList.remove("d-none");
      $("done-title").textContent = DATA.mode === "count"
        ? `Counted ${res.units} unit${res.units === 1 ? "" : "s"} of ${res.products} product${res.products === 1 ? "" : "s"}.`
        : `Added ${res.units} unit${res.units === 1 ? "" : "s"} of ${res.products} product${res.products === 1 ? "" : "s"} to stock.`
          + (res.putaway.length ? " Where to put it:" : "");
      $("putaway").innerHTML = res.putaway.map((p) => {
        const exp = p.expiry_short ? ` (exp ${p.expiry_short})` : "";
        const where = p.place ? `<b>${esc(p.place)}</b>` : "its shelf";
        const how = p.how === "front" ? `, <b>in front of</b> the ${p.old_left} already there${p.old_expiry_short ? ` (exp ${p.old_expiry_short})` : ""}: this one expires sooner`
                  : p.how === "behind" ? `, behind the ${p.old_left} already there${p.old_expiry_short ? ` (exp ${p.old_expiry_short})` : ""}` : "";
        return `<li class="list-group-item"><i class="bi bi-arrow-right-circle" aria-hidden="true"></i> ${esc(p.name_en)} ×${p.qty}${exp} → ${where}${how}</li>`;
      }).join("") + (res.taught ? `<li class="list-group-item small muted">The camera learned ${res.taught} new photo${res.taught === 1 ? "" : "s"}.</li>` : "");
      status.textContent = "";
      rows = []; scans.length = 0; allCrops.length = 0;
    } catch (e) {
      status.textContent = "Not saved: " + e.message;
      btn.disabled = false;
    }
  });

  // --- the camera ----------------------------------------------------------------------------------------
  snap.addEventListener("click", scanNow);
  $("btn-undo").addEventListener("click", undoScan);
  document.addEventListener("keydown", (e) => {
    if (e.key === " " && e.target === document.body) { e.preventDefault(); scanNow(); }
  });
  $("scan-files").addEventListener("change", async (e) => {
    for (const file of e.target.files) {
      status.textContent = `Looking at ${file.name}…`;
      try { status.textContent = saidAboutScan(await scan(file, false)); }
      catch (err) { status.textContent = `${file.name}: ${err.message}`; }
    }
    e.target.value = "";
  });
  function showCamera() {
    const [w, h] = cam.resolution();
    $("cam-res").textContent = `${w}×${h}`;
    $("cam-res").hidden = !w;
    $("btn-flip").hidden = cam.devices.length < 2;
    $("btn-torch").hidden = !cam.canTorch();
    zoomUi.refresh();
  }
  const zoomUi = cam.controls({ surface: overlay });
  cam.onResize(() => { if (cam.track) showCamera(); });
  $("btn-flip").addEventListener("click", async () => {
    try { if (await cam.next()) showCamera(); } catch (e) { status.textContent = "That camera didn't open: " + e.message; }
  });
  $("btn-torch").addEventListener("click", async () => {
    try { await cam.setTorch(!cam.torch); } catch { status.textContent = "The flash can't be turned on here."; }
    $("btn-torch").setAttribute("aria-pressed", cam.torch);
  });
  const noCamera = (why) => {
    snap.disabled = true;
    status.textContent = why + " Use “Use photos instead” below.";
    $("no-camera").open = true;
  };
  if (LuyCam.available()) {
    cam.start().then(() => { liveCamera = true; showCamera(); })
      .catch((e) => noCamera(LuyCam.whyNot() || "No camera available here (" + e.message + ")."));
  } else noCamera(LuyCam.whyNot() || "This browser has no camera access.");
  render();
})();
