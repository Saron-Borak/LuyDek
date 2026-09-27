// Gallery photo tiles: tap 🗑 once to arm it ("Delete?"), tap again within 3 s to delete.
// Two taps instead of a dialog: quick for the owner, still safe from a stray tap.
// Tick boxes select several photos for the "Delete selected" button.
(() => {
  function bind(tile) {
    const del = tile.querySelector(".del");
    const pick = tile.querySelector(".pick");
    let timer = null;
    if (del) del.addEventListener("click", async () => {
      if (!del.classList.contains("armed")) {
        del.classList.add("armed");
        del.textContent = "Delete?";
        timer = setTimeout(() => { del.classList.remove("armed"); del.textContent = "🗑"; }, 3000);
        return;
      }
      clearTimeout(timer);
      tile.classList.add("removing");
      const r = await fetch(`/api/gallery/${tile.dataset.gid}/delete`, { method: "POST" });
      if (r.ok) {
        tile.remove();
        document.dispatchEvent(new CustomEvent("photos-changed"));
      } else {
        tile.classList.remove("removing");
        del.classList.remove("armed"); del.textContent = "🗑";
        alert("Could not delete this photo.");
      }
    });
    if (pick) pick.addEventListener("change", () => {
      tile.classList.toggle("selected", pick.checked);
      document.dispatchEvent(new CustomEvent("photos-changed"));
    });
  }

  function refreshBulk() {
    document.querySelectorAll("[data-bulk-delete]").forEach((btn) => {
      const n = document.querySelectorAll(`.pick[form="${btn.form.id}"]:checked`).length;
      btn.classList.toggle("d-none", n === 0);
      btn.textContent = `🗑 Delete ${n} selected`;
    });
  }

  document.querySelectorAll(".photo-tile").forEach(bind);
  document.addEventListener("photos-changed", refreshBulk);
  refreshBulk();
  window.LuyDekPhotos = { bind };
})();
