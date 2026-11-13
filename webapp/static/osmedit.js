/* ==========================================================================
 * osmedit.js — OSM 走向编辑器
 *
 * 【底图】只用腾讯地图（合规）。Key 由用户填、存 localStorage，代码不含 Key；
 *         没 Key / SDK 加载不了 → 自动降级为离线自绘 Canvas（不画国界），
 *         降级后**照样能加点 / 拖点 / 删点**（2026-11-09）。
 * 【坐标】库里是 WGS-84；腾讯底图是 GCJ-02，喂给 TMap 前统一 wgs2gcj，
 *         从 TMap 拿回的点击/拖拽点再 gcj2wgs 还原成 WGS-84 存库。
 * 【存储】编辑存 line_geom_edit（草稿，不破坏原几何）；「应用到线路库」才写 line_geom。
 *          **整理台已确认线路**（列表里红名）另外写回 linedesk.db / ld_geom，
 *          不动 ld_line 的确认字段 —— 保存完仍是整理台已确认线路。
 * ========================================================================== */
(() => {
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const r6 = n => Math.round(n * 1e6) / 1e6;

function toast(msg, ms = 2600) {
  const el = $('#oeToast');
  el.textContent = msg; el.classList.remove('hidden');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.add('hidden'), ms);
}
const api = async p => {
  const r = await fetch(p, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};
const postJSON = async (p, body) => {
  const r = await fetch(p, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
  return r.json().catch(() => ({ ok: false, error: '无响应' }));
};

/* ---------------- WGS-84 ↔ GCJ-02 ---------------- */
const GCJ = (() => {
  const PI = Math.PI, A = 6378245.0, EE = 0.00669342162296594323;
  const out = (lng, lat) => lng < 73.66 || lng > 135.05 || lat < 3.86 || lat > 53.55;
  const tLat = (x, y) => {
    let r = -100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    r += (20 * Math.sin(y * PI) + 40 * Math.sin(y / 3 * PI)) * 2 / 3;
    r += (160 * Math.sin(y / 12 * PI) + 320 * Math.sin(y * PI / 30)) * 2 / 3;
    return r;
  };
  const tLng = (x, y) => {
    let r = 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    r += (20 * Math.sin(x * PI) + 40 * Math.sin(x / 3 * PI)) * 2 / 3;
    r += (150 * Math.sin(x / 12 * PI) + 300 * Math.sin(x / 30 * PI)) * 2 / 3;
    return r;
  };
  return (lng, lat) => {
    if (out(lng, lat)) return [lng, lat];
    let dLat = tLat(lng - 105, lat - 35), dLng = tLng(lng - 105, lat - 35);
    const rad = lat / 180 * PI, m = 1 - EE * Math.sin(rad) ** 2, sm = Math.sqrt(m);
    dLat = dLat * 180 / ((A * (1 - EE)) / (m * sm) * PI);
    dLng = dLng * 180 / (A / sm * Math.cos(rad) * PI);
    return [lng + dLng, lat + dLat];
  };
})();
// GCJ → WGS84（两次迭代足够）
function gcj2wgs(lng, lat) {
  let wlng = lng, wlat = lat;
  for (let i = 0; i < 2; i++) {
    const g = GCJ(wlng, wlat);
    wlng += lng - g[0]; wlat += lat - g[1];
  }
  return [wlng, wlat];
}
const toTMap = (lat, lon) => { const [x, y] = GCJ(lon, lat); return new TMap.LatLng(y, x); };

/* ---------------- 状态 ---------------- */
const S = {
  name: '', loadName: '', source: '', hasEdit: false,
  confirmed: false,    // 这条是不是整理台「完成全部确认」的线路（红名）
  geomName: '',        // 确认线路 ① 选的走向来源名（可能 ≠ 规范名）
  ldId: 0,             // 整理台 ld_line.id，保存时把几何绑到它身上
  isNew: false,       // 正在新建一条库里还没有的线
  pts: [],            // [[lon,lat], ...] 各段拼接后的扁平可编辑点
  breaks: [],         // 新段起点在 pts 里的下标；段间不连线（主站本来就是多段）
  links: [],          // [[i,j], ...] 手动「连接两点」的额外直线段，端点引用 pts 下标
  linkMode: false,    // 连接两点模式：依次点两个已有红点
  linkA: -1,          // 已选中的第一个点下标
  shiftDown: false,   // 按住 Shift 点两个红点 = 临时连接两点
  stops: [],          // [{name,lon,lat,km}] 地图上画的车站（= 选中的候选站）
  cand: [],           // 沿线抓站的全部候选
  sel: new Set(),     // 选中的候选站名
  addMode: false,
  drawing: false,     // 正在「按点序画线」：接着往后接，不按最近线段往回插
  showStops: false,   // 只关心走向：站点参考点默认不画
  selPt: -1,          // 当前选中的折线顶点（删除点用）
  map: null, tmap: false, layers: {}, info: null,
  canvas: null, ctx: null, view: null, drag: null, undo: [],
  listQ: '', listOffset: 0, listMore: false, listBusy: false,
};

/* ---------------- 图标 ---------------- */
function markerIcon(color, r, ring = 2) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">` +
    `<circle cx="12" cy="12" r="${r}" fill="${color}" stroke="#fff" stroke-width="${ring}"/></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}
const ICON_V = markerIcon('#d93025', 8, 3);
const ICON_V_SEL = markerIcon('#2563eb', 9, 3);
const ICON_S = markerIcon('#2563eb', 6, 2);

/* ---------------- 地图初始化 ---------------- */
function loadSDK(url) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = url; s.onload = () => res(true); s.onerror = () => rej(new Error('SDK 加载失败'));
    document.head.appendChild(s);
  });
}
async function initMap() {
  const proxy = 'http://127.0.0.1:__WB_HTTP_PORT__/_TMapService/_wbt/__WB_TMAP_SECRET__';
  const hasProxy = !proxy.includes('__WB_');
  const key = (localStorage.getItem('railcn.mapkey') || '').trim();
  let url = '';
  if (hasProxy) {
    window._TMapSecurityConfig = { serviceHost: proxy };
    url = 'https://map.qq.com/api/gljs?v=1.exp';
  } else if (key) {
    url = 'https://map.qq.com/api/gljs?v=1.exp&key=' + encodeURIComponent(key);
  }
  if (!url) return initCanvas('未设置地图 Key · 离线自绘（可编辑）');
  try { await loadSDK(url); } catch (e) { return initCanvas('地图 SDK 加载失败 · 离线自绘（可编辑）'); }
  if (!window.TMap) return initCanvas('地图 SDK 不可用 · 离线自绘（可编辑）');
  try {
    S.map = new TMap.Map('oeMap', { zoom: 5, center: new TMap.LatLng(35.5, 104), viewMode: '2D' });
    S.tmap = true;
    $('#oeMapMode').textContent = '腾讯地图 · GCJ-02';
    buildLayers();
    bindMap();
    S.map.on('zoom', () => { $('#oeZoom').textContent = '缩放 ' + Math.round(S.map.getZoom()); });
  } catch (e) {
    console.error(e);
    return initCanvas('地图初始化失败 · 离线自绘（可编辑）');
  }
}
function buildLayers() {
  S.layers.route = new TMap.MultiPolyline({
    map: S.map,
    styles: { route: new TMap.PolylineStyle({ color: '#d93025', width: 4, lineCap: 'round', borderWidth: 0, opacity: .95 }) },
    geometries: [], zIndex: 5,
  });
  S.layers.vert = new TMap.MultiMarker({
    map: S.map,
    styles: {
      v: new TMap.MarkerStyle({ width: 22, height: 22, anchor: { x: 11, y: 11 }, src: ICON_V }),
      vsel: new TMap.MarkerStyle({ width: 26, height: 26, anchor: { x: 13, y: 13 }, src: ICON_V_SEL }),
    },
    geometries: [], zIndex: 9,
  });
  S.layers.station = new TMap.MultiMarker({
    map: S.map,
    styles: { s: new TMap.MarkerStyle({ width: 16, height: 16, anchor: { x: 8, y: 8 }, src: ICON_S }) },
    geometries: [], zIndex: 7,
  });
  S.info = new TMap.InfoWindow({ map: S.map, position: new TMap.LatLng(35.5, 104), offset: { x: 0, y: -14 } });
  S.info.close();
  // 顶点：点一下选中、拖拽改走向、右键删除；连接模式下点两下 = 直连这两点
  S.layers.vert.on('click', e => {
    S.lastVertClick = Date.now();
    onVertClick(+e.geometry.id.slice(1));
  });
  S.layers.vert.on('drag', e => {
    const i = +e.geometry.id.slice(1);
    const ll = e.geometry.position;
    const [lon, lat] = gcj2wgs(ll.getLng(), ll.getLat());
    if (!S.drag) pushUndo();
    S.drag = { i }; S.selPt = i;
    S.pts[i] = [r6(lon), r6(lat)];
    updateRoutePath();
  });
  S.layers.vert.on('dragend', e => {
    const i = +e.geometry.id.slice(1);
    const ll = e.geometry.position;
    const [lon, lat] = gcj2wgs(ll.getLng(), ll.getLat());
    S.pts[i] = [r6(lon), r6(lat)];
    S.drag = null;
    renderAll(); updateStats();
  });
  S.layers.vert.on('rightclick', e => deletePoint(+e.geometry.id.slice(1)));
  S.layers.station.on('click', e => {
    const i = +e.geometry.id.slice(1);
    const st = S.stops[i]; if (!st) return;
    S.info.setPosition(toTMap(st.lat, st.lon));
    S.info.setContent(`<div class="tm-iw"><b>${esc(st.name)}</b>${st.km != null ? ' · ' + st.km + ' km' : ''}</div>`);
    S.info.open();
  });
}
function bindMap() {
  S.map.on('click', e => {
    // 点红点是「选中 / 连接」，别同时当成「在空白处插点」
    if (Date.now() - (S.lastVertClick || 0) < 300) return;
    if (S.linkMode) { toast('连接两点：请点两个已有的红点'); return; }
    if (!S.addMode) return;
    const ll = e.latLng;
    const [lon, lat] = gcj2wgs(ll.getLng(), ll.getLat());
    insertPoint(lon, lat);
  });
}

/* ---------------- Canvas 离线自绘降级（可编辑） ---------------- */
async function initCanvas(reason) {
  S.tmap = false;
  $('#oeMap').classList.add('hidden');
  S.canvas = $('#oeCanvas'); S.canvas.classList.remove('hidden');
  S.ctx = S.canvas.getContext('2d');
  $('#oeMapMode').textContent = reason || '离线自绘 · 可编辑';
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  bindCanvas();
  drawCanvas();
  toast('离线自绘模式：照样可以加点 / 拖点 / 删点；填地图 Key 可换真实底图', 4400);
}
function resizeCanvas() {
  if (!S.canvas) return;
  const r = S.canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  S.canvas.width = Math.max(1, Math.round(r.width * dpr));
  S.canvas.height = Math.max(1, Math.round(r.height * dpr));
  S.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawCanvas();
}
function drawCanvas() {
  if (!S.ctx) return;
  const cv = S.canvas, ctx = S.ctx, r = cv.getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  const drawStops = (S.isNew || S.showStops) ? S.stops : [];
  const all = S.pts.concat(drawStops.map(s => [s.lon, s.lat]));
  let minx = 73, maxx = 135, miny = 18, maxy = 54;   // 空画布也给个默认全国视野
  if (all.length) {
    minx = 1e9; maxx = -1e9; miny = 1e9; maxy = -1e9;
    for (const [x, y] of all) {
      minx = Math.min(minx, x); maxx = Math.max(maxx, x);
      miny = Math.min(miny, y); maxy = Math.max(maxy, y);
    }
  }
  const pad = 40, w = (maxx - minx) || 1, h = (maxy - miny) || 1;
  const sc = Math.min((r.width - pad * 2) / w, (r.height - pad * 2) / h);
  const cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
  S.view = { sc, cx, cy, w: r.width, h: r.height };  // 点选 / 拖动 / 加点的反算基准
  const px = lon => r.width / 2 + (lon - cx) * sc;
  const py = lat => r.height / 2 - (lat - cy) * sc;
  if (!all.length) return;
  // 折线：按段画，断点处另起一笔 —— 绝不把两段用一条假直线接起来
  ctx.strokeStyle = '#d93025'; ctx.lineWidth = 2;
  splitParts().forEach(seg => {
    ctx.beginPath();
    seg.forEach((p, i) => { const X = px(p[0]), Y = py(p[1]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
    ctx.stroke();
  });
  // 顶点（选中的点放大 + 变蓝）
  S.pts.forEach((p, i) => {
    ctx.beginPath(); ctx.arc(px(p[0]), py(p[1]), i === S.selPt ? 6 : 3, 0, 7);
    ctx.fillStyle = i === S.selPt ? '#2563eb' : '#d93025'; ctx.fill();
  });
  // 站点参考点（默认不画）
  ctx.fillStyle = '#2563eb';
  drawStops.forEach(s => { ctx.beginPath(); ctx.arc(px(s.lon), py(s.lat), 3, 0, 7); ctx.fill(); });
}

/* ---------- 离线自绘模式：同样能加点 / 拖点 / 删点 ---------- */
function canvasPos(e) {
  const r = S.canvas.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}
function canvasXY(lon, lat) {
  const v = S.view;
  return v ? [v.w / 2 + (lon - v.cx) * v.sc, v.h / 2 - (lat - v.cy) * v.sc] : null;
}
function canvasLL(x, y) {
  const v = S.view;
  return v ? [v.cx + (x - v.w / 2) / v.sc, v.cy - (y - v.h / 2) / v.sc] : null;
}
function nearestVert(x, y, tol = 10) {
  let best = -1, bd = tol;
  S.pts.forEach((p, i) => {
    const c = canvasXY(p[0], p[1]); if (!c) return;
    const d = Math.hypot(c[0] - x, c[1] - y);
    if (d < bd) { bd = d; best = i; }
  });
  return best;
}
function bindCanvas() {
  const cv = S.canvas;
  cv.addEventListener('contextmenu', e => {
    e.preventDefault();
    const [x, y] = canvasPos(e);
    const i = nearestVert(x, y, 12);
    if (i >= 0) deletePoint(i);
  });
  cv.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    const [x, y] = canvasPos(e);
    const i = nearestVert(x, y, 10);
    if (i < 0 || S.linkMode || e.shiftKey) return;   // 连接两点时别把点拖跑
    S.selPt = i;
    S.drag = { i, undo: false };
    renderAll();
  });
  cv.addEventListener('mousemove', e => {
    if (!S.drag) return;
    const [x, y] = canvasPos(e);
    const ll = canvasLL(x, y); if (!ll) return;
    if (!S.drag.undo) { pushUndo(); S.drag.undo = true; }
    S.pts[S.drag.i] = [r6(ll[0]), r6(ll[1])];
    renderAll();
  });
  window.addEventListener('mouseup', () => {
    if (!S.drag) return;
    S.drag = null;
    updateStats();
  });
  cv.addEventListener('click', e => {
    const [x, y] = canvasPos(e);
    const i = nearestVert(x, y, 10);
    if (i >= 0) { onVertClick(i, e.shiftKey); return; }
    if (S.linkMode) { toast('连接两点：请点两个已有的红点'); return; }
    if (S.addMode) {
      const ll = canvasLL(x, y);
      if (ll) insertPoint(ll[0], ll[1]);
    } else if (S.selPt >= 0) {
      S.selPt = -1; renderAll(); updateStats();
    }
  });
}

/* ---------------- 多段：断点 ----------------
   主站画的是 MultiPolyline 多段，段与段之间本来就不相连。以前编辑器把
   所有段压成一条 chain，段间补出几十/几百公里的假直线，看着完全不像
   同一条线。现在 pts 是各段拼接后的扁平数组，breaks 记新段起点下标。 */
function isBreak(i) { return S.breaks.indexOf(i) >= 0; }
function splitParts() {
  if (!S.pts.length) return [];
  let out;
  if (!S.breaks.length) out = [S.pts.map(p => p.slice())];
  else {
    out = []; let start = 0;
    for (const b of S.breaks) {
      if (b > start && b <= S.pts.length) { out.push(S.pts.slice(start, b)); start = b; }
    }
    out.push(S.pts.slice(start));
  }
  // 手动「连接两点」补的直线段：端点直接引用 pts 下标，拖点 / 撤销自动跟着走
  for (const [i, j] of S.links)
    if (i >= 0 && j >= 0 && i < S.pts.length && j < S.pts.length && i !== j)
      out.push([S.pts[i].slice(), S.pts[j].slice()]);
  return out.filter(a => a.length >= 2);
}
function shiftBreaks(at, delta) {
  S.breaks = S.breaks.map(b => b >= at ? b + delta : b)
    .filter(b => b > 0 && b < S.pts.length).sort((a, b) => a - b);
}
function shiftLinks(at, delta) {
  S.links = S.links.map(([i, j]) => [i >= at ? i + delta : i, j >= at ? j + delta : j])
    .filter(([i, j]) => i >= 0 && j >= 0 && i < S.pts.length && j < S.pts.length && i !== j);
}

/* ---------------- 几何工具 ---------------- */
function haversineKm(a, b) {
  const R = 6371.0088, rd = Math.PI / 180;
  const dlon = (b[0] - a[0]) * rd, dlat = (b[1] - a[1]) * rd;
  const h = Math.sin(dlat / 2) ** 2 + Math.cos(a[1] * rd) * Math.cos(b[1] * rd) * Math.sin(dlon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}
function segProj(p, a, b) {   // 点到线段：d = 距离（米，等距投影），t = 投影参数 0..1
  const lat0 = a[1] * Math.PI / 180, sx = 111320 * Math.cos(lat0), sy = 110574;
  const xy = o => [ (o[0] - a[0]) * sx, (o[1] - a[1]) * sy ];
  const [px0, py0] = xy(p), [bx, by] = xy(b);
  const l2 = bx * bx + by * by;
  if (l2 === 0) return { d: Math.hypot(px0 - bx, py0 - by), t: 0 };
  const t = Math.max(0, Math.min(1, (px0 * bx + py0 * by) / l2));
  return { d: Math.hypot(px0 - t * bx, py0 - t * by), t };
}
function routeKm() {
  let s = 0;
  for (let i = 1; i < S.pts.length; i++)
    if (!isBreak(i)) s += haversineKm(S.pts[i - 1], S.pts[i]);
  for (const [i, j] of S.links)
    if (S.pts[i] && S.pts[j]) s += haversineKm(S.pts[i], S.pts[j]);
  return s;
}

/* ---------------- 连接两点 ----------------
   用户要的「直接连接原有的 2 点」：依次点两个已有红点，在它们之间画一条直线。
   - 两点正好卡在断点两侧 → 去掉断点，两段合成一段（不再断着）；
   - 其余情况 → 记一条额外的两点直连段，原折线一点不动；再点同一对可断开。
   连线段端点用 pts 下标引用，拖点、插点、删点、撤销都会自动跟着走。 */
function linkPick(i) {
  if (S.linkA < 0) {
    S.linkA = i; S.selPt = i;
    renderAll(); updateStats();
    toast(`已选第 ${i + 1} 点 · 再点另一个红点就把它俩直接连上（Esc 取消）`, 3200);
    return;
  }
  if (i === S.linkA) { toast('这是同一个点，换一个红点'); return; }
  connectPoints(S.linkA, i);
  S.linkA = -1;
  renderAll(); updateStats();
}
function connectPoints(a, b) {
  const lo = Math.min(a, b), hi = Math.max(a, b);
  if (hi === lo + 1 && isBreak(hi)) {          // 断点两侧：两段直接接通
    pushUndo();
    S.breaks = S.breaks.filter(x => x !== hi);
    toast(`已接通第 ${lo + 1} / 第 ${hi + 1} 点：两段合成一段`, 3200);
    return;
  }
  if (hi === lo + 1) { toast('这两点本来就相连，不用再连'); return; }
  const k = S.links.findIndex(([i, j]) =>
    (i === lo && j === hi) || (i === hi && j === lo));
  pushUndo();
  if (k >= 0) {
    S.links.splice(k, 1);
    toast(`已断开第 ${lo + 1} / 第 ${hi + 1} 点的直连`);
  } else {
    S.links.push([lo, hi]);
    toast(`已把第 ${lo + 1} 点和第 ${hi + 1} 点直接连成一条直线`, 3200);
  }
}
function onVertClick(i, shiftHeld) {
  if (S.linkMode || shiftHeld || S.shiftDown) { linkPick(i); return; }
  S.selPt = i;
  renderAll(); updateStats();
}
function exitLinkMode(quiet) {
  S.linkMode = false; S.linkA = -1;
  const btn = $('#oeLink'); if (btn) btn.classList.remove('on');
  if (!quiet) toast('已退出「连接两点」');
  updateStats();
}
function insertPoint(lon, lat) {
  pushUndo();                              // 先记账再改，否则撤销等于没动
  // 画线追加：新建线 / 刚起头（不足 2 点）/ 正在按点序往下画 —— 一律接到末尾。
  // 以前只判「不足 2 点」，画到第 3 点就掉进「最近线段插入」，被插到第 1、2 点
  // 之间，看着像新点直连回第 1 点（2026-11-11）。
  if (S.isNew || S.drawing || S.pts.length < 2) {
    S.pts.push([r6(lon), r6(lat)]);
    S.drawing = true;
    S.selPt = S.pts.length - 1;
  } else {
    let best = 0, bd = 1e18, bt = 0;
    for (let i = 1; i < S.pts.length; i++) {
      if (isBreak(i)) continue;          // 断点两侧没有连线，别往假直线上插点
      const pr = segProj([lon, lat], S.pts[i - 1], S.pts[i]);
      if (pr.d < bd) { bd = pr.d; best = i; bt = pr.t; }
    }
    if (best === S.pts.length - 1 && bt > 0.999) {
      // 点落在线尾的延长线上（t 已夹到 1）→ 接着往后接；
      // 否则会插到倒数第二点前面，画出一条回折线
      S.pts.push([r6(lon), r6(lat)]);
      S.selPt = S.pts.length - 1;
    } else {
      S.pts.splice(best, 0, [r6(lon), r6(lat)]);
      shiftBreaks(best, 1);
      shiftLinks(best, 1);        // 连线段引用的下标整体后移，别连错端点
      S.selPt = best;
    }
  }
  S.linkA = -1;                  // 插点会打乱下标，取消半截连接
  renderAll(); updateStats();
}

/** 删掉一个折线点（右键红点 / 选中后点按钮 / Delete 键都走这里） */
function deletePoint(i) {
  if (i == null || i < 0 || i >= S.pts.length) { toast('先点一个红点选中它'); return; }
  if (S.pts.length <= 2) { toast('折线至少要留 2 个点'); return; }
  pushUndo();
  S.pts.splice(i, 1);
  // 删掉的是段起点时，原段第二点顺位成为新起点（下标仍是 i），其余前移
  S.breaks = S.breaks.map(b => b > i ? b - 1 : b)
    .filter(b => b > 0 && b < S.pts.length);
  // 连到被删点的直线段一起删掉，其余端点下标前移
  S.links = S.links.filter(([a, b]) => a !== i && b !== i)
    .map(([a, b]) => [a > i ? a - 1 : a, b > i ? b - 1 : b]);
  S.selPt = -1; S.linkA = -1;
  renderAll(); updateStats();
}

/* ---------------- 渲染 ---------------- */
function routePaths() {
  // MultiPolyline 的 paths 支持二维（每段一条），断点处自然断开
  return splitParts().map(seg => seg.map(p => toTMap(p[1], p[0])));
}
function updateRoutePath() {
  if (S.tmap) S.layers.route.setGeometries([{ id: 'route', paths: routePaths() }]);
  else drawCanvas();
}
function renderAll() {
  if (S.tmap) {
    S.layers.route.setGeometries([{ id: 'route', paths: routePaths() }]);
    S.layers.vert.setGeometries(
      S.pts.map((p, i) => ({ id: 'v' + i, position: toTMap(p[1], p[0]),
        draggable: true, styleId: i === S.selPt ? 'vsel' : 'v' })));
    const drawStops = (S.isNew || S.showStops) ? S.stops : [];
    S.layers.station.setGeometries(
      drawStops.map((s, i) => ({ id: 's' + i, position: toTMap(s.lat, s.lon) })));
  } else {
    drawCanvas();
  }
}
function fitTo() {
  if (!S.tmap || !S.pts.length) return;
  let minlat = 90, maxlat = -90, minlon = 180, maxlon = -180;
  for (const p of S.pts) { minlat = Math.min(minlat, p[1]); maxlat = Math.max(maxlat, p[1]);
    minlon = Math.min(minlon, p[0]); maxlon = Math.max(maxlon, p[0]); }
  const pad = 0.5;
  const sw = toTMap(minlat - pad, minlon - pad), ne = toTMap(maxlat + pad, maxlon + pad);
  try { S.map.fitBounds(new TMap.LatLngBounds(sw, ne)); } catch (e) { /* ignore */ }
}
function updateStats() {
  const selN = S.isNew
    ? `已选站：<b>${S.sel.size}</b>${S.cand.length ? ' / ' + S.cand.length : ''}<br>`
    : (S.showStops ? `车站参考点：<b>${S.stops.length}</b>` : '站点参考点：已隐藏（只编辑走向）');
  const cf = (!S.isNew && S.confirmed)
    ? `<span class="oe-cf-badge">整理台已确认</span> · 应用后仍是确认线路` +
      (S.geomName && S.geomName !== S.name ? ` · 走向源「${esc(S.geomName)}」` : '') + '<br>'
    : '';
  $('#oeStat').innerHTML =
    `当前线路：<b>${esc(S.name || '—')}</b>${S.isNew ? ' <span class="oe-tag-new">新建</span>' : ''}<br>` +
    cf +
    `来源：<b>${esc(S.isNew ? '地图手绘' : (SRC_CN[S.source] || S.source || '—'))}</b>${S.hasEdit ? ' · 已有草稿' : ''}<br>` +
    `可编辑点数：<b>${S.pts.length}</b> · <b>${Math.max(1, S.breaks.length + 1)}</b> 段` +
    `${S.links.length ? ` + 直连 <b>${S.links.length}</b> 段` : ''}` +
    `${S.drawing ? ' · <b>画线追加</b>' : (S.pts.length >= 2 ? ' · 插点' : '')}` +
    `${S.linkMode ? ` · <b>连接两点</b>${S.linkA >= 0 ? `（已选第 ${S.linkA + 1} 点）` : '（点第一个红点）'}` : ''}` +
    `${S.selPt >= 0 ? ` · 已选中第 <b>${S.selPt + 1}</b> 点` : ''}<br>` +
    `估算里程：<b>${routeKm().toFixed(1)} km</b><br>` +
    selN;
}

/* ---------------- 撤销 ---------------- */
function pushUndo() {
  S.undo.push({ pts: S.pts.map(p => p.slice()), breaks: S.breaks.slice(),
                links: S.links.map(l => l.slice()) });
  if (S.undo.length > 50) S.undo.shift();
}
function undo() {
  if (!S.undo.length) { toast('没有可撤销的操作'); return; }
  const st = S.undo.pop();
  S.pts = Array.isArray(st) ? st : st.pts;      // 兼容老格式
  S.breaks = Array.isArray(st) ? [] : (st.breaks || []);
  S.links = Array.isArray(st) ? [] : (st.links || []).map(l => l.slice());
  S.linkA = -1;
  if (S.selPt >= S.pts.length) S.selPt = -1;
  renderAll(); updateStats();
}

/* ---------------- 列表 / 载入 ---------------- */
const SRC_CN = { line_geom: '线路库', route: '来源包', desk: '整理台',
  osm: 'OSM', edit: '草稿', alias: '借走向', confirmed: '已确认',
  main: '主站走向', 'confirmed-spine': '确认线路' };

/** 所有「有走向」的线路都在列表里；整理台已确认线路红名并排最前。一次 100 条，滚到底自动续 */
async function loadLines(q, append = false) {
  const box = $('#oeList');
  if (!append) { S.listQ = q || ''; S.listOffset = 0; box.innerHTML = ''; }
  if (S.listBusy) return;
  S.listBusy = true;
  try {
    const d = await api('/api/osmedit/lines?q=' + encodeURIComponent(S.listQ) +
      '&limit=100&offset=' + S.listOffset);
    const old = box.querySelector('.oe-more'); if (old) old.remove();
    (d.items || []).forEach(it => {
      const el = document.createElement('div');
      // 整理台已确认线路：线名标红 + 红底「已确认」标签，排在最前
      el.className = 'oe-item' + (it.name === S.name ? ' active' : '') +
        (it.confirmed ? ' confirmed' : '');
      const tags = (it.src || []).map(s =>
        `<span class="tag${s === 'edit' ? ' edit' : (s === 'confirmed' ? ' cf' : '')}">` +
        `${esc(SRC_CN[s] || s)}</span>`).join('');
      const meta = [it.km != null ? it.km + ' km' : '',
        it.alias ? '借「' + esc(it.aliasOf) + '」走向' : '',
        (it.confirmed && it.geomName && it.geomName !== it.name)
          ? '走向源「' + esc(it.geomName) + '」' : ''].filter(Boolean).join(' · ');
      el.innerHTML = `<div class="nm">${esc(it.name)} ${tags}</div><div class="meta">${meta}</div>`;
      el.title = it.confirmed
        ? '整理台已确认线路 · 改完应用会写回整理台，确认状态不变' : '';
      el.onclick = () => selectLine(it, it.confirmed ? it.name : (it.loadName || it.name));
      box.appendChild(el);
    });
    S.listOffset = (d.offset || 0) + (d.items || []).length;
    S.listMore = !!d.hasMore;
    const cnt = $('#oeCount');
    if (cnt) cnt.innerHTML = `有走向 ${d.total} 条` +
      ` · <b class="oe-cf-num">整理台已确认 ${d.confirmed || 0} 条（红名，排最前）</b>` +
      (S.listQ ? ` · 匹配 ${d.count}` : '') + (S.listMore ? ` · 已显示 ${S.listOffset}` : '');
    if (!box.children.length) box.innerHTML = '<div class="oe-hint" style="padding:8px">没有匹配的有走向线路</div>';
    if (S.listMore) {
      const more = document.createElement('div');
      more.className = 'oe-more';
      more.textContent = `继续向下滚动加载更多（还有 ${d.count - S.listOffset} 条）`;
      more.onclick = () => loadLines(S.listQ, true);
      box.appendChild(more);
    }
  } catch (e) { toast('列表加载失败: ' + e.message); }
  finally { S.listBusy = false; }
}

async function selectLine(item, loadName) {
  const name = typeof item === 'string' ? item : ((item && item.name) || '');
  const meta = typeof item === 'string' ? {} : (item || {});
  S.name = name;
  S.loadName = loadName || meta.loadName || name;
  // 确认线路一律按「规范名」载入和保存：后端走 confirmed.geom() 专属通道，
  // 写回也绑到这条线的 ld_geom 上，不再借别人那份几何。
  S.confirmed = !!meta.confirmed;
  S.geomName = meta.geomName || '';
  S.ldId = meta.ldId || 0;
  S.isNew = false;
  S.drawing = false;               // 载入已有线路 = 编辑模式：点空白处按最近线段插入
  S.selPt = -1;
  exitLinkMode(true);              // 换线退出连接模式，别把上一条的半截选择带过去
  S.pts = []; S.breaks = []; S.links = [];  // 换线先清干净，载入失败也别留上一条的断点
  S.cand = []; S.sel = new Set();
  $('#oeNewName').value = '';
  $$('.oe-item').forEach(x => x.classList.remove('active'));
  await loadGeom();
  loadLines(S.listQ);          // 保留当前列表/搜索，不把列表缩成一条
  renderSta();
  setModeUI();
}

/* ---------------- 新建线路 ---------------- */
function setModeUI() {
  const isNew = !!S.isNew;
  $('#oeCreate').classList.toggle('hidden', !isNew);
  $('#oeApply').classList.toggle('hidden', isNew);
  $('#oeStaSec').classList.toggle('hidden', !isNew);
  $('#oeKind').textContent = isNew ? '新建中'
    : (S.confirmed ? '整理台已确认' : (S.name ? '编辑已有' : ''));
  $('#oeMode').textContent = isNew ? '新建线路' : (S.name ? '编辑已有线路' : '—');
  $('#oeSaveHint').innerHTML = isNew
    ? '草稿只存本地编辑版。<b>建线入库</b>才真正写进线路库（走 GeoJSON 导入通道，以你画的走向算里程、车牌站序）；建完就能在「车站/线路」页搜到。'
    : (S.confirmed
      ? '<b class="oe-cf-tip">整理台已确认线路</b>：载入的是主站正在显示的那份多段走向，段间不会补假直线；点「应用到线路库」把编辑后的多段写回整理台 <code>ld_geom</code>，主站「车站/线路」立刻生效，<b>线路仍是已确认状态</b>（站序 / 来源 / 确认时间都不改）。'
      : '草稿只存本地编辑版（不破坏原几何）；「应用到线路库」会写回 line_geom，让贴线 / 列车地图真正采用这份走向。');
  $('#oeStat').classList.toggle('is-new', isNew);
}

function startNew(name) {
  name = (name || '').trim();
  if (!name) { toast('先在上面填一个线名'); $('#oeNewName').focus(); return; }
  if (S.pts.length && S.name && !S.isNew &&
      !confirm(`切换到新线「${name}」？\n当前「${S.name}」未保存的改动会丢掉。`)) return;
  S.isNew = true;
  S.name = name;
  S.loadName = name;
  S.confirmed = false; S.geomName = ''; S.ldId = 0;   // 新建线不是整理台确认线路
  S.source = ''; S.hasEdit = false;
  S.selPt = -1;
  S.drawing = true;                 // 新线就是按点序往后接
  exitLinkMode(true);
  S.pts = []; S.breaks = []; S.links = [];
  S.stops = []; S.cand = []; S.sel = new Set(); S.undo = [];
  S.addMode = true;                 // 离线自绘模式同样能画线
  $('#oeAdd').classList.toggle('on', S.addMode);
  $('#oeQ').value = '';
  $$('.oe-item').forEach(x => x.classList.remove('active'));
  renderSta(); renderAll(); updateStats(); setModeUI();
  if (S.tmap) { try { S.map.setZoomAndCenter(5, new TMap.LatLng(35.5, 104)); } catch (e) { /* ignore */ } }
  toast(`新线「${name}」：在地图上依次点出走向（至少 2 点），再「沿线抓站」→「建线入库」`, 5200);
}

function syncStaMarkers() {
  S.stops = S.cand.filter(s => S.sel.has(s.name));
  renderAll();
}
function renderSta() {
  const box = $('#oeStaList');
  if (!box) return;
  if (!S.cand.length) {
    box.innerHTML = '<div class="oe-sta-empty">还没有站 —— 先画走向，再点「沿线抓站」</div>';
    $('#oeStaN').textContent = '';
    return;
  }
  box.innerHTML = '';
  S.cand.forEach(s => {
    const on = S.sel.has(s.name);
    const row = document.createElement('label');
    row.className = 'oe-sta' + (on ? '' : ' off');
    row.innerHTML = `<input type="checkbox" ${on ? 'checked' : ''}>` +
      `<span class="nm">${esc(s.name)}</span>` +
      `<span class="km">${s.km} km</span>` +
      `<span class="dist">${s.dist} m</span>`;
    row.querySelector('input').onchange = e => {
      if (e.target.checked) S.sel.add(s.name); else S.sel.delete(s.name);
      row.classList.toggle('off', !e.target.checked);
      syncStaMarkers(); updateStats();
    };
    box.appendChild(row);
  });
  $('#oeStaN').textContent = `已选 ${S.sel.size} / ${S.cand.length}`;
}

async function snapStations() {
  if (S.pts.length < 2) { toast('先在地图上画出走向（至少 2 个点）'); return; }
  const radius = +$('#oeRadius').value || 2000;
  const btn = $('#oeSnap');
  btn.disabled = true; btn.textContent = '抓站中…';
  try {
    const r = await postJSON('/api/osmedit/stations', {
      name: S.isNew ? '' : S.name, parts: splitParts(), radius,
    });
    if (!r || r.error) throw new Error((r && r.error) || '无结果');
    S.cand = r.items || [];
    S.sel = new Set(S.cand.map(s => s.name));
    $('#oeStaSec').classList.remove('hidden');
    renderSta(); syncStaMarkers(); updateStats();
    toast(S.cand.length
      ? `沿线认出 ${S.cand.length} 个站（按原始轨道，半径 ${radius / 1000} km）`
      : `这条走向 ${radius / 1000} km 内没找到国铁站 —— 把半径调大，或按名字手动加站`, 4200);
  } catch (e) {
    toast('抓站失败: ' + (e.message || e));
  } finally {
    btn.disabled = false; btn.textContent = '沿线抓站';
  }
}

/* 按名字加一站：坐标回查站库，里程按当前走向投影算 */
async function lookupStation(q) {
  if (!q) return [];
  try { const d = await api('/api/osmedit/stations?q=' + encodeURIComponent(q)); return d.items || []; }
  catch (e) { return []; }
}
function projOnChain(lon, lat) {
  let km = 0, best = { dist: Infinity, km: 0 };
  for (let i = 1; i < S.pts.length; i++) {
    if (isBreak(i)) continue;          // 断点两侧没有连线
    const a = S.pts[i - 1], b = S.pts[i];
    const segKm = haversineKm(a, b);
    const lat0 = a[1] * Math.PI / 180, sx = 111320 * Math.cos(lat0), sy = 110574;
    const px = (lon - a[0]) * sx, py = (lat - a[1]) * sy;
    const bx = (b[0] - a[0]) * sx, by = (b[1] - a[1]) * sy;
    const l2 = bx * bx + by * by;
    const t = l2 ? Math.max(0, Math.min(1, (px * bx + py * by) / l2)) : 0;
    const d = Math.hypot(px - t * bx, py - t * by);
    if (d < best.dist) best = { dist: d, km: km + segKm * t };
    km += segKm;
  }
  return best;
}
async function addStaByName() {
  const q = $('#oeStaAdd').value.trim();
  if (!q) return;
  if (S.pts.length < 2) { toast('先画出走向，才能算这一站在线上的里程'); return; }
  const hits = await lookupStation(q);
  if (!hits.length) { toast(`站库里没找到「${q}」`); return; }
  const st = hits.find(h => h.name === q) || hits[0];
  if (S.sel.has(st.name)) { toast(`「${st.name}」已经在列表里`); return; }
  const p = projOnChain(st.lon, st.lat);
  S.cand.push({ name: st.name, lon: st.lon, lat: st.lat,
    km: Math.round(p.km * 10) / 10, dist: Math.round(p.dist) });
  S.cand.sort((a, b) => a.km - b.km);
  S.sel.add(st.name);
  $('#oeStaAdd').value = '';
  renderSta(); syncStaMarkers(); updateStats();
  toast(`已加「${st.name}」（${Math.round(p.km)} km）`);
}
async function fillStaDatalist(q) {
  const dl = $('#oeStaDl');
  if (!dl || (q || '').length < 1) { if (dl) dl.innerHTML = ''; return; }
  const hits = await lookupStation(q);
  dl.innerHTML = hits.map(h => `<option value="${esc(h.name)}"></option>`).join('');
}

async function createLine() {
  if (!S.isNew) return;
  if (S.pts.length < 2) { toast('走向至少需要 2 个点'); return; }
  const sel = S.cand.filter(s => S.sel.has(s.name));
  if (sel.length < 2) { toast('至少要选 2 个车站（不足就调大抓站半径，或按名字加站）'); return; }
  // 站序只覆盖走向一小段 = 多半漏选了站，先提醒（否则会建出一条几何很长、里程很短的线）
  const spanKm = Math.abs(sel[sel.length - 1].km - sel[0].km);
  const totalKm = routeKm();
  if (totalKm > 1 && spanKm < totalKm * 0.5) {
    const pct = Math.round(spanKm / totalKm * 100);
    if (!confirm(`选中的站只覆盖走向的 ${pct}%（${spanKm.toFixed(0)} / ${totalKm.toFixed(0)} km）。\n` +
                 `是不是漏选了站？仍然建线吗？`)) return;
  }
  const btn = $('#oeCreate');
  btn.disabled = true; btn.textContent = '建线中…';
  const body = { name: S.name, parts: splitParts(), stops: sel };
  try {
    let r = await postJSON('/api/osmedit/new', body);
    if (r && !r.ok && /已经有/.test(r.error || '')) {
      if (!confirm(r.error + '\n\n要用现在这条走向覆盖同名自建线吗？')) return;
      r = await postJSON('/api/osmedit/new', { ...body, replace: true });
    }
    if (r && r.ok) {
      S.isNew = false; S.drawing = false; S.hasEdit = true; S.source = '手绘';
      setModeUI(); updateStats(); loadLines(S.name);
      toast(`已建线「${r.name}」：${r.n} 站 / ${r.totalKm} km，已可在「车站/线路」页搜到`, 5000);
    } else {
      toast('建线失败: ' + ((r && r.error) || '未知错误'), 5000);
    }
  } catch (e) {
    toast('建线失败: ' + (e.message || e));
  } finally {
    btn.disabled = false; btn.textContent = '建线入库';
  }
}
async function loadGeom() {
  if (!S.name) return;
  if (S.isNew) {
    if (!S.hasEdit) { toast('新线还没有草稿 —— 先点「保存到草稿」，再从草稿接着改'); return; }
    if (!confirm('重新载入会回到上次保存的草稿，地图上未保存的改动会丢。继续？')) return;
  }
  const n = +$('#oeN').value || 600;
  // 确认线路按规范名请求：后端用 confirmed.get() 识别后走 merged_geom / OSM
  // 来源名解析，草稿也按规范名读写，不会再出现「存济南线、载入京沪四线」。
  const nm = S.confirmed ? S.name : (S.loadName || S.name);
  try {
    const g = await api('/api/osmedit/geom?name=' + encodeURIComponent(nm) + '&n=' + n);
    // 后端给的就是主站 /api/line/osm 那份多段 parts：拼成扁平 pts + 断点，
    // 段与段之间不连线，画布和腾讯地图看到的形状才和主站一致。
    const parts = (g.parts || []).filter(s => s && s.length >= 2);
    S.pts = []; S.breaks = []; S.links = [];
    exitLinkMode(true);
    S.drawing = false;      // 载入的是既有走向：加点按「最近线段插入」，不往后追加
    for (const seg of parts) {
      if (S.pts.length) S.breaks.push(S.pts.length);
      for (const p of seg) S.pts.push([p[0], p[1]]);
    }
    S.stops = g.stops || [];
    S.source = g.source || '';
    S.hasEdit = !!g.hasEdit;
    S.confirmed = !!g.confirmed;         // 以后端确认白名单为准
    S.ldId = g.ldId || S.ldId;
    S.geomName = g.geomName || S.geomName;
    S.loadName = g.loadName || nm;
    S.selPt = -1;
    S.undo = [];
    renderAll(); fitTo(); updateStats(); setModeUI();
    toast(`已载入 ${S.pts.length} 个点 / ${parts.length} 段（${SRC_CN[S.source] || S.source || '—'}）` +
      (S.confirmed ? ' · 整理台已确认线路，保存后仍是确认线路'
        : (S.loadName !== S.name ? ` · 走向借自「${S.loadName}」` : '')));
  } catch (e) { toast('载入失败: ' + (e.message || e)); }
}

/* ---------------- 保存 ---------------- */
async function save(apply) {
  if (!S.name) { toast('请先选择线路'); return; }
  if (S.pts.length < 2) { toast('折线至少需要 2 个点'); return; }
  toast(apply ? '正在应用到线路库…' : '正在保存草稿…', 1200);
  const r = await postJSON('/api/osmedit/geom', {
    name: S.name, parts: splitParts(), totalKm: null, src: S.source, apply: !!apply,
    confirmed: S.confirmed,
    // 借走向的普通线写回被借那份；确认线路一律写回自己名下的整理台 ld_geom
    target: S.confirmed ? S.name : (S.loadName || S.name),
  });
  if (r && r.ok) {
    S.hasEdit = true;
    if (r.confirmed) S.confirmed = true;      // 后端确认白名单为准
    if (apply && r.missing) {
      toast('草稿已存，但线路库里没有这条线 —— 新建的线要点「建线入库」才会进库', 5000);
    } else if (apply && r.confirmed) {
      toast(`已写回整理台（${r.nPt} 点 / ${r.totalKm} km）· 仍是整理台已确认线路`, 4200);
    } else {
      toast(apply ? `已应用到线路库（${r.nPt} 点 / ${r.totalKm} km）` : `已存草稿（${r.nPt} 点 / ${r.totalKm} km）`);
    }
    loadLines(S.listQ);   // 保留当前搜索，刷新索引（刚保存的草稿 / 确认标记都在）
  } else {
    toast('保存失败: ' + ((r && r.error) || '未知错误'));
  }
}

/* ---------------- Key 弹窗 ---------------- */
function openKey() { $('#oeKeyInput').value = localStorage.getItem('railcn.mapkey') || ''; $('#oeKeyModal').classList.remove('hidden'); }
function closeKey() { $('#oeKeyModal').classList.add('hidden'); }
function saveKey() {
  const v = $('#oeKeyInput').value.trim();
  if (v) localStorage.setItem('railcn.mapkey', v); else localStorage.removeItem('railcn.mapkey');
  closeKey();
  location.reload();
}

/* ---------------- 绑定 UI ---------------- */
function bindUI() {
  let qT = null;
  $('#oeQ').addEventListener('input', e => {
    clearTimeout(qT);
    const v = e.target.value.trim();
    qT = setTimeout(() => loadLines(v), 180);
  });
  $('#oeList').addEventListener('scroll', e => {
    const el = e.target;
    if (S.listMore && !S.listBusy &&
        el.scrollTop + el.clientHeight >= el.scrollHeight - 40) loadLines(S.listQ, true);
  });
  $('#oeN').addEventListener('input', () => { $('#oeNLab').textContent = $('#oeN').value + ' 点'; });
  $('#oeNLab').textContent = $('#oeN').value + ' 点';
  $('#oeResample').onclick = () => loadGeom();
  $('#oeReload').onclick = () => loadGeom();

  // 新建线路
  $('#oeNew').onclick = () => startNew($('#oeNewName').value);
  $('#oeNewName').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); startNew($('#oeNewName').value); }
  });

  // 车站（建线用）
  $('#oeSnap').onclick = snapStations;
  $('#oeStaAddBtn').onclick = addStaByName;
  $('#oeStaAdd').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addStaByName(); }
  });
  let staT = null;
  $('#oeStaAdd').addEventListener('input', e => {
    clearTimeout(staT);
    const v = e.target.value.trim();
    staT = setTimeout(() => fillStaDatalist(v), 220);
  });

  $('#oeAdd').onclick = () => {
    S.addMode = !S.addMode; $('#oeAdd').classList.toggle('on', S.addMode);
    if (S.addMode) exitLinkMode(true);
    toast(S.addMode
      ? (S.isNew || S.drawing
        ? '画线模式：点序就是走向，新点接在末尾；点线尾延长线也往后接'
        : '插点模式：点地图空白处插进最近的线段；点线尾延长线会往后接')
      : '已退出添加点模式');
  };
  // 载入已有线路后也能「接着末尾画」：显式切回按点序追加，不影响默认插点
  $('#oeAppend').onclick = () => {
    S.drawing = true;
    S.addMode = true; $('#oeAdd').classList.add('on');
    exitLinkMode(true);
    toast('接着末尾画：之后点空白处都接在最后一个点后面；想插回中间就重新载入线路', 3600);
    updateStats();
  };
  // 连接两点：点两个已有红点，直接在它们之间连一条直线（断点两侧则合并两段）
  $('#oeLink').onclick = () => {
    if (S.linkMode) { exitLinkMode(); return; }
    S.linkMode = true; S.linkA = -1;
    S.addMode = false; $('#oeAdd').classList.remove('on');
    $('#oeLink').classList.add('on');
    toast('连接两点：依次点两个已有红点 → 直接连成一条直线；再点同一对可断开，Esc 退出', 4200);
    updateStats();
  };
  $('#oeDel').onclick = () => deletePoint(S.selPt);
  $('#oeShowSta').onchange = e => {
    S.showStops = !!e.target.checked;
    renderAll(); updateStats();
  };
  $('#oeUndo').onclick = undo;
  $('#oeClear').onclick = () => {
    if (!S.pts.length) return;
    if (confirm('确定清空当前折线？')) {
      pushUndo(); S.pts = []; S.breaks = []; S.links = []; S.selPt = -1;
      exitLinkMode(true);
      if (S.isNew) { S.cand = []; S.sel = new Set(); S.stops = []; renderSta(); }
      renderAll(); updateStats();
    }
  };
  $('#oeSave').onclick = () => save(false);
  $('#oeCreate').onclick = createLine;
  $('#oeApply').onclick = () => {
    const t = (S.loadName && S.loadName !== S.name) ? `「${S.loadName}」` : '这条线';
    const msg = S.confirmed
      ? `把「${S.name}」编辑后的多段走向写回整理台？\n\n` +
        '主站「车站/线路」会立刻改用这条折线；ld_line 的确认状态 / 站序 / 来源 / 确认时间都不变，' +
        '保存后仍是整理台已确认线路。'
      : `应用到线路库会覆盖${t}在 line_geom / 整理台里的几何（影响贴线 / 列车地图）。继续？`;
    if (!confirm(msg)) return;
    save(true);
  };
  $('#oeKey').onclick = openKey;
  $('#oeKeyCancel').onclick = closeKey;
  $('#oeKeySave').onclick = saveKey;
  document.addEventListener('keydown', e => {
    if (e.key === 'Shift') S.shiftDown = true;
    if (e.key === 'Escape' && (S.linkMode || S.linkA >= 0)) {
      e.preventDefault(); exitLinkMode(); return;
    }
    const tag = ((e.target && e.target.tagName) || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (S.selPt >= 0) { e.preventDefault(); deletePoint(S.selPt); }
    }
  });
  document.addEventListener('keyup', e => { if (e.key === 'Shift') S.shiftDown = false; });
  window.addEventListener('blur', () => { S.shiftDown = false; });
}

/* ---------------- 启动 ---------------- */
(async function boot() {
  bindUI();
  setModeUI();
  await initMap();
  // 数据校对页「编辑器」按钮带 ?line= 过来时，直接搜出并载入这条线
  const want = new URLSearchParams(location.search).get('line') || '';
  if (want) {
    $('#oeQ').value = want;
    await loadLines(want);
    const first = document.querySelector('#oeList .oe-item');
    if (first) first.click();
  } else {
    loadLines('');
  }
  updateStats();
})();
})();
