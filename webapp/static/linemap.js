/* 确认线路地图（/linemap.html）
 *
 * 口径：左侧只列整理台「全部确认」的线路（/api/linemap/index），
 *      右侧只画这些线路的多段确认几何（/api/linemap/geom）。
 * 地图：腾讯地图 GL JS；Key 存在 localStorage['railcn.mapkey']，
 *      和主站 / 数据校对 / 走向编辑器共用，代码里不含任何 Key。
 *      WGS-84 库坐标统一 wgs2gcj 后再喂给 TMap。
 *      没有 Key 时降级为自绘 Canvas 示意图：只有网格和线路，
 *      不画国界、不引入任何第三方瓦片。
 */
(() => {
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ---------------- WGS-84 → GCJ-02 ---------------- */
const GCJ = (() => {
  const PI = Math.PI, A = 6378245.0, EE = 0.00669342162296594323;
  const out = (lng, lat) => lng < 73.66 || lng > 135.05 || lat < 3.86 || lat > 53.55;
  function tLat(x, y) {
    let r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    r += (20 * Math.sin(y * PI) + 40 * Math.sin(y / 3 * PI)) * 2 / 3;
    r += (160 * Math.sin(y / 12 * PI) + 320 * Math.sin(y * PI / 30)) * 2 / 3;
    return r;
  }
  function tLng(x, y) {
    let r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    r += (20 * Math.sin(x * PI) + 40 * Math.sin(x / 3 * PI)) * 2 / 3;
    r += (150 * Math.sin(x / 12 * PI) + 300 * Math.sin(x / 30 * PI)) * 2 / 3;
    return r;
  }
  return (lng, lat) => {
    if (out(lng, lat)) return [lng, lat];
    let dLat = tLat(lng - 105, lat - 35), dLng = tLng(lng - 105, lat - 35);
    const rad = lat / 180 * PI, m = 1 - EE * Math.sin(rad) ** 2,
      sm = Math.sqrt(m);
    dLat = (dLat * 180) / ((A * (1 - EE)) / (m * sm) * PI);
    dLng = (dLng * 180) / (A / sm * Math.cos(rad) * PI);
    return [lng + dLng, lat + dLat];
  };
})();
const toTMap = (lon, lat) => { const [x, y] = GCJ(lon, lat); return new TMap.LatLng(y, x); };

/* 黄金角分散的配色，42 条线也能分辨 */
function hsl2hex(h, s, l) {
  s /= 100; l /= 100;
  const k = n => (n + h / 30) % 12,
    a = s * Math.min(l, 1 - l),
    f = n => Math.round(255 * (l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))));
  return '#' + [f(0), f(8), f(4)].map(v => v.toString(16).padStart(2, '0')).join('');
}
const colorOf = i => hsl2hex(Math.round((i * 137.508 + 12) % 360), 72, 42);

function markerIcon(color, r, ring = 2) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">` +
    `<circle cx="10" cy="10" r="${r}" fill="${color}" stroke="#fff" stroke-width="${ring}"/></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}

/* ---------------- 状态 ---------------- */
const S = {
  items: [], sel: new Set(), data: new Map(), color: new Map(),
  mode: 'multi', solo: false, showStops: false, fit: true, detail: 900,
  backend: null, map: null, layers: {}, info: null,
  reqSeq: 0, timer: null, keyLoading: null,
  cv: { zoom: 1, dx: 0, dy: 0, drag: null, bbox: null },
};
const mapKey = () => (localStorage.getItem('railcn.mapkey') || '').trim();

function status(txt) { const el = $('#lmStatus'); if (el) el.textContent = txt; }
function keyBadge(txt, cls) {
  const el = $('#lmKeyState');
  el.textContent = txt; el.className = 'lm-hud-tag' + (cls ? ' ' + cls : '');
}
function sorted() { return S.items.filter(x => S.sel.has(x.name)); }

/* ---------------- 左栏 ---------------- */
function renderList() {
  const kw = ($('#lmQ').value || '').trim().toLowerCase();
  const host = $('#lmList');
  const rows = S.items.filter(x => !kw ||
    [x.name, x.fromSta, x.toSta, x.geomSrc].join(' ').toLowerCase().includes(kw));
  if (!rows.length) { host.innerHTML = '<div class="lm-empty">没有匹配的确认线路</div>'; return; }
  const single = S.mode === 'single';
  host.innerHTML = rows.map(x => {
    const i = S.items.indexOf(x), on = S.sel.has(x.name);
    const km = x.totalKm != null ? Math.round(x.totalKm) + 'km' : (x.nStop || 0) + '站';
    return `<div class="lm-row ${on ? 'on' : 'off'}" data-name="${esc(x.name)}" title="${esc(x.name)} · ${esc(x.fromSta)}→${esc(x.toSta)}">
      <input type="${single ? 'radio' : 'checkbox'}" name="lmsel" ${on ? 'checked' : ''} data-name="${esc(x.name)}">
      <span class="lm-swatch" style="background:${colorOf(i)}"></span>
      <span class="lm-name">${esc(x.name)}</span><span class="lm-meta">${esc(km)}</span>
    </div>`;
  }).join('');
  host.querySelectorAll('.lm-row').forEach(row => {
    const nm = row.dataset.name;
    row.addEventListener('click', e => {
      const isInput = e.target.tagName === 'INPUT';
      if (isInput) return onToggle(nm, e.target.checked, e);
      // 点名字：单选模式 / 点名单选 = 只看这一条；复选模式 = 反选
      if (S.mode === 'single' || S.solo) {
        S.sel = new Set([nm]);
        renderList(); schedule(true);
      } else onToggle(nm, !S.sel.has(nm), e);
    });
  });
}
function onToggle(nm, on, e) {
  if (S.mode === 'single') {
    S.sel = on ? new Set([nm]) : new Set();
  } else if (on) S.sel.add(nm); else S.sel.delete(nm);
  renderList();
  schedule(S.fit && !(e && e.shiftKey));
}
function schedule(doFit) {
  clearTimeout(S.timer);
  S.timer = setTimeout(() => reload(doFit), 130);
}

async function loadIndex() {
  try {
    const d = await (await fetch('/api/linemap/index')).json();
    S.items = d.items || [];
    S.items.forEach((x, i) => S.color.set(x.name, colorOf(i)));
    S.sel = new Set(S.items.map(x => x.name));   // 默认全部选中
    $('#lmTotal').textContent = `整理台已确认 ${S.items.length} 条 · 默认全选`;
    renderList();
    updateFoot();
    await initMap();
    await reload(true);
  } catch (e) {
    status('清单读取失败：' + e.message);
  }
}

/* ---------------- 批量取几何并画 ---------------- */
async function reload(doFit) {
  const names = sorted().map(x => x.name);
  updateFoot();
  if (!names.length) {
    S.data.clear(); draw(); status('没有选中线路'); return;
  }
  const seq = ++S.reqSeq;
  status(`正在载入 ${names.length} 条线路走向…`);
  let d;
  try {
    const u = '/api/linemap/geom?names=' + encodeURIComponent(names.join('|')) +
      '&detail=' + S.detail + '&stops=' + (S.showStops ? 1 : 0);
    d = await (await fetch(u)).json();
  } catch (e) {
    status('走向读取失败：' + e.message); return;
  }
  if (seq !== S.reqSeq) return;                 // 勾选又变了，丢掉过期结果
  S.data = new Map((d.items || []).map(x => [x.name, x]));
  const nPt = [...S.data.values()].reduce((a, x) => a + (x.nPt || 0), 0);
  const nSta = [...S.data.values()].reduce((a, x) => a + ((x.stations || []).length), 0);
  const miss = names.filter(n => !(S.data.get(n) || {}).found);
  draw();
  if (doFit) fitAll();
  status(`已显示 ${S.data.size} 条 · ${nPt.toLocaleString()} 个几何点` +
    (S.showStops ? ` · ${nSta.toLocaleString()} 个车站` : '') +
    (miss.length ? ` · ${miss.length} 条暂无走向：${miss.slice(0, 3).join('、')}${miss.length > 3 ? '…' : ''}` : ''));
  $('#lmFoot').textContent = `已选 ${S.sel.size} / ${S.items.length} 条 · ${nPt.toLocaleString()} 点`;
}
function updateFoot() {
  $('#lmFoot').textContent = `已选 ${S.sel.size} / ${S.items.length} 条`;
}

function allBBox() {
  let b = null;
  for (const g of S.data.values()) {
    if (!g.found || !g.bbox) continue;
    const q = g.bbox;
    b = b ? [Math.min(b[0], q[0]), Math.min(b[1], q[1]),
             Math.max(b[2], q[2]), Math.max(b[3], q[3])] : q.slice();
  }
  return b;
}

/* ---------------- 腾讯地图 ---------------- */
function loadSDK(url) {
  return new Promise((res, rej) => {
    const old = document.getElementById('tmap-sdk');
    if (old) old.remove();
    window.TMap = undefined;
    const s = document.createElement('script');
    s.id = 'tmap-sdk'; s.src = url;
    s.onload = () => window.TMap ? res(true) : rej(new Error('SDK 加载了但没有 TMap'));
    s.onerror = () => rej(new Error('SDK 脚本加载失败（检查网络 / Key / 域名白名单）'));
    document.head.appendChild(s);
  });
}
async function initMap() {
  const proxy = 'http://127.0.0.1:__WB_HTTP_PORT__/_TMapService/_wbt/__WB_TMAP_SECRET__';
  const hasProxy = !proxy.includes('__WB_');
  const key = mapKey();
  let url = '';
  if (hasProxy) {
    window._TMapSecurityConfig = { serviceHost: proxy };
    url = 'https://map.qq.com/api/gljs?v=1.exp';
  } else if (key) {
    delete window._TMapSecurityConfig;
    url = 'https://map.qq.com/api/gljs?v=1.exp&key=' + encodeURIComponent(key);
  }
  if (!url) {
    keyBadge('未设置 Key · 示意图', 'warn');
    return initCanvas();
  }
  // 换 Key 会重新注入 SDK，先把旧实例拆掉（销毁要用到当前这版全局 TMap）
  if (S.map && S.map.destroy) { try { S.map.destroy(); } catch (_) { } }
  S.map = null;
  try { await loadSDK(url); }
  catch (e) { keyBadge('底图不可用 · 示意图', 'warn'); return initCanvas(e.message); }
  try {
    $('#lmMap').innerHTML = '';
    S.map = new TMap.Map('lmMap', {
      zoom: 5, center: new TMap.LatLng(35.5, 104), viewMode: '2D', showControl: false,
    });
    // 所有线路的样式一次建好：之后只 setGeometries，避免依赖未必存在的
    // setStyles / 动态 styleId。每条线一个颜色，站点圆点同色。
    const lineStyles = {}, stopStyles = {};
    S.items.forEach((x, i) => {
      const c = S.color.get(x.name) || colorOf(i);
      lineStyles['L' + i] = new TMap.PolylineStyle({
        color: c, width: 4, lineCap: 'round', borderWidth: 0, opacity: .9,
      });
      stopStyles['L' + i] = new TMap.MarkerStyle({
        width: 12, height: 12, anchor: { x: 6, y: 6 }, src: markerIcon(c, 4, 2),
      });
    });
    S.layers.line = new TMap.MultiPolyline({ map: S.map, styles: lineStyles, geometries: [], zIndex: 5 });
    S.layers.stop = new TMap.MultiMarker({ map: S.map, styles: stopStyles, geometries: [], zIndex: 7 });
    S.info = new TMap.InfoWindow({ map: S.map, position: new TMap.LatLng(35.5, 104), offset: { x: 0, y: -12 } });
    S.info.close();
    S.layers.line.on('click', e => {
      const nm = e.geometry && e.geometry.id;
      const it = S.items.find(x => x.name === nm);
      const g = S.data.get(nm);
      if (!it) return;
      const pos = e.latLng || e.position || S.map.getCenter();
      S.info.setPosition(pos);
      S.info.setContent(`<div class="lm-iw"><b>${esc(nm)}</b><br>` +
        `<span>${esc(it.fromSta)} → ${esc(it.toSta)}</span><br>` +
        `${it.totalKm != null ? Math.round(it.totalKm) + ' km · ' : ''}${it.nStop || 0} 站` +
        (g ? `<br><span>${g.nPart} 段 / ${g.nPt} 点${g.geomSrc ? ' · ' + esc(g.geomSrc) : ''}</span>` : '') +
        `</div>`);
      S.info.open();
    });
    S.backend = 'tmap';
    $('#lmMap').classList.remove('hidden');
    $('#lmCanvas').classList.add('hidden');
    $('#lmTip').textContent = TIP_DEFAULT;
    keyBadge(key ? '腾讯地图 ✓' : '腾讯地图（代理）', 'ok');
    return true;
  } catch (e) {
    keyBadge('底图未就绪 · 示意图', 'warn');
    return initCanvas('地图初始化失败：' + e.message);
  }
}
function drawTMap() {
  if (!S.map) return;
  const geoms = [], marks = [];
  S.items.forEach((x, i) => {
    const g = S.data.get(x.name);
    if (!g || !g.found) return;
    const sid = 'L' + i;
    geoms.push({ id: x.name, styleId: sid,
      paths: g.parts.map(p => p.map(q => toTMap(q[0], q[1]))) });
    if (S.showStops) {
      (g.stations || []).forEach((s, j) => marks.push({
        id: i + '_' + j, styleId: sid, position: toTMap(s.lon, s.lat),
      }));
    }
  });
  S.layers.line.setGeometries(geoms);
  S.layers.stop.setGeometries(S.showStops ? marks : []);
  const leg = $('#lmLegend');
  leg.innerHTML = sorted().filter(x => S.sel.has(x.name) && (S.data.get(x.name) || {}).found)
    .map(x => `<div><span class="lm-swatch" style="background:${S.color.get(x.name)}"></span>${esc(x.name)}</div>`).join('');
}
function fitTMap() {
  const b = allBBox();
  if (!b || !S.map) return;
  const pad = Math.min(1.0, Math.max(0.03, (b[2] - b[0] + b[3] - b[1]) * 0.02));
  try {
    S.map.fitBounds(new TMap.LatLngBounds(
      toTMap(b[0] - pad, b[1] - pad), toTMap(b[2] + pad, b[3] + pad)));
  } catch (e) { status('视野缩放失败：' + e.message); }
}

/* ---------------- Canvas 降级（无 Key 时的示意图，不画国界 / 不搬瓦片） ---------------- */
const TIP_DEFAULT = '点击地图上的线路可查看线路信息；左侧勾选控制显示哪些线路。';
function initCanvas(reason) {
  S.backend = 'canvas';
  S.map = null;
  $('#lmMap').classList.add('hidden');
  $('#lmCanvas').classList.remove('hidden');
  keyBadge(reason ? '底图未就绪 · 示意图' : '未设置 Key · 示意图', 'warn');
  $('#lmTip').textContent = reason
    ? '腾讯底图加载失败：' + reason + ' · 可点右上角「地图 Key」检查 Key / 域名白名单'
    : '本页还没读到地图 Key — 点右上角「地图 Key」填一次即可（与主站共用；注意 localhost 与 127.0.0.1 各存各的）';
  status((reason ? reason + ' · ' : '') + '线路形状用自绘示意图显示，不影响左侧选择');
  const cv = $('#lmCanvas');
  if (!cv._bound) {
    cv._bound = true;
    cv.addEventListener('mousedown', e => { S.cv.drag = [e.clientX, e.clientY, S.cv.dx, S.cv.dy]; });
    window.addEventListener('mouseup', () => { S.cv.drag = null; });
    window.addEventListener('mousemove', e => {
      if (!S.cv.drag) return;
      S.cv.dx = S.cv.drag[2] + e.clientX - S.cv.drag[0];
      S.cv.dy = S.cv.drag[3] + e.clientY - S.cv.drag[1];
      drawCanvas();
    });
    cv.addEventListener('wheel', e => {
      e.preventDefault();
      S.cv.zoom = Math.max(.3, Math.min(30, S.cv.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      drawCanvas();
    }, { passive: false });
    window.addEventListener('resize', () => { if (S.backend === 'canvas') drawCanvas(); });
  }
  drawCanvas();
  return false;
}
function drawCanvas() {
  const cv = $('#lmCanvas');
  if (!cv) return;
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(r.width * dpr));
  cv.height = Math.max(1, Math.round(r.height * dpr));
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.fillStyle = '#f7f9fc'; c.fillRect(0, 0, r.width, r.height);
  // 网格（只是坐标参考，不是任何地图边界）
  c.strokeStyle = '#e7edf5'; c.lineWidth = 1;
  for (let x = 0; x < r.width; x += 60) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, r.height); c.stroke(); }
  for (let y = 0; y < r.height; y += 60) { c.beginPath(); c.moveTo(0, y); c.lineTo(r.width, y); c.stroke(); }
  const b = allBBox();
  if (!b) {
    c.fillStyle = '#93a1b5'; c.font = '13px sans-serif'; c.textAlign = 'center';
    c.fillText('选中的线路还没有可用走向', r.width / 2, r.height / 2);
    return;
  }
  const cw = Math.max(.001, b[2] - b[0]), ch = Math.max(.001, b[3] - b[1]);
  const k = Math.min(r.width / cw, r.height / ch) * .86 * S.cv.zoom;
  const ox = r.width / 2 - (b[0] + b[2]) / 2 * k + S.cv.dx;
  const oy = r.height / 2 + (b[1] + b[3]) / 2 * k + S.cv.dy;
  const X = lon => ox + lon * k, Y = lat => oy - lat * k;
  for (const [nm, g] of S.data) {
    if (!g.found) continue;
    c.strokeStyle = S.color.get(nm) || '#2563eb';
    c.lineWidth = 2; c.lineJoin = 'round'; c.lineCap = 'round';
    for (const p of g.parts) {
      c.beginPath();
      p.forEach((q, i) => i ? c.lineTo(X(q[0]), Y(q[1])) : c.moveTo(X(q[0]), Y(q[1])));
      c.stroke();
    }
    if (S.showStops) {
      c.fillStyle = c.strokeStyle;
      for (const s of (g.stations || [])) {
        c.beginPath(); c.arc(X(s.lon), Y(s.lat), 2, 0, Math.PI * 2); c.fill();
      }
    }
  }
  c.fillStyle = '#8493a8'; c.font = '11px sans-serif'; c.textAlign = 'left';
  c.fillText('自绘示意图 · 无底图 · 滚轮缩放 / 拖动平移', 12, r.height - 14);
}

function draw() { if (S.backend === 'tmap') drawTMap(); else drawCanvas(); }
function fitAll() { if (S.backend === 'tmap') fitTMap(); else { S.cv.zoom = 1; S.cv.dx = 0; S.cv.dy = 0; drawCanvas(); } }

/* ---------------- 交互 ---------------- */
function bind() {
  $('#lmQ').addEventListener('input', renderList);
  $('#lmAll').addEventListener('click', () => {
    S.sel = new Set(S.items.map(x => x.name)); renderList(); schedule(S.fit);
  });
  $('#lmNone').addEventListener('click', () => {
    S.sel = new Set(); renderList(); schedule(false);
  });
  $('#lmInvert').addEventListener('click', () => {
    const n = new Set(S.items.filter(x => !S.sel.has(x.name)).map(x => x.name));
    S.sel = n; renderList(); schedule(S.fit);
  });
  document.querySelectorAll('input[name="lmMode"]').forEach(r => r.addEventListener('change', () => {
    S.mode = r.value;
    if (S.mode === 'single' && S.sel.size > 1) S.sel = new Set([sorted()[0].name]);
    renderList(); schedule(S.fit);
  }));
  $('#lmSolo').addEventListener('change', e => { S.solo = e.target.checked; });
  $('#lmStops').addEventListener('change', e => { S.showStops = e.target.checked; schedule(false); });
  $('#lmFit').addEventListener('change', e => { S.fit = e.target.checked; });
  $('#lmDetail').addEventListener('change', e => { S.detail = +e.target.value; schedule(false); });
  $('#lmFitAll').addEventListener('click', fitAll);
  $('#lmReset').addEventListener('click', () => reload(true));
  $('#lmCollapse').addEventListener('click', () => {
    $('.lm-app').classList.add('folded');
    $('#lmExpand').classList.remove('hidden');
    setTimeout(() => { if (S.backend === 'tmap' && S.map) S.map.resize && S.map.resize(); }, 200);
  });
  $('#lmExpand').addEventListener('click', () => {
    $('.lm-app').classList.remove('folded');
    $('#lmExpand').classList.add('hidden');
    setTimeout(() => { if (S.backend === 'tmap' && S.map) S.map.resize && S.map.resize(); }, 200);
  });

  // Key：保存后原地重建地图，绝不 location.reload()
  const modal = $('#lmKeyModal');
  $('#lmKey').addEventListener('click', () => {
    $('#lmKeyInput').value = mapKey();
    $('#lmSite').textContent = location.origin;
    $('#lmKeyNote').textContent = mapKey()
      ? '已读到本机保存的 Key；保存后会原地重建底图，不会刷新整个页面。'
      : '保存后会原地重建底图，不会刷新整个页面。';
    modal.classList.remove('hidden');
  });
  $('#lmKeyCancel').addEventListener('click', () => modal.classList.add('hidden'));
  async function saveKey(val) {
    if (val) localStorage.setItem('railcn.mapkey', val);
    else localStorage.removeItem('railcn.mapkey');
    modal.classList.add('hidden');
    $('#lmKeyNote').textContent = '';
    keyBadge('正在加载底图…');
    await initMap();
    if (S.data.size) { draw(); fitAll(); }
    else await reload(true);
  }
  $('#lmKeySave').addEventListener('click', () => saveKey($('#lmKeyInput').value.trim()));
  $('#lmKeyClear').addEventListener('click', () => saveKey(''));
  $('#lmKeyInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') saveKey($('#lmKeyInput').value.trim());
  });
  modal.addEventListener('click', e => { if (e.target === modal) modal.classList.add('hidden'); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') modal.classList.add('hidden'); });
}

bind();
loadIndex();
})();
