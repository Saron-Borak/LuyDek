// The Today page's check list, answered in place.
// A tap on Yes / It's… / Not a sale removes the card at once and shows Undo; the
// answer is sent 10 seconds later (see undo.js). How-many buttons update in place.
// Every 20 seconds, and when the phone comes back to the page, the list is
// fetched again so items from new sales appear, and ones answered on Telegram go.
// "New product" names something the camera didn't know; afterwards the list is
// fetched again at once, because other sales that look like it now ask about it.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const items = JSON.parse($("#picker-data").textContent);
  const answered = new Set();  // answered here, waiting out their undo time

  const root = () => $("#check");
  const cards = () => [...document.querySelectorAll("#check .check-item")];
  const card = (id) => document.querySelector(`#check [data-item="${id}"]`);  // a check or a name-later item
  const open = () => cards().filter((c) => !answered.has(c.dataset.item));

  function counts() {
    const left = open(), n = left.length, guessed = left.filter((c) => c.dataset.guess === "1").length;
    const title = $("[data-check-title]");
    if (title) title.textContent = `Check ${n} item${n === 1 ? "" : "s"}`;
    const all = $("#check form[data-act=all]");
    if (all) {
      all.classList.toggle("d-none", !guessed);
      $("[data-all-n]", all).textContent = guessed;
      const note = $("[data-pick-note]", all);
      note.textContent = `${n - guessed} need you to pick below`;
      note.classList.toggle("d-none", n === guessed);
    }
    $("#check .check-card").classList.toggle("d-none", n === 0);
    const names = [...document.querySelectorAll("#check .name-item")].filter((c) => !answered.has(c.dataset.item));
    $("#check .name-card")?.classList.toggle("d-none", names.length === 0);
    const nameN = $("[data-name-n]");
    if (nameN) nameN.textContent = names.length;
    $("#check .all-clear").classList.toggle("d-none", n > 0);
    document.querySelectorAll("[data-pending-badge]").forEach((b) => { b.textContent = n; b.classList.toggle("d-none", !n); });
    const tile = $("[data-stat-check]");
    if (tile) {
      tile.textContent = n;
      tile.closest(".stat").classList.toggle("warn", n > 0);
      tile.closest(".stat").classList.toggle("ok", n === 0);
      $("[data-stat-check-sub]").textContent = n ? "waiting above" : "all caught up ✓";
    }
  }

  function hide(id) {
    answered.add(String(id));
    const c = card(id);
    if (c) { c.classList.add("leaving"); setTimeout(() => answered.has(String(id)) && c.classList.add("d-none"), 250); }
  }
  function unhide(id) {
    answered.delete(String(id));
    const c = card(id);  // looked up again: a refresh may have redrawn the list meanwhile
    if (c) c.classList.remove("d-none", "leaving");
  }

  function answer(id, data, [icon, text], url = `/items/${id}/resolve`) {
    hide(id);
    counts();
    const renamed = url.endsWith("/new-product");
    LuyDek.later({
      icon, text,
      commit: async () => {
        const r = await LuyDek.send(url, data, text);
        if (renamed) { last = 0; setTimeout(refresh, 300); }  // look-alike sales now ask about it
        return r;
      },
      undo: () => { unhide(id); counts(); },
      failed: () => { unhide(id); counts(); },
    });
  }

  function wording(act, c, form, submitter) {
    const name = c.dataset.name;
    if (act === "yes") return ["check-circle-fill", name];
    if (act === "pick") return ["pencil-fill", form.dataset.label || submitter?.textContent.trim() || "Changed"];
    if (act === "remove") return ["x-circle", "Not a sale"];
    if (act === "new" || act === "name") {
      const typed = form.querySelector('[name="name"]')?.value.trim();
      return ["plus-circle-fill", `New product: ${form.dataset.label || typed}`];
    }
    return ["clock", "Saved to name later"];
  }

  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("#check form[data-act]");
    if (!form) return;
    e.preventDefault();
    const act = form.dataset.act, submitter = e.submitter;
    const data = new FormData(form);
    if (submitter && submitter.name) data.set(submitter.name, submitter.value);

    if (act === "all") {
      const targets = open().filter((c) => c.dataset.guess === "1").map((c) => c.dataset.item);
      if (!targets.length) return;
      data.set("ids", targets.join(","));
      targets.forEach(hide);
      counts();
      LuyDek.later({
        icon: "check2-all", text: `${targets.length} item${targets.length === 1 ? "" : "s"} recorded as guessed`,
        commit: () => LuyDek.send("/items/confirm-all", data, "all correct"),
        undo: () => { targets.forEach(unhide); counts(); },
        failed: () => { targets.forEach(unhide); counts(); },
      });
      return;
    }
    const c = form.closest(".check-item, .name-item"), id = c.dataset.item;
    if (act === "qty") {
      try {
        const r = await LuyDek.post(`/items/${id}/resolve`, data);
        if (r.count != null) c.querySelectorAll("[data-count]").forEach((el) => (el.textContent = r.count));
      } catch { LuyDek.note("Couldn't change the number — check the Wi-Fi."); }
      return;
    }
    if (act === "new" || act === "name") {
      if (!String(data.get("name") || "").trim()) { form.querySelector('[name="name"]')?.focus(); return; }
      answer(id, data, wording(act, c, form, submitter), form.getAttribute("action"));
      return;
    }
    answer(id, data, wording(act, c, form, submitter));
  });

  // "Something else…": the product picker in a sheet. "Several items": the same sheet, collecting
  // every product in one photo (the camera saw them as one), each recorded as its own sale.
  let pickFor = null, several = null;
  const sheet = $("#pick-modal"), modal = bootstrap.Modal.getOrCreateInstance(sheet);
  const bar = $("#several-bar"), list = $("#several-list"), save = $("#several-save");
  const title = $("#pick-title"), plainTitle = title.textContent;
  function drawSeveral() {
    list.replaceChildren(...several.map((s, k) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip active";
      chip.dataset.k = k;
      chip.textContent = `${s.label} ×${s.n} `;
      chip.insertAdjacentHTML("beforeend", '<i class="bi bi-x" aria-hidden="true"></i>');
      return chip;
    }));
    const n = several.reduce((t, s) => t + s.n, 0);
    save.disabled = !n;
    save.textContent = n ? `Record ${n} item${n === 1 ? "" : "s"}` : "Record them";
  }
  list.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-k]");
    if (!chip) return;
    several.splice(Number(chip.dataset.k), 1);
    drawSeveral();
  });
  save.addEventListener("click", () => {
    const data = new FormData();
    data.set("picks", several.map((s) => `${s.pack}:${s.n}`).join(","));
    const n = several.reduce((t, s) => t + s.n, 0);
    modal.hide();
    answer(pickFor, data, ["grid-3x3-gap", `${n} items from one photo`], `/items/${pickFor}/several`);
  });
  const picker = LuyDek.picker($("#pick-picker"), items, {
    onPick(i) {
      if (several) {
        const hit = several.find((s) => s.pack === i.pack);
        if (hit) hit.n += 1; else several.push({ pack: i.pack, label: i.label, n: 1 });
        drawSeveral();
        picker.reset();
        picker.focus();
        return;
      }
      modal.hide();
      const data = new FormData();
      data.set("pack_id", i.pack);
      if (card(pickFor)?.classList.contains("name-item")) {  // set aside earlier: it's a product after all
        answer(pickFor, data, ["pencil-fill", i.label], `/items/${pickFor}/new-product`);
        return;
      }
      data.set("action", "pick");
      answer(pickFor, data, ["pencil-fill", i.label]);
    },
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-open-picker], [data-several]");
    if (!b) return;
    pickFor = b.dataset.openPicker || b.dataset.several;
    several = b.dataset.several ? [] : null;
    bar.classList.toggle("d-none", !several);
    title.textContent = several ? "What's in this photo? Pick each one" : plainTitle;
    if (several) drawSeveral();
    picker.reset();
    modal.show();
  });
  sheet.addEventListener("shown.bs.modal", () => picker.focus());
  // "New product" opens with the cursor in the name, so a phone's keyboard is already up.
  document.addEventListener("shown.bs.collapse", (e) => {
    if (e.target.classList.contains("new-product")) e.target.querySelector('input[name="name"]')?.focus();
  });

  // Answers saved on this phone but not sent yet (no Wi-Fi) stay answered, across reloads too.
  function hideQueued() {
    for (const e of LuyDek.outbox()) {
      const m = e.url.match(/\/items\/(\d+)\/(?:resolve|new-product|several)$/);
      const ids = m ? [m[1]] : e.url.endsWith("/items/confirm-all")
        ? (e.data.find(([k]) => k === "ids")?.[1] || "").split(",").filter(Boolean) : [];
      ids.forEach((id) => { answered.add(id); card(id)?.classList.add("d-none"); });
    }
    counts();
  }
  hideQueued();

  // New sales bring new items; answers on Telegram take them away.
  const signature = (el) => [...el.querySelectorAll(".check-item, .name-item")].map((c) => `${c.dataset.item}:${c.dataset.rev}`).join(",");
  let busy = false, last = 0;
  async function refresh() {
    if (document.visibilityState !== "visible" || document.querySelector(".modal.show")) return;
    // One at a time, and not more than every few seconds: phones can fire bursts of visibility events.
    if (busy || Date.now() - last < 3000) return;
    busy = true; last = Date.now();
    let html;
    try {
      const r = await fetch("/fragments/checks", { headers: { Accept: "text/html" } });
      if (!r.ok) return;
      html = await r.text();
    } catch { return; }  // offline for a moment: try again next time
    finally { busy = false; }
    const tmp = document.createElement("div");
    tmp.innerHTML = html.trim();
    const fresh = tmp.firstElementChild;
    const sales = $("[data-stat-sales]");
    if (sales) sales.textContent = fresh.dataset.sales;
    if (signature(fresh) === signature(root())) return;
    root().replaceWith(fresh);
    answered.forEach((id) => card(id)?.classList.add("d-none"));
    hideQueued();
  }
  setInterval(refresh, 20000);
  document.addEventListener("visibilitychange", refresh);
})();
