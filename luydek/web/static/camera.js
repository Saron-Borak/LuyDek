// The camera, as close to a phone's own camera app as a web page gets (Counter page, product photos).
//
// - It opens the best picture the camera offers: its largest size, in its own shape (4:3, 16:9,
//   portrait on a phone held upright), never stretched or cropped. The viewfinder takes that shape.
// - Zoom: pinch, the 1× / 2× buttons, or a trackpad pinch on a laptop. The camera's own zoom where
//   the browser offers it (most Android phones); elsewhere a digital zoom (the middle of the
//   picture, enlarged). Either way, what is sent is exactly what the viewfinder shows.
// - Tap to focus, the flash (torch) and switching cameras, where the phone allows them.
// - The zoom is remembered per camera, so the counter's view still matches its empty-mat photo
//   after the page is reloaded.
// Photos are sent at most SEND_MAX pixels on the long side: full HD (1920×1080). Items close together
// need it: at 512×384 a sweet is a few dozen pixels and where one item ends and the next begins is a
// guess. Digital zoom crops the picture (2× keeps a quarter of its pixels), so `hd()` says whether
// what is sent is still at least HD, and the Counter page warns when it isn't.
(() => {
  const KEY = "luydek-camera";
  const SEND_MAX = 1920;
  const DIGITAL_MAX = 4;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window: pick again next time */ } },
  };
  const supported = () => (navigator.mediaDevices && navigator.mediaDevices.getSupportedConstraints
    ? navigator.mediaDevices.getSupportedConstraints() : {});

  class Camera {
    constructor(video, stage) {
      this.video = video;
      this.stage = stage;
      this.track = null;
      this.caps = {};
      this.hw = false;          // the camera zooms itself (else: digital)
      this.zoom = 1;
      this.devices = [];
      this.deviceId = null;
      this.onzoom = null;       // (zoom) after the owner changes it
      this.resized = [];        // called when the picture's size or shape changes (a phone turned)
      const fit = () => this.fit();
      video.addEventListener("loadedmetadata", fit);
      video.addEventListener("resize", fit);
      if (window.ResizeObserver) new ResizeObserver(() => this.changed()).observe(stage);
    }

    onResize(fn) { this.resized.push(fn); }
    changed() { this.resized.forEach((fn) => fn()); }

    static available() { return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia); }
    static saved() { return store.get(KEY); }
    // Browsers only give pages the camera on https:// or on the computer itself (localhost).
    static whyNot() {
      return window.isSecureContext ? ""
        : "This address isn't secure, so the browser won't share the camera. Open LuyDek through its https:// address (see Setup).";
    }

    stop() {
      if (this.video.srcObject) this.video.srcObject.getTracks().forEach((t) => t.stop());
      this.video.srcObject = null;
      this.track = null;
    }

    // Open a camera: the one asked for, else the one used last, else a phone's back camera.
    async start(deviceId = Camera.saved()) {
      this.stop();
      const sup = supported();
      const want = (id, ptz) => {
        // Ask for more than any camera has, as wide as it is tall: the browser then picks the
        // camera's largest picture, and in the camera's own shape.
        const v = { width: { ideal: 4096 }, height: { ideal: 4096 }, frameRate: { ideal: 30 } };
        if (sup.resizeMode) v.resizeMode = { ideal: "none" };   // the camera's own picture, not rescaled
        if (ptz && sup.zoom) v.zoom = true;                       // and its own zoom, where the browser has it
        if (id) v.deviceId = { exact: id }; else v.facingMode = { ideal: "environment" };
        return { video: v, audio: false };
      };
      const tries = [want(deviceId, true)];
      if (sup.zoom) tries.push(want(deviceId, false));            // zoom control refused: the camera alone
      if (deviceId) tries.push(want(null, false));                // the saved camera is gone
      let stream = null, error = null;
      for (const c of tries) {
        try { stream = await navigator.mediaDevices.getUserMedia(c); break; }
        catch (e) { error = e; if (e.name === "NotAllowedError" && !c.video.zoom) break; }
      }
      if (!stream) throw error;

      this.track = stream.getVideoTracks()[0];
      this.video.srcObject = stream;
      await this.video.play().catch(() => { /* autoplay (muted, inline) starts it anyway */ });
      if (!this.video.videoWidth) await new Promise((r) => this.video.addEventListener("loadedmetadata", r, { once: true }));
      this.caps = this.track.getCapabilities ? this.track.getCapabilities() : {};
      await this.largest();
      this.hw = !!(this.caps.zoom && this.caps.zoom.max > this.caps.zoom.min);
      this.deviceId = this.track.getSettings().deviceId || deviceId;
      if ((this.caps.focusMode || []).includes("continuous"))
        this.track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
      this.torch = false;
      this.zoom = this.hw ? (this.track.getSettings().zoom || this.caps.zoom.min) : 1;
      this.video.style.transform = "";
      const saved = parseFloat(store.get(`${KEY}-zoom:${this.deviceId}`));
      if (saved && Math.abs(saved - this.zoom) > 0.01) await this.setZoom(saved, true);
      this.devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
      this.fit();
      return this;
    }

    remember() { if (this.deviceId) store.set(KEY, this.deviceId); }

    // Some cameras open at a small default size even when asked for their biggest: switch to the
    // largest picture the camera says it has, in its own shape.
    async largest() {
      const cw = this.caps.width && this.caps.width.max, ch = this.caps.height && this.caps.height.max;
      if (!cw || !ch || !this.track.applyConstraints) return;
      const [w, h] = [this.video.videoWidth, this.video.videoHeight];
      if (w * h >= cw * ch) return;
      const portrait = h > w;
      try {
        await this.track.applyConstraints({ width: { ideal: portrait ? ch : cw }, height: { ideal: portrait ? cw : ch } });
        await new Promise((r) => setTimeout(r, 300));  // the new size reaches the video a moment later
      } catch { /* refused: keep what it gave */ }
    }

    // The viewfinder takes the picture's shape.
    fit() {
      const w = this.video.videoWidth, h = this.video.videoHeight;
      if (w && h) this.stage.style.setProperty("--ar", (w / h).toFixed(4));
      this.changed();
    }

    label() {
      const d = this.devices.find((x) => x.deviceId === this.deviceId);
      return (d && d.label) || "Camera";
    }
    resolution() { return [this.video.videoWidth, this.video.videoHeight]; }

    // --- zoom ------------------------------------------------------------------------------------
    range() { return this.hw ? { min: this.caps.zoom.min, max: this.caps.zoom.max } : { min: 1, max: DIGITAL_MAX }; }

    async setZoom(z, quiet = false) {
      const { min, max } = this.range();
      const step = (this.hw && this.caps.zoom.step) || 0.01;
      z = Math.min(max, Math.max(min, Math.round(z / step) * step));
      if (Math.abs(z - this.zoom) < 1e-3) return;
      this.zoom = z;
      if (this.hw) this.applyHw(z);
      else this.video.style.transform = z > 1.001 ? `scale(${z})` : "";
      store.set(`${KEY}-zoom:${this.deviceId}`, String(z));
      if (!quiet && this.onzoom) this.onzoom(z);
    }

    // One zoom request at a time, the latest wins, so a pinch never queues up dozens.
    applyHw(z) {
      this.wanted = z;
      if (this.zooming) return;
      this.zooming = true;
      const next = async () => {
        const target = this.wanted;
        this.wanted = null;
        try { await this.track.applyConstraints({ advanced: [{ zoom: target }] }); } catch { /* refused: keep the last */ }
        if (this.wanted != null) return next();
        this.zooming = false;
      };
      next();
    }

    // --- what the viewfinder shows, and what is sent -------------------------------------------------
    // The part of the camera's picture on screen: all of it, or the middle for a digital zoom.
    region() {
      const w = this.video.videoWidth, h = this.video.videoHeight, z = this.hw ? 1 : this.zoom;
      const sw = w / z, sh = h / z;
      return [(w - sw) / 2, (h - sh) / 2, sw, sh];
    }
    draw(ctx, w, h) {
      const [sx, sy, sw, sh] = this.region();
      ctx.drawImage(this.video, sx, sy, sw, sh, 0, 0, w, h);
    }
    size(max = SEND_MAX) {
      const [, , sw, sh] = this.region();
      const k = Math.min(1, max / Math.max(sw, sh));
      return [Math.round(sw * k), Math.round(sh * k)];
    }
    // At least HD (720 pixels on the short side) in what is sent.
    hd() {
      const [w, h] = this.size();
      return Math.min(w, h) >= 720;
    }
    // The camera's largest picture, as it says itself (0, 0 when it doesn't say).
    maxSize() {
      return [(this.caps.width && this.caps.width.max) || 0, (this.caps.height && this.caps.height.max) || 0];
    }
    grab(max = SEND_MAX, quality = 0.9) {
      const [w, h] = this.size(max);
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      const ctx = c.getContext("2d");
      ctx.imageSmoothingQuality = "high";
      this.draw(ctx, w, h);
      return new Promise((res) => c.toBlob(res, "image/jpeg", quality));
    }

    // --- focus, flash, switching ---------------------------------------------------------------------
    canFocus() {
      const modes = this.caps.focusMode || [];
      return !!supported().pointsOfInterest && (modes.includes("single-shot") || modes.includes("continuous"));
    }
    async focusAt(fx, fy) {
      // Where the tap was, in the whole picture (a digital zoom shows only its middle).
      const z = this.hw ? 1 : this.zoom;
      const point = { x: 0.5 + (fx - 0.5) / z, y: 0.5 + (fy - 0.5) / z };
      const modes = this.caps.focusMode || [];
      const c = { pointsOfInterest: [point] };
      if (modes.includes("single-shot")) c.focusMode = "single-shot";
      try { await this.track.applyConstraints({ advanced: [c] }); } catch { return false; }
      clearTimeout(this.refocus);
      if (modes.includes("continuous"))  // like a phone: back to following the scene after a while
        this.refocus = setTimeout(() => this.track && this.track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {}), 8000);
      return true;
    }
    canTorch() { return !!this.caps.torch; }
    async setTorch(on) {
      await this.track.applyConstraints({ advanced: [{ torch: on }] });
      this.torch = on;
    }
    async next() {
      if (this.devices.length < 2) return null;
      const i = this.devices.findIndex((d) => d.deviceId === this.deviceId);
      const d = this.devices[(i + 1) % this.devices.length];
      await this.start(d.deviceId);
      this.remember();
      return d;
    }

    // --- the controls on the viewfinder: zoom buttons, pinch, tap to focus -------------------------
    // `surface` gets the touches (the video, or a canvas drawn over it); `enabled()` says when.
    controls({ surface = this.stage, enabled = () => true } = {}) {
      const stage = this.stage;
      const pills = document.createElement("div");
      pills.className = "zoom-pills";
      pills.setAttribute("role", "group");
      pills.setAttribute("aria-label", "Zoom");
      const flash = document.createElement("div");
      flash.className = "zoom-flash";
      flash.setAttribute("aria-hidden", "true");
      const ring = document.createElement("div");
      ring.className = "focus-ring";
      stage.append(pills, flash, ring);

      const fmt = (z) => (z < 1 ? z.toFixed(1).replace(/^0/, "") : Number.isInteger(Math.round(z * 10) / 10) ? String(Math.round(z)) : z.toFixed(1));
      const presets = () => {
        const { min, max } = this.range();
        const out = [];
        if (min < 0.95) out.push(min);
        out.push(Math.max(1, min));
        if (max >= 2) out.push(2);
        if (max >= 5) out.push(5); else if (max >= 3) out.push(3);
        return out;
      };
      const drawPills = () => {
        const list = presets();
        const on = list.filter((p) => p <= this.zoom + 0.01).pop() ?? list[0];
        pills.replaceChildren(...list.map((p) => {
          const b = document.createElement("button");
          b.type = "button";
          const active = p === on;
          b.className = active ? "on" : "";
          b.textContent = active ? `${fmt(this.zoom)}×` : fmt(p);
          b.setAttribute("aria-label", `Zoom ${fmt(p)} times`);
          b.setAttribute("aria-pressed", active && Math.abs(this.zoom - p) < 0.05);
          b.addEventListener("click", async (e) => { e.stopPropagation(); await this.setZoom(p); drawPills(); showFlash(); });
          return b;
        }));
      };
      let flashTimer = null;
      const showFlash = () => {
        flash.textContent = `${fmt(this.zoom)}×`;
        flash.classList.add("show");
        clearTimeout(flashTimer);
        flashTimer = setTimeout(() => flash.classList.remove("show"), 700);
      };
      const show = () => {
        const on = enabled();
        pills.hidden = !on || !this.video.videoWidth;
        if (on) drawPills();
      };

      // Pinch with two fingers; one quick tap focuses there.
      const pts = new Map();
      let pinch = null, tap = null;
      const gap = () => { const [a, b] = [...pts.values()]; return Math.hypot(a[0] - b[0], a[1] - b[1]) || 1; };
      surface.addEventListener("pointerdown", (e) => {
        if (!enabled() || !this.track || e.target.closest("button")) return;   // the zoom buttons, the flash
        pts.set(e.pointerId, [e.clientX, e.clientY]);
        if (pts.size === 2) { pinch = { d: gap(), z: this.zoom }; tap = null; }
        else if (pts.size === 1) tap = { x: e.clientX, y: e.clientY, t: Date.now() };
      });
      surface.addEventListener("pointermove", (e) => {
        if (!pts.has(e.pointerId)) return;
        pts.set(e.pointerId, [e.clientX, e.clientY]);
        if (pinch && pts.size >= 2) {
          e.preventDefault();
          this.setZoom(pinch.z * (gap() / pinch.d)).then(() => { drawPills(); showFlash(); });
        } else if (tap && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 12) tap = null;
      });
      const up = (e) => {
        if (!pts.has(e.pointerId)) return;
        pts.delete(e.pointerId);
        if (pts.size < 2) pinch = null;
        if (e.type === "pointerup" && tap && !pts.size && Date.now() - tap.t < 450 && this.canFocus()) {
          const r = stage.getBoundingClientRect();
          const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
          this.focusAt(fx, fy).then((ok) => {
            if (!ok) return;
            ring.style.left = `${fx * 100}%`;
            ring.style.top = `${fy * 100}%`;
            ring.classList.remove("show");
            void ring.offsetWidth;      // restart the animation
            ring.classList.add("show");
          });
        }
        if (!pts.size) tap = null;
      };
      surface.addEventListener("pointerup", up);
      surface.addEventListener("pointercancel", up);
      // A laptop trackpad's pinch arrives as a wheel with Ctrl held; a plain wheel still scrolls the page.
      surface.addEventListener("wheel", (e) => {
        if (!e.ctrlKey || !enabled() || !this.track) return;
        e.preventDefault();
        this.setZoom(this.zoom * Math.exp(-e.deltaY * 0.01)).then(() => { drawPills(); showFlash(); });
      }, { passive: false });
      stage.addEventListener("gesturestart", (e) => e.preventDefault());   // older iPhones: no page zoom here

      this.onResize(show);
      show();
      return { refresh: show };
    }
  }

  window.LuyCam = Camera;
})();
