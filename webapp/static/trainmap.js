/* ==========================================================================
 * trainmap.js — 全路网列车位置地图
 *
 * 【口径】位置由后端按 12306 时刻表推算，不是 GPS。晚点/施工绕行会有偏差。
 * 【底图】只用腾讯地图（合规地图服务）。Key 由用户自己填、存 localStorage，
 *         代码里不含任何 Key；WorkBuddy 预览环境下走官方 _TMapSecurityConfig 代理。
 *         没有 Key / SDK 加载不了 → 自动降级为自绘 Canvas（车站散点，离线可用）。
 * 【坐标】库里是 WGS-84；腾讯底图是 GCJ-02，喂给 TMap 前统一 wgs2gcj。
 * ========================================================================== */
(() => {
'use strict';

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pad = n => String(n).padStart(2, '0');
const fmtDT = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;

// 2026-11-25 车次改号：同一字母在新旧时代含义不同（T 旧=特快 / 新=高速动车组；
// K 旧=快速 / 新=高速动车；N 旧=纯数字车次 / 新=快速普速；新增 V 普通快速、X 超高速）。
// 标签按「当前查看的日期」与切换日比较来选，别用一套标签硬套两代数据
// （docs/renumber-2026.md：禁止用车次字头推断历史数据的车种 / 速度）。
const TM_CUTOFF = '2026-11-25';
const TYPE_KEYS = ['X', 'G', 'T', 'K', 'D', 'C', 'S', 'Z', 'N', 'V', 'Y', '0', 'B', 'other'];
// 旧时代（< 2026-11-25）
const TYPE_LABELS_OLD = {
  X: 'X 字头', G: '高铁 G', T: '特快 T', K: '快速 K', D: '动车 D', C: '城际 C',
  S: '市郊 S', Z: '直达 Z', N: 'N 字头', V: 'V 字头', Y: '旅游 Y', B: 'B 字头',
  '0': '普快/数字', other: '其他',
};
// 新时代（>= 2026-11-25，2026 新版车次代号）
const TYPE_LABELS_NEW = {
  X: '超高速 X', G: '高铁 G', T: '高速动车组 T', K: '高速动车 K', D: '动车 D',
  C: '城际 C', S: '市郊 S', Z: '直达特快 Z', N: '快速普速 N', V: '普通快速 V',
  Y: '旅游 Y', B: 'B 字头', '0': '数字', other: '其他',
};
const tmIsNewEra = () => (S.when || '').slice(0, 10) >= TM_CUTOFF;
const typeLabelOf = v => (tmIsNewEra() ? TYPE_LABELS_NEW : TYPE_LABELS_OLD)[v] || v;
const TYPE_OF = c => {
  const a = (c || '').slice(0, 1).toUpperCase();
  return /[0-9]/.test(a) ? '0' : (TYPE_KEYS.includes(a) ? a : 'other');
};
// 日期跨过切换日时，车种筛选按钮的文字跟着换（选中状态不受影响）
function relabelTypeChips() {
  $$('#tmTypes .chip').forEach(b => { b.textContent = typeLabelOf(b.dataset.v); });
}

/* ---------------- 状态 ---------------- */
const S = {
  items: [], snap: null, seq: 0, busy: false, fitKey: null,
  q: '', from: '', to: '', station: '', types: new Set(), status: 'in',
  inView: false, hideAssumed: false, labelMode: 'auto', sort: 'code',
  when: fmtDT(new Date()), live: true, playing: false,
  selected: null, detail: null, track: null,
  map: null, layers: {}, info: null, tmap: false,
  canvas: null, ctx: null, backbone: [], cv: { cx: 104, cy: 35, ppd: 10 },
  bounds: null, zoom: 5, lastView: '',
  playTimer: null, liveTimer: null, viewTimer: null, debounce: null,
};

function toast(msg, ms = 2600) {
  const el = $('#tmToast');
  el.textContent = msg; el.classList.remove('hidden');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

const api = async p => {
  const r = await fetch(p, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};

/* ---------------- WGS-84 → GCJ-02 ---------------- */
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
const toTMap = (lat, lon) => { const [x, y] = GCJ(lon, lat); return new TMap.LatLng(y, x); };

/* ---------------- 图标 ---------------- */
function icon(color, r = 7, ring = 2) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28">` +
    `<circle cx="14" cy="14" r="${r}" fill="${color}" stroke="#fff" stroke-width="${ring}"/></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}
const ICON_RUN = icon('#e8590c', 6), ICON_STOP = icon('#1f6feb', 6);
const ICON_SEL = icon('#d93025', 9, 3);
const ICON_CL = [
  icon('#8a5cf6', 9), icon('#7c4de0', 10), icon('#6f3fd0', 11), icon('#5f32bf', 12),
];
const clTier = n => n >= 100 ? 3 : n >= 30 ? 2 : n >= 8 ? 1 : 0;

/* ---------------- 地图初始化 ---------------- */
function loadSDK(url) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = url; s.onload = () => res(true); s.onerror = () => rej(new Error('SDK 加载失败'));
    document.head.appendChild(s);
  });
}

async function initMap() {
  // WorkBuddy 运行时会把这两个占位符替换成本地代理；独立跑 8686 服务时不会替换。
  const proxy = 'http://127.0.0.1:__WB_HTTP_PORT__/_TMapService/_wbt/__WB_TMAP_SECRET__';
  const hasProxy = !proxy.includes('__WB_');
  const key = (localStorage.getItem('railcn.mapkey') || '').trim();
  let url = '';
  if (hasProxy) {
    window._TMapSecurityConfig = { serviceHost: proxy };
    url = 'https://map.qq.com/api/gljs?v=1.exp';          // 代理模式：URL 不带 key
  } else if (key) {
    url = 'https://map.qq.com/api/gljs?v=1.exp&key=' + encodeURIComponent(key);
  }
  if (!url) return initCanvas('未设置地图 Key · 自绘铁路网模式');
  try { await loadSDK(url); } catch (e) { return initCanvas('地图 SDK 加载失败 · 自绘铁路网模式'); }
  if (!window.TMap) return initCanvas('地图 SDK 不可用 · 自绘铁路网模式');
  try {
    S.map = new TMap.Map('tmMap', {
      zoom: 5, center: new TMap.LatLng(35.5, 104), viewMode: '2D',
    });
    S.tmap = true;
    $('#tmMapMode').textContent = '腾讯地图 · GCJ-02';
    buildLayers();
    S.viewTimer = setInterval(viewChanged, 900);        // 不依赖 SDK 事件名
  } catch (e) {
    console.error(e);
    return initCanvas('地图初始化失败 · 自绘铁路网模式');
  }
}

function buildLayers() {
  const mk = (id, src, w) => new TMap.MarkerStyle({
    width: w, height: w, anchor: { x: w / 2, y: w / 2 }, src,
  });
  S.layers.marker = new TMap.MultiMarker({
    map: S.map,
    styles: {
      run: mk('run', ICON_RUN, 20), stop: mk('stop', ICON_STOP, 20),
      sel: mk('sel', ICON_SEL, 28),
      cl0: mk('cl0', ICON_CL[0], 24), cl1: mk('cl1', ICON_CL[1], 26),
      cl2: mk('cl2', ICON_CL[2], 28), cl3: mk('cl3', ICON_CL[3], 30),
    },
    geometries: [], zIndex: 8,
  });
  S.layers.label = new TMap.MultiLabel({
    map: S.map,
    styles: {
      t: new TMap.LabelStyle({
        color: '#1a2b45', size: 10, offset: { x: 0, y: -14 },
        alignment: 'center', verticalAlignment: 'bottom',
        backgroundColor: 'rgba(255,255,255,.92)', backgroundPadding: 2,
        borderColor: 'rgba(140,158,180,.5)', borderRadius: 3,
      }),
      c: new TMap.LabelStyle({
        color: '#fff', size: 10, offset: { x: 0, y: 0 },
        alignment: 'center', verticalAlignment: 'middle',
      }),
    },
    geometries: [], zIndex: 9,
  });
  S.layers.track = new TMap.MultiPolyline({
    map: S.map,
    styles: {
      // 红色实线 = 已整理线路的真实走向；橙色细线 = 没几何、只能站间直线
      real: new TMap.PolylineStyle({ color: '#d93025', width: 3.5, lineCap: 'round', borderWidth: 0, opacity: .95 }),
      straight: new TMap.PolylineStyle({ color: '#f0a04b', width: 2, lineCap: 'round', borderWidth: 0, opacity: .85 }),
    },
    geometries: [], zIndex: 5,
  });
  S.info = new TMap.InfoWindow({
    map: S.map, position: new TMap.LatLng(35.5, 104),
    offset: { x: 0, y: -16 }, enableCustom: false,
  });
  S.info.close();
  S.layers.marker.on('click', evt => onMarkerClick(evt.geometry.id));
}

function onMarkerClick(id) {
  const i = Number(id);
  const c = S.clusters && S.clusters[i];
  if (!c) return;
  if (c.n > 1) {
    // 点聚合簇 = 放大拆散
    if (S.tmap) S.map.easeTo({ center: toTMap(c.lat, c.lon), zoom: Math.min(14, S.map.getZoom() + 2) });
    else { S.cv.cx = c.lon; S.cv.cy = c.lat; S.cv.ppd *= 2.2; draw(); }
    return;
  }
  const it = c.one;
  selectTrain(it.code, it.date);
  if (S.tmap) {
    S.info.setPosition(toTMap(it.lat, it.lon));
    S.info.setContent(infoHtml(it));
    S.info.open();
  }
}

function infoHtml(it) {
  return `<div class="tm-iw"><b>${esc(it.code)}</b> <span class="g">${esc(it.from)} → ${esc(it.to)}</span>
    <div class="row"><span>状态</span><span>${it.phase === 'run' ? '运行中' : '停靠中'}</span></div>
    <div class="row"><span>位置</span><span>${esc(it.prev)} → ${esc(it.next)}</span></div>
    <div class="row"><span>区间进度</span><span>${it.pct}% · ${it.speed} km/h</span></div>
    <div class="row"><span>下一站</span><span>${esc(it.next)} ${it.etaMin ? it.etaMin + ' 分钟后' : '（已到）'}</span></div>
    <div class="g">${esc(it.date)} ${esc(it.clock)} · 第 ${it.stopIdx}/${it.nStops} 站${it.assumed ? ' · 推定开行' : ''}</div>
    <div class="g">时刻表推算，非 GPS</div></div>`;
}

/* ---------------- Canvas 降级 ---------------- */
async function initCanvas(reason) {
  S.tmap = false;
  $('#tmMap').classList.add('hidden');
  S.canvas = $('#tmCanvas'); S.canvas.classList.remove('hidden');
  S.ctx = S.canvas.getContext('2d');
  $('#tmMapMode').textContent = reason || '自绘铁路网模式';
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  bindCanvas();
  try {
    const bb = await api('/api/trainmap/backbone');
    S.backbone = bb.points || [];
  } catch (e) { S.backbone = []; }
  draw();
}

let _cvInit = false;
function resizeCanvas() {
  if (!S.canvas) return;
  const r = S.canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  S.canvas.width = Math.max(1, Math.round(r.width * dpr));
  S.canvas.height = Math.max(1, Math.round(r.height * dpr));
  S.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!_cvInit && r.width > 100) {                 // 初次按容器大小铺满中国范围
    S.cv.ppd = Math.max(6, Math.min(r.width / 68, r.height / 40));
    _cvInit = true;
  }
  draw();
}
const cw = () => S.canvas ? S.canvas.getBoundingClientRect().width : 800;
const ch = () => S.canvas ? S.canvas.getBoundingClientRect().height : 600;
const proj = (lon, lat) => [(lon - S.cv.cx) * S.cv.ppd + cw() / 2, (S.cv.cy - lat) * S.cv.ppd + ch() / 2];
const unproj = (x, y) => [S.cv.cx + (x - cw() / 2) / S.cv.ppd, S.cv.cy - (y - ch() / 2) / S.cv.ppd];

function bindCanvas() {
  const cv = S.canvas;
  let drag = null;
  cv.addEventListener('mousedown', e => { drag = { x: e.clientX, y: e.clientY, cx: S.cv.cx, cy: S.cv.cy }; cv.classList.add('drag'); });
  window.addEventListener('mouseup', () => { drag = null; cv.classList.remove('drag'); });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    S.cv.cx = drag.cx - (e.clientX - drag.x) / S.cv.ppd;
    S.cv.cy = drag.cy + (e.clientY - drag.y) / S.cv.ppd;
    draw();
  });
  cv.addEventListener('wheel', e => {
    e.preventDefault();
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const [lo, la] = unproj(mx, my);
    S.cv.ppd = Math.max(4, Math.min(1800, S.cv.ppd * (e.deltaY < 0 ? 1.22 : 1 / 1.22)));
    const [lo2, la2] = unproj(mx, my);
    S.cv.cx += lo - lo2; S.cv.cy += la - la2;
    draw();
  }, { passive: false });
  cv.addEventListener('click', e => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = -1, bd = 14;
    (S.clusters || []).forEach((c, i) => {
      const [x, y] = proj(c.lon, c.lat), d = Math.hypot(x - mx, y - my);
      if (d < bd) { bd = d; best = i; }
    });
    if (best >= 0) onMarkerClick(String(best));
  });
}

/* ---------------- 聚合 ---------------- */
function cluster(items) {
  const deg = S.tmap
    ? Math.max(.008, 32 / Math.pow(2, Math.min(14, S.map.getZoom() || 5)))
    : Math.max(.02, 2600 / S.cv.ppd / 100);
  const g = new Map();
  for (const it of items) {
    const k = `${Math.floor(it.lat / deg)}:${Math.floor(it.lon / deg)}`;
    let c = g.get(k);
    if (!c) { c = { lat: 0, lon: 0, n: 0, one: it }; g.set(k, c); }
    c.lat += it.lat; c.lon += it.lon; c.n++;
  }
  return Array.from(g.values()).map(c => ({
    lat: c.lat / c.n, lon: c.lon / c.n, n: c.n, one: c.one,
  }));
}

function labelText(it, mode) {
  if (mode === 'full') return `${it.code} ${it.from}→${it.to}`;
  if (mode === 'code') return it.code;
  return '';
}

/* ---------------- 绘制 ---------------- */
function draw() {
  const items = visibleItems();
  S.clusters = cluster(items);
  if (S.tmap) drawTMap(); else drawCanvas();
  $('#tmZoom').textContent = S.tmap
    ? `缩放 ${S.map.getZoom().toFixed(0)} · ${S.clusters.length} 个标记`
    : `1° ≈ ${S.cv.ppd.toFixed(0)}px · ${S.clusters.length} 个标记`;
}

function drawTMap() {
  let mode = S.labelMode;
  if (mode === 'auto') mode = S.items.length <= 40 ? 'full' : (S.items.length <= 220 ? 'code' : 'none');
  const mg = [], lg = [];
  S.clusters.forEach((c, i) => {
    const pos = toTMap(c.lat, c.lon);
    if (c.n > 1) {
      mg.push({ id: String(i), styleId: 'cl' + clTier(c.n), position: pos });
      lg.push({ id: 'c' + i, styleId: 'c', position: pos, content: String(c.n) });
    } else {
      const it = c.one;
      const sel = S.selected && S.selected.code === it.code && S.selected.date === it.date;
      mg.push({ id: String(i), styleId: sel ? 'sel' : it.phase, position: pos });
      const txt = labelText(it, mode);
      if (txt) lg.push({ id: 't' + i, styleId: 't', position: pos, content: txt });
    }
  });
  try {
    S.layers.marker.setGeometries(mg);
    S.layers.label.setGeometries(lg);
  } catch (e) { console.error('图层刷新失败', e); }
}

function drawCanvas() {
  const ctx = S.ctx;
  if (!ctx) return;
  const W = cw(), H = ch();
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#f7fafc'; ctx.fillRect(0, 0, W, H);
  // 经纬网格
  ctx.strokeStyle = '#e6edf5'; ctx.lineWidth = 1; ctx.font = '10px sans-serif'; ctx.fillStyle = '#aeb9c8';
  for (let lo = 70; lo <= 140; lo += 10) {
    const [x] = proj(lo, 0); ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillText(lo + '°E', x + 3, 14);
  }
  for (let la = 15; la <= 55; la += 5) {
    const [, y] = proj(0, la); ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillText(la + '°N', 4, y - 3);
  }
  // 车站散点当底图（不画国界）
  ctx.fillStyle = '#cfdceb';
  for (const p of S.backbone) {
    const [x, y] = proj(p[0], p[1]);
    if (x >= 0 && x <= W && y >= 0 && y <= H) ctx.fillRect(x, y, 1.2, 1.2);
  }
  // 选中车的行车路线：已整理线路贴真实走向（实线），其余停站间直线（虚线）
  if (S.track && S.track.pieces) {
    for (const p of S.track.pieces) {
      const pts = p.pts || [];
      if (pts.length < 2) continue;
      ctx.beginPath();
      pts.forEach((q, i) => {
        const [x, y] = proj(q[1], q[0]);       // 后端给的是 [lat, lon]
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      });
      if (p.mode === 'real') {
        ctx.setLineDash([]); ctx.strokeStyle = 'rgba(217,48,37,.92)'; ctx.lineWidth = 2.4;
      } else {
        ctx.setLineDash([5, 4]); ctx.strokeStyle = 'rgba(224,140,50,.9)'; ctx.lineWidth = 1.6;
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
  } else if (S.detail && S.detail.stops) {
    ctx.strokeStyle = 'rgba(217,48,37,.75)'; ctx.lineWidth = 2; ctx.beginPath();
    let first = true;
    for (const s of S.detail.stops) {
      if (s.lat == null) continue;
      const [x, y] = proj(s.lon, s.lat);
      if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  let mode = S.labelMode;
  if (mode === 'auto') mode = S.items.length <= 40 ? 'full' : (S.items.length <= 220 ? 'code' : 'none');
  ctx.font = '10px "Microsoft YaHei",sans-serif';
  for (const c of S.clusters) {
    const [x, y] = proj(c.lon, c.lat);
    if (x < -30 || x > W + 30 || y < -30 || y > H + 30) continue;
    if (c.n > 1) {
      const r = 8 + clTier(c.n) * 2;
      ctx.fillStyle = 'rgba(138,92,246,.88)'; ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.font = 'bold 10px sans-serif'; ctx.fillText(String(c.n), x, y);
    } else {
      const it = c.one;
      const sel = S.selected && S.selected.code === it.code && S.selected.date === it.date;
      ctx.fillStyle = sel ? '#d93025' : (it.phase === 'run' ? '#e8590c' : '#1f6feb');
      ctx.beginPath(); ctx.arc(x, y, sel ? 7 : 4.2, 0, 7); ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.2; ctx.stroke();
      const txt = labelText(it, mode);
      if (txt) {
        ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
        ctx.font = '10px "Microsoft YaHei",sans-serif';
        const w = ctx.measureText(txt).width + 6;
        ctx.fillStyle = 'rgba(255,255,255,.9)'; ctx.fillRect(x - w / 2, y - 16, w, 13);
        ctx.fillStyle = '#1a2b45'; ctx.fillText(txt, x, y - 4);
      }
    }
  }
}

/* ---------------- 取数 ---------------- */
function visibleItems() {
  return S.hideAssumed ? S.items.filter(x => !x.assumed) : S.items;
}

function currentBounds() {
  if (S.tmap) {
    try {
      const b = S.map.getBounds(), sw = b.getSouthWest(), ne = b.getNorthEast();
      // 底图是 GCJ-02，库是 WGS-84，放宽 0.03° 容差避免边缘漏车
      return [sw.getLng() - .03, sw.getLat() - .03, ne.getLng() + .03, ne.getLat() + .03];
    } catch (e) { return null; }
  }
  const [lo1, la2] = unproj(0, 0), [lo2, la1] = unproj(cw(), ch());
  return [lo1, la1, lo2, la2];
}

function query() {
  const p = new URLSearchParams();
  if (!S.live && S.when) p.set('t', S.when);
  if (S.q) p.set('q', S.q);
  if (S.types.size) p.set('types', Array.from(S.types).join(','));
  if (S.status) p.set('status', S.status);
  if (S.from) p.set('from', S.from);
  if (S.to) p.set('to', S.to);
  if (S.station) p.set('station', S.station);
  if (S.inView) {
    const b = currentBounds();
    if (b) p.set('bbox', b.map(x => x.toFixed(3)).join(','));
  }
  p.set('limit', '5000');
  return p.toString();
}

async function refresh(quiet) {
  const seq = ++S.seq;
  const qstr = query();
  // 条件变了（不是时间变了）才自动把视野对准少量结果
  const condKey = qstr.replace(/&?t=[^&]*/, '').replace(/&?limit=\d+/, '');
  const condChanged = condKey !== S.fitKey;
  S.fitKey = condKey;
  if (!quiet) $('#tmStat').textContent = '正在计算…';
  try {
    const d = await api('/api/trainmap/snapshot?' + qstr);
    if (seq !== S.seq) return;
    S.snap = d; S.items = d.items || [];
    renderStat(); renderList(); draw();
    if (condChanged && S.items.length >= 1 && S.items.length <= 8) fitFew(S.items);
    if (S.selected) {
      const still = S.items.find(x => x.code === S.selected.code && x.date === S.selected.date);
      if (!still) { S.selected = null; S.detail = null; $('#tmDetail').classList.add('hidden'); clearTrack(); }
    }
  } catch (e) {
    if (seq === S.seq) { $('#tmStat').textContent = '取数失败：' + e.message; toast('取数失败：' + e.message); }
  }
}

function schedule(ms = 260) { clearTimeout(S.debounce); S.debounce = setTimeout(() => refresh(), ms); }

function fitFew(items) {
  const lat = items.reduce((a, x) => a + x.lat, 0) / items.length;
  const lon = items.reduce((a, x) => a + x.lon, 0) / items.length;
  if (S.tmap) S.map.easeTo({ center: toTMap(lat, lon), zoom: items.length === 1 ? 9 : 7 });
  else { S.cv.cx = lon; S.cv.cy = lat; S.cv.ppd = Math.max(S.cv.ppd, 45); draw(); }
}

/* 把视野对准整条行车路线（默认只对着列车当前位置，全程往往在屏幕外） */
function fitTrack() {
  const path = S.track && S.track.path;
  if (!path || path.length < 2) return;
  let la1 = 90, la2 = -90, lo1 = 180, lo2 = -180;
  for (const [la, lo] of path) {
    if (la < la1) la1 = la;
    if (la > la2) la2 = la;
    if (lo < lo1) lo1 = lo;
    if (lo > lo2) lo2 = lo;
  }
  if (S.tmap) {
    try {
      const b = new TMap.LatLngBounds(new TMap.LatLng(la1, lo1), new TMap.LatLng(la2, lo2));
      S.map.fitBounds(b, { padding: 60 });
      return;
    } catch (e) { /* 老版本 SDK 没有 fitBounds → 退回中心点 + 全国视野 */ }
    try { S.map.easeTo({ center: toTMap((la1 + la2) / 2, (lo1 + lo2) / 2), zoom: 5 }); }
    catch (_) { /* SDK 不可用就什么都不做 */ }
  } else {
    S.cv.cx = (lo1 + lo2) / 2;
    S.cv.cy = (la1 + la2) / 2;
    S.cv.ppd = Math.max(4, Math.min(1800, Math.min(
      cw() / Math.max(1, (lo2 - lo1) * 1.25),
      ch() / Math.max(1, (la2 - la1) * 1.25))));
    draw();
  }
}

function renderStat() {
  relabelTypeChips();          // 查看日期跨过改号切换日时，车种标签跟着换
  const d = S.snap;
  if (!d) return;
  const st = d.stats || {}, sc = d.scope || {};
  $('#tmClock').textContent = (S.live ? '实时 ' : '回放 ') + d.minute;
  $('#tmTimeHint').textContent = S.live
    ? '实时模式：每 30 秒自动刷新一次位置'
    : `回放 ${d.minute}：点「回到实时」恢复跟随本机时钟`;
  $('#tmStat').innerHTML = `
    <div class="tm-kpis">
      <div class="tm-kpi"><b>${d.total}</b><span>在途列车</span></div>
      <div class="tm-kpi"><b>${st.run || 0}</b><span>运行中</span></div>
      <div class="tm-kpi"><b>${st.stop || 0}</b><span>停靠中</span></div>
      <div class="tm-kpi warn"><b>${st.assumed || 0}</b><span>推定开行</span></div>
    </div>
    <div class="tm-line"><span>车次模板 ${sc.templates || 0} · 开行实例 ${sc.instances || 0}</span><span>${d.elapsed}s</span></div>
    <div class="tm-line"><span>覆盖 ${(sc.dates || []).length} 天 · 含跨天长途车</span>
      <span>${d.truncated ? '结果已截断 ' + d.truncated : '缺坐标 ' + (st.noCoord || 0)}</span></div>
    <div class="tm-line"><span>区间内匀速插值，晚点会有偏差</span><span>非 GPS</span></div>`;
}

function sortItems(a) {
  const f = {
    code: (x, y) => x.code.localeCompare(y.code, 'zh', { numeric: true }),
    speed: (x, y) => y.speed - x.speed,
    pct: (x, y) => y.pct - x.pct,
    type: (x, y) => TYPE_OF(x.code).localeCompare(TYPE_OF(y.code)) || x.code.localeCompare(y.code),
  }[S.sort] || (() => 0);
  return a.slice().sort(f);
}

function renderList() {
  const v = sortItems(visibleItems()), MAX = 300;
  $('#tmListCount').textContent = `共 ${v.length} 趟`;
  const el = $('#tmList');
  if (!v.length) { el.innerHTML = '<div class="tm-empty">没有符合条件的在途列车<br>换个时间、放宽车种或取消「只统计视野内」试试</div>'; return; }
  el.innerHTML = v.slice(0, MAX).map(it => {
    const on = S.selected && S.selected.code === it.code && S.selected.date === it.date;
    const pctAll = Math.round((it.stopIdx - 1 + it.pct / 100) / Math.max(1, it.nStops - 1) * 100);
    return `<div class="tm-item ${on ? 'on' : ''}" data-code="${esc(it.code)}" data-date="${esc(it.date)}">
      <div class="r1"><span class="tm-code">${esc(it.code)}</span>
        <span class="tm-od">${esc(it.from)} → ${esc(it.to)}</span>
        <span class="tm-phase ${it.phase}">${it.phase === 'run' ? '运行' : '停靠'}</span></div>
      <div class="r2"><span>${esc(it.prev)} → ${esc(it.next)}</span><span>${it.pct}%</span>
        <span>${it.speed} km/h</span><span>全程 ${pctAll}%</span>${it.assumed ? '<em>推定</em>' : ''}</div>
    </div>`;
  }).join('') + (v.length > MAX ? `<div class="tm-empty">仅列出前 ${MAX} 趟，请用筛选缩小范围</div>` : '');
}

function clearTrack() {
  S.track = null;
  if (S.tmap && S.layers.track) { try { S.layers.track.setGeometries([]); } catch (e) {} }
}

/* 行车路线：一段一段画，真实走向与站间直线用不同线型区分 */
function drawTrack(tr) {
  if (!S.tmap || !S.layers.track) return;
  const geoms = [];
  if (tr && tr.ok) (tr.pieces || []).forEach((p, i) => {
    const pts = p.pts || [];
    if (pts.length < 2) return;
    geoms.push({
      id: 'tk' + i,
      styleId: p.mode === 'real' ? 'real' : 'straight',
      paths: pts.map(q => toTMap(q[0], q[1])),   // [lat, lon] → GCJ-02
    });
  });
  try { S.layers.track.setGeometries(geoms); } catch (e) { console.error('行车路线绘制失败', e); }
}

async function selectTrain(code, date) {
  S.selected = { code, date };
  renderList(); draw();
  const el = $('#tmList .tm-item.on');
  if (el) el.scrollIntoView({ block: 'nearest' });
  try {
    const p = new URLSearchParams({ code, date });
    if (!S.live && S.when) p.set('t', S.when);
    const qs = p.toString();
    const [d, tr] = await Promise.all([
      api('/api/trainmap/train?' + qs),
      api('/api/trainmap/track?' + qs).catch(() => null),
    ]);
    if (S.selected && S.selected.code !== code) return;
    S.detail = d;
    S.track = (tr && tr.ok) ? tr : null;
    renderDetail(d, S.track);
    drawTrack(S.track);
    if (!S.tmap) draw();
  } catch (e) { toast('详情取数失败：' + e.message); }
}

function renderDetail(d, tr) {
  const el = $('#tmDetail');
  el.classList.remove('hidden');
  const pos = d.pos || {};
  const cur = d.stops ? d.stops.findIndex(s => s.name === pos.next) : -1;
  const trHtml = tr ? `<div class="tm-track-note">
    <b>行车路线</b>：实际走向 ${tr.realSegs}/${tr.totalSegs} 段（${Math.round((tr.coverage || 0) * 100)}%）
    <button class="tm-mini" data-act="fittrack" style="float:right">看全程路线</button>
    ${(tr.lines || []).length
      ? `<br>贴线：${esc(tr.lines.slice(0, 4).join('、'))}${tr.lines.length > 4 ? ' 等' : ''}` : ''}
    <br><i>红线 = 已整理线路的真实走向；橙虚线 = 没有走向数据，停站间直线</i>
  </div>` : '';
  el.innerHTML = `<button class="tm-close" title="关闭详情">×</button>
    <h4>${esc(d.code)} <span style="font-size:12px;font-weight:400;color:var(--muted)">${esc(d.from)} → ${esc(d.to)}</span></h4>
    <div class="tm-grid">
      <span>发车 / 终到</span><b>${esc(d.start)} → ${esc(d.end)}</b>
      <span>全程</span><b>${d.nStops} 站 · 约 ${d.totalKm} km</b>
      <span>当前</span><b>${pos.phase === 'run' ? `${esc(pos.prev)} → ${esc(pos.next)}，区间 ${pos.pct}%` : `停靠 ${esc(pos.at || pos.next)}`}</b>
      <span>速度 / 下一站</span><b>${pos.speed || 0} km/h · ${esc(pos.next || '—')}${pos.etaMin ? '（' + pos.etaMin + ' 分钟后）' : ''}</b>
      <span>推算时刻</span><b>${esc(d.clock)} · 时刻表推算，非 GPS</b>
    </div>
    <div class="tm-prog"><i style="width:${Math.round((d.pos ? (pos.stopIdx - 1 + pos.pct / 100) : 0) / Math.max(1, d.nStops - 1) * 100)}%"></i></div>
    ${trHtml}
    <div class="tm-stops">${(d.stops || []).map((s, i) =>
      `<div class="tm-stop ${i === cur ? 'cur' : ''}"><b>${esc(s.arr || '—')}</b><b>${esc(s.dep || '—')}</b><span>${i + 1}. ${esc(s.name)}</span></div>`).join('')}</div>`;
}

/* ---------------- 视野变化 ---------------- */
function viewChanged() {
  if (!S.tmap || !S.map) return;
  let z = 5, c = null, b = null;
  try { z = S.map.getZoom(); c = S.map.getCenter(); b = currentBounds(); } catch (e) { return; }
  const key = `${z.toFixed(1)}|${c ? c.getLat().toFixed(2) + ',' + c.getLng().toFixed(2) : ''}`;
  if (key === S.lastView) return;
  S.lastView = key; S.zoom = z; S.bounds = b;
  draw();                                             // 聚合随缩放重算（不用重新取数）
  if (S.inView) schedule(500);                        // 勾了「只看视野内」才重新取数
}

/* ---------------- UI 绑定 ---------------- */
function bindUI() {
  // 车种
  $('#tmTypes').innerHTML = TYPE_KEYS
    .map(v => `<button class="chip" data-v="${v}">${esc(typeLabelOf(v))}</button>`).join('');
  $$('#tmTypes .chip').forEach(b => b.addEventListener('click', () => {
    b.classList.toggle('on');
    syncTypes(); schedule();
  }));
  // 状态
  $$('#tmStatus .chip').forEach(b => b.addEventListener('click', () => {
    $$('#tmStatus .chip').forEach(x => x.classList.remove('on'));
    b.classList.add('on'); S.status = b.dataset.v; schedule();
  }));
  // 文本条件
  const bindText = (sel, key) => {
    const el = $(sel);
    el.addEventListener('input', () => { S[key] = el.value.trim(); schedule(330); });
    if (key === 'q') el.addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(S.debounce); refresh(); } });
  };
  bindText('#tmQ', 'q'); bindText('#tmFrom', 'from');
  bindText('#tmTo', 'to'); bindText('#tmStation', 'station');
  $('#tmInView').addEventListener('change', e => { S.inView = e.target.checked; schedule(100); });
  $('#tmHideAssumed').addEventListener('change', e => {
    S.hideAssumed = e.target.checked; renderList(); renderStat(); draw();
  });
  $('#tmLabelMode').addEventListener('change', e => { S.labelMode = e.target.value; draw(); });
  $('#tmSort').addEventListener('change', e => { S.sort = e.target.value; renderList(); });
  $('#tmReset').addEventListener('click', () => {
    ['#tmQ', '#tmFrom', '#tmTo', '#tmStation'].forEach(s => $(s).value = '');
    S.q = S.from = S.to = S.station = ''; S.types.clear();
    $$('#tmTypes .chip').forEach(b => b.classList.remove('on'));
    $('#tmInView').checked = false; S.inView = false;
    $('#tmHideAssumed').checked = false; S.hideAssumed = false;
    S.status = 'in';
    $$('#tmStatus .chip').forEach(b => b.classList.toggle('on', b.dataset.v === 'in'));
    schedule(60);
  });
  $('#tmDetail').addEventListener('click', e => {
    if (e.target.closest('[data-act="fittrack"]')) { fitTrack(); return; }
    if (!e.target.closest('.tm-close')) return;
    $('#tmDetail').classList.add('hidden');
    S.selected = null; S.detail = null; clearTrack(); renderList(); draw();
    if (S.info) { try { S.info.close(); } catch (_) {} }
  });
  $('#tmList').addEventListener('click', e => {
    const row = e.target.closest('.tm-item');
    if (!row) return;
    const it = S.items.find(x => x.code === row.dataset.code && x.date === row.dataset.date);
    selectTrain(row.dataset.code, row.dataset.date);
    if (it && S.tmap) {
      S.map.easeTo({ center: toTMap(it.lat, it.lon), zoom: Math.max(8, S.map.getZoom()) });
      S.info.setPosition(toTMap(it.lat, it.lon)); S.info.setContent(infoHtml(it)); S.info.open();
    } else if (it) { S.cv.cx = it.lon; S.cv.cy = it.lat; S.cv.ppd = Math.max(S.cv.ppd, 60); draw(); }
  });

  // 时间
  $('#tmWhen').value = S.when;
  $('#tmWhen').addEventListener('change', e => {
    S.when = e.target.value || fmtDT(new Date()); S.live = false; stopPlay(); schedule(120);
  });
  $('#tmNow').addEventListener('click', () => {
    S.live = true; stopPlay(); S.when = fmtDT(new Date()); $('#tmWhen').value = S.when; schedule(60);
  });
  $$('.tm-timebtns button[data-d]').forEach(b => b.addEventListener('click', () => {
    const d = new Date((S.live ? fmtDT(new Date()) : S.when).replace('T', ' ').replace(/-/g, '/'));
    d.setMinutes(d.getMinutes() + Number(b.dataset.d));
    S.when = fmtDT(d); S.live = false; $('#tmWhen').value = S.when; schedule(80);
  }));
  $('#tmPlay').addEventListener('click', () => S.playing ? stopPlay() : startPlay());

  // 顶栏
  $('#tmReload').addEventListener('click', () => refresh());
  $('#tmRebuild').addEventListener('click', async () => {
    toast('正在重建位置索引（约 8 秒）…');
    try { await api('/api/trainmap/rebuild'); toast('索引已重建'); refresh(); }
    catch (e) { toast('重建失败：' + e.message); }
  });
  $('#tmKey').addEventListener('click', () => {
    $('#tmKeyInput').value = localStorage.getItem('railcn.mapkey') || '';
    $('#tmKeyModal').classList.remove('hidden');
  });
  $('#tmKeyCancel').addEventListener('click', () => $('#tmKeyModal').classList.add('hidden'));
  $('#tmKeySave').addEventListener('click', () => {
    const v = $('#tmKeyInput').value.trim();
    if (v) localStorage.setItem('railcn.mapkey', v); else localStorage.removeItem('railcn.mapkey');
    location.reload();
  });
}

function syncTypes() {
  // 数字车次在后端按首字母 0-9 过滤，这里把「普快/数字」展开成 10 个字符
  const raw = new Set();
  $$('#tmTypes .chip.on').forEach(b => {
    if (b.dataset.v === '0') '0123456789'.split('').forEach(c => raw.add(c));
    else if (b.dataset.v === 'other') raw.add('other');
    else raw.add(b.dataset.v);
  });
  S.types = raw;
}

function startPlay() {
  S.playing = true; $('#tmPlay').textContent = '⏸ 暂停'; $('#tmPlay').classList.add('on');
  S.live = false;
  S.playTimer = setInterval(() => {
    const d = new Date(S.when.replace('T', ' ').replace(/-/g, '/'));
    d.setMinutes(d.getMinutes() + 2);
    S.when = fmtDT(d); $('#tmWhen').value = S.when;
    refresh(true);
  }, 700);
}
function stopPlay() {
  S.playing = false; clearInterval(S.playTimer);
  $('#tmPlay').textContent = '▶ 播放'; $('#tmPlay').classList.remove('on');
}

/* ---------------- 启动 ---------------- */
async function boot() {
  bindUI();
  await initMap();
  refresh();
  S.liveTimer = setInterval(() => {
    if (!S.live || S.playing) return;
    S.when = fmtDT(new Date()); $('#tmWhen').value = S.when;
    refresh(true);
  }, 30000);
  // 时钟单独走，不依赖取数
  setInterval(() => {
    if (S.live) $('#tmClock').textContent = '实时 ' + fmtDT(new Date()).replace('T', ' ').slice(0, 16);
  }, 1000);
}
boot();
})();
