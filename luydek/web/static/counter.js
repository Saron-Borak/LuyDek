// Counter capture: a small state machine over 160x120 grayscale frames.
//   EMPTY  - the mat matches the empty-counter reference
//   ACTIVE - something is on the mat and moving (hands, items landing)
//   STILL  - something is on the mat and has stopped moving for ~1 s -> capture
// A sale closes when the mat has been EMPTY for ~1.6 s. Only the marked mat
// area counts, so hands and things beside the mat never start a sale.
// Pharmacies can also mark a tablet tray: loose tablets put down on it count
// as something on the counter, and the server counts them.
(() => {
  const W = 160, H = 120, TICK_MS = 200;
  const OCCUPIED_PIXEL = 28, OCCUPIED_FRAC = 0.012;   // vs. the empty reference
  const MOTION_PIXEL = 18, MOTION_FRAC = 0.006;       // vs. the previous frame
  const TRAY_FRAC = 0.005;                            // one tablet is a small share of even a small tray
  const STILL_TICKS = 5, EMPTY_TICKS = 8;
  // The empty-mat photo keeps up with slow light changes: after this long with the mat empty and
  // still, a fresh one is sent (the server refuses it if anything item-sized is on the mat).
  const REFRESH_TICKS = 150, REFRESH_EVERY_MS = 120000, DRIFT_FRAC = 0.002;
  let quietTicks = 0, lastRefresh = 0, lastMs = null, lastReadout = 0, lastFound = null;

  const $ = (id) => document.getElementById(id);
  const video = $("video"), overlay = $("overlay");
  const cam = new LuyCam(video, video.parentElement);
  const small = document.createElement("canvas"); small.width = W; small.height = H;
  const sctx = small.getContext("2d", { willReadFrequently: true });

  let ref = null, prev = null, state = "NOREF";
  let stillTicks = 0, emptyTicks = 0, needCapture = true, busy = false;
  let txnId = null, lastDetections = null;
  let roi = null, trayBox = null;               // [x0, y0, x1, y1] as fractions of the frame
  let marking = null, dragFrom = null, dragBox = null;   // marking: null, "mat" or "tray"
  let paused = false;                           // the camera no longer sees what it was calibrated on
  let zoomUi = null;                            // the zoom buttons on the picture, while setting up

  function setState(s, text) {
    state = s;
    const el = $("state");
    el.className = "state-pill state-" + s;
    el.textContent = text || {
      EMPTY: "Counter empty", ACTIVE: "Items arriving…", STILL: "Still — captured",
      NOREF: "Calibrate first", SCENE: "Recalibrate",
    }[s];
  }

  // --- feedback: a beep for each item it sees, and the sale shown when it is recorded ------------
  // Nobody should have to wonder whether a sale was recorded. Each item the camera sees gets a beep
  // (high: it knows the item; low double: it will ask about it) and a "+1" with its photo on the
  // picture. When the counter is cleared, a chime and a Sold card list what was sold; the card stays
  // until the next item is put down or it is tapped, and every sale stays in the list beside it.
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const Sound = (() => {
    const KEY = "luydek-sound";
    const AC = window.AudioContext || window.webkitAudioContext;
    let on = true, ctx = null;
    try { on = localStorage.getItem(KEY) !== "off"; } catch { /* private window: sound on */ }
    const ready = () => !!ctx && ctx.state === "running";
    // Browsers only allow sound after the page has been tapped or clicked once.
    function unlock() {
      if (!AC) return;
      if (!ctx) { try { ctx = new AC(); ctx.onstatechange = show; } catch { return; } }
      if (ctx.state === "suspended") ctx.resume().then(show, show);
      show();
    }
    function play(notes) {  // [frequency Hz, start s, length s, wave]
      if (!on || !ready()) return;
      const t0 = ctx.currentTime + 0.02;
      for (const [f, at, len, wave] of notes) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = wave || "sine";
        o.frequency.value = f;
        const vol = wave === "square" ? 0.07 : 0.25;  // a square wave sounds much louder at the same level
        g.gain.setValueAtTime(0.0001, t0 + at);
        g.gain.exponentialRampToValueAtTime(vol, t0 + at + 0.008);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + len);
        o.connect(g).connect(ctx.destination);
        o.start(t0 + at);
        o.stop(t0 + at + len + 0.03);
      }
    }
    function show() {
      const b = $("btn-sound");
      if (!b) return;
      if (!AC) { b.hidden = true; return; }
      const locked = on && !ready();
      b.setAttribute("aria-pressed", String(on));
      b.className = "btn btn-sm " + (locked ? "btn-warning" : "btn-outline-secondary");
      b.innerHTML = `<i class="bi bi-volume-${on ? "up" : "mute"}" aria-hidden="true"></i> `
        + `<span>${locked ? "Tap to turn sound on" : on ? "Sound on" : "Sound off"}</span>`;
    }
    return {
      init() {
        unlock();  // allowed straight away where the browser lets this site play sound
        for (const ev of ["pointerdown", "keydown"]) document.addEventListener(ev, unlock, { capture: true });
        const b = $("btn-sound");
        if (b) b.addEventListener("click", () => {
          if (on && !ready()) unlock();
          else {
            on = !on;
            try { localStorage.setItem(KEY, on ? "on" : "off"); } catch { /* private window */ }
          }
          show();
          setTimeout(() => play([[1850, 0, 0.09, "square"]]), 60);  // so the owner hears what an item sounds like
        });
        show();
      },
      // One beep per new item, a little apart: high for a known item, a low double beep for one it will ask about.
      items(sure) {
        const notes = [];
        sure.slice(0, 6).forEach((ok, i) => {
          const at = i * 0.22;
          if (ok) notes.push([1850, at, 0.09, "square"]);
          else notes.push([700, at, 0.07, "square"], [700, at + 0.11, 0.07, "square"]);
        });
        play(notes);
      },
      sold() { play([[1047, 0, 0.16], [1319, 0.13, 0.16], [1568, 0.26, 0.35]]); },
    };
  })();

  let seen = {}, seenTray = 0;  // per product, the most of it seen at once in this sale: only new ones beep
  function announce(dets, tray) {
    const now = {};
    for (const d of dets) if (!d.ignored) (now[d.pack_id ?? "?"] = now[d.pack_id ?? "?"] || []).push(d);
    const fresh = [];
    for (const [key, list] of Object.entries(now)) {
      const extra = list.length - (seen[key] || 0);
      if (extra > 0) { fresh.push(...list.slice(-extra)); seen[key] = list.length; }
    }
    const pieces = tray && tray.pieces ? tray.pieces : 0;
    const trayNew = pieces > seenTray;
    seenTray = Math.max(seenTray, pieces);
    if (!fresh.length && !trayNew) return;
    hideSold();
    Sound.items(fresh.map((d) => d.tier === "high").concat(trayNew ? [!!tray.sure] : []));
    for (const d of fresh) chip(d.crop, d.label === "?" ? "Something it doesn't know yet" : d.label, d.tier === "high");
    if (trayNew) chip(null, trayText(tray), !!tray.sure, "capsule");
  }

  function chip(photo, text, sure, icon) {
    const feed = $("seen");
    const el = document.createElement("div");
    el.className = "seen-chip " + (sure ? "sure" : "unsure");
    el.innerHTML = (photo ? `<img src="${esc(photo)}" alt="">` : `<i class="bi bi-${icon || "question-lg"}" aria-hidden="true"></i>`)
      + `<span><b>+1</b> ${esc(text)}${sure ? "" : "<small>it will ask you about this one</small>"}</span>`;
    feed.append(el);
    while (feed.children.length > 4) feed.firstElementChild.remove();
    setTimeout(() => el.classList.add("out"), 3200);
    setTimeout(() => el.remove(), 3700);
  }

  function lineName(l) {
    const name = l.label === "?" ? "Something it doesn't know yet" : l.label;
    return l.method === "tray" ? `${l.count} × ${name}` : name;
  }

  function showSold(s) {
    const lines = s.lines.filter((l) => l.status !== "removed");
    if (!lines.length) return;
    const n = lines.reduce((sum, l) => sum + (l.method === "tray" ? 1 : l.count || 1), 0);
    const ask = lines.filter((l) => l.status === "pending").length;
    const card = $("sold");
    card.innerHTML = `<div class="sold-head"><i class="bi bi-check-circle-fill" aria-hidden="true"></i>
        <span>Sold: ${n} item${n === 1 ? "" : "s"}${ask ? ` <small>· ${ask} to check</small>` : ""}</span>
        <span class="sold-time">${clock()}</span></div>
      <ul>${lines.map((l) => `<li class="${l.status === "auto" ? "done" : "ask"}">${l.photo ? `<img src="${esc(l.photo)}" alt="">` : '<i class="bi bi-question-lg" aria-hidden="true"></i>'}
        <span class="name">${esc(lineName(l))}${l.method !== "tray" && l.count > 1 ? ` <b>×${l.count}</b>` : ""}
          <small>${l.status === "auto" ? "recorded" : "you'll be asked to check it"}</small></span></li>`).join("")}</ul>`;
    card.classList.toggle("has-checks", ask > 0);
    card.classList.remove("d-none", "show");
    void card.offsetWidth;  // restart the animation for a sale right after another
    card.classList.add("show");
    Sound.sold();
  }
  function hideSold() { $("sold").classList.add("d-none"); }

  // --- frames -----------------------------------------------------------------------

  function toGray(d) {
    const g = new Uint8Array(W * H);
    for (let i = 0, j = 0; i < d.length; i += 4, j++) g[j] = (d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8;
    return g;
  }
  function gray() {
    cam.draw(sctx, W, H);             // what the viewfinder shows (the zoomed part, for a digital zoom)
    return toGray(sctx.getImageData(0, 0, W, H).data);
  }
  function grid(box) {
    // An area on the 160x120 grid (the whole view when none is marked).
    const [x0, y0, x1, y1] = box || [0, 0, 1, 1];
    return [Math.round(x0 * W), Math.round(y0 * H), Math.max(Math.round(x1 * W), 1), Math.max(Math.round(y1 * H), 1)];
  }
  function frac(a, b, thr, box) {
    const [x0, y0, x1, y1] = grid(box);
    let n = 0;
    for (let y = y0; y < y1; y++)
      for (let i = y * W + x0, end = y * W + x1; i < end; i++) if (Math.abs(a[i] - b[i]) > thr) n++;
    return n / Math.max(1, (x1 - x0) * (y1 - y0));
  }
  const snapshot = () => cam.grab();

  async function post(url, form) {
    const r = await fetch(url, { method: "POST", body: form });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(typeof body.detail === "string" ? body.detail : r.statusText);
      e.status = r.status; e.body = body;
      throw e;
    }
    return body;
  }

  // --- the camera moved or the light changed ------------------------------------------------

  function sceneChanged(body, title, text) {
    paused = true;
    $("scene-title").textContent = title || "Camera moved or the light changed";
    $("scene-text").textContent = text || `${Math.round((body.changed || 1) * 100)}% of the mat looks different from the empty-counter photo.`;
    $("scene-alert").classList.remove("d-none");
    setState("SCENE");
  }
  // A new zoom or another camera sees the counter differently from the empty-mat photo: stop
  // before a wrong sale is recorded, and ask for a fresh calibration.
  let viewTimer = null;
  function viewChanged(what) {
    if (!ref) return;
    clearTimeout(viewTimer);
    viewTimer = setTimeout(() => sceneChanged({}, what === "zoom" ? "The zoom changed" : "Another camera",
      "The picture no longer matches the empty-counter photo. Frame the mat, check the marked mat area still fits, then calibrate."), 700);
  }
  function sceneOk() {
    paused = false;
    $("scene-alert").classList.add("d-none");
  }

  // --- capture and close -------------------------------------------------------------------

  async function capture() {
    if (busy || !video.videoWidth) return;
    busy = true;
    try {
      const f = new FormData();
      f.append("image", await snapshot(), "frame.jpg");
      if (txnId) f.append("txn_id", txnId);
      const res = await post("/api/capture", f);
      lastMs = res.ms;
      lastFound = res.detections.filter((d) => !d.ignored).length;
      txnId = res.txn_id;
      lastDetections = res;
      $("txn").textContent = "#" + txnId + " (frame " + res.frame_no + ")";
      showDetections(res.detections, res.tray);
      announce(res.detections, res.tray);
      draw();
    } catch (e) {
      if (e.status === 409) sceneChanged(e.body);
      else { console.error(e); $("hint").textContent = "Capture failed: " + e.message; }
    } finally { busy = false; }
  }

  async function closeSale() {
    if (!txnId) return;
    const id = txnId; txnId = null; lastDetections = null; draw();
    seen = {}; seenTray = 0;
    $("txn").textContent = "–";
    try { logSale(await post(`/api/transactions/${id}/close`, new FormData())); }
    catch (e) { console.error(e); }
  }

  function trayText(t) {
    if (!t.pieces) return "";
    if (!t.with_source) return `${t.pieces} piece${t.pieces > 1 ? "s" : ""} on the tray — put the box beside it`;
    return t.sure ? `${t.count} tablet${t.count > 1 ? "s" : ""} on the tray` : `About ${t.count} on the tray — count to check`;
  }

  function showDetections(dets, tray) {
    const ul = $("detections");
    ul.innerHTML = "";
    if (tray && tray.pieces) {
      const li = document.createElement("li");
      li.className = "list-group-item fw-semibold";
      li.innerHTML = '<i class="bi bi-capsule" aria-hidden="true"></i> ' + trayText(tray);
      ul.appendChild(li);
    }
    if (!dets.length && !(tray && tray.pieces)) { ul.innerHTML = '<li class="list-group-item text-secondary">nothing found on the mat</li>'; return; }
    for (const d of dets) {
      const li = document.createElement("li");
      li.className = "list-group-item d-flex justify-content-between";
      li.innerHTML = d.ignored
        ? `<span class="text-secondary">ignored: ${d.ignored}</span>`
        : `<span>${d.label}</span><span class="pill tier-${d.tier}">${d.tier} ${d.score.toFixed(2)}${d.method !== "image" ? " · " + d.method : ""}</span>`;
      ul.appendChild(li);
    }
  }

  function logSale(s) {
    if (!s.closed || s.empty) return;
    showSold(s);
    const box = $("salelog");
    if (box.querySelector(":scope > .text-secondary")) box.innerHTML = "";
    box.querySelectorAll(".sale.fresh").forEach((el) => el.classList.remove("fresh"));
    const div = document.createElement("div");
    div.className = "sale fresh";
    const items = s.lines.map((l) => `<span class="sale-item status-${esc(l.status)}">${l.photo ? `<img src="${esc(l.photo)}" alt="">` : ""}`
      + `${l.method === "tray" ? '<i class="bi bi-capsule" aria-hidden="true"></i> ' : ""}${esc(lineName(l))}`
      + `${l.method !== "tray" && l.count > 1 ? " ×" + l.count : ""}</span>`).join("");
    div.innerHTML = `<div class="d-flex justify-content-between"><b>Sale #${s.txn_id}</b><span class="muted">${clock()}</span></div>`
      + `<div class="muted">${s.auto} recorded${s.pending ? `, ${s.pending} to check` : ""}</div><div class="sale-items">${items}</div>`;
    box.prepend(div);
    while (box.children.length > 20) box.lastElementChild.remove();
  }

  // --- drawing: the mat, the tray and the last capture -----------------------------------------

  function view() {
    // Where the picture sits inside the overlay. The viewfinder takes the picture's shape, so
    // normally it fills it; a photo uploaded instead may be letterboxed.
    const [, , sw, sh] = video.videoWidth ? cam.region() : [0, 0, 0, 0];
    const width = sw || (lastDetections && lastDetections.width) || 4;
    const height = sh || (lastDetections && lastDetections.height) || 3;
    const s = Math.min(overlay.width / width, overlay.height / height);
    return { s, width, height, ox: (overlay.width - width * s) / 2, oy: (overlay.height - height * s) / 2 };
  }
  function toPx(box, v) {
    const [x0, y0, x1, y1] = box;
    return [v.ox + x0 * v.width * v.s, v.oy + y0 * v.height * v.s, (x1 - x0) * v.width * v.s, (y1 - y0) * v.height * v.s];
  }

  function draw() {
    const rect = overlay.getBoundingClientRect();
    overlay.width = rect.width; overlay.height = rect.height;
    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    const v = view();
    const mat = marking === "mat" && dragBox ? dragBox : roi;
    if (mat) {
      const [px, py, pw, ph] = toPx(mat, v);
      ctx.fillStyle = "rgba(0,0,0,0.45)";        // dim everything that isn't the mat
      ctx.fillRect(0, 0, overlay.width, py);
      ctx.fillRect(0, py + ph, overlay.width, overlay.height - py - ph);
      ctx.fillRect(0, py, px, ph);
      ctx.fillRect(px + pw, py, overlay.width - px - pw, ph);
      ctx.setLineDash([8, 6]); ctx.lineWidth = 2; ctx.strokeStyle = "#fff";
      ctx.strokeRect(px, py, pw, ph);
      ctx.setLineDash([]);
    }
    const tray = marking === "tray" && dragBox ? dragBox : trayBox;
    if (tray) {
      const [px, py, pw, ph] = toPx(tray, v);
      const t = lastDetections && lastDetections.tray;
      const color = t && t.pieces ? (t.sure ? "#3ecf8e" : "#f5b700") : "#7dd3fc";
      ctx.setLineDash([6, 4]); ctx.lineWidth = 2; ctx.strokeStyle = color;
      ctx.strokeRect(px, py, pw, ph);
      ctx.setLineDash([]);
      const label = t && t.pieces ? (t.sure ? `Tablets: ${t.count}` : `Tablets: ${t.count}?`) : "Tablet tray";
      ctx.font = "600 13px system-ui";
      const tw = ctx.measureText(label).width + 8;
      ctx.fillStyle = color; ctx.fillRect(px, py - 18, tw, 18);
      ctx.fillStyle = "#111"; ctx.fillText(label, px + 4, py - 5);
    }
    if (!lastDetections) return;
    const colors = { high: "#3ecf8e", medium: "#f5b700", low: "#ef4444" };
    ctx.font = "600 13px system-ui"; ctx.lineWidth = 3;
    const k = v.width / (lastDetections.width || v.width);   // boxes come in the size the photo was sent
    for (const d of lastDetections.detections) {
      const [x, y, w, h] = d.bbox.map((n) => n * k);
      const c = d.ignored ? "#9ca3af" : colors[d.tier];
      ctx.strokeStyle = c; ctx.strokeRect(v.ox + x * v.s, v.oy + y * v.s, w * v.s, h * v.s);
      const label = d.ignored ? d.ignored : `${d.label} ${d.score.toFixed(2)}`;
      const tw = ctx.measureText(label).width + 8;
      ctx.fillStyle = c; ctx.fillRect(v.ox + x * v.s, v.oy + y * v.s - 18, tw, 18);
      ctx.fillStyle = "#111"; ctx.fillText(label, v.ox + x * v.s + 4, v.oy + y * v.s - 5);
    }
  }

  // --- marking the mat and the tray ---------------------------------------------------------------

  const MIN_SIDE = { mat: 0.1, tray: 0.04 };

  function atPointer(e) {
    const r = overlay.getBoundingClientRect(), v = view();
    const clamp = (n) => Math.min(1, Math.max(0, n));
    return [clamp((e.clientX - r.left - v.ox) / (v.width * v.s)), clamp((e.clientY - r.top - v.oy) / (v.height * v.s))];
  }
  function showAreas() {
    $("btn-roi").textContent = marking === "mat" ? "Cancel" : (roi ? "Redo the mat area" : "Mark the mat");
    $("btn-roi").classList.toggle("btn-warning", marking === "mat");
    $("btn-tray").innerHTML = marking === "tray" ? "Cancel"
      : '<i class="bi bi-capsule" aria-hidden="true"></i> ' + (trayBox ? "Redo tablet tray" : "Mark the tablet tray");
    $("btn-tray").classList.toggle("btn-warning", marking === "tray");
    $("btn-roi-clear").classList.toggle("d-none", !roi);
    $("btn-tray-clear").classList.toggle("d-none", !trayBox);
    $("roi-help").classList.toggle("d-none", marking !== "mat");
    $("tray-help").classList.toggle("d-none", marking !== "tray");
    overlay.classList.toggle("marking", !!marking);
    if (zoomUi) zoomUi.refresh();
    draw();
  }
  function setMarking(target) {
    marking = marking === target ? null : target;
    dragFrom = null; dragBox = null;
    if (marking) hideSold();  // the whole picture is needed to drag the box
    showAreas();
  }
  async function saveArea(area, box) {
    const f = new FormData();
    f.append("area", area);
    if (box) box.forEach((n, i) => f.append(["x0", "y0", "x1", "y1"][i], n.toFixed(4)));
    else f.append("clear", "true");
    try {
      const saved = (await post("/api/roi", f)).roi;
      if (area === "tray") trayBox = saved; else roi = saved;
      prev = null; stillTicks = 0; emptyTicks = 0;
    } catch (e) { alert(e.message); }
    showAreas();
  }

  overlay.addEventListener("pointerdown", (e) => {
    if (!marking) return;
    overlay.setPointerCapture(e.pointerId);
    dragFrom = atPointer(e); dragBox = null;
  });
  overlay.addEventListener("pointermove", (e) => {
    if (!marking || !dragFrom) return;
    const [x, y] = atPointer(e);
    dragBox = [Math.min(dragFrom[0], x), Math.min(dragFrom[1], y), Math.max(dragFrom[0], x), Math.max(dragFrom[1], y)];
    draw();
  });
  overlay.addEventListener("pointerup", async () => {
    if (!marking || !dragFrom) return;
    const area = marking, box = dragBox, min = MIN_SIDE[area];
    marking = null; dragFrom = null; dragBox = null;
    showAreas();
    if (box && box[2] - box[0] >= min && box[3] - box[1] >= min) await saveArea(area, box);
    else $("hint").textContent = `That box was too small. Press the button again and drag from one corner of the ${area} to the other.`;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && marking) setMarking(marking); });

  // --- the loop ------------------------------------------------------------------------------

  function tick() {
    if (!video.videoWidth) return;
    const g = gray();
    if (!ref) { prev = g; setState("NOREF"); return; }
    const onTray = trayBox && frac(g, ref, OCCUPIED_PIXEL, trayBox) > TRAY_FRAC;
    const occupied = frac(g, ref, OCCUPIED_PIXEL, roi) > OCCUPIED_FRAC || onTray;
    const changed = frac(g, ref, OCCUPIED_PIXEL, roi), motion = prev ? frac(g, prev, MOTION_PIXEL, roi) : 1;
    const moving = prev ? motion > MOTION_FRAC
                          || (trayBox && frac(g, prev, MOTION_PIXEL, trayBox) > TRAY_FRAC) : true;
    prev = g;
    readout(changed, motion);
    // Nothing on the mat and nothing moving for a while, but the light has drifted: refresh the empty photo.
    quietTicks = !occupied && !moving && !txnId ? quietTicks + 1 : 0;
    if (quietTicks >= REFRESH_TICKS && changed > DRIFT_FRAC && Date.now() - lastRefresh > REFRESH_EVERY_MS && !paused) {
      quietTicks = 0;
      lastRefresh = Date.now();
      refreshReference();
    }
    if (!$("auto").checked || paused || marking) return;
    if (occupied) {
      emptyTicks = 0;
      if (moving) { stillTicks = 0; needCapture = true; setState("ACTIVE"); return; }
      stillTicks++;
      if (stillTicks >= STILL_TICKS && needCapture) { needCapture = false; setState("STILL"); capture(); }
    } else {
      stillTicks = 0; needCapture = true;
      if (state !== "EMPTY") setState("EMPTY");
      if (txnId && ++emptyTicks >= EMPTY_TICKS) { emptyTicks = 0; closeSale(); }
    }
  }

  // Plain numbers for "why isn't it capturing?": how much of the mat differs from the empty photo
  // (over 1.2% counts as something there) and how much is moving (under 0.6% counts as still).
  function readout(changed, motion) {
    if (Date.now() - lastReadout < 500) return;
    lastReadout = Date.now();
    const pct = (x) => (x * 100).toFixed(1) + "%";
    const parts = [`On the mat: ${pct(changed)} differs from empty (${changed > OCCUPIED_FRAC ? "something there" : "empty"})`,
                   `moving: ${pct(motion)} (${motion > MOTION_FRAC ? "waiting for it to stop" : "still"})`];
    if (lastMs != null) parts.push(`last photo recognised in ${(lastMs / 1000).toFixed(1)} s`);
    // Stuck: the mat looks different from the empty photo, yet the last photo found nothing on it.
    // Usually the light changed; the sale can't close until the mat looks empty again.
    if (txnId && lastFound === 0 && changed > OCCUPIED_FRAC && stillTicks > 50)
      parts.push("nothing found, but the mat looks different: if it is empty, press Calibrate");
    $("readout").textContent = parts.join(" · ");
  }

  async function refreshReference() {
    try {
      const f = new FormData();
      f.append("image", await snapshot(), "empty.jpg");
      const res = await post("/api/reference/refresh", f);
      if (res.refreshed) {
        ref = gray();
        $("refimg").src = res.reference + "?t=" + Date.now();
      }
    } catch (e) { console.warn("empty-mat refresh skipped", e); }
  }

  async function calibrate() {
    const blob = await snapshot();
    const f = new FormData(); f.append("image", blob, "empty.jpg");
    const res = await post("/api/calibrate", f);
    ref = gray();
    $("refimg").src = res.reference + "?t=" + Date.now(); $("refimg").classList.remove("d-none");
    sceneOk();
    setState("EMPTY");
  }

  async function loadReference(url) {
    // Rebuild the small grayscale reference from the saved empty-counter photo.
    const img = new Image();
    img.src = url;
    await img.decode();
    sctx.drawImage(img, 0, 0, W, H);
    ref = toGray(sctx.getImageData(0, 0, W, H).data);
    $("refimg").src = url; $("refimg").classList.remove("d-none");
  }

  async function startCamera(deviceId) {
    await cam.start(deviceId);
    const sel = $("camera");
    sel.replaceChildren(...cam.devices.map((d, i) => new Option(d.label || "Camera " + (i + 1), d.deviceId)));
    if (cam.deviceId) sel.value = cam.deviceId;
    showCamInfo();
  }
  function showCamInfo() {
    const [w, h] = cam.resolution(), [sw, sh] = cam.size();
    const shape = (a, b) => { const r = a / b; return Math.abs(r - 4 / 3) < 0.02 ? "4:3" : Math.abs(r - 16 / 9) < 0.02 ? "16:9"
      : Math.abs(r - 3 / 4) < 0.02 ? "3:4" : Math.abs(r - 9 / 16) < 0.02 ? "9:16" : r.toFixed(2) + ":1"; };
    const zoom = cam.zoom !== 1 ? ` · zoom ${cam.zoom.toFixed(1)}× (${cam.hw ? "the camera's own" : "digital"})` : "";
    $("cam-info").textContent = `Camera picture ${w}×${h} (${shape(w, h)})${zoom} · photos sent at ${sw}×${sh}`;
    showHd();
  }

  // Items close together need HD photos (720 pixels or more on the short side). Say so on the main
  // view, with the reason: a small camera picture, or a digital zoom cropping a bigger one.
  function showHd() {
    const [w, h] = cam.resolution(), [sw, sh] = cam.size(), [mw, mh] = cam.maxSize();
    const low = w > 0 && !cam.hd();
    $("hd-alert").classList.toggle("d-none", !low);
    if (!low) return;
    $("hd-title").textContent = `Photos are sent at ${sw}×${sh}, below HD: items close together are hard to tell apart.`;
    const why = [];
    if (!cam.hw && cam.zoom > 1.05)
      why.push(`The ${cam.zoom.toFixed(1)}× zoom is digital: it keeps only the middle of the camera's ${w}×${h} picture.`
               + " Zoom out to 1× and put the camera closer to the mat (or lower) instead.");
    if (Math.min(w, h) < 720)
      why.push(Math.min(mw, mh) > Math.min(w, h)
        ? `The camera can give ${mw}×${mh} but opened at ${w}×${h}: choose it again under Camera setup, or reload the page.`
        : `This camera's largest picture is ${w}×${h}. A 1080p webcam (1920×1080) or a phone camera sees far more.`);
    $("hd-text").textContent = why.join(" ");
  }
  const setupOpen = () => $("setup").classList.contains("show");
  zoomUi = cam.controls({ surface: overlay, enabled: () => setupOpen() && !marking });
  cam.onzoom = () => { showCamInfo(); viewChanged("zoom"); };
  cam.onResize(draw);
  cam.onResize(() => { if (cam.track) showCamInfo(); });  // a new picture size: is it still HD?
  $("setup").addEventListener("shown.bs.collapse", zoomUi.refresh);
  $("setup").addEventListener("hidden.bs.collapse", zoomUi.refresh);

  async function refreshState() {
    try {
      const s = await (await fetch("/api/state")).json();
      $("ready").textContent = s.recognizer_ready ? `ready · items found by ${s.finder}` : "loading… (the first photo waits for it)";
      $("gallery").textContent = s.gallery;
      if (s.open_txn && !txnId) { txnId = s.open_txn; $("txn").textContent = "#" + txnId; }
      const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
      if (!marking && (!same(s.roi, roi) || !same(s.tray, trayBox))) { roi = s.roi; trayBox = s.tray; showAreas(); }
      if (s.reference && !ref) await loadReference(s.reference);  // last: the photo can be slow to load
    } catch (e) { console.error(e); }
  }

  $("btn-calibrate").onclick = () => calibrate().catch((e) => alert(e.message));
  $("btn-scene-cal").onclick = () => calibrate().catch((e) => alert(e.message));
  $("btn-scene-ignore").onclick = () => { sceneOk(); needCapture = false; setState("EMPTY", "Waiting…"); };
  $("btn-roi").onclick = () => setMarking("mat");
  $("btn-roi-clear").onclick = () => saveArea("mat", null);
  $("btn-tray").onclick = () => setMarking("tray");
  $("btn-tray-clear").onclick = () => saveArea("tray", null);
  $("btn-capture").onclick = () => capture();
  $("btn-close").onclick = () => closeSale();
  $("camera").onchange = async (e) => {
    await startCamera(e.target.value).catch((err) => { $("hint").textContent = "That camera didn't open: " + err.message; });
    cam.remember();
    viewChanged("camera");
  };
  window.addEventListener("resize", draw);

  if ($("upload")) $("upload").onsubmit = async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button"); btn.disabled = true; btn.textContent = "Recognizing…";
    try {
      const res = await post("/api/sale-photo", new FormData(e.target));
      lastDetections = null; draw();
      showDetections(res.detections, res.tray);
      seen = {}; seenTray = 0;
      announce(res.detections, res.tray);
      seen = {}; seenTray = 0;
      setTimeout(() => logSale(res.summary), 900);  // the beeps first, then the sale, as at the counter
    } catch (err) { alert(err.message); }
    finally { btn.disabled = false; btn.textContent = "Recognize as one sale"; }
  };

  Sound.init();
  $("sold").addEventListener("click", hideSold);

  // The Setup page links here with #setup: open the camera setup panel.
  if (location.hash === "#setup") bootstrap.Collapse.getOrCreateInstance($("setup"), { toggle: false }).show();

  refreshState();
  setInterval(refreshState, 5000);
  // A phone used as the counter camera must stay awake: the camera stops when the screen locks.
  // When the page comes back (the phone was unlocked, or the owner switched back), reopen the camera.
  async function keepAwake() {
    try { if ("wakeLock" in navigator) await navigator.wakeLock.request("screen"); }
    catch (e) { /* refused (battery saver): the phone's own screen timeout applies */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    keepAwake();
    const track = video.srcObject && video.srcObject.getVideoTracks()[0];
    if (track && track.readyState === "ended") startCamera($("camera").value || LuyCam.saved()).catch(console.error);
  });

  if (LuyCam.available()) {
    startCamera(LuyCam.saved()).then(() => { keepAwake(); setInterval(tick, TICK_MS); }).catch((e) => {
      setState("NOREF", "No camera");
      $("hint").textContent = "Camera unavailable (" + e.message + "). Use the upload backup on the right.";
    });
  } else {
    setState("NOREF", "No camera");
    $("hint").textContent = LuyCam.whyNot() || "This browser has no camera access. Use the upload backup on the right.";
  }
})();
