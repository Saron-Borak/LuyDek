// Small page-wide behaviours.
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
