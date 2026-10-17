// A type-to-search product picker: the shop's best sellers first, big buttons,
// English or Khmer names (and the other names invoices use). Enter picks the top match.
//   LuyDek.picker(element, items, { onPick, baseOnly, top })
// `items` come from the page as JSON: {pack, product, label, km, units, sold, base, find}.
window.LuyDek = window.LuyDek || {};
LuyDek.picker = (root, items, { onPick, baseOnly = false, top = 8 } = {}) => {
  const list = baseOnly ? items.filter((i) => i.base) : items;
  // The words come from the page (window.LD_WORDS, in its language); English if it has none.
  const W = Object.assign({ find: "Type a product name…", none: "Nothing found. Try another spelling.",
    best: "Sold most in the last 2 weeks", yours: "Your products", others: "Type to find the other {n}, and multipacks." },
  window.LD_WORDS || {});
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  root.innerHTML = `
    <input type="search" class="form-control form-control-lg" placeholder="${esc(W.find)}" aria-label="find a product" autocomplete="off">
    <div class="picker-head small muted mt-2 mb-1"></div>
    <div class="picker-list"></div>`;
  const input = root.querySelector("input"), head = root.querySelector(".picker-head"), box = root.querySelector(".picker-list");
  let shown = [];

  function render() {
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    // Before any typing: one line per product (its single unit). Typing also finds the 6-packs and boxes.
    const products = list.filter((i) => i.base);
    shown = words.length ? list.filter((i) => words.every((w) => i.find.includes(w))).slice(0, 30) : products.slice(0, top);
    if (words.length) head.textContent = shown.length ? "" : W.none;
    else head.textContent = list.some((i) => i.sold > 0) ? W.best : W.yours;
    box.innerHTML = shown.map((i, n) => `
      <button type="button" class="picker-item" data-n="${n}">
        ${i.img ? `<img class="pic" src="${esc(i.img)}" width="44" height="44" alt="" loading="lazy">`
                : '<span class="pic pic-none" style="width:44px;height:44px"><i class="bi bi-image" aria-hidden="true"></i></span>'}
        <span><span class="name d-block">${esc(i.label)}</span>${i.km && i.km !== i.label ? `<span class="km small muted">${esc(i.km)}</span>` : ""}</span>
      </button>`).join("");
    if (!words.length && products.length > top) {
      box.insertAdjacentHTML("beforeend", `<div class="small muted mt-1">${esc(W.others.replace("{n}", products.length - top))}</div>`);
    }
  }

  box.addEventListener("click", (e) => {
    const b = e.target.closest(".picker-item");
    if (b) onPick(shown[Number(b.dataset.n)]);
  });
  input.addEventListener("input", render);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (shown[0]) onPick(shown[0]); }
  });
  render();
  return {
    reset() { input.value = ""; render(); },
    // Phones: no keyboard popping up over the list; the best sellers are usually right there.
    focus() { if (!matchMedia("(pointer: coarse)").matches) input.focus(); },
  };
};
