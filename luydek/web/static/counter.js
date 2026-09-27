// Counter capture: a small state machine over 160x120 grayscale frames.
//   EMPTY  - the frame matches the empty-counter reference
//   ACTIVE - something is on the counter and moving (hands, items landing)
//   STILL  - something is on the counter and has stopped moving for ~1 s -> capture
// A sale closes when the counter has been EMPTY for ~1.6 s.
(() => {
  const W = 160, H = 120, TICK_MS = 200;
  const OCCUPIED_PIXEL = 28, OCCUPIED_FRAC = 0.012;   // vs. the empty reference
  const MOTION_PIXEL = 18, MOTION_FRAC = 0.006;       // vs. the previous frame
  const STILL_TICKS = 5, EMPTY_TICKS = 8;

  const $ = (id) => document.getElementById(id);
  const video = $("video"), overlay = $("overlay");
  const small = document.createElement("canvas"); small.width = W; small.height = H;
  const sctx = small.getContext("2d", { willReadFrequently: true });

  let ref = null, prev = null, state = "NOREF";
  let stillTicks = 0, emptyTicks = 0, needCapture = true, busy = false;
  let txnId = null, lastDetections = null;

  function setState(s, text) {
    state = s;
    const el = $("state");
    el.className = "state-pill state-" + s;
    el.textContent = text || { EMPTY: "Counter empty", ACTIVE: "Items arriving…", STILL: "Still — captured", NOREF: "Calibrate first" }[s];
  }

  function gray() {
    sctx.drawImage(video, 0, 0, W, H);
    const d = sctx.getImageData(0, 0, W, H).data;
    const g = new Uint8Array(W * H);
    for (let i = 0, j = 0; i < d.length; i += 4, j++) g[j] = (d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8;
    return g;
  }
  function frac(a, b, thr) {
    let n = 0;
    for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > thr) n++;
    return n / a.length;
  }
  function snapshot() {
    const c = document.createElement("canvas");
    c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0);
    return new Promise((res) => c.toBlob(res, "image/jpeg", 0.9));
  }

  async function post(url, form) {
    const r = await fetch(url, { method: "POST", body: form });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  async function capture() {
    if (busy || !video.videoWidth) return;
    busy = true;
    try {
      const f = new FormData();
      f.append("image", await snapshot(), "frame.jpg");
      if (txnId) f.append("txn_id", txnId);
      const res = await post("/api/capture", f);
      txnId = res.txn_id;
      lastDetections = res;
      $("txn").textContent = "#" + txnId + " (frame " + res.frame_no + ")";
      showDetections(res.detections);
      draw();
    } catch (e) { console.error(e); $("hint").textContent = "Capture failed: " + e.message; }
    finally { busy = false; }
  }

  async function closeSale() {
    if (!txnId) return;
    const id = txnId; txnId = null; lastDetections = null; draw();
    $("txn").textContent = "–";
    try { logSale(await post(`/api/transactions/${id}/close`, new FormData())); }
    catch (e) { console.error(e); }
  }

  function showDetections(dets) {
    const ul = $("detections");
    ul.innerHTML = "";
    if (!dets.length) { ul.innerHTML = '<li class="list-group-item text-secondary">nothing found on the mat</li>'; return; }
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
    const box = $("salelog");
    if (box.querySelector(".text-secondary")) box.innerHTML = "";
    const div = document.createElement("div");
    div.className = "sale";
    const items = s.lines.map((l) => `<span class="pill status-${l.status}">${l.label}</span>`).join(" ");
    div.innerHTML = `<b>Sale #${s.txn_id}</b> · ${s.auto} auto, ${s.pending} to check<br>${items}`;
    box.prepend(div);
  }

  function draw() {
    const rect = overlay.getBoundingClientRect();
    overlay.width = rect.width; overlay.height = rect.height;
    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (!lastDetections) return;
    const { width, height } = lastDetections;
    const s = Math.min(overlay.width / width, overlay.height / height);
    const ox = (overlay.width - width * s) / 2, oy = (overlay.height - height * s) / 2;
    const colors = { high: "#3ecf8e", medium: "#f5b700", low: "#ef4444" };
    ctx.font = "600 13px system-ui"; ctx.lineWidth = 3;
    for (const d of lastDetections.detections) {
      const [x, y, w, h] = d.bbox;
      const c = d.ignored ? "#9ca3af" : colors[d.tier];
      ctx.strokeStyle = c; ctx.strokeRect(ox + x * s, oy + y * s, w * s, h * s);
      const label = d.ignored ? d.ignored : `${d.label} ${d.score.toFixed(2)}`;
      const tw = ctx.measureText(label).width + 8;
      ctx.fillStyle = c; ctx.fillRect(ox + x * s, oy + y * s - 18, tw, 18);
      ctx.fillStyle = "#111"; ctx.fillText(label, ox + x * s + 4, oy + y * s - 5);
    }
  }

  function tick() {
    if (!video.videoWidth) return;
    const g = gray();
    if (!ref) { prev = g; setState("NOREF"); return; }
    const occupied = frac(g, ref, OCCUPIED_PIXEL) > OCCUPIED_FRAC;
    const moving = prev ? frac(g, prev, MOTION_PIXEL) > MOTION_FRAC : true;
    prev = g;
    if (!$("auto").checked) return;
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

  async function calibrate() {
    const blob = await snapshot();
    const f = new FormData(); f.append("image", blob, "empty.jpg");
    const res = await post("/api/calibrate", f);
    ref = gray();
    $("refimg").src = res.reference + "?t=" + Date.now(); $("refimg").classList.remove("d-none");
    setState("EMPTY");
  }

  async function loadReference(url) {
    // Rebuild the small grayscale reference from the saved empty-counter photo.
    const img = new Image();
    img.src = url;
    await img.decode();
    sctx.drawImage(img, 0, 0, W, H);
    const d = sctx.getImageData(0, 0, W, H).data;
    ref = new Uint8Array(W * H);
    for (let i = 0, j = 0; i < d.length; i += 4, j++) ref[j] = (d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8;
    $("refimg").src = url; $("refimg").classList.remove("d-none");
  }

  async function startCamera(deviceId) {
    if (video.srcObject) video.srcObject.getTracks().forEach((t) => t.stop());
    const stream = await navigator.mediaDevices.getUserMedia({
      video: deviceId ? { deviceId: { exact: deviceId }, width: { ideal: 1280 }, height: { ideal: 960 } }
                      : { width: { ideal: 1280 }, height: { ideal: 960 } },
      audio: false,
    });
    video.srcObject = stream;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    const sel = $("camera");
    sel.innerHTML = devices.map((d, i) => `<option value="${d.deviceId}">${d.label || "Camera " + (i + 1)}</option>`).join("");
    const active = stream.getVideoTracks()[0].getSettings().deviceId;
    if (active) sel.value = active;
  }

  async function refreshState() {
    try {
      const s = await (await fetch("/api/state")).json();
      $("ready").textContent = s.recognizer_ready ? "ready" : "loading CLIP… (first capture waits for it)";
      $("gallery").textContent = s.gallery;
      if (s.reference && !ref) await loadReference(s.reference);
      if (s.open_txn && !txnId) { txnId = s.open_txn; $("txn").textContent = "#" + txnId; }
    } catch (e) { console.error(e); }
  }

  $("btn-calibrate").onclick = () => calibrate().catch((e) => alert(e.message));
  $("btn-capture").onclick = () => capture();
  $("btn-close").onclick = () => closeSale();
  $("camera").onchange = (e) => startCamera(e.target.value);
  window.addEventListener("resize", draw);

  $("upload").onsubmit = async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button"); btn.disabled = true; btn.textContent = "Recognizing…";
    try {
      const res = await post("/api/sale-photo", new FormData(e.target));
      lastDetections = null; draw();
      showDetections(res.detections);
      logSale(res.summary);
    } catch (err) { alert(err.message); }
    finally { btn.disabled = false; btn.textContent = "Recognize as one sale"; }
  };

  refreshState();
  setInterval(refreshState, 5000);
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    startCamera().then(() => setInterval(tick, TICK_MS)).catch((e) => {
      setState("NOREF", "No camera");
      $("hint").textContent = "Camera unavailable (" + e.message + "). Use the upload backup on the right.";
    });
  } else {
    setState("NOREF", "No camera");
  }
})();
