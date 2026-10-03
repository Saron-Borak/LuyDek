// Fixing what the camera did, from the camera log and a product's history (see _fix_modal.html).
// A button with data-fix="item" (a recorded sale) or data-fix="ignored" (something set aside)
// opens the sheet with the photo the camera saw; picking a product, a count, "Not a sale" or a
// new product's name saves it and reloads the page.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const sheet = $("#fix-modal");
  if (!sheet) return;
  const modal = bootstrap.Modal.getOrCreateInstance(sheet);
  const items = JSON.parse($("#picker-data").textContent);
  let target = null;

  async function send(fields) {
    const url = target.kind === "item" ? `/items/${target.id}/fix` : `/ignored/${target.id}/sale`;
    const data = new FormData();
    Object.entries(fields).forEach(([k, v]) => data.set(k, v));
    const status = $("#fix-status");
    status.textContent = "Saving…";
    try {
      await LuyDek.post(url, data);
      location.reload();
    } catch (e) {
      status.textContent = "Couldn't save it: " + e.message;
    }
  }

  const picker = LuyDek.picker($("#fix-picker"), items, {
    onPick: (i) => send(target.kind === "item" ? { action: "pick", pack_id: i.pack, count: $("#fix-count").textContent }
                                                : { pack_id: i.pack }),
  });

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-fix]");
    if (!b) return;
    target = { kind: b.dataset.fix, id: b.dataset.id };
    $("#fix-title").textContent = target.kind === "item" ? "Fix this sale" : "It was a sale: what was it?";
    $("#fix-photo").src = b.dataset.photo || "";
    $("#fix-photo").classList.toggle("d-none", !b.dataset.photo);
    $("#fix-said").textContent = b.dataset.said || "";
    $("#fix-count").textContent = b.dataset.count || "1";
    sheet.querySelectorAll("[data-only]").forEach((el) => el.classList.toggle("d-none", el.dataset.only !== target.kind));
    $("#fix-status").textContent = "";
    $("#fix-new-name").value = b.dataset.guess || "";
    picker.reset();
    modal.show();
  });
  sheet.addEventListener("shown.bs.modal", () => picker.focus());

  const count = $("#fix-count");
  $("#fix-minus").addEventListener("click", () => { count.textContent = Math.max(1, Number(count.textContent) - 1); });
  $("#fix-plus").addEventListener("click", () => { count.textContent = Number(count.textContent) + 1; });
  $("#fix-count-save").addEventListener("click", () => send({ action: "qty", count: count.textContent }));
  $("#fix-remove").addEventListener("click", () => send({ action: "remove" }));
  $("#fix-new").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#fix-new-name").value.trim();
    if (name) send({ name });
  });
})();
