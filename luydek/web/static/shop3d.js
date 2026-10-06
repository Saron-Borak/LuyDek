// The shop in 3D (/map, "3D shop"): every place a rack on the shop floor, every product a box on its
// level, coloured by the measure chosen. A warehouse visualisation, for a mini-mart.
//
// Drawn on a canvas by a small perspective camera of its own that turns round the shop: no 3D library, so
// nothing comes from the internet. Racks are drawn far to near; inside a rack, the levels farthest from the
// eye first and each level's products from the outside in, so nearer boxes cover farther ones. Drag to turn
// the shop, scroll or pinch to zoom, right-drag (or shift-drag, or two fingers) to move it. Hover or tap a
// box for what it is; tap a rack's name to see it face on. "Arrange the shop" drags racks on the floor.
// The suggested move (shop3d.py) is drawn as two arcs: the best seller to the easy spot, and back.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
  };
  const canvas = $("shop3d");
  if (!canvas || !window.LuyMap) return;
  const ctx = canvas.getContext("2d");
  const LM = window.LuyMap;
  let M = LM.data();

  const money = (v) => "$" + (Math.round(v * 100) / 100).toFixed(2);
  const short = (iso) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(2, 4)}` : "");
  const nth = (n) => n + (["th", "st", "nd", "rd"][(n % 100 >= 11 && n % 100 <= 13) ? 0 : n % 10] || "th");
  const prod = (id) => M.products[id] || { name: "?" };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  // --- what the colours say ---------------------------------------------------------------------------
  const PALETTES = {
    "Red, yellow, green": ["#a50026", "#d73027", "#f46d43", "#fdae61", "#fee08b", "#ffffbf", "#d9ef8b", "#a6d96a", "#66bd63", "#1a9850", "#006837"],
    Viridis: ["#440154", "#482878", "#3e4989", "#31688e", "#26828e", "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725"],
    Blues: ["#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b"],
    Violets: ["#efedf5", "#dadaeb", "#bcbddc", "#9e9ac8", "#807dba", "#6a51a3", "#54278f", "#3f007d"],
    "Yellow to red": ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c", "#fc4e2a", "#e31a1c", "#bd0026", "#800026"],
  };
  const STATUS = [
    ["expired", "Expired", "#C0392B"], ["soon", "Expires soon", "#EFA027"], ["clear", "Won't sell in time", "#8A5A14"],
    ["sleeping", "Sleeping money", "#534AB7"], ["fine", "Fine", "#6BA34A"], ["none", "None here, or not counted", "#CFCCC2"],
  ];
  const METRICS = {
    status: { label: "What to do", cat: true },
    expiry: { label: "Days to the first expiry", palette: "Red, yellow, green", none: "no date",
              fmt: (d) => (d < 0 ? `expired ${-d} d ago` : d === 0 ? "expires today" : `${d} days to expiry`), rank: 1 },
    units: { label: "Units on the shelf", palette: "Blues", none: "none here", fmt: (v) => `${v} on the shelf`, rank: 1 },
    value: { label: "Money on the shelf, at cost", palette: "Viridis", none: "none here", fmt: (v) => `${money(v)} at cost`, rank: -1 },
    sleeping: { label: "Sleeping money, at cost", palette: "Violets", none: "none here", fmt: (v) => `${money(v)} sleeping`, rank: -1 },
    sold: { label: "Sold in the last 4 weeks", palette: "Viridis", none: "not counted yet", fmt: (v) => `${v} sold in 4 weeks`, rank: -1 },
    since: { label: "Days since the last sale", palette: "Red, yellow, green", invert: true, none: "never sold",
             fmt: (d) => (d === 0 ? "sold today" : `${d} days since a sale`), rank: -1 },
  };
  let metric = METRICS[store.get("luydek-3d-metric")] ? store.get("luydek-3d-metric") : "expiry";
  let palette = METRICS[metric].palette || "Red, yellow, green";
  let invert = !!METRICS[metric].invert;
  let spread = "linear";
  let photos = store.get("luydek-3d-photos") === "1";
  let showMove = true;

  const sleepValue = (s) => s.batches.filter((b) => b.status.includes("sleeping")).reduce((t, b) => t + b.value, 0);
  function statusOf(s) {
    if (s.out) return "out";
    if (!s.batches.length) return "none";
    if (s.batches.some((b) => b.status.includes("expired"))) return "expired";
    if (s.batches.some((b) => b.status.includes("expiring"))) return "soon";
    if (s.batches.some((b) => b.status.includes("clear"))) return "clear";
    if (s.batches.some((b) => b.status.includes("sleeping"))) return "sleeping";
    return "fine";
  }
  function valueOf(s) {
    const pr = prod(s.product);
    const here = s.batches.length > 0;
    switch (metric) {
      case "expiry": { const f = LM.firstDate(s); return f ? f.days_left : null; }
      case "units": return s.out ? 0 : here ? s.batches.reduce((t, b) => t + b.left, 0) : null;
      case "value": return s.out ? 0 : here ? Math.round(s.batches.reduce((t, b) => t + b.value, 0) * 100) / 100 : null;
      case "sleeping": return s.out ? 0 : here ? Math.round(sleepValue(s) * 100) / 100 : null;
      case "sold": return pr.tracked ? pr.sold_28d : null;
      case "since": return pr.since_sale ?? null;
      default: return null;
    }
  }
  const SPREAD = { linear: [(t) => t, (t) => t], sqrt: [Math.sqrt, (t) => t * t],
                   log: [(t) => Math.log1p(9 * t) / Math.log(10), (t) => (10 ** t - 1) / 9] };
  const rgb = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  function along(stops, t) {
    t = clamp(t, 0, 1);
    const n = stops.length - 1, i = Math.min(n - 1, Math.floor(t * n)), k = t * n - i;
    const a = rgb(stops[i]), b = rgb(stops[i + 1]);
    return a.map((v, j) => Math.round(v + (b[j] - v) * k));
  }
  let domain = [0, 1];
  function colourOf(s) {     // [r, g, b], or null: drawn as a gap
    if (s.out) return null;
    if (METRICS[metric].cat) return rgb(STATUS.find((x) => x[0] === statusOf(s))[2]);
    const v = valueOf(s);
    if (v == null) return rgb("#CFCCC2");
    const t = SPREAD[spread][0](domain[1] > domain[0] ? (v - domain[0]) / (domain[1] - domain[0]) : 0.5);
    return along(PALETTES[palette], invert ? 1 - t : t);
  }
  const css = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const shade = (c, k) => c.map((v) => Math.round(k >= 0 ? v + (255 - v) * k : v * (1 + k)));

  // --- the shop as boxes ---------------------------------------------------------------------------------
  const KIND = {
    shelf: { frame: [143, 126, 102], board: [185, 165, 138], back: [236, 230, 216] },
    fridge: { frame: [92, 110, 128], board: [201, 215, 227], back: [226, 236, 244] },
    counter: { frame: [122, 90, 58], board: [164, 124, 82], back: null },
    storage: { frame: [111, 109, 102], board: [169, 167, 159], back: null },
    other: { frame: [143, 126, 102], board: [185, 165, 138], back: [236, 230, 216] },
    pallet: { frame: [160, 130, 90], board: [196, 166, 120], back: null },
  };
  const TURN = [[1, 0], [0, 1], [-1, 0], [0, -1]];   // (cos, sin) of each quarter turn
  let draft = null;          // arrange mode: {placeId: {x, z, turn}}
  let arranging = false, picked = null;

  function floorOf(p) { return { ...p.floor, ...((draft && draft[p.id]) || {}) }; }

  // A rack: its parts in its own frame (x along its width, z out of its front), and its products.
  function rack(p) {
    const f = floorOf(p), K = KIND[p.kind] || KIND.shelf, W = f.w, D = f.d, H = f.h, n = p.levels;
    const sh = LM.shelf(p.id), U = M.floor.facing;
    const parts = [];
    const add = (box, colour, role, extra = {}) => parts.push({ box, colour, role, ...extra });
    if (p.kind === "counter") {
      add([-W / 2, 0, -D / 2, W / 2, f.boards[n - 1], D / 2], K.board, "slab", { level: n, top: f.boards[n - 1] });
    } else {
      add([-W / 2, 0, -D / 2, W / 2, 0.06, D / 2], K.frame, "slab", { level: n + 1, top: 0.06 });            // plinth
      if (K.back) add([-W / 2, 0.06, -D / 2, W / 2, H, -D / 2 + 0.02], K.back, "back");
      for (const [x, z, role] of [[-W / 2, -D / 2, "back"], [W / 2 - 0.04, -D / 2, "back"], [-W / 2, D / 2 - 0.04, "front"], [W / 2 - 0.04, D / 2 - 0.04, "front"]]) {
        add([x, 0, z, x + 0.04, H, z + 0.04], K.frame, role);
      }
      for (let lv = 1; lv <= n; lv++) {
        const y = f.boards[lv - 1];
        if (y > 0.07) add([-W / 2 + 0.04, y - 0.03, -D / 2 + 0.02, W / 2 - 0.04, y, D / 2 - 0.01], K.board, "slab", { level: lv, top: y });
      }
      if (p.kind === "fridge") add([-W / 2, H - 0.06, -D / 2, W / 2, H, D / 2], K.frame, "slab", { level: 0, top: H });
    }
    const spots = [];
    (sh.levels || []).forEach((row, i) => {
      const lv = i + 1, y = f.boards[i], ph = p.kind === "counter" ? 0.26 : Math.min(f.lh * 0.78, 0.36);
      let x = -W / 2 + 0.05;
      row.forEach((s, j) => {
        const fw = (s.width || 1) * U;
        const part = { box: [x + 0.015, y, -D / 2 + 0.07, x + fw - 0.015, y + ph, D / 2 - 0.05], role: "product", level: lv,
                       spot: s, index: j, place: p.id };
        parts.push(part); spots.push(part);
        x += fw;
      });
    });
    let cx = -W / 2 + 0.04;
    for (const s of sh.loose || []) {                       // on this shelf, on no level: a crate in front of it
      const part = { box: [cx, 0, D / 2 + 0.12, cx + 0.2, 0.2, D / 2 + 0.38], role: "crate", spot: s, place: p.id, level: null };
      parts.push(part); spots.push(part);
      cx += 0.24;
    }
    return { id: p.id, name: p.name, kind: p.kind, p, x: f.x, z: f.z, turn: f.turn || 0, w: W, d: D, h: H, parts, spots };
  }

  // Stock with no place yet: a pallet at the front of the shop.
  function pallet() {
    const sh = LM.shelf("none");
    if (!sh.loose || !sh.loose.length) return null;
    const per = Math.ceil(Math.sqrt(sh.loose.length)), W = Math.max(1.0, per * 0.3 + 0.1), D = Math.max(0.8, Math.ceil(sh.loose.length / per) * 0.3 + 0.1);
    const fb = bounds();
    const parts = [{ box: [-W / 2, 0, -D / 2, W / 2, 0.12, D / 2], colour: KIND.pallet.board, role: "slab", level: 1, top: 0.12 }];
    const spots = [];
    sh.loose.forEach((s, i) => {
      const x = -W / 2 + 0.06 + (i % per) * 0.3, z = -D / 2 + 0.06 + Math.floor(i / per) * 0.3;
      const part = { box: [x, 0.12, z, x + 0.24, 0.34, z + 0.24], role: "product", level: 1, spot: s, index: i, place: "none" };
      parts.push(part); spots.push(part);
    });
    return { id: "none", name: "No place yet", kind: "pallet", x: fb.x0 + M.floor.margin + W / 2, z: fb.z1 - M.floor.margin / 2 - D / 2,
             turn: 0, w: W, d: D, h: 0.34, parts, spots };
  }

  function bounds() {       // the floor: the server's, grown to take in racks being moved
    let { x0, z0, x1, z1 } = M.floor;
    const m = M.floor.margin;
    for (const p of M.places) {
      const f = floorOf(p), [hw, hd] = f.turn % 2 ? [f.d / 2, f.w / 2] : [f.w / 2, f.d / 2];
      x0 = Math.min(x0, f.x - hw - m); x1 = Math.max(x1, f.x + hw + m);
      z0 = Math.min(z0, f.z - hd - m); z1 = Math.max(z1, f.z + hd + m);
    }
    return { x0, z0, x1, z1 };
  }

  const toWorld = (r, b) => {
    const [c, s] = TURN[r.turn % 4];
    const ax = r.x + b[0] * c + b[2] * s, az = r.z - b[0] * s + b[2] * c;
    const bx = r.x + b[3] * c + b[5] * s, bz = r.z - b[3] * s + b[5] * c;
    return [Math.min(ax, bx), b[1], Math.min(az, bz), Math.max(ax, bx), b[4], Math.max(az, bz)];
  };
  const toLocal = (r, p) => {
    const [c, s] = TURN[r.turn % 4], dx = p[0] - r.x, dz = p[2] - r.z;
    return [dx * c - dz * s, p[1], dx * s + dz * c];
  };

  // --- the camera ---------------------------------------------------------------------------------------
  const FOV = 45 * Math.PI / 180;
  const cam = { yaw: -0.45, pitch: 0.68, dist: 14, t: [5, 0.6, 5] };
  let C = null;             // this frame's camera: position, axes, focal length, centre
  let Wpx = 600, Hpx = 400;
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const unit = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
  function setCamera() {
    const cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
    const pos = [cam.t[0] + cam.dist * cp * Math.sin(cam.yaw), cam.t[1] + cam.dist * sp, cam.t[2] + cam.dist * cp * Math.cos(cam.yaw)];
    const f = unit(sub(cam.t, pos)), r = unit([-f[2], 0, f[0]]);
    const u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]];
    C = { pos, f, r, u, F: (Hpx / 2) / Math.tan(FOV / 2), cx: Wpx / 2, cy: Hpx / 2 };
  }
  function project(p) {
    const d = sub(p, C.pos), z = dot(d, C.f);
    if (z < 0.05) return null;
    return [C.cx + C.F * dot(d, C.r) / z, C.cy - C.F * dot(d, C.u) / z, z];
  }
  function floorAt(sx, sy) {   // the point on the floor under a pixel
    const d = unit([C.f[0] + C.r[0] * (sx - C.cx) / C.F - C.u[0] * (sy - C.cy) / C.F,
                    C.f[1] + C.r[1] * (sx - C.cx) / C.F - C.u[1] * (sy - C.cy) / C.F,
                    C.f[2] + C.r[2] * (sx - C.cx) / C.F - C.u[2] * (sy - C.cy) / C.F]);
    if (d[1] > -1e-3) return null;
    const t = -C.pos[1] / d[1];
    return [C.pos[0] + d[0] * t, 0, C.pos[2] + d[2] * t];
  }
  function fit(view = "corner") {   // the whole room in view, filling the frame
    Object.assign(cam, { corner: { yaw: -0.45, pitch: 0.68 }, front: { yaw: 0, pitch: 0.5 }, above: { yaw: 0, pitch: 1.42 } }[view]);
    const b = bounds();
    cam.t = [(b.x0 + b.x1) / 2, 0.4, (b.z0 + b.z1) / 2];
    cam.dist = 2 * Math.hypot(b.x1 - b.x0, b.z1 - b.z0, 2.6);
    const corners = [];
    for (const x of [b.x0, b.x1]) for (const z of [b.z0, b.z1]) for (const y of [0, 2.6]) corners.push([x, y, z]);
    for (let i = 0; i < 8; i++) {
      setCamera();
      const ps = corners.map(project);
      if (ps.some((q) => !q)) { cam.dist *= 1.5; continue; }
      const xs = ps.map((q) => q[0]), ys = ps.map((q) => q[1]);
      const k = Math.max((Math.max(...xs) - Math.min(...xs)) / (Wpx * 0.94), (Math.max(...ys) - Math.min(...ys)) / (Hpx * 0.9));
      cam.dist = clamp(cam.dist * k, 2, 90);
    }
  }

  // --- drawing ------------------------------------------------------------------------------------------
  const FACES = [   // outward normal axis and sign; corners top-left, top-right, bottom-right, bottom-left from outside
    { n: [0, 0, 1], vis: (b, p) => p[2] > b[5], c: (b) => [[b[0], b[4], b[5]], [b[3], b[4], b[5]], [b[3], b[1], b[5]], [b[0], b[1], b[5]]], k: 0 },
    { n: [0, 0, -1], vis: (b, p) => p[2] < b[2], c: (b) => [[b[3], b[4], b[2]], [b[0], b[4], b[2]], [b[0], b[1], b[2]], [b[3], b[1], b[2]]], k: 0 },
    { n: [1, 0, 0], vis: (b, p) => p[0] > b[3], c: (b) => [[b[3], b[4], b[5]], [b[3], b[4], b[2]], [b[3], b[1], b[2]], [b[3], b[1], b[5]]], k: -0.14 },
    { n: [-1, 0, 0], vis: (b, p) => p[0] < b[0], c: (b) => [[b[0], b[4], b[2]], [b[0], b[4], b[5]], [b[0], b[1], b[5]], [b[0], b[1], b[2]]], k: -0.14 },
    { n: [0, 1, 0], vis: (b, p) => p[1] > b[4], c: (b) => [[b[0], b[4], b[2]], [b[3], b[4], b[2]], [b[3], b[4], b[5]], [b[0], b[4], b[5]]], k: 0.2 },
    { n: [0, -1, 0], vis: (b, p) => p[1] < b[1], c: (b) => [[b[0], b[1], b[5]], [b[3], b[1], b[5]], [b[3], b[1], b[2]], [b[0], b[1], b[2]]], k: -0.35 },
  ];
  const images = {};
  function image(gid) {
    if (!images[gid]) {
      const im = new Image();
      im.onload = () => draw();
      im.src = `/thumb/${gid}.jpg`;
      images[gid] = im;
    }
    return images[gid].complete && images[gid].naturalWidth ? images[gid] : null;
  }
  const rr = (x, y, w, h, r) => { ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h); };
  function poly(pts) { ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]); for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]); ctx.closePath(); }

  // One box: its faces turned to the camera, shaded. Returns them on screen (for tapping and outlines).
  function box(wb, colour, { alpha = 1, edge = "rgba(255,255,255,.35)", gap = false, front = null, photo = null } = {}) {
    const out = [];
    for (const F of FACES) {
      if (!F.vis(wb, C.pos)) continue;
      const pts = F.c(wb).map(project);
      if (pts.some((q) => !q)) continue;
      poly(pts);
      if (gap) {
        ctx.fillStyle = "rgba(255,255,255,.35)"; ctx.fill();
        ctx.setLineDash([4, 3]); ctx.strokeStyle = "#B42318"; ctx.lineWidth = 1.4; ctx.stroke(); ctx.setLineDash([]);
      } else {
        ctx.fillStyle = css(shade(colour, F.k), alpha); ctx.fill();
        if (edge) { ctx.strokeStyle = edge; ctx.lineWidth = 0.8; ctx.stroke(); }
        if (photo && front && F.n[0] === front[0] && F.n[2] === front[1] && alpha > 0.5) picture(pts, wb, photo);
      }
      out.push(pts);
    }
    return out;
  }
  function picture(pts, wb, im) {   // the product's photo on its front, inside a frame of its colour
    const [tl, tr, , bl] = pts;
    if (Math.hypot(tr[0] - tl[0], tr[1] - tl[1]) < 16) return;
    const fw = Math.max(wb[3] - wb[0], wb[5] - wb[2]), fh = wb[4] - wb[1];
    const ia = im.naturalWidth / im.naturalHeight, fa = fw / fh;
    let w = 0.84, h = 0.84;
    if (ia > fa) h = (w * fa) / ia; else w = (h * ia) / fa;
    ctx.save();
    poly(pts); ctx.clip();
    ctx.transform(tr[0] - tl[0], tr[1] - tl[1], bl[0] - tl[0], bl[1] - tl[1], tl[0], tl[1]);
    ctx.drawImage(im, (1 - w) / 2, (1 - h) / 2 + 0.04, w, h);
    ctx.restore();
  }

  // Racks far to near (separated along x or z: the one on the camera's side is nearer).
  function behind(a, b) {
    const A = a.wb, B = b.wb, p = C.pos;
    if (A[3] <= B[0]) return p[0] > B[0];
    if (A[0] >= B[3]) return p[0] < B[3];
    if (A[5] <= B[2]) return p[2] > B[2];
    if (A[2] >= B[5]) return p[2] < B[5];
    return null;
  }
  function sortRacks(list) {
    for (const r of list) {
      r.wb = toWorld(r, [-r.w / 2, 0, -r.d / 2 - 0.02, r.w / 2, r.h, r.d / 2 + (r.spots.some((s) => s.role === "crate") ? 0.4 : 0)]);
      r.depth = -Math.hypot(C.pos[0] - (r.wb[0] + r.wb[3]) / 2, C.pos[2] - (r.wb[2] + r.wb[5]) / 2);
    }
    list.sort((a, b) => a.depth - b.depth);
    for (let pass = 0; pass < list.length; pass++) {
      let moved = false;
      for (let i = 0; i < list.length - 1; i++) {
        if (behind(list[i + 1], list[i]) === true) { [list[i], list[i + 1]] = [list[i + 1], list[i]]; moved = true; }
      }
      if (!moved) break;
    }
    return list;
  }
  // Inside a rack: the back away from the camera first; levels away from the eye first; products outside in.
  function partsInOrder(r) {
    const lc = toLocal(r, C.pos);
    const role = (x) => r.parts.filter((q) => q.role === x);
    const first = [], last = [];
    (lc[2] >= -r.d / 2 ? first : last).push(...role("back"));
    const slabs = {};
    for (const q of r.parts) {
      if (q.role === "slab") (slabs[q.level] = slabs[q.level] || { level: q.level, top: q.top, items: [] }).board = q;
      if (q.role === "product") (slabs[q.level] = slabs[q.level] || { level: q.level, top: q.box[1], items: [] }).items.push(q);
    }
    const list = Object.values(slabs);
    const below = list.filter((s) => s.top < lc[1]).sort((a, b) => a.top - b.top);
    const above = list.filter((s) => s.top >= lc[1]).sort((a, b) => b.top - a.top);
    const middle = [];
    for (const s of [...below, ...above]) {
      const items = s.items.sort((a, b) => Math.abs((b.box[0] + b.box[3]) / 2 - lc[0]) - Math.abs((a.box[0] + a.box[3]) / 2 - lc[0]));
      if (!s.board) middle.push(...items);
      else if (lc[1] > s.top) middle.push(s.board, ...items);
      else middle.push(...items, s.board);
    }
    const fronts = role("front"), crates = role("crate");
    return [...first, ...(lc[2] > r.d / 2 ? [] : fronts), ...(lc[2] > r.d / 2 ? [] : crates), ...middle,
            ...(lc[2] > r.d / 2 ? fronts : []), ...(lc[2] > r.d / 2 ? crates : []), ...last];
  }

  let hits = [], labels = [], where = {}, hover = null, pinned = null, scene = [];
  const keyOf = (q) => `${q.place}:${q.spot.product}`;

  function draw() {
    if (document.body.dataset.view !== "3d") return;
    M = LM.data();
    setCamera();
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    const sky = ctx.createLinearGradient(0, 0, 0, Hpx);
    sky.addColorStop(0, "#E9E7DF"); sky.addColorStop(1, "#D9DCCB");
    ctx.fillStyle = sky; ctx.fillRect(0, 0, Wpx, Hpx);
    hits = []; labels = []; where = {};
    if (!M.places.length) { message("No shelves yet: add one with New shelf."); return; }
    const racksNow = M.places.map(rack);
    const pal = pallet();
    if (pal) racksNow.push(pal);
    scene = racksNow;
    // measure: the range of the values shown, for the colours and the legend
    const values = racksNow.flatMap((r) => r.spots.map((q) => valueOf(q.spot))).filter((v) => v != null);
    domain = values.length ? [Math.min(...values), Math.max(...values)] : [0, 1];
    if (metric === "expiry") domain = [Math.min(domain[0], 0), Math.min(Math.max(domain[1], 1), 365)];
    floor();
    const looking = LM.looking();
    for (const r of sortRacks(racksNow)) {
      const front = [TURN[r.turn % 4][1], TURN[r.turn % 4][0]];      // the world direction its front faces (x, z)
      for (const q of partsInOrder(r)) {
        const wb = toWorld(r, q.box);
        if (q.role !== "product" && q.role !== "crate") {
          box(wb, q.colour, { edge: "rgba(0,0,0,.18)", alpha: arranging && picked === r.id ? 0.85 : 1 });
          continue;
        }
        const s = q.spot, colour = colourOf(s);
        const dim = looking && !LM.isHit(s);
        const gid = photos ? prod(s.product).photo : null;
        const faces = box(wb, colour || [255, 255, 255], { gap: !colour, alpha: dim ? 0.16 : 1, front,
                                                           photo: gid && !dim ? image(gid) : null });
        q.wb = wb;
        where[`${q.place}:${q.level}:${q.index}`] = q;
        for (const pts of faces) hits.push({ pts, q, r });
      }
      if (arranging) for (const pts of box(r.wb, [0, 0, 0], { alpha: 0, edge: picked === r.id ? "#534AB7" : null })) hits.push({ pts, r, rackOnly: true });
      label(r);
    }
    walls(true);
    for (const target of [hover, pinned]) if (target) outline(target);
    if (showMove && M.move && !arranging) arcs();
  }
  function message(text) {
    ctx.fillStyle = "#5F5E5A"; ctx.font = "600 15px system-ui, sans-serif"; ctx.textAlign = "center";
    ctx.fillText(text, Wpx / 2, Hpx / 2);
  }
  function floor() {
    const b = bounds();
    const corners = [[b.x0, 0, b.z0], [b.x1, 0, b.z0], [b.x1, 0, b.z1], [b.x0, 0, b.z1]].map(project);
    if (corners.every(Boolean)) { poly(corners); ctx.fillStyle = "#F7F5EF"; ctx.fill(); }
    ctx.strokeStyle = "#E3E0D6"; ctx.lineWidth = 1;
    for (let x = Math.ceil(b.x0); x <= b.x1; x++) line([x, 0, b.z0], [x, 0, b.z1]);
    for (let z = Math.ceil(b.z0); z <= b.z1; z++) line([b.x0, 0, z], [b.x1, 0, z]);
    walls(false);
  }
  // The room's outline, as in a warehouse view: the far edges under everything, the near ones over it.
  function walls(near) {
    const b = bounds(), H = 2.6, mid = [(b.x0 + b.x1) / 2, 0, (b.z0 + b.z1) / 2];
    const dm = Math.hypot(...sub(mid, C.pos));
    const corner = (x, z) => [x, 0, z];
    const cs = [corner(b.x0, b.z0), corner(b.x1, b.z0), corner(b.x1, b.z1), corner(b.x0, b.z1)];
    ctx.strokeStyle = near ? "rgba(44,44,42,.45)" : "rgba(44,44,42,.6)"; ctx.lineWidth = 1;
    cs.forEach((c, i) => {
      const n = cs[(i + 1) % 4];
      const isNear = Math.hypot(...sub([(c[0] + n[0]) / 2, 0, (c[2] + n[2]) / 2], C.pos)) < dm;
      if (isNear === near) { line(c, n); ctx.setLineDash([3, 4]); line([c[0], H, c[2]], [n[0], H, n[2]]); ctx.setLineDash([]); }
      const cNear = Math.hypot(...sub(c, C.pos)) < dm;
      if (cNear === near) line(c, [c[0], H, c[2]]);
    });
  }
  function line(a, b) {
    const p = project(a), q = project(b);
    if (!p || !q) return;
    ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
  }
  function label(r) {
    const at = project([r.x, r.h + 0.18, r.z]);
    if (!at) return;
    const size = clamp(C.F * 0.12 / at[2], 10, 14);
    ctx.font = `700 ${size}px system-ui, sans-serif`;
    const w = ctx.measureText(r.name).width + 14, h = size + 9;
    const x = at[0] - w / 2, y = at[1] - h;
    ctx.fillStyle = arranging && picked === r.id ? "#534AB7" : "rgba(38,33,92,.88)";
    rr(x, y, w, h, h / 2); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(r.name, at[0], y + h / 2 + 0.5);
    ctx.textBaseline = "alphabetic";
    labels.push({ x, y, w, h, r });
  }
  function outline(target) {
    const q = Object.values(where).find((x) => keyOf(x) === target);
    if (!q || !q.wb) return;
    for (const F of FACES) {
      if (!F.vis(q.wb, C.pos)) continue;
      const pts = F.c(q.wb).map(project);
      if (pts.some((p) => !p)) continue;
      poly(pts); ctx.strokeStyle = "#534AB7"; ctx.lineWidth = 2.5; ctx.stroke();
    }
  }

  // The suggested move: an arc from the best seller to the easy spot, and one back, a box riding each.
  let animStart = performance.now(), raf = null;
  function arcs() {
    const a = where[`${M.move.from.place}:${M.move.from.level}:${M.move.from.index}`];
    const b = where[`${M.move.to.place}:${M.move.to.level}:${M.move.to.index}`];
    if (!a || !b) return;
    const top = (q) => [(q.wb[0] + q.wb[3]) / 2, q.wb[4], (q.wb[2] + q.wb[5]) / 2];
    const t = ((performance.now() - animStart) / 2600) % 1;
    const lift = Math.max(1.2, Math.hypot(...sub(top(a), top(b))) * 0.35);
    ride(top(a), top(b), lift, colourOf(a.spot) || [83, 74, 183], t);
    ride(top(b), top(a), lift * 0.7, colourOf(b.spot) || [83, 74, 183], (t + 0.5) % 1);
    if (!raf) raf = requestAnimationFrame(() => { raf = null; draw(); });
  }
  function ride(p0, p2, lift, colour, t) {
    const p1 = [(p0[0] + p2[0]) / 2, Math.max(p0[1], p2[1]) + lift, (p0[2] + p2[2]) / 2];
    const at = (u) => [0, 1, 2].map((i) => (1 - u) ** 2 * p0[i] + 2 * (1 - u) * u * p1[i] + u * u * p2[i]);
    const pts = [];
    for (let i = 0; i <= 40; i++) { const q = project(at(i / 40)); if (q) pts.push(q); }
    if (pts.length < 2) return;
    ctx.save();
    ctx.lineWidth = 3; ctx.strokeStyle = css(colour, 0.9); ctx.setLineDash([7, 6]); ctx.lineDashOffset = -t * 60;
    ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]); for (const q of pts.slice(1)) ctx.lineTo(q[0], q[1]); ctx.stroke();
    ctx.setLineDash([]);
    const m = project(at(t));
    if (m) {
      const r = clamp(C.F * 0.07 / m[2], 4, 11);
      ctx.fillStyle = css(colour); ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
      rr(m[0] - r, m[1] - r, r * 2, r * 2, 3); ctx.fill(); ctx.stroke();
    }
    ctx.restore();
  }

  // --- what is under the pointer ------------------------------------------------------------------------
  function inside(pts, x, y) {
    let inn = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      if ((pts[i][1] > y) !== (pts[j][1] > y) && x < ((pts[j][0] - pts[i][0]) * (y - pts[i][1])) / (pts[j][1] - pts[i][1]) + pts[i][0]) inn = !inn;
    }
    return inn;
  }
  function hitAt(x, y) {
    const lab = labels.slice().reverse().find((l) => x >= l.x && x <= l.x + l.w && y >= l.y && y <= l.y + l.h);
    if (lab) return { label: lab.r };
    const h = hits.slice().reverse().find((k) => inside(k.pts, x, y));
    return h || null;
  }
  function placeLine(q) {
    if (q.place === "none") return "No place yet";
    const p = M.places.find((x) => x.id === q.place);
    if (!p) return "?";
    if (q.level == null) return `${p.name} · not on a level yet`;
    const lv = p.levels === 1 ? "" : q.level === 1 ? " · top level" : q.level === p.levels ? " · bottom level" : ` · level ${q.level} of ${p.levels}`;
    const before = p.facings.filter((f) => f.level === q.level);
    const pos = before.findIndex((f) => f.product === q.spot.product) + 1;
    return `${p.name}${lv}${pos ? ` · ${nth(pos)} from the left` : ""}`;
  }
  function valueLine(s) {
    if (s.out) return "Out of stock: a gap on the shelf";
    if (METRICS[metric].cat) return STATUS.find((x) => x[0] === statusOf(s))[1];
    const v = valueOf(s);
    return v == null ? METRICS[metric].none : METRICS[metric].fmt(v);
  }
  function showTip(q, x, y) {
    const tip = $("tip3d");
    if (!q) { tip.classList.add("d-none"); return; }
    const pr = prod(q.spot.product);
    tip.innerHTML = `<div class="fw-semibold">${esc(pr.name)}</div><div class="small">${esc(placeLine(q))}</div>
      <div class="small"><b>${esc(valueLine(q.spot))}</b></div>`;
    tip.classList.remove("d-none");
    const st = $("stage3d").getBoundingClientRect(), tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = `${clamp(x + 14, 4, st.width - tw - 4)}px`;
    tip.style.top = `${clamp(y - th - 10, 4, st.height - th - 4)}px`;
  }

  // --- the panel, the legend, the controls -----------------------------------------------------------------
  function panel() {
    const box3 = $("panel3d");
    const all = scene.flatMap((r) => r.spots);
    let html = "";
    const q = pinned && all.find((x) => keyOf(x) === pinned);
    if (q) {
      const s = q.spot, pr = prod(s.product), first = LM.firstDate(s);
      const units = s.batches.reduce((t, b) => t + b.left, 0);
      html += `<div class="card-header d-flex justify-content-between align-items-start gap-2">
          <div class="d-flex gap-2 align-items-center">${pr.photo ? `<img src="/thumb/${pr.photo}.jpg" alt="" class="map-thumb">` : '<span class="map-thumb"></span>'}
            <div><span class="fw-semibold">${esc(pr.name)}</span><div class="sub"><i class="bi bi-geo-alt-fill" aria-hidden="true"></i> ${esc(placeLine(q))}</div></div></div>
          <button type="button" class="btn-close" data-act="unpin" aria-label="Close"></button></div>
        <ul class="list-group list-group-flush small">
          <li class="list-group-item"><b>${esc(valueLine(s))}</b></li>
          <li class="list-group-item">${s.out ? "None left" : `${units} here`}${first ? ` · first date ${short(first.expiry)}` : ""}</li>
          <li class="list-group-item">${pr.tracked ? `${pr.sold_28d} sold in 4 weeks, ${pr.sold_7d} in the last week` : "Not counted yet"}${pr.since_sale != null ? ` · last sale ${pr.since_sale} d ago` : ""}</li>
        </ul>
        ${q.place !== "none" ? `<div class="card-body pt-2"><button type="button" class="btn btn-sm btn-dark" data-open="${q.place}" data-product="${s.product}">
          <i class="bi bi-bookshelf" aria-hidden="true"></i> See this shelf face on</button></div>` : ""}`;
    } else {
      const m = METRICS[metric];
      let rows;
      if (m.cat) {
        const order = ["expired", "soon", "clear", "sleeping"];
        rows = all.filter((x) => order.includes(statusOf(x.spot)) || x.spot.out)
          .sort((a, b) => (a.spot.out ? 9 : order.indexOf(statusOf(a.spot))) - (b.spot.out ? 9 : order.indexOf(statusOf(b.spot))));
      } else {
        rows = all.filter((x) => valueOf(x.spot) != null).sort((a, b) => m.rank * (valueOf(a.spot) - valueOf(b.spot)));
      }
      const top = rows.slice(0, 6);
      html += `<div class="card-header">${esc(m.label)}<div class="sub">${m.cat ? "What needs doing first" : m.rank > 0 ? "Lowest first" : "Highest first"} ·
          tap one to find it</div></div>
        <ul class="list-group list-group-flush">${top.map((x) => {
          const c = colourOf(x.spot);
          return `<li class="list-group-item map-pick" data-pin="${esc(keyOf(x))}" role="button" tabindex="0">
            <span class="swatch" style="${c ? `background:${css(c)}` : "border:2px dashed #B42318;background:#fff"}"></span>
            <span class="flex-grow-1" style="min-width:0"><span class="fw-semibold">${esc(prod(x.spot.product).name)}</span>
              <span class="d-block small muted">${esc(placeLine(x))}</span></span>
            <span class="small text-nowrap">${esc(valueLine(x.spot))}</span></li>`;
        }).join("") || '<li class="list-group-item small muted">Nothing to show for this yet.</li>'}</ul>`;
    }
    if (M.move) {
      html += `<div class="card-body border-top move-card">
          <div class="fw-semibold mb-1"><i class="bi bi-arrow-left-right" aria-hidden="true"></i> Suggested move</div>
          <div class="small">${esc(M.move.why)}</div>
          <div class="small muted mt-1">From the sales in the ledger${M.simulated ? ", which include simulated history" : ""}.</div>
        </div>`;
    }
    box3.innerHTML = html;
  }
  function legend() {
    const box3 = $("legend3d"), m = METRICS[metric];
    const gap = '<span class="leg"><span class="swatch t-out"></span>Out of stock: a gap</span>';
    if (m.cat) {
      box3.innerHTML = STATUS.map(([, label, c]) => `<span class="leg"><span class="swatch" style="background:${c}"></span>${esc(label)}</span>`).join("") + gap;
      return;
    }
    const stops = Array.from({ length: 11 }, (_, i) => css(along(PALETTES[palette], invert ? 1 - i / 10 : i / 10))).join(",");
    const tick = (t) => {
      const v = domain[0] + (domain[1] - domain[0]) * SPREAD[spread][1](t);
      return m.fmt(metric === "value" || metric === "sleeping" ? Math.round(v * 100) / 100 : Math.round(v)).replace(/ (days to expiry|on the shelf|at cost|sleeping|sold in 4 weeks|days since a sale)$/, "");
    };
    box3.innerHTML = `<span class="leg3d-title">${esc(m.label)}</span>
      <span class="leg3d-bar"><span class="leg3d-grad" style="background:linear-gradient(90deg,${stops})"></span>
        <span class="leg3d-ticks"><span>${esc(tick(0))}</span><span>${esc(tick(0.5))}</span><span>${esc(tick(1))}</span></span></span>
      <span class="leg"><span class="swatch" style="background:#CFCCC2"></span>${esc(m.none)}</span>${gap}`;
  }
  function controls() {
    $("m3-metric").innerHTML = Object.entries(METRICS).map(([k, m]) => `<option value="${k}" ${k === metric ? "selected" : ""}>${esc(m.label)}</option>`).join("");
    $("m3-palette").innerHTML = Object.keys(PALETTES).map((k) => `<option ${k === palette ? "selected" : ""}>${esc(k)}</option>`).join("");
    for (const id of ["m3-palette", "m3-spread", "m3-invert"]) $(id).disabled = !!METRICS[metric].cat;
    $("m3-spread").value = spread;
    $("m3-invert").checked = invert;
    $("m3-photos").checked = photos;
    $("m3-move").checked = showMove;
    $("m3-move").disabled = !M.move;
    $("m3-move").closest(".tool-check").title = M.move ? "" : "No move to suggest: needs sales clear enough to say so";
    $("btn-arrange3d").setAttribute("aria-pressed", arranging);
    $("btn-arrange3d").classList.toggle("active", arranging);
    $("arrange3d").classList.toggle("d-none", !arranging);
    $("btn-arrange3d").closest(".shelf-bar").classList.toggle("d-none", arranging);
    $("a3-turn").disabled = picked == null || picked === "none";
    $("arrange3d-text").textContent = picked != null && picked !== "none"
      ? `${(M.places.find((p) => p.id === picked) || {}).name || ""}: drag it, or turn it.` : "Drag a rack to where it stands in your shop.";
  }
  function refresh() { draw(); panel(); legend(); controls(); }

  // --- pointer, wheel, keys ------------------------------------------------------------------------------
  const pts = new Map();
  let gesture = null;
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId);
    const at = local(e);
    pts.set(e.pointerId, at);
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      gesture = { kind: "pinch", d: Math.hypot(a[0] - b[0], a[1] - b[1]), mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], dist: cam.dist, t: [...cam.t] };
      return;
    }
    const h = hitAt(at[0], at[1]);
    if (arranging && h && (h.r || h.label) && (h.r || h.label).id !== "none") {
      const r = h.r || h.label, f = floorAt(at[0], at[1]), p = M.places.find((x) => x.id === r.id);
      const fl = floorOf(p);
      picked = r.id;
      gesture = { kind: "rack", id: r.id, from: f, x: fl.x, z: fl.z, moved: false, start: at };
      controls(); draw();
      return;
    }
    gesture = { kind: e.button === 2 || e.shiftKey ? "pan" : "orbit", start: at, last: at, moved: false, hit: h };
  });
  canvas.addEventListener("pointermove", (e) => {
    const at = local(e);
    if (pts.has(e.pointerId)) pts.set(e.pointerId, at);
    if (!gesture) {
      const h = hitAt(at[0], at[1]);
      const key = h && h.q ? keyOf(h.q) : null;
      canvas.style.cursor = h ? "pointer" : "grab";
      if (key !== hover) { hover = key; draw(); }
      showTip(h && h.q ? h.q : null, at[0], at[1]);
      return;
    }
    if (gesture.kind === "pinch" && pts.size === 2) {
      const [a, b] = [...pts.values()];
      cam.dist = clamp(gesture.dist * gesture.d / Math.max(1, Math.hypot(a[0] - b[0], a[1] - b[1])), 2, 90);
      const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      pan(mid[0] - gesture.mid[0], mid[1] - gesture.mid[1], gesture.t);
      draw();
      return;
    }
    const dx = at[0] - (gesture.last || gesture.start)[0], dy = at[1] - (gesture.last || gesture.start)[1];
    if (Math.hypot(at[0] - gesture.start[0], at[1] - gesture.start[1]) > 5) gesture.moved = true;
    if (gesture.kind === "rack") {
      const f = floorAt(at[0], at[1]);
      if (!f || !gesture.from) return;
      const snap = (v) => Math.round(v * 20) / 20;
      const cur = floorOf(M.places.find((x) => x.id === gesture.id));
      draft = draft || {};
      draft[gesture.id] = { x: snap(gesture.x + f[0] - gesture.from[0]), z: snap(gesture.z + f[2] - gesture.from[2]), turn: cur.turn || 0 };
      draw();
      return;
    }
    gesture.last = at;
    if (gesture.kind === "orbit") {
      cam.yaw -= dx * 0.008;
      cam.pitch = clamp(cam.pitch + dy * 0.006, 0.08, 1.5);
    } else {
      pan(dx, dy);
    }
    showTip(null);
    draw();
  });
  function pan(dx, dy, from = null) {   // move the shop under the finger, along the floor
    const k = cam.dist / C.F;
    const fx = C.f[0], fz = C.f[2], l = Math.hypot(fx, fz) || 1;
    const t = from || cam.t;
    const mx = -dx * k * C.r[0] + dy * k * (fx / l) / Math.max(0.3, Math.sin(cam.pitch));
    const mz = -dx * k * C.r[2] + dy * k * (fz / l) / Math.max(0.3, Math.sin(cam.pitch));
    cam.t = [t[0] + mx, t[1], t[2] + mz];
  }
  const end = (e) => {
    pts.delete(e.pointerId);
    const g = gesture;
    if (pts.size === 0) gesture = null;
    if (!g || g.kind === "pinch") { if (g && pts.size === 1) gesture = null; return; }
    if (g.kind === "rack") { controls(); return; }
    if (g.moved) return;
    const h = g.hit;
    if (h && h.label && !arranging) { openShelf(h.label.id); return; }
    if (h && h.q) { pinned = keyOf(h.q) === pinned ? null : keyOf(h.q); refresh(); return; }
    if (pinned) { pinned = null; refresh(); }
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("pointerleave", () => { if (!gesture && hover) { hover = null; showTip(null); draw(); } });
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); cam.dist = clamp(cam.dist * Math.exp(e.deltaY * 0.0012), 2, 90); draw(); }, { passive: false });
  canvas.addEventListener("keydown", (e) => {
    const k = { ArrowLeft: () => (cam.yaw += 0.12), ArrowRight: () => (cam.yaw -= 0.12), ArrowUp: () => (cam.pitch = clamp(cam.pitch + 0.08, 0.08, 1.5)),
                ArrowDown: () => (cam.pitch = clamp(cam.pitch - 0.08, 0.08, 1.5)), "+": () => (cam.dist = clamp(cam.dist / 1.2, 2, 90)),
                "-": () => (cam.dist = clamp(cam.dist * 1.2, 2, 90)) }[e.key];
    if (k) { e.preventDefault(); k(); draw(); }
  });
  function local(e) { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

  function openShelf(id, product = null) {
    if (id === "none") { LM.open("none", product); } else { LM.open(id, product); }
    setView("face");
    document.querySelector(".shelf-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  $("panel3d").addEventListener("click", (e) => {
    const t = e.target.closest("[data-pin], [data-open], [data-act]");
    if (!t) return;
    if (t.dataset.act === "unpin") { pinned = null; refresh(); return; }
    if (t.dataset.open) { openShelf(t.dataset.open === "none" ? "none" : +t.dataset.open, +t.dataset.product); return; }
    if (t.dataset.pin) {
      pinned = t.dataset.pin;
      const q = scene.flatMap((r) => r.spots).find((x) => keyOf(x) === pinned);
      if (q && q.wb) { cam.t = [(q.wb[0] + q.wb[3]) / 2, 0.6, (q.wb[2] + q.wb[5]) / 2]; cam.dist = Math.min(cam.dist, 7); }
      refresh();
    }
  });
  $("panel3d").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[role=button]")) { e.preventDefault(); e.target.click(); }
  });
  document.querySelectorAll("[data-cam]").forEach((b) => b.addEventListener("click", () => {
    const v = b.dataset.cam;
    if (v === "in") cam.dist = clamp(cam.dist / 1.3, 2, 90);
    else if (v === "out") cam.dist = clamp(cam.dist * 1.3, 2, 90);
    else fit(v);
    draw();
  }));
  $("m3-metric").onchange = (e) => {
    metric = e.target.value;
    store.set("luydek-3d-metric", metric);
    palette = METRICS[metric].palette || palette; invert = !!METRICS[metric].invert;
    refresh();
  };
  $("m3-palette").onchange = (e) => { palette = e.target.value; refresh(); };
  $("m3-spread").onchange = (e) => { spread = e.target.value; refresh(); };
  $("m3-invert").onchange = (e) => { invert = e.target.checked; refresh(); };
  $("m3-photos").onchange = (e) => { photos = e.target.checked; store.set("luydek-3d-photos", photos ? "1" : "0"); draw(); };
  $("m3-move").onchange = (e) => { showMove = e.target.checked; animStart = performance.now(); draw(); };

  // --- arranging the racks on the floor ----------------------------------------------------------------------
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : r.statusText);
    return data;
  }
  $("btn-arrange3d").onclick = () => { arranging = true; draft = null; picked = null; pinned = null; fit("above"); refresh(); };
  $("a3-cancel").onclick = () => { arranging = false; draft = null; picked = null; refresh(); };
  $("a3-turn").onclick = () => {
    const p = M.places.find((x) => x.id === picked);
    if (!p) return;
    const f = floorOf(p);
    draft = draft || {};
    draft[p.id] = { x: f.x, z: f.z, turn: ((f.turn || 0) + 1) % 4 };
    draw();
  };
  $("a3-save").onclick = async () => {
    try {
      const places = Object.entries(draft || {}).map(([id, f]) => ({ id: +id, ...f }));
      if (places.length) LM.set(await post("/api/map/floor", { places }));
      arranging = false; draft = null; picked = null;
      fit(); refresh();
    } catch (err) { alert(err.message); }
  };
  $("a3-auto").onclick = async () => {
    if (!confirm("Lay every rack out on its own again, by what it is? Where you put them is forgotten.")) return;
    try {
      LM.set(await post("/api/map/floor", { places: M.places.map((p) => ({ id: p.id, auto: true })) }));
      arranging = false; draft = null; picked = null;
      fit(); refresh();
    } catch (err) { alert(err.message); }
  };

  // --- which view -----------------------------------------------------------------------------------------
  let fitted = false;
  function resize() {
    const st = $("stage3d").getBoundingClientRect();
    Wpx = Math.max(200, st.width); Hpx = Math.max(200, st.height);
    canvas.width = Math.round(Wpx * devicePixelRatio); canvas.height = Math.round(Hpx * devicePixelRatio);
    canvas.style.width = `${Wpx}px`; canvas.style.height = `${Hpx}px`;
  }
  function setView(v) {
    document.body.dataset.view = v;
    store.set("luydek-map-view", v);
    document.querySelectorAll(".view-face").forEach((n) => n.classList.toggle("d-none", v === "3d"));
    document.querySelectorAll(".view-3d").forEach((n) => n.classList.toggle("d-none", v !== "3d"));
    document.querySelectorAll("#views [data-view]").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === v);
      b.setAttribute("aria-pressed", b.dataset.view === v);
    });
    const u = new URL(location.href);
    if (v === "3d") u.searchParams.set("view", "3d"); else u.searchParams.delete("view");
    history.replaceState(null, "", u);
    if (v === "3d") {
      resize();
      if (!fitted) { fit(); fitted = true; }
      refresh();
    } else {
      showTip(null);
    }
  }
  $("views").addEventListener("click", (e) => { const b = e.target.closest("[data-view]"); if (b) setView(b.dataset.view); });
  new ResizeObserver(() => { if (document.body.dataset.view === "3d") { resize(); draw(); } }).observe($("stage3d"));
  LM.onChange(() => { M = LM.data(); if (document.body.dataset.view === "3d") refresh(); });
  window.LuyShop3D = { cam, fit, draw, setView, hitAt, scene: () => scene, project: (p) => (setCamera(), project(p)) };   // for checks

  const want = new URL(location.href).searchParams.get("view") || store.get("luydek-map-view") || "face";
  setView(want === "3d" ? "3d" : "face");
})();
