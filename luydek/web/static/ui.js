// Small page-wide behaviours.
// What happened? Anything with data-open-sheet="what" opens the sheet; a tap on the dimmed page, Close
// or Escape shuts it. A <dialog> keeps the keyboard inside while it is open.
(() => {
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-open-sheet]");
    const d = b && document.getElementById(b.dataset.openSheet);
    if (d && d.showModal && !d.open) { d.showModal(); d.focus(); }
  });
  document.querySelectorAll("dialog.sheet").forEach((d) => d.addEventListener("click", (e) => {
    if (e.target !== d) return;
    const r = d.getBoundingClientRect();
    if (e.clientY < r.top || e.clientY > r.bottom || e.clientX < r.left || e.clientX > r.right) d.close();
  }));
})();

// − and + beside a number (counting, how many): one tap changes it by one; typing still works.
document.addEventListener("click", (e) => {
  const b = e.target.closest(".stepper [data-step]");
  if (!b) return;
  const input = b.parentElement.querySelector("input");
  const min = input.min === "" ? -Infinity : Number(input.min);
  input.value = Math.max(min, (Number(input.value) || 0) + Number(b.dataset.step));
  input.dispatchEvent(new Event("input", { bubbles: true }));
});

// Slow forms (reading an invoice, saving a count) show a spinner and what is happening, so a tap
// never looks ignored: give the form data-busy="Reading the invoice…".
(() => {
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form.dataset || !form.dataset.busy) return;
    // Let page scripts that answer in place (and cancel the submit) go first.
    queueMicrotask(() => {
      if (e.defaultPrevented) return;
      const button = e.submitter || form.querySelector("button[type=submit], button:not([type])");
      if (!button) return;
      button.disabled = true;
      button.innerHTML = `<span class="spinner-border" aria-hidden="true"></span> ${form.dataset.busy}`;
    });
  });
})();
