// Tooltips for the small SVG charts and sparklines (web/charts.py): each chart's hit areas carry
// their text in data-tip. Mouse, touch and keyboard focus all show it.
(() => {
  const hits = document.querySelectorAll(".mini-chart .hit");
  if (!hits.length) return;
  let tip = document.getElementById("tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "tip"; tip.className = "chart-tip"; tip.setAttribute("role", "tooltip"); tip.hidden = true;
    document.body.appendChild(tip);
  }
  function show(el, x, y) {
    tip.textContent = el.dataset.tip;   // labels are data: textContent, never innerHTML
    tip.hidden = false;
    tip.style.left = Math.max(8, Math.min(x + 12, window.innerWidth - tip.offsetWidth - 8)) + "px";
    tip.style.top = (y - tip.offsetHeight - 10) + "px";
  }
  hits.forEach((el) => {
    el.addEventListener("pointermove", (e) => show(el, e.clientX, e.clientY));
    el.addEventListener("pointerleave", () => (tip.hidden = true));
    el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show(el, r.left, r.top); });
    el.addEventListener("blur", () => (tip.hidden = true));
  });
})();
