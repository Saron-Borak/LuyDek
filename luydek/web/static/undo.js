// Act now, undo for 10 seconds, and never lose an answer to bad Wi-Fi.
//
// An answer or a delete takes effect on screen at once, but waits here before it
// is sent: Undo simply never sends it, so nothing has to be reversed on the server
// (stock, learned photos, Telegram messages). When the time is up it goes into an
// outbox kept on the phone (localStorage) and is sent from there. If the Wi-Fi is
// down it stays in the outbox, a small notice says so, and it is sent as soon as
// the connection is back, from whichever LuyDek page is open. Anything still
// waiting when the page closes goes into the outbox straight away.
//
// Sending twice is harmless: an item already answered, or a photo already gone,
// is simply skipped by the server.
window.LuyDek = window.LuyDek || {};
(() => {
  const DELAY = 10000, KEY = "luydek-outbox";
  const waiting = new Map();
  let seq = 0, shown = null, toast = null, banner = null, sending = false;

  // --- the outbox ------------------------------------------------------------------------
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; } };
  const save = (list) => { try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { /* private mode: best effort */ } };
  const drop = (id) => save(load().filter((e) => e.id !== id));

  class Refused extends Error {}  // the server said no (4xx): retrying won't help

  async function deliver(entry) {
    const data = new FormData();
    entry.data.forEach(([k, v]) => data.append(k, v));
    let r;
    try {
      r = await fetch(entry.url, { method: "POST", body: data, headers: { Accept: "application/json" }, keepalive: true });
    } catch { return { queued: true }; }                  // offline: stays in the outbox
    if (r.status >= 500) return { queued: true };           // server hiccup: try again later
    drop(entry.id);
    if (!r.ok) throw new Refused(`${entry.url}: ${r.status}`);
    return r.json().catch(() => ({}));
  }

  /** Put a POST in the outbox and try to send it now. Resolves {queued: true} while offline. */
  LuyDek.send = async (url, data, label = "") => {
    const entry = { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, url, label,
                    data: [...data.entries()].map(([k, v]) => [k, String(v)]), at: Date.now() };
    save([...load(), entry]);
    const result = await deliver(entry);
    status();
    return result;
  };

  /** Send whatever is in the outbox, oldest first. */
  LuyDek.retry = async () => {
    if (sending) return;
    sending = true;
    try {
      for (const entry of load()) {
        try {
          if ((await deliver(entry)).queued) break;           // still offline: stop for now
        } catch (e) { console.warn("dropped", entry.url, e); }  // refused: nothing to retry
      }
    } finally { sending = false; status(); }
  };

  /** Outbox entries, for pages that hide what is already answered (checks.js, photos.js). */
  LuyDek.outbox = load;

  function status() {
    const n = load().length;
    if (!banner) {
      if (!n) return;
      banner = document.createElement("div");
      banner.className = "offline-note";
      banner.setAttribute("role", "status");
      banner.innerHTML = '<i class="bi bi-wifi-off" aria-hidden="true"></i> <span></span> <button type="button" class="btn btn-sm btn-dark">Try now</button>';
      banner.querySelector("button").addEventListener("click", LuyDek.retry);
      document.body.appendChild(banner);
    }
    banner.querySelector("span").textContent =
      `No connection: ${n} answer${n === 1 ? "" : "s"} saved on this phone, sent when the Wi-Fi is back`;
    banner.classList.toggle("d-none", n === 0);
  }

  // --- undo --------------------------------------------------------------------------------
  function ensureToast() {
    if (toast) return toast;
    toast = document.createElement("div");
    toast.className = "undo-toast d-none";
    toast.setAttribute("role", "status");
    toast.innerHTML = '<span class="t"></span><button type="button" class="btn btn-sm btn-light fw-semibold">Undo</button><span class="bar"></span>';
    toast.querySelector("button").addEventListener("click", () => shown && cancel(shown));
    document.body.appendChild(toast);
    return toast;
  }

  function label(el, icon, text) {
    el.textContent = "";
    if (icon) {
      const i = document.createElement("i");
      i.className = `bi bi-${icon} me-2`;
      i.setAttribute("aria-hidden", "true");
      el.appendChild(i);
    }
    el.appendChild(document.createTextNode(text));
  }

  function show(id, icon, text, delay) {
    const t = ensureToast();
    shown = id;
    label(t.querySelector(".t"), icon, text);
    t.classList.remove("d-none", "error");
    t.querySelector("button").classList.remove("d-none");
    const bar = t.querySelector(".bar");
    bar.style.transition = "none"; bar.style.width = "100%";
    void bar.offsetWidth;  // restart the countdown bar
    bar.style.transition = `width ${delay}ms linear`; bar.style.width = "0%";
  }

  function hide(id) {
    if (toast && shown === id) { toast.classList.add("d-none"); shown = null; }
  }

  /** A message with nothing to undo; it goes by itself. */
  LuyDek.note = (text) => {
    const t = ensureToast();
    shown = null;
    label(t.querySelector(".t"), "exclamation-circle", text);
    t.querySelector("button").classList.add("d-none");
    t.querySelector(".bar").style.width = "0%";
    t.classList.remove("d-none");
    t.classList.add("error");
    setTimeout(() => { if (!shown) t.classList.add("d-none"); }, 6000);
  };

  async function run(id) {
    const a = waiting.get(id);
    if (!a) return;
    waiting.delete(id);
    hide(id);
    try { await a.commit(); }
    catch (e) {
      console.error(e);
      if (a.failed) a.failed(e);
      LuyDek.note("That couldn't be saved. It may have been answered already.");
    }
  }

  function cancel(id) {
    const a = waiting.get(id);
    if (!a) return;
    clearTimeout(a.timer);
    waiting.delete(id);
    hide(id);
    a.undo();
  }

  /** Show `text` with Undo; `commit` runs after the delay unless undone (then `undo` runs). */
  LuyDek.later = ({ icon, text, commit, undo, failed, delay = DELAY }) => {
    const id = ++seq;
    waiting.set(id, { commit, undo, failed, timer: setTimeout(() => run(id), delay) });
    show(id, icon, text, delay);
    return id;
  };

  /** Send everything still waiting for its undo time, now. */
  LuyDek.flush = () => {
    for (const [id, a] of [...waiting]) { clearTimeout(a.timer); run(id); }
  };
  LuyDek.waiting = () => waiting.size;

  /** POST and read the JSON answer, right now (for small in-place changes such as −/+). */
  LuyDek.post = async (url, data) => {
    const r = await fetch(url, { method: "POST", body: data, headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json().catch(() => ({}));
  };

  window.addEventListener("pagehide", LuyDek.flush);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") LuyDek.flush(); });
  window.addEventListener("online", LuyDek.retry);
  setInterval(() => { if (load().length) LuyDek.retry(); }, 15000);
  LuyDek.retry();  // whatever an earlier page left behind
})();
