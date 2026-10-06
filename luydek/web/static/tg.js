// The Telegram Mini App (templates/tg.html, web/miniapp.py).
//
// "To check": the waiting items as a stack of cards. Swipe right (or tap the tick) when the camera
// is right; swipe left (or the cross) to correct it in a sheet: another product, the count, not a
// sale, several items in one photo, a new product. Each answer waits a few seconds before it is
// sent, so Undo simply never sends it. "Recorded today": fix anything recorded wrongly.
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = (tg && tg.initData) || new URLSearchParams(location.hash.slice(1)).get("tgWebAppData") || "";
  if (tg) { tg.ready(); tg.expand(); }
  const $ = (id) => document.getElementById(id);
  const haptic = (kind) => { try { tg && tg.HapticFeedback && (kind === "ok" ? tg.HapticFeedback.notificationOccurred("success") : tg.HapticFeedback.impactOccurred(kind)); } catch (e) { /* older Telegram */ } };

  // --- words, in the owner's language ---------------------------------------------------------------
  const WORDS = {
    en: {
      to_check: "To check", recorded: "Recorded today", how: "Swipe right if the camera is right, left to correct it.",
      rec_how: "Everything recorded today. Tap one that's wrong to fix it.", all_done: "All checked",
      how_many: "How many?", search: "Search your products", record_them: "Record them", add: "Add",
      name_ph: "Its name and size (e.g. Oishi 380ml)", several: "Several items", new: "New product",
      not_sale: "Not a sale", later: "Not now", is_it: "Is this", what_is: "What is this?", its: "It's",
      add_new: "Add as new:", not_sure: "not sure", fairly_sure: "fairly sure", clutter: "It looked like clutter to the camera. If it's a product, say which.",
      mat: "It looks a lot like the mat itself. A shadow, a paper that moved? Then: Not a sale.",
      size: "It isn't the size this product usually is on the mat. Another size of it?",
      lookalike: "The camera mixes this up with a look-alike. Check the label.",
      reads: "The package reads", saved: "Saved", undo: "Undo", fix_title: "Fix this sale", recorded_as: "Recorded as",
      what_was: "What was it?", pick_each: "Pick each item in the photo", auto: "automatic", checked: "checked",
      rest_right: (n) => `All ${n} are right`, left: (n) => `${n} left`, done_s: (a, b) => `${a} right, ${b} corrected. Thank you!`,
      offline: "No connection: your answers are kept and sent when it's back.", nothing: "Nothing to check right now.",
      not_registered: "Open this from the shop's Telegram bot, after sending /start with the shop PIN.",
      confirm_rest: (n) => `Record all ${n} as the camera guessed?`, fixed: "Fixed",
      unknown: "Not in your products", nothing_rec: "Nothing recorded yet today.", right_stamp: "RIGHT", fix_stamp: "FIX",
      stock: "Stock", today: "Today", sales: "sales", items_sold: "items sold", running_low: "low or out",
      f_all: "All", f_watch: "Low or out", f_sold: "Sold today", left_lbl: "left", not_counted: "not counted",
      below_zero: "below zero", st_out: "Out", st_low: "Running low", st_expiring: "Expiring", st_new: "Not counted",
      must: "Must-have", sold_today_n: (n) => `${n} sold today`, days_n: (d) => `about ${d} days left`,
      sold_line: (a, b, c) => `Sold ${a} today · ${b} in 7 days · ${c} in 28 days`,
      pending_n: (n) => `${n} more waiting to be checked (not taken off yet)`, expiry_d: (d) => `Earliest expiry ${d}`,
      supplier_s: (s) => `Agent: ${s}`, worth: (v) => `Worth $${v} at cost`,
      uncounted_n: (n) => `Never counted: ${n} sold since it was added. Count the shelf once to track it.`,
      to_check_note: (n) => `${n} still to check: counted once checked`, received_note: (n) => `Received +${n} today`,
      on_shelf: (v) => `Stock on the shelf ≈ $${v} at cost`, no_match: "No product matches.",
      simulated: "Includes SIMULATED sales history",
      f_expiry: "Expiring", no_place: "No place set", walk_none: "Nothing expires soon. All good.",
      act_remove: "Expired: take it off", act_front: "Put it in front", act_clear: "Won't sell in time",
      exp_on: (d) => `exp ${d}`, expired_ago: (n) => `expired ${n} day${n === 1 ? "" : "s"} ago`,
      exp_in: (n) => (n === 0 ? "expires today" : `expires in ${n} day${n === 1 ? "" : "s"}`),
      taken_off: "Taken off", removed: (name, n) => `${name} ×${n} taken off the stock`,
      clear_tip: "Discount it, bundle it or send it back.", batches_lbl: "On the shelf",
    },
    km: {
      to_check: "ត្រូវពិនិត្យ", recorded: "បានកត់ថ្ងៃនេះ", how: "អូសទៅស្ដាំ បើកាមេរ៉ាត្រូវ អូសទៅឆ្វេង ដើម្បីកែ។",
      rec_how: "អ្វីៗដែលបានកត់ថ្ងៃនេះ។ ចុចលើមួយដែលខុស ដើម្បីកែ។", all_done: "ពិនិត្យរួចទាំងអស់",
      how_many: "ចំនួនប៉ុន្មាន?", search: "ស្វែងរកទំនិញ", record_them: "កត់ទុក", add: "បន្ថែម",
      name_ph: "ឈ្មោះ និងទំហំ (ឧ. Oishi 380ml)", several: "ទំនិញច្រើនមុខ", new: "ទំនិញថ្មី",
      not_sale: "មិនមែនលក់", later: "ពេលក្រោយ", is_it: "តើនេះជា", what_is: "តើនេះជាអ្វី?", its: "វាជា",
      add_new: "បន្ថែមថ្មី៖", not_sure: "មិនច្បាស់", fairly_sure: "ប្រហែលត្រូវ", clutter: "កាមេរ៉ាគិតថាជារបស់ផ្សេងៗ។ បើជាទំនិញ សូមប្រាប់ថាមួយណា។",
      mat: "មើលទៅដូចកម្រាលតុ។ ស្រមោល ឬក្រដាសរំកិល? បើដូច្នេះ៖ មិនមែនលក់។",
      size: "ទំហំវាមិនដូចទំនិញនេះធម្មតានៅលើកម្រាលទេ។ ទំហំផ្សេងរបស់វាឬ?",
      lookalike: "កាមេរ៉ាច្រឡំវាជាមួយទំនិញស្រដៀងគ្នា។ សូមពិនិត្យស្លាក។",
      reads: "នៅលើកញ្ចប់សរសេរថា", saved: "បានរក្សាទុក", undo: "មិនធ្វើវិញ", fix_title: "កែការលក់នេះ", recorded_as: "បានកត់ថាជា",
      what_was: "តើវាជាអ្វី?", pick_each: "ជ្រើសទំនិញនីមួយៗក្នុងរូប", auto: "ស្វ័យប្រវត្តិ", checked: "បានពិនិត្យ",
      rest_right: (n) => `ត្រូវទាំង ${n}`, left: (n) => `នៅសល់ ${n}`, done_s: (a, b) => `ត្រូវ ${a} កែ ${b}។ អរគុណ!`,
      offline: "គ្មានអ៊ីនធឺណិត៖ ចម្លើយត្រូវបានរក្សាទុក ហើយនឹងផ្ញើពេលមានវិញ។", nothing: "គ្មានអ្វីត្រូវពិនិត្យទេ។",
      not_registered: "សូមបើកពី Telegram bot របស់ហាង បន្ទាប់ពីផ្ញើ /start ជាមួយលេខ PIN។",
      confirm_rest: (n) => `កត់ទាំង ${n} តាមការទាយរបស់កាមេរ៉ា?`, fixed: "បានកែ",
      unknown: "មិនមានក្នុងបញ្ជី", nothing_rec: "មិនទាន់មានការកត់ថ្ងៃនេះទេ។", right_stamp: "ត្រូវ", fix_stamp: "កែ",
      stock: "ស្តុក", today: "ថ្ងៃនេះ", sales: "ការលក់", items_sold: "ទំនិញលក់", running_low: "ជិតអស់",
      f_all: "ទាំងអស់", f_watch: "ជិតអស់ ឬអស់", f_sold: "លក់ថ្ងៃនេះ", left_lbl: "នៅសល់", not_counted: "មិនទាន់រាប់",
      below_zero: "ក្រោមសូន្យ", st_out: "អស់ស្តុក", st_low: "ជិតអស់", st_expiring: "ជិតផុតកំណត់", st_new: "មិនទាន់រាប់",
      must: "ត្រូវតែមាន", sold_today_n: (n) => `លក់ ${n} ថ្ងៃនេះ`, days_n: (d) => `គ្រប់ប្រហែល ${d} ថ្ងៃ`,
      sold_line: (a, b, c) => `លក់ ${a} ថ្ងៃនេះ · ${b} ក្នុង ៧ ថ្ងៃ · ${c} ក្នុង ២៨ ថ្ងៃ`,
      pending_n: (n) => `${n} ទៀតកំពុងរង់ចាំពិនិត្យ (មិនទាន់ដកពីស្តុក)`, expiry_d: (d) => `ផុតកំណត់មុនគេ ${d}`,
      supplier_s: (s) => `ភ្នាក់ងារ៖ ${s}`, worth: (v) => `តម្លៃ $${v} (តម្លៃទិញ)`,
      uncounted_n: (n) => `មិនទាន់រាប់ស្តុក៖ លក់បាន ${n} តាំងពីបន្ថែម។ រាប់ធ្នើម្តង ដើម្បីតាមដានស្តុក។`,
      to_check_note: (n) => `នៅសល់ ${n} មុខត្រូវពិនិត្យ — នឹងរាប់ពេលពិនិត្យរួច`, received_note: (n) => `ទទួលបាន +${n} ថ្ងៃនេះ`,
      on_shelf: (v) => `ទំនិញលើធ្នើ ≈ $${v} (តម្លៃទិញ)`, no_match: "រកមិនឃើញទំនិញទេ។",
      simulated: "មានប្រវត្តិលក់សាកល្បង (simulated)",
      f_expiry: "ជិតផុតកំណត់", no_place: "មិនទាន់កំណត់កន្លែង", walk_none: "គ្មានអ្វីជិតផុតកំណត់ទេ។",
      act_remove: "ផុតកំណត់៖ ដកចេញ", act_front: "ដាក់ខាងមុខ", act_clear: "លក់មិនទាន់",
      exp_on: (d) => `ផុត ${d}`, expired_ago: (n) => `ផុតកំណត់ ${n} ថ្ងៃមុន`,
      exp_in: (n) => (n === 0 ? "ផុតកំណត់ថ្ងៃនេះ" : `ផុតកំណត់ក្នុង ${n} ថ្ងៃ`),
      taken_off: "បានដក", removed: (name, n) => `បានដក ${name} ×${n} ចេញពីស្តុក`,
      clear_tip: "បញ្ចុះតម្លៃ លក់ជាឈុត ឬប្រគល់ឲ្យភ្នាក់ងារ។", batches_lbl: "នៅលើធ្នើ",
    },
  };
  let W = WORDS.en;
  function translate() {
    document.querySelectorAll("[data-t]").forEach((el) => { if (typeof W[el.dataset.t] === "string") el.textContent = W[el.dataset.t]; });
    document.querySelectorAll("[data-tp]").forEach((el) => { el.placeholder = W[el.dataset.tp]; });
  }

  // --- talking to the server ------------------------------------------------------------------------
  const OUTBOX = "luydek-app-outbox";
  const outbox = () => { try { return JSON.parse(localStorage.getItem(OUTBOX) || "[]"); } catch { return []; } };
  const keep = (list) => { try { localStorage.setItem(OUTBOX, JSON.stringify(list)); } catch { /* private: best effort */ } };

  async function api(path, fields) {
    const opts = { headers: { "X-Telegram-Init-Data": initData, Accept: "application/json" } };
    if (fields) {
      const body = new FormData();
      Object.entries(fields).forEach(([k, v]) => v !== undefined && v !== null && body.set(k, v));
      Object.assign(opts, { method: "POST", body, keepalive: true });
    }
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(data.error || data.detail || r.statusText); e.status = r.status; throw e; }
    return data;
  }
  async function deliver(entry) {
    try { await api(entry.path, entry.fields); return true; }
    catch (e) { if (e.status && e.status < 500) return true; return false; }  // refused: nothing to retry
  }
  async function flushOutbox() {
    let list = outbox();
    for (const entry of [...list]) {
      if (!(await deliver(entry))) { toast(W.offline); break; }
      list = outbox().filter((x) => x.id !== entry.id);
      keep(list);
    }
  }
  function send(path, fields) {
    keep([...outbox(), { id: Date.now() + Math.random(), path, fields }]);
    return flushOutbox();
  }
  window.addEventListener("online", flushOutbox);
  setInterval(flushOutbox, 15000);

  // --- state ----------------------------------------------------------------------------------------
  let cards = [], recorded = [], products = [], stats = { right: 0, fixed: 0 }, total = 0;
  let held = null;  // the last answer, not sent yet: {card, path, fields, timer}

  function answer(card, fields, label) {
    commitHeld();
    cards = cards.filter((c) => c !== card);
    held = { card, path: `/api/tg/items/${card.id}`, fields, timer: setTimeout(commitHeld, 4000) };
    if (fields.action === "yes") stats.right++; else stats.fixed++;
    $("btn-undo").disabled = false;
    toast(label, true);
    render();
  }
  function commitHeld() {
    if (!held) return;
    clearTimeout(held.timer);
    const h = held;
    held = null;
    $("btn-undo").disabled = true;
    send(h.path, h.fields);
  }
  function undo() {
    if (!held) return;
    clearTimeout(held.timer);
    if (held.fields.action === "yes") stats.right--; else stats.fixed--;
    cards.unshift(held.card);
    held = null;
    $("btn-undo").disabled = true;
    hideToast();
    haptic("light");
    render();
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") commitHeld(); });
  window.addEventListener("pagehide", commitHeld);

  // --- the card stack -------------------------------------------------------------------------------
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const img = (src, cls) => { const i = el("img", cls); i.alt = ""; if (src) i.src = src; else i.style.visibility = "hidden"; return i; };

  function cardEl(c, depth) {
    const root = el("div", "card" + (depth ? ` back${depth}` : ""));
    const photo = el("div", "photo");
    if (c.photo) photo.append(img(c.photo));
    photo.append(el("div", "stamp yes", "✓ " + W.right_stamp), el("div", "stamp no", W.fix_stamp));
    root.append(photo);
    const info = el("div", "info");
    const meta = el("div", "meta", `${c.time}`);
    meta.append(el("span", `pill ${c.tier}`, c.tier === "low" ? W.not_sure : W.fairly_sure));
    info.append(meta);
    const q = el("div", "q");
    if (c.guess) {
      q.append(img(c.guess.img));
      const name = el("span", "name");
      name.append(document.createTextNode(`${W.is_it} `), el("span", null, c.guess.label), document.createTextNode("?"));
      q.append(name);
    } else {
      q.append(el("span", "name", W.what_is));
    }
    const chip = el("button", "count-chip", `×${c.count}`);
    chip.type = "button";
    chip.addEventListener("click", (e) => { e.stopPropagation(); openSheet(c, "check"); });
    q.append(chip);
    info.append(q);
    if (c.hint === "junk") info.append(el("div", "note", W.clutter));
    if (c.hint === "background") info.append(el("div", "note", W.mat));
    if (c.hint === "size") info.append(el("div", "note", W.size));
    if (c.hint === "lookalike") info.append(el("div", "note", W.lookalike));
    if (c.name_guess) info.append(el("div", "note new", `${W.reads} “${c.name_guess}”`));
    root.append(info);
    return root;
  }

  function render() {
    const stack = $("stack");
    stack.replaceChildren();
    cards.slice(0, 3).reverse().forEach((c, i, shown) => {
      const depth = shown.length - 1 - i;
      const node = cardEl(c, depth);
      if (depth === 0) swipeable(node, c);
      stack.append(node);
    });
    const left = cards.length;
    $("n-check").textContent = left;
    $("progress-bar").style.width = total ? `${((total - left) / total) * 100}%` : "0";
    const empty = left === 0;
    $("stack").hidden = $("actions").hidden = $("how").hidden = empty;
    $("done").hidden = !empty || total === 0;
    $("state").hidden = !(empty && total === 0);
    $("state").textContent = W.nothing;
    $("done-s").textContent = W.done_s(stats.right, stats.fixed);
    const guessed = cards.filter((c) => c.guess && !c.tray);
    $("btn-all").hidden = guessed.length < 2;
    $("btn-all").textContent = W.rest_right(guessed.length);
    if (tg && tg.MainButton) {
      if (empty && total) { tg.MainButton.setText("OK"); tg.MainButton.show(); } else tg.MainButton.hide();
    }
    if (empty && total) haptic("ok");
  }

  function swipeable(node, c) {
    let x0 = null, y0 = null, dx = 0;
    const yes = node.querySelector(".stamp.yes"), no = node.querySelector(".stamp.no");
    node.addEventListener("pointerdown", (e) => { x0 = e.clientX; y0 = e.clientY; dx = 0; node.setPointerCapture(e.pointerId); node.classList.add("dragging"); });
    node.addEventListener("pointermove", (e) => {
      if (x0 === null) return;
      dx = e.clientX - x0;
      const dy = e.clientY - y0;
      if (Math.abs(dy) > Math.abs(dx) * 1.5 && Math.abs(dx) < 20) return;  // scrolling, not swiping
      node.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`;
      yes.style.opacity = Math.max(0, Math.min(1, dx / 110));
      no.style.opacity = Math.max(0, Math.min(1, -dx / 110));
    });
    const end = () => {
      if (x0 === null) return;
      x0 = null;
      node.classList.remove("dragging");
      if (dx > 110) right(c, node);
      else if (dx < -110) wrong(c, node);
      else { node.style.transform = ""; yes.style.opacity = no.style.opacity = 0; }
    };
    node.addEventListener("pointerup", end);
    node.addEventListener("pointercancel", end);
  }
  function flyOut(node, dir) {
    if (!node) return;
    node.style.transform = `translateX(${dir * 140}%) rotate(${dir * 18}deg)`;
    node.style.opacity = "0";
  }
  function topNode() { return $("stack").lastElementChild; }
  function right(c, node) {
    if (!c.guess) { haptic("medium"); if (node) node.style.transform = ""; openSheet(c, "check"); return; }
    haptic("light");
    flyOut(node || topNode(), 1);
    setTimeout(() => answer(c, { action: "yes", count: c.count }, `✓ ${c.guess.label}`), 180);
  }
  function wrong(c, node) {
    haptic("light");
    const n = node || topNode();
    if (n) n.style.transform = "translateX(-30%) rotate(-6deg)";
    openSheet(c, "check");
  }
  $("btn-right").addEventListener("click", () => cards[0] && right(cards[0]));
  $("btn-wrong").addEventListener("click", () => cards[0] && wrong(cards[0]));
  $("btn-undo").addEventListener("click", undo);
  $("btn-all").addEventListener("click", () => {
    const ids = cards.filter((c) => c.guess && !c.tray);
    const go = (ok) => {
      if (!ok) return;
      commitHeld();
      send("/api/tg/confirm-all", { ids: ids.map((c) => c.id).join(",") });
      stats.right += ids.length;
      cards = cards.filter((c) => !ids.includes(c));
      render();
    };
    if (tg && tg.showConfirm) tg.showConfirm(W.confirm_rest(ids.length), go); else go(confirm(W.confirm_rest(ids.length)));
  });

  // --- the correction sheet -------------------------------------------------------------------------
  let sheetFor = null, sheetMode = "check", several = null, count = 1;
  function openSheet(c, mode) {
    sheetFor = c; sheetMode = mode; several = null; count = c.count;
    $("sheet-photo").src = c.photo || "";
    $("sheet-title").textContent = mode === "fix" ? W.fix_title : W.what_was;
    $("sheet-sub").textContent = mode === "fix" && c.guess ? `${W.recorded_as} ${c.guess.label} ×${c.count}` : `${c.date || ""} ${c.time}`;
    $("c-n").textContent = count;
    $("new-form").hidden = true;
    $("new-name").value = c.name_guess || "";
    $("several-bar").hidden = true;
    $("count-row").hidden = false;
    $("s-several").hidden = $("s-later").hidden = $("s-new").hidden = mode === "fix" || c.tray;
    $("search").value = "";
    drawQuick();
    drawProducts();
    $("sheet").hidden = $("sheet-back").hidden = false;
    if (tg && tg.BackButton) tg.BackButton.show();
  }
  function closeSheet(keepCard) {
    $("sheet").hidden = $("sheet-back").hidden = true;
    if (tg && tg.BackButton) tg.BackButton.hide();
    if (!keepCard) { const n = topNode(); if (n) n.style.transform = ""; }
    sheetFor = null;
  }
  $("sheet-back").addEventListener("click", () => closeSheet());
  if (tg && tg.BackButton) tg.BackButton.onClick(() => closeSheet());

  function tile(p, prefix, cls, onPick) {
    const b = el("button", `tile ${cls || ""}`);
    b.type = "button";
    b.append(img(p.img), el("span", null, prefix ? `${prefix} ${p.label}` : p.label));
    b.addEventListener("click", () => onPick(p));
    return b;
  }
  function drawQuick() {
    const box = $("quick");
    box.replaceChildren();
    const c = sheetFor;
    if (sheetMode === "check" && c.name_guess && !c.tray) {
      const b = el("button", "tile primary");
      b.type = "button";
      b.append(el("span", null, `${W.add_new} ${c.name_guess}`));
      b.addEventListener("click", () => decide({ action: "new", name: c.name_guess }, `+ ${c.name_guess}`));
      box.append(b);
    }
    const seen = new Set();
    [c.guess && sheetMode === "fix" ? null : c.guess, ...c.candidates].filter(Boolean).forEach((p) => {
      if (seen.has(p.pack)) return;
      seen.add(p.pack);
      box.append(tile(p, W.its, "pick", pick));
    });
  }
  function drawProducts() {
    const words = $("search").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const list = words.length ? products.filter((p) => words.every((w) => p.find.includes(w))).slice(0, 30)
                              : products.filter((p) => p.base).slice(0, 8);
    $("plist").replaceChildren(...list.map((p) => tile(p, "", "", pick)));
  }
  $("search").addEventListener("input", drawProducts);

  function pick(p) {
    if (several) {
      const hit = several.find((s) => s.pack === p.pack);
      if (hit) hit.n++; else several.push({ pack: p.pack, label: p.label, n: 1 });
      drawSeveral();
      haptic("light");
      return;
    }
    if (sheetMode === "fix") return fix({ action: "fix-pick", pack_id: p.pack, count }, `${W.fixed}: ${p.label}`);
    decide({ action: "pick", pack_id: p.pack, count }, `→ ${p.label}`);
  }
  function decide(fields, label) {
    const c = sheetFor;
    closeSheet(true);
    flyOut(topNode(), -1);
    setTimeout(() => answer(c, fields, label), 180);
  }
  async function fix(fields, label) {
    const c = sheetFor;
    closeSheet(true);
    try {
      await api(`/api/tg/items/${c.id}`, fields);
      toast(label);
      haptic("ok");
      await load(false);
    } catch (e) { toast(e.message); }
  }

  $("c-minus").addEventListener("click", () => { count = Math.max(1, count - 1); $("c-n").textContent = count; });
  $("c-plus").addEventListener("click", () => { count += 1; $("c-n").textContent = count; });
  $("s-notsale").addEventListener("click", () => sheetMode === "fix"
    ? fix({ action: "fix-remove" }, `${W.fixed}: ${W.not_sale}`) : decide({ action: "remove" }, W.not_sale));
  $("s-later").addEventListener("click", () => decide({ action: "unknown" }, W.later));
  $("s-new").addEventListener("click", () => { $("new-form").hidden = false; $("new-name").focus(); });
  $("new-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("new-name").value.trim();
    if (name) decide({ action: "new", name }, `+ ${name}`);
  });
  $("s-several").addEventListener("click", () => {
    several = [];
    $("sheet-title").textContent = W.pick_each;
    $("quick").replaceChildren();
    $("count-row").hidden = true;       // each chip carries its own count
    $("several-bar").hidden = false;
    drawSeveral();
  });
  function drawSeveral() {
    $("several-chips").replaceChildren(...several.map((s, k) => {
      const chip = el("button", "chip", `${s.label} ×${s.n} `);
      chip.insertAdjacentHTML("beforeend", '<i class="bi bi-x" aria-hidden="true"></i>');
      chip.type = "button";
      chip.addEventListener("click", () => { several.splice(k, 1); drawSeveral(); });
      return chip;
    }));
    const n = several.reduce((t, s) => t + s.n, 0);
    $("several-save").disabled = !n;
    $("several-save").textContent = n ? `${W.record_them} (${n})` : W.record_them;
  }
  $("several-save").addEventListener("click", () => {
    const picks = several.map((s) => `${s.pack}:${s.n}`).join(",");
    const n = several.reduce((t, s) => t + s.n, 0);
    decide({ action: "several", picks }, `${W.several}: ${n}`);
  });

  // --- recorded today -------------------------------------------------------------------------------
  function drawRecorded() {
    $("n-rec").textContent = recorded.length;
    const list = $("rec-list");
    if (!recorded.length) { list.replaceChildren(el("div", "state", W.nothing_rec)); return; }
    list.replaceChildren(...recorded.map((c) => {
      const row = el("button", "row");
      row.type = "button";
      row.append(img(c.photo, "crop"));
      const txt = el("div");
      txt.append(el("div", "t", c.guess ? `${c.guess.label}${c.count > 1 ? ` ×${c.count}` : ""}` : W.unknown),
                 el("div", "s", `${c.time} · ${c.status === "auto" ? W.auto : W.checked}`));
      const pen = el("span", "fix");
      pen.innerHTML = '<i class="bi bi-pencil" aria-hidden="true"></i>';
      row.append(txt, pen);
      row.addEventListener("click", () => openSheet(c, "fix"));
      return row;
    }));
  }

  // --- stock: today's numbers, and what is left of every product ------------------------------------
  let stock = null, filter = "all";
  const opened = new Set();
  const WATCH = ["out", "low"];

  async function loadStock() {
    try {
      stock = await api("/api/tg/stock");
      drawStock();
    } catch (e) {
      $("stock-list").replaceChildren(el("div", "state", e.status === 401 || e.status === 403 ? W.not_registered : e.message));
    }
  }
  function drawStock() {
    if (!stock) return;
    const [, m, d] = stock.date.split("-");
    $("today-date").textContent = `${d}/${m}`;
    $("st-sales").textContent = stock.txns;
    $("st-units").textContent = stock.units;
    $("st-watch").textContent = stock.watch;
    $("n-stock").textContent = stock.watch;
    $("n-stock").hidden = !stock.watch;
    const toDo = stock.walk.filter((w) => w.action !== "front").length;
    $("n-walk").textContent = toDo;
    $("n-walk").hidden = !toDo;
    const notes = [];
    if (stock.pending) notes.push(W.to_check_note(stock.pending));
    if (stock.received) notes.push(W.received_note(stock.received));
    $("today-note").textContent = notes.join(" · ");
    $("today-note").hidden = !notes.length;

    const words = $("stock-search").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (filter === "expiry") { drawWalk(words); return; }
    let list = stock.products;
    if (filter === "watch") list = list.filter((p) => p.tracked && WATCH.includes(p.status));
    if (filter === "sold") list = list.filter((p) => p.sold_today > 0).sort((a, b) => b.sold_today - a.sold_today);
    if (words.length) list = list.filter((p) => words.every((w) => p.find.includes(w) || p.name.toLowerCase().includes(w)));
    $("stock-list").replaceChildren(...(list.length ? list.map(stockRow) : [el("div", "state", W.no_match)]));
    const foot = [];
    if (stock.value) foot.push(W.on_shelf(stock.value.toFixed(2)));
    if (stock.simulated) foot.push(W.simulated);
    $("stock-foot").textContent = foot.join(" · ");
  }
  function stockRow(p) {
    const box = el("div", "srow");
    const head = el("button");
    head.type = "button";
    head.setAttribute("aria-expanded", opened.has(p.id));
    const mid = el("div", "mid");
    mid.append(el("div", "t", p.name));
    const sub = el("div", "s");
    const pill = { out: W.st_out, low: W.st_low, expiring: W.st_expiring, new: W.st_new }[p.status];
    if (pill) sub.append(el("span", `pill st-${p.status}`, pill));
    if (p.critical && WATCH.includes(p.status)) sub.append(el("span", "pill st-must", W.must));
    const bits = [];
    if (p.sold_today) bits.push(W.sold_today_n(p.sold_today));
    if (p.tracked && p.days != null && p.left > 0) bits.push(W.days_n(Math.max(1, Math.round(p.days))));
    if (p.place) {
      const where = el("span", "where");
      where.innerHTML = '<i class="bi bi-geo-alt" aria-hidden="true"></i> ';
      where.append(document.createTextNode(p.place));
      sub.append(where);
    }
    sub.append(document.createTextNode(bits.join(" · ")));
    mid.append(sub);
    const n = el("div", "n");
    if (!p.tracked) n.append(el("b", null, "–"), el("span", null, W.not_counted));
    else if (p.negative) { n.classList.add("out"); n.append(el("b", null, "!"), el("span", null, W.below_zero)); }
    else { if (p.left <= 0) n.classList.add("out"); n.append(el("b", null, Math.max(p.left, 0)), el("span", null, W.left_lbl)); }
    head.append(img(p.img), mid, n);
    box.append(head);
    if (opened.has(p.id)) {
      const more = el("div", "more");
      if (p.breakdown) more.append(el("div", null, p.breakdown));
      if (!p.tracked) more.append(el("div", null, W.uncounted_n(p.uncounted)));
      more.append(el("div", null, W.sold_line(p.sold_today, p.sold_7d, p.sold_28d)));
      if (p.pending) more.append(el("div", "h", W.pending_n(p.pending)));
      const dated = p.batches.filter((b) => b.expiry);
      if (p.batches.length > 1 && dated.length) {
        more.append(el("div", "h", W.batches_lbl));
        p.batches.forEach((b) => more.append(el("div", null,
          `${b.left}${b.expiry ? " · " + W.exp_on(b.expiry) : ""}${b.place ? " · " + b.place : ""}`)));
      } else if (p.expiry) more.append(el("div", null, W.expiry_d(p.expiry)));
      if (p.supplier) more.append(el("div", "h", W.supplier_s(p.supplier)));
      if (p.tracked && p.value) more.append(el("div", "h", W.worth(p.value.toFixed(2))));
      box.append(more);
    }
    head.addEventListener("click", () => {
      if (opened.has(p.id)) opened.delete(p.id); else opened.add(p.id);
      haptic("light");
      drawStock();
    });
    return box;
  }
  // The shelf check: place by place, what to take off, what to put in front, what won't sell in time.
  function drawWalk(words) {
    let list = stock.walk;
    if (words.length) {
      const find = new Map(stock.products.map((p) => [p.id, p.find + " " + p.name.toLowerCase()]));
      list = list.filter((w) => words.every((x) => (find.get(w.product) || "").includes(x)));
    }
    if (!list.length) { $("stock-list").replaceChildren(el("div", "state", W.walk_none)); return; }
    const out = [];
    let place;
    for (const w of list) {
      if (w.place !== place || !out.length) {
        place = w.place;
        const head = el("div", "walk-place");
        head.innerHTML = '<i class="bi bi-geo-alt" aria-hidden="true"></i> ';
        head.append(document.createTextNode(place || W.no_place));
        out.push(head);
      }
      const row = el("div", "walk");
      row.append(img(w.img));
      const mid = el("div", "mid");
      mid.append(el("div", "t", `${w.name} ×${w.left}`));
      const s = el("div", "s");
      s.append(el("span", `pill act-${w.action}`, W["act_" + w.action]), document.createTextNode(
        " " + (w.days < 0 ? W.expired_ago(-w.days) : W.exp_in(w.days)) + (w.action === "clear" ? ". " + W.clear_tip : "")));
      mid.append(s);
      row.append(mid);
      if (w.action !== "front") {
        const b = el("button", null, W.taken_off);
        b.type = "button";
        b.addEventListener("click", async () => {
          b.disabled = true;
          try {
            await api(`/api/tg/batches/${w.batch}/remove`, {});
            row.classList.add("done");
            haptic("ok");
            toast(W.removed(w.name, w.left));
            loadStock();
          } catch (e) { b.disabled = false; toast(e.message); }
        });
        row.append(b);
      }
      out.push(row);
    }
    $("stock-list").replaceChildren(...out);
  }

  document.querySelectorAll(".filters button").forEach((b) => b.addEventListener("click", () => {
    filter = b.dataset.f;
    document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("on", x === b));
    drawStock();
  }));
  $("stock-search").addEventListener("input", drawStock);

  // --- tabs, toast, loading -------------------------------------------------------------------------
  let tab = "check";
  function showTab(name) {
    tab = name;
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === name));
    $("check-view").hidden = name !== "check";
    $("recorded-view").hidden = name !== "recorded";
    $("stock-view").hidden = name !== "stock";
    if (name === "stock") loadStock();
    commitHeld();
  }
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  let toastTimer = null;
  function toast(text, withUndo) {
    const t = $("toast");
    t.replaceChildren(el("span", null, text));
    if (withUndo) {
      const u = el("button", null, W.undo);
      u.type = "button";
      u.addEventListener("click", undo);
      t.append(u);
    }
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, withUndo ? 4000 : 2600);
  }
  function hideToast() { $("toast").hidden = true; }

  async function load(first = true) {
    try {
      const s = await api("/api/tg/state");
      W = WORDS[s.lang] || WORDS.en;
      document.documentElement.lang = s.lang;
      translate();
      products = s.products;
      recorded = s.recorded;
      if (first) { cards = s.pending; total = cards.length; }
      else {
        // Keep the owner's place: add new items at the end, drop ones answered elsewhere.
        const known = new Set(cards.map((c) => c.id)), live = new Set(s.pending.map((c) => c.id));
        cards = cards.filter((c) => live.has(c.id)).concat(s.pending.filter((c) => !known.has(c.id) && (!held || held.card.id !== c.id)));
        total = Math.max(total, cards.length);
      }
      render();
      drawRecorded();
    } catch (e) {
      $("stack").hidden = $("actions").hidden = true;
      $("state").hidden = false;
      $("state").textContent = e.status === 401 || e.status === 403 ? W.not_registered : e.message;
    }
  }
  if (tg && tg.MainButton) tg.MainButton.onClick(() => tg.close());
  translate();
  flushOutbox();
  // The day's summary opens straight on the Stock tab (/tg?tab=stock).
  const firstTab = new URLSearchParams(location.search).get("tab");
  const firstFilter = new URLSearchParams(location.search).get("f");
  if (firstFilter === "expiry" || firstFilter === "watch" || firstFilter === "sold") {
    filter = firstFilter;
    document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("on", x.dataset.f === filter));
  }
  load().then(() => { if (firstTab === "stock" || firstTab === "recorded") showTab(firstTab); else loadStock(); });
  setInterval(() => {
    if (document.visibilityState !== "visible" || sheetFor) return;
    if (tab === "stock") loadStock(); else load(false);
  }, 30000);
})();
