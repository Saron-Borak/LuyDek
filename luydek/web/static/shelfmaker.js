// New shelf (/map): from a photo of it, from the owner's spreadsheet, or empty.
//
// Photo: the camera finds each product and its level; every one it isn't sure of is asked about (the
// closest products, another product, a new one, or "not a product") before anything is saved. Price
// tags and shelf labels are set aside in view: a tap puts one back if it is a product after all.
// Spreadsheet: any .xlsx or .csv. The columns are guessed from their headings and can be changed; each
// row's product is matched to the list (known, a close guess to check, or a new product) and the owner
// can leave rows out. Saving puts the products on their shelves and, if asked, counts the stock.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
  };
  const dlg = $("maker");
  const M = () => window.LuyMap.data();
  const name = (id) => (M().products[id] || {}).name || "?";
  let tab = store.get("luydek-maker-tab") || "photo";

  async function post(url, body, form = false) {
    const r = await fetch(url, form ? { method: "POST", body } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : r.statusText);
    return data;
  }
  function idByName(text) {   // a product typed into the box: one of the list, by its name
    const t = (text || "").trim().toLowerCase();
    if (!t) return null;
    const hit = Object.entries(M().products).find(([, p]) => p.name.toLowerCase() === t || (p.name_km || "").toLowerCase() === t);
    return hit ? +hit[0] : null;
  }

  function show(which) {
    tab = which || tab;
    store.set("luydek-maker-tab", tab);
    dlg.querySelectorAll("[data-tab]").forEach((b) => { b.classList.toggle("on", b.dataset.tab === tab); b.setAttribute("aria-selected", b.dataset.tab === tab); });
    dlg.querySelectorAll("[data-pane]").forEach((s) => { s.hidden = s.dataset.pane !== tab; });
  }
  $("btn-new").onclick = () => {
    $("all-products").innerHTML = Object.values(M().products).map((p) => `<option value="${esc(p.name)}"></option>`).join("");
    const opts = M().places.map((p) => `<option value="${p.id}">${esc(p.name)} (replace what is drawn on it)</option>`).join("");
    $("ph-target").innerHTML = `<option value="">A new shelf</option>${opts}`;
    const here = window.LuyMap.current();
    if (typeof here === "number" && !(M().places.find((p) => p.id === here) || { facings: [1] }).facings.length) $("ph-target").value = here;
    targetChanged();
    show();
    dlg.showModal();
  };
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });   // a tap outside the box
  dlg.querySelector(".maker-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) show(b.dataset.tab); });

  // --- empty ------------------------------------------------------------------------------------------
  $("mk-kind").onchange = () => { $("mk-levels").value = M().default_levels[$("mk-kind").value] || 3; };
  $("mk-empty").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const data = await post("/api/map/places", { name: f.get("name"), kind: f.get("kind"), levels: +f.get("levels") || null });
      dlg.close();
      e.target.reset();
      window.LuyMap.update(data, { goTo: data.added, edit: true });
    } catch (err) { alert(err.message); }
  };

  // --- from a photo ---------------------------------------------------------------------------------------
  let photo = null;   // {levels: [[spot]], extra: empty levels added}
  function targetChanged() { $("ph-name-box").classList.toggle("d-none", !!$("ph-target").value); }
  $("ph-target").onchange = targetChanged;

  async function shrink(file, max = 2560) {   // a phone photo, small enough to send quickly, big enough for small items
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
      const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
      const c = document.createElement("canvas");
      c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
      c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
      return await new Promise((ok) => c.toBlob((b) => ok(b || file), "image/jpeg", 0.9));
    } catch { return file; }
  }
  $("ph-file").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (!$("ph-target").value && !$("ph-name").value.trim()) { $("ph-name").focus(); $("ph-status").textContent = "Give the new shelf a name first."; return; }
    $("ph-status").innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Finding the products on it… (the first photo takes longer)';
    try {
      const fd = new FormData();
      fd.append("image", await shrink(file), "shelf.jpg");
      const data = await post("/api/map/photo", fd, true);
      if (!data.found) { $("ph-status").textContent = "No products found on that photo. Try again closer, straight on, with the whole shelf in view."; return; }
      photo = { levels: data.levels.map((lv) => lv.map((s) => ({ ...s, choice: s.product != null ? String(s.product) : "", typed: "" }))),
                aside: data.aside || [], extra: 0 };
      if (!photo.levels.length) photo.levels.push([]);
      $("ph-status").textContent = "";
      reviewPhoto();
    } catch (err) { $("ph-status").textContent = err.message; }
  };

  function reviewPhoto() {
    const box = $("ph-review");
    const asks = photo.levels.flat().filter((s) => !s.sure).length;
    const n = photo.levels.length + photo.extra;
    const target = $("ph-target").value ? (M().places.find((p) => p.id === +$("ph-target").value) || {}).name : $("ph-name").value.trim();
    box.innerHTML = `<div class="d-flex flex-wrap gap-2 align-items-center mb-2">
        <div class="flex-grow-1"><b>${esc(target)}</b>: ${photo.levels.flat().length} spots on ${photo.levels.length} level${photo.levels.length === 1 ? "" : "s"}.
          ${asks ? `<span class="pill action-clear">${asks} to check</span>` : '<span class="pill action-front">all sure</span>'}</div>
        <span class="small">Levels</span>
        <button type="button" class="btn btn-sm btn-outline-dark" data-ph="fewer" aria-label="One level fewer" ${photo.extra <= 0 ? "disabled" : ""}>−</button><b>${n}</b>
        <button type="button" class="btn btn-sm btn-outline-dark" data-ph="more" aria-label="One more level">+</button>
      </div>
      <div class="small muted mb-2">Empty levels the camera can't see: add them with +. Each <span class="pill action-clear">?</span> needs an answer.</div>
      ${photo.levels.map((lv, li) => `<div class="rv-level"><div class="rv-no">Level ${li + 1}</div><div class="rv-row">
        ${lv.map((s, si) => spotCard(s, li, si)).join("") || '<span class="small muted">Nothing found on this level.</span>'}</div></div>`).join("")}
      ${photo.aside.length ? `<div class="rv-aside"><div class="small"><b>Set aside: ${photo.aside.length} price tag${photo.aside.length === 1 ? "" : "s"} or label${photo.aside.length === 1 ? "" : "s"}.</b>
          Tap one that is really a product to put it back on its level.</div>
        <div class="rv-aside-row">${photo.aside.map((a, i) => `<button type="button" class="rv-tag" data-back="${i}" aria-label="Put this back on level ${a.level + 1}: it is a product">
          <img src="${esc(a.crops[0])}" alt=""></button>`).join("")}</div></div>` : ""}
      <div class="d-flex gap-2 mt-3 flex-wrap">
        <button type="button" class="btn btn-outline-dark" data-ph="again">Another photo</button>
        <button type="button" class="btn btn-success ms-auto" data-ph="save">Save this shelf</button>
      </div>`;
    box.classList.remove("d-none");
    $("ph-start").classList.add("d-none");
  }
  function spotCard(s, li, si) {
    const known = [s.guess, ...s.offer].filter((x, i, a) => x != null && a.indexOf(x) === i);
    const ask = !s.choice;
    return `<div class="rv-spot${s.sure ? "" : " ask"}${ask ? " unanswered" : ""}" data-li="${li}" data-si="${si}">
      <div class="rv-img">${s.crops.slice(0, 1).map((c) => `<img src="${esc(c)}" alt="">`).join("")}${s.width > 1 ? `<span class="rv-n">×${s.width}</span>` : ""}
        ${s.sure ? "" : '<span class="rv-q" aria-label="Check this one">?</span>'}</div>
      <select class="form-select form-select-sm" data-choose aria-label="What is it?">
        <option value="" ${ask ? "selected" : ""} disabled>What is this?</option>
        ${known.map((id) => `<option value="${id}" ${s.choice === String(id) ? "selected" : ""}>${esc(name(id))}</option>`).join("")}
        <option value="other" ${s.choice === "other" ? "selected" : ""}>Another product…</option>
        <option value="new" ${s.choice === "new" ? "selected" : ""}>A new product…</option>
        <option value="skip" ${s.choice === "skip" ? "selected" : ""}>Not a product</option>
      </select>
      ${s.choice === "other" || s.choice === "new" ? `<input class="form-control form-control-sm mt-1" data-typed list="${s.choice === "other" ? "all-products" : ""}"
          placeholder="${s.choice === "other" ? "Type its name" : "Name of the new product"}" value="${esc(s.typed)}">` : ""}
      <div class="rv-w"><button type="button" data-w="-1" aria-label="Fewer side by side" ${s.width <= 1 ? "disabled" : ""}>−</button>
        <span>${s.width} wide</span><button type="button" data-w="1" aria-label="More side by side">+</button></div>
    </div>`;
  }
  $("ph-review").addEventListener("change", (e) => {
    const card = e.target.closest(".rv-spot");
    if (!card || !e.target.matches("[data-choose]")) return;
    const s = photo.levels[+card.dataset.li][+card.dataset.si];
    s.choice = e.target.value;
    reviewPhoto();
    const again = $("ph-review").querySelector(`.rv-spot[data-li="${card.dataset.li}"][data-si="${card.dataset.si}"] [data-typed]`);
    if (again) again.focus();
  });
  $("ph-review").addEventListener("input", (e) => {
    const card = e.target.closest(".rv-spot");
    if (card && e.target.matches("[data-typed]")) photo.levels[+card.dataset.li][+card.dataset.si].typed = e.target.value;
  });
  $("ph-review").addEventListener("click", async (e) => {
    const back = e.target.closest("[data-back]");
    if (back) {
      const a = photo.aside.splice(+back.dataset.back, 1)[0];
      photo.levels[Math.min(a.level, photo.levels.length - 1)].push({ product: null, guess: a.guess, sure: false, width: 1, offer: [],
        crops: a.crops, crop_paths: a.crop_paths, choice: "", typed: "" });
      reviewPhoto();
      return;
    }
    const w = e.target.closest("[data-w]");
    if (w) {
      const card = w.closest(".rv-spot");
      const s = photo.levels[+card.dataset.li][+card.dataset.si];
      s.width = Math.max(1, Math.min(12, s.width + +w.dataset.w));
      reviewPhoto();
      return;
    }
    const b = e.target.closest("[data-ph]");
    if (!b) return;
    const act = b.dataset.ph;
    if (act === "more") { photo.extra += 1; reviewPhoto(); return; }
    if (act === "fewer") { photo.extra = Math.max(0, photo.extra - 1); reviewPhoto(); return; }
    if (act === "again") { photo = null; $("ph-review").classList.add("d-none"); $("ph-start").classList.remove("d-none"); return; }
    if (act === "save") {
      const items = [];
      let missing = 0;
      photo.levels.forEach((lv, li) => lv.forEach((s) => {
        if (s.choice === "skip") return;
        let product = /^\d+$/.test(s.choice) ? +s.choice : null;
        let newName = null;
        if (s.choice === "other") { product = idByName(s.typed); if (product == null) newName = s.typed.trim() || null; }
        if (s.choice === "new") newName = s.typed.trim() || null;
        if (product == null && !newName) { missing += 1; return; }
        items.push({ product, new_name: newName, level: li + 1, width: s.width, crops: s.crop_paths });
      }));
      if (missing) {
        alert(`${missing} spot${missing === 1 ? " still needs" : "s still need"} an answer: pick what ${missing === 1 ? "it is" : "they are"}, or "Not a product".`);
        const first = $("ph-review").querySelector(".unanswered, [data-typed]");
        if (first) first.scrollIntoView({ block: "center" });
        return;
      }
      const levels = photo.levels.length + photo.extra;
      try {
        b.disabled = true;
        const id = $("ph-target").value ? +$("ph-target").value : null;
        const data = id ? await post(`/api/map/places/${id}/shelf`, { levels, items })
          : await post("/api/map/places", { name: $("ph-name").value.trim(), kind: "shelf", levels, items });
        dlg.close();
        photo = null;
        $("ph-review").classList.add("d-none");
        $("ph-start").classList.remove("d-none");
        window.LuyMap.update(data, { goTo: id || data.added });
      } catch (err) { alert(err.message); } finally { b.disabled = false; }
    }
  });

  // --- from a spreadsheet ---------------------------------------------------------------------------------------
  const FIELDS = [["product", "Product", true], ["shelf", "Shelf / place"], ["level", "Level (1 = top)"], ["position", "Position from the left"],
                  ["quantity", "Quantity"], ["expiry", "Expiry date"], ["lot", "Lot / batch"], ["barcode", "Barcode or code"], ["facings", "Facings (how many wide)"]];
  let sheet = null;   // {file, table, sheets, sheet, heading, mapping, rows, skipped, choice: {key: value}, forAll, newShelf, count}
  const keyOf = (r) => `${r.name.toLowerCase()}|${r.barcode}`;
  const letter = (i) => { let s = ""; i += 1; while (i > 0) { const m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = Math.floor((i - 1) / 26); } return s; };

  async function readSheet(file, which = "") {
    $("sh-status").innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Reading…';
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("sheet", which);
    const data = await post("/api/map/sheet/read", fd, true);
    sheet = { file, ...data, choice: {}, forAll: "", newShelf: "", count: data.mapping.quantity != null };
    $("sh-status").textContent = "";
    reviewSheet();
  }
  $("sh-file").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try { await readSheet(file); } catch (err) { $("sh-status").textContent = err.message; }
  };
  async function recheck() {
    const forAll = sheet.mapping.shelf != null ? "" : sheet.forAll === "new" ? sheet.newShelf : sheet.forAll;
    const data = await post("/api/map/sheet/check", { table: sheet.table, heading: sheet.heading, mapping: sheet.mapping, shelf_for_all: forAll });
    sheet.rows = data.rows; sheet.skipped = data.skipped;
    reviewSheet();
  }
  function choiceOf(r) {
    const k = keyOf(r);
    if (sheet.choice[k] != null) return sheet.choice[k];
    return r.product != null ? String(r.product) : "new";
  }

  function reviewSheet() {
    const box = $("sh-review");
    const heads = sheet.table[sheet.heading] || [];
    const width = Math.max(heads.length, ...sheet.table.slice(sheet.heading, sheet.heading + 30).map((r) => r.length));
    const colOpts = (f) => `<option value="">—</option>` + Array.from({ length: width }, (_, i) =>
      `<option value="${i}" ${sheet.mapping[f] === i ? "selected" : ""}>${letter(i)}${heads[i] !== "" && heads[i] != null ? `: ${esc(String(heads[i]).slice(0, 24))}` : ""}</option>`).join("");
    const rows = sheet.rows;
    const keep = rows.filter((r) => choiceOf(r) !== "skip");
    const known = keep.filter((r) => r.how === "barcode" || r.how === "name").length;
    const close = keep.filter((r) => r.how === "close" && choiceOf(r) === String(r.product)).length;
    const fresh = new Set(keep.filter((r) => choiceOf(r) === "new").map((r) => r.name.toLowerCase())).size;
    const lookalike = new Set(keep.filter((r) => choiceOf(r) === "new" && (r.offer || []).length).map((r) => r.name.toLowerCase())).size;
    const s_ = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    const problems = rows.filter((r) => r.problems.length).length;
    const shelves = new Set(keep.map((r) => r.shelf).filter(Boolean));
    const placeOpts = M().places.map((p) => `<option value="${esc(p.name)}" ${sheet.forAll === p.name ? "selected" : ""}>${esc(p.name)}</option>`).join("");
    const shown = rows.slice(0, 300);
    box.innerHTML = `
      ${sheet.sheets.length > 1 ? `<div class="mb-2"><label class="form-label mb-0" for="sh-sheet">Sheet</label>
        <select id="sh-sheet" class="form-select form-select-sm" style="max-width:16rem">${sheet.sheets.map((s) => `<option ${s === sheet.sheet ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></div>` : ""}
      <div class="sh-map">
        <div class="sh-field"><label class="form-label mb-0" for="sh-heading">Headings on row</label>
          <input id="sh-heading" type="number" min="1" max="${sheet.table.length}" value="${sheet.heading + 1}" class="form-control form-control-sm"></div>
        ${FIELDS.map(([f, label, req]) => `<div class="sh-field"><label class="form-label mb-0" for="sh-f-${f}">${label}${req ? " *" : ""}</label>
          <select id="sh-f-${f}" class="form-select form-select-sm" data-field="${f}">${colOpts(f)}</select></div>`).join("")}
      </div>
      ${sheet.mapping.product == null && sheet.mapping.barcode == null ? '<div class="alert alert-warning py-2 small mt-2">Which column has the product names? Choose it under <b>Product</b>.</div>' : ""}
      ${sheet.mapping.shelf == null ? `<div class="d-flex gap-2 flex-wrap align-items-end mt-2">
          <div><label class="form-label mb-0" for="sh-all">No shelf column: put them all on</label>
          <select id="sh-all" class="form-select form-select-sm"><option value="" ${!sheet.forAll ? "selected" : ""}>No shelf (just the products and their stock)</option>
            ${placeOpts}<option value="new" ${sheet.forAll === "new" ? "selected" : ""}>A new shelf…</option></select></div>
          ${sheet.forAll === "new" ? `<div><label class="form-label mb-0" for="sh-new">Its name</label><input id="sh-new" class="form-control form-control-sm" value="${esc(sheet.newShelf)}" placeholder="e.g. Shelf D"></div>` : ""}
        </div>` : ""}
      <div class="form-check mt-2">
        <input class="form-check-input" type="checkbox" id="sh-count" ${sheet.count ? "checked" : ""} ${sheet.mapping.quantity == null ? "disabled" : ""}>
        <label class="form-check-label" for="sh-count">Count the stock: set what is on each shelf in the sheet to its quantities${sheet.mapping.expiry != null ? ", with their expiry dates" : ""}
          <span class="small muted d-block">${sheet.mapping.quantity == null ? "Choose the Quantity column to count the stock too." : "What is on those shelves now. Stock on shelves the sheet doesn't mention is kept."}</span></label>
      </div>
      <div class="sh-sum small mt-2">${s_(keep.length, "row", "rows")}: <b>${s_(known, "known product", "known products")}</b>${close ? `, <b>${s_(close, "close guess", "close guesses")}</b> to check` : ""}${fresh ? `, <b>${s_(fresh, "new product", "new products")}</b>` : ""}${shelves.size ? ` on <b>${s_(shelves.size, "shelf", "shelves")}</b>` : ""}.
        ${lookalike ? `<span class="text-warning-emphasis"><b>${lookalike}</b> of the new ones look${lookalike === 1 ? "s" : ""} like ${lookalike === 1 ? "a product" : "products"} you have: check the highlighted rows.</span>` : ""}
        ${problems ? `<span class="text-danger">${problems} with something not understood (see the last column).</span>` : ""}
        ${sheet.skipped ? `<span class="muted">${sheet.skipped} row${sheet.skipped === 1 ? "" : "s"} with no product name left out (totals, notes).</span>` : ""}</div>
      <div class="sh-table-wrap"><table class="table table-sm sh-table">
        <thead><tr><th>Row</th><th>Product</th><th>Is it</th><th>Shelf</th><th>Level</th><th>Qty</th><th>Expiry</th><th></th></tr></thead>
        <tbody>${shown.map((r, i) => {
          const c = choiceOf(r);
          const opts = [r.product, ...(r.offer || [])].filter((x, j, a) => x != null && a.indexOf(x) === j);
          const sure = r.how === "barcode" || r.how === "name";
          const check = r.how === "close" || (c === "new" && (r.offer || []).length);
          return `<tr class="${c === "skip" ? "left-out" : ""}${check ? " guess" : ""}" data-i="${i}">
            <td class="muted">${r.row}</td><td>${esc(r.name || r.barcode)}</td>
            <td>${sure ? `<span class="text-success"><i class="bi bi-check-lg" aria-hidden="true"></i> ${esc(name(r.product))}</span>
                  <button type="button" class="btn btn-link btn-sm p-0 ms-1" data-leave="${i}">${c === "skip" ? "keep" : "leave out"}</button>`
              : `<select class="form-select form-select-sm" data-pick="${i}" aria-label="Which product is row ${r.row}">
                ${opts.map((id) => `<option value="${id}" ${c === String(id) ? "selected" : ""}>${r.how === "close" && id === r.product ? "≈ " : ""}${esc(name(id))}</option>`).join("")}
                <option value="new" ${c === "new" ? "selected" : ""}>New product: ${esc(r.name || r.barcode)}</option>
                <option value="skip" ${c === "skip" ? "selected" : ""}>Leave this row out</option></select>`}</td>
            <td>${esc(r.shelf || "")}</td><td>${esc(r.level ?? "")}</td><td>${r.quantity ?? ""}</td>
            <td>${r.expiry ? `${r.expiry.slice(8, 10)}/${r.expiry.slice(5, 7)}/${r.expiry.slice(0, 4)}` : ""}</td>
            <td class="small text-danger">${esc(r.problems.join("; "))}</td></tr>`;
        }).join("")}</tbody></table>
        ${rows.length > shown.length ? `<div class="small muted">…and ${rows.length - shown.length} more rows, matched the same way.</div>` : ""}</div>
      <div class="d-flex gap-2 mt-3 flex-wrap">
        <button type="button" class="btn btn-outline-dark" data-sh="again">Another file</button>
        <button type="button" class="btn btn-success ms-auto" data-sh="save" ${!keep.length ? "disabled" : ""}>Put ${keep.length} row${keep.length === 1 ? "" : "s"} on the shelves</button>
      </div>`;
    box.classList.remove("d-none");
    $("sh-start").classList.add("d-none");
  }
  $("sh-review").addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.id === "sh-sheet") { await readSheet(sheet.file, t.value); return; }
      if (t.id === "sh-heading") { sheet.heading = Math.max(0, Math.min(sheet.table.length - 1, (+t.value || 1) - 1)); await recheck(); return; }
      if (t.dataset.field) { sheet.mapping[t.dataset.field] = t.value === "" ? null : +t.value; if (t.dataset.field === "quantity") sheet.count = t.value !== ""; await recheck(); return; }
      if (t.id === "sh-all") { sheet.forAll = t.value; await recheck(); return; }
      if (t.id === "sh-new") { sheet.newShelf = t.value; await recheck(); return; }
      if (t.id === "sh-count") { sheet.count = t.checked; return; }
      if (t.dataset.pick != null) { sheet.choice[keyOf(sheet.rows[+t.dataset.pick])] = t.value; reviewSheet(); }
    } catch (err) { alert(err.message); }
  });
  $("sh-review").addEventListener("click", async (e) => {
    const leave = e.target.closest("[data-leave]");
    if (leave) {
      const r = sheet.rows[+leave.dataset.leave];
      sheet.choice[keyOf(r)] = choiceOf(r) === "skip" ? String(r.product) : "skip";
      reviewSheet();
      return;
    }
    const b = e.target.closest("[data-sh]");
    if (!b) return;
    if (b.dataset.sh === "again") { sheet = null; $("sh-review").classList.add("d-none"); $("sh-start").classList.remove("d-none"); return; }
    const rows = sheet.rows.filter((r) => choiceOf(r) !== "skip").map((r) => {
      const c = choiceOf(r);
      return { row: r.row, name: r.name, product: c === "new" ? null : +c, new_name: c === "new" ? (r.name || r.barcode) : null,
               shelf: r.shelf, level: r.level, position: r.position, quantity: r.quantity, expiry: r.expiry, lot: r.lot,
               barcode: r.barcode, facings: r.facings };
    });
    if (sheet.count && !confirm("Set the stock on the shelves in the sheet to the sheet's quantities? Stock on other shelves is kept.")) return;
    try {
      b.disabled = true;
      const data = await post("/api/map/sheet/save", { rows, count_stock: sheet.count });
      const d = data.done;
      dlg.close();
      const first = data.places.find((p) => rows.some((r) => (r.shelf || "").toLowerCase() === p.name.toLowerCase()));
      sheet = null;
      $("sh-review").classList.add("d-none");
      $("sh-start").classList.remove("d-none");
      window.LuyMap.update(data, { goTo: first ? first.id : null });
      alert(`Done: ${d.spots} product${d.spots === 1 ? "" : "s"} on ${d.places} shel${d.places === 1 ? "f" : "ves"}`
        + (d.created.length ? `, ${d.created.length} new product${d.created.length === 1 ? "" : "s"} added` : "")
        + (d.counted ? `, stock counted for ${d.counted} (${d.units} units)` : "") + ".");
    } catch (err) { alert(err.message); } finally { b.disabled = false; }
  });
})();
