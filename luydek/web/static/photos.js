// Gallery photo tiles: tap the bin and the photo goes at once, with Undo for 10 seconds
// (undo.js holds the delete back until then). Tick boxes select several photos for
// the "Delete selected" button, which works the same way.
(() => {
  const changed = () => document.dispatchEvent(new CustomEvent("photos-changed"));

  function remove(tiles, text, commit) {
    tiles.forEach((t) => {
      t.classList.add("d-none");
      const pick = t.querySelector(".pick");
      if (pick) pick.checked = false;
      t.classList.remove("selected");
    });
    changed();
    const back = () => { tiles.forEach((t) => t.classList.remove("d-none")); changed(); };
    LuyDek.later({
      icon: "trash3", text,
      commit: async () => { await commit(); tiles.forEach((t) => t.remove()); changed(); },
      undo: back,
      failed: back,
    });
  }

  function bind(tile) {
    const del = tile.querySelector(".del");
    const pick = tile.querySelector(".pick");
    if (del) del.addEventListener("click", () =>
      remove([tile], "Photo deleted", () => LuyDek.send(`/api/gallery/${tile.dataset.gid}/delete`, new FormData(), "photo")));
    if (pick) pick.addEventListener("change", () => {
      tile.classList.toggle("selected", pick.checked);
      changed();
    });
  }

  document.querySelectorAll("form[data-bulk]").forEach((form) => form.addEventListener("submit", (e) => {
    e.preventDefault();
    const picks = [...document.querySelectorAll(`.pick[form="${form.id}"]:checked`)];
    if (!picks.length) return;
    const data = new FormData();
    picks.forEach((p) => data.append("gid", p.value));
    remove(picks.map((p) => p.closest(".photo-tile")), `${picks.length} photo${picks.length === 1 ? "" : "s"} deleted`,
           () => LuyDek.send(form.action, data, "photos"));
  }));

  function refreshBulk() {
    document.querySelectorAll("[data-bulk-delete]").forEach((btn) => {
      const n = document.querySelectorAll(`.pick[form="${btn.form.id}"]:checked`).length;
      btn.classList.toggle("d-none", n === 0);
      btn.innerHTML = `<i class="bi bi-trash3" aria-hidden="true"></i> Delete ${n} selected`;
    });
  }

  document.querySelectorAll(".photo-tile").forEach(bind);
  // Deletes saved on this phone but not sent yet (no Wi-Fi): keep those photos hidden.
  const gone = new Set();
  for (const e of LuyDek.outbox()) {
    const m = e.url.match(/\/api\/gallery\/(\d+)\/delete$/);
    if (m) gone.add(m[1]);
    if (e.url.endsWith("/catalog/gallery/delete")) e.data.filter(([k]) => k === "gid").forEach(([, v]) => gone.add(v));
  }
  document.querySelectorAll(".photo-tile").forEach((t) => gone.has(t.dataset.gid) && t.classList.add("d-none"));
  if (gone.size) changed();
  document.addEventListener("photos-changed", refreshBulk);
  refreshBulk();
  window.LuyDekPhotos = { bind };
})();
