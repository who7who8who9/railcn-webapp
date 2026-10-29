/* linedesk.js — 线路整理台：手动归并线路、理站序
 *
 * 只跟 /api/ld/* 打交道；整理结果落在独立的 linedesk.db，不碰 rail.db。
 * 地图三种底图（左下方切换，选择记在 localStorage）：
 *   tmap 腾讯位置服务 GL JS —— 国内底图最准，要 Key，WGS-84 数据按 GCJ-02 纠偏后画
 *   osm  OpenStreetMap 栅格瓦片（Carto）—— 免 Key，不经纠偏，和 OSM 走向严丝合缝
 *   svg  无底图示意 —— 自己画墨卡托折线，离线也能用，Key 失效时兜底
 */
'use strict';

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const SRC = { lib: '底表', custom: '自建', huirui: '资料库', ce: 'china-emu', osm: 'OSM' };
const KIND = { gs: '高速', cj: '城际', sj: '市域', lj: '联络线', zx: '支线', px: '普速', '': '未分' };

const S = {
  q: '', source: '', kind: '', status: '', hasStops: '',
  items: [], total: 0,
  sel: null, detail: null, geom: null,
  merge: false, picks: new Set(),
};

/* ------------------------------------------------------------------ 工具 */

async function api(p, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(p, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let toastT = 0;
function toast(msg, isErr) {
  let el = $('#ldToast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'ldToast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'toast on' + (isErr ? ' err' : '');
  clearTimeout(toastT);
  toastT = setTimeout(() => { el.className = 'toast' + (isErr ? ' err' : ''); }, 2200);
}

function fmtKm(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return (Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' km';
}

/* ------------------------------------------------------------------ 地图 */

/* 与主站共用同一个 Key（localStorage.railcn.mapkey），两处填一次即可 */
const mapKey = () => localStorage.getItem('railcn.mapkey') || '';
const basePref = () => localStorage.getItem('railcn.ldbase') || '';

/* WGS-84 → GCJ-02：腾讯底图用 GCJ-02，我们的坐标是 OSM 的 WGS-84 */
const GCJ = (() => {
  const PI = Math.PI, A = 6378245.0, EE = 0.00669342162296594323;
  const outOfChina = (lng, lat) => lng < 73.66 || lng > 135.05 || lat < 3.86 || lat > 53.55;
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
  return function wgs2gcj(lng, lat) {
    if (outOfChina(lng, lat)) return [lng, lat];
    let dLat = tLat(lng - 105, lat - 35), dLng = tLng(lng - 105, lat - 35);
    const rad = lat / 180 * PI;
    let magic = Math.sin(rad);
    magic = 1 - EE * magic * magic;
    const sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180) / ((A * (1 - EE)) / (magic * sqrtMagic) * PI);
    dLng = (dLng * 180) / (A / sqrtMagic * Math.cos(rad) * PI);
    return [lng + dLng, lat + dLat];
  };
})();

const _loaded = {};
function loadScript(src) {
  if (_loaded[src]) return _loaded[src];
  _loaded[src] = new Promise((res, rej) => {
    const el = document.createElement('script');
    el.src = src;
    el.onload = res;
    el.onerror = () => { delete _loaded[src]; rej(new Error('加载失败：' + src)); };
    document.head.appendChild(el);
  });
  return _loaded[src];
}
function loadCss(href) {
  if (_loaded[href]) return _loaded[href];
  _loaded[href] = new Promise(res => {
    const el = document.createElement('link');
    el.rel = 'stylesheet';
    el.href = href;
    el.onload = res;
    el.onerror = res;
    document.head.appendChild(el);
  });
  return _loaded[href];
}

/* 站名标签太密就抽稀：站多的时候每 5 个标一个 + 首末站 + 选中站 */
function labelWanted(i, n, sel) {
  return n <= 34 || i === sel || i === 0 || i === n - 1 || i % 5 === 0;
}

/* 点中地图上的站 → 右侧站序行跟着走 */
function pickStop(i) {
  MAP.select(i);
  const row = $(`.st[data-i="${i}"]`);
  if (row) {
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    row.classList.add('hit');
    setTimeout(() => row.classList.remove('hit'), 900);
  }
}

/* 无底图示意：自己画的 SVG（墨卡托 + 拖动缩放） */
const M = {
  name: 'svg',
  svg: null, gGeo: null, gStops: null, gLabels: null,
  W: 0, H: 0, scale: 1, tx: 0, ty: 0,
  parts: [], stops: [], selStop: -1,
  ready: false,
  ok: true,                     /* 示意模式永远可用，不参与降级判断 */

  init() {
    if (this.ready) return;
    this.svg = $('#ldSvg');
    const NS = 'http://www.w3.org/2000/svg';
    this.gGeo = document.createElementNS(NS, 'g');
    this.gStops = document.createElementNS(NS, 'g');
    this.gLabels = document.createElementNS(NS, 'g');
    this.svg.appendChild(this.gGeo);
    this.svg.appendChild(this.gStops);
    this.svg.appendChild(this.gLabels);
    this.ready = true;
    this.resize();
    window.addEventListener('resize', () => { this.resize(); this.draw(); });
    this.bindPan();
  },

  resize() {
    const r = this.svg.getBoundingClientRect();
    this.W = Math.max(200, Math.round(r.width));
    this.H = Math.max(160, Math.round(r.height));
    this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
  },

  /* 墨卡托：x 0..1，y 0..1（y 向下） */
  proj(lat, lon) {
    const x = (lon + 180) / 360;
    const s = Math.sin(Math.max(-85, Math.min(85, lat)) * Math.PI / 180);
    return [x, 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)];
  },

  setData(stops, parts) {
    this.stops = (stops || []).filter(s => s.lat != null && s.lon != null);
    this.parts = (parts || []).filter(p => p && p.length);
    this.selStop = -1;
    this.fit();
    this.draw();
  },

  select(i) {
    this.selStop = i;
    this.resize();
    this.draw();
  },

  bbox() {
    let x0 = 1, x1 = 0, y0 = 1, y1 = 0, n = 0;
    const add = (lat, lon) => {
      const [x, y] = this.proj(lat, lon);
      x0 = Math.min(x0, x); x1 = Math.max(x1, x);
      y0 = Math.min(y0, y); y1 = Math.max(y1, y);
      n++;
    };
    this.parts.forEach(p => p.forEach(pt => add(pt[1], pt[0])));
    this.stops.forEach(s => add(s.lat, s.lon));
    return n ? { x0, x1, y0, y1 } : null;
  },

  fit() {
    this.resize();
    const b = this.bbox();
    if (!b) { this.scale = 1; this.tx = 0; this.ty = 0; return; }
    const w = Math.max(1e-6, b.x1 - b.x0), h = Math.max(1e-6, b.y1 - b.y0);
    // 线是一维的（东西向时 h≈0），留一点最小尺寸免得放大到离谱
    const pad = 0.9;
    this.scale = Math.min(this.W / w, this.H / h) * pad;
    this.scale = Math.min(this.scale, 2.6e6);
    const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
    this.tx = this.W / 2 - cx * this.scale;
    this.ty = this.H / 2 - cy * this.scale;
  },

  px(lat, lon) {
    const [x, y] = this.proj(lat, lon);
    return [x * this.scale + this.tx, y * this.scale + this.ty];
  },

  zoomAt(f, cx, cy) {
    const ns = Math.max(40, Math.min(2.6e6, this.scale * f));
    const k = ns / this.scale;
    this.tx = cx - (cx - this.tx) * k;
    this.ty = cy - (cy - this.ty) * k;
    this.scale = ns;
    this.draw();
  },

  bindPan() {
    let down = false, lx = 0, ly = 0, moved = 0;
    this.svg.addEventListener('pointerdown', e => {
      down = true; moved = 0; lx = e.clientX; ly = e.clientY;
      this.svg.classList.add('dragging');
      this.svg.setPointerCapture(e.pointerId);
    });
    this.svg.addEventListener('pointermove', e => {
      if (!down) return;
      const dx = e.clientX - lx, dy = e.clientY - ly;
      moved += Math.abs(dx) + Math.abs(dy);
      lx = e.clientX; ly = e.clientY;
      this.tx += dx; this.ty += dy;
      this.draw();
    });
    const up = e => {
      if (!down) return;
      down = false;
      this.svg.classList.remove('dragging');
      try { this.svg.releasePointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
    };
    this.svg.addEventListener('pointerup', up);
    this.svg.addEventListener('pointercancel', up);
    this.svg.addEventListener('wheel', e => {
      e.preventDefault();
      const r = this.svg.getBoundingClientRect();
      const f = e.deltaY < 0 ? 1.18 : 1 / 1.18;
      this.zoomAt(f, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
  },

  draw() {
    if (!this.ready) return;
    const NS = 'http://www.w3.org/2000/svg';
    const mk = (tag, attrs, parent) => {
      const el = document.createElementNS(NS, tag);
      Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
      (parent || this.gGeo).appendChild(el);
      return el;
    };
    this.gGeo.innerHTML = '';
    this.gStops.innerHTML = '';
    this.gLabels.innerHTML = '';

    // 走向
    const geoPts = [];
    // OSM 的几何常常是几十段碎线，段与段之间是数据断口而不是真的断轨：
    // 先垫一条很淡的连线把它们串起来，免得看着像铁路断成了好几截
    if (this.parts.length > 1) {
      let d = '';
      for (let i = 0; i < this.parts.length - 1; i++) {
        const b = this.parts[i][this.parts[i].length - 1];
        const a = this.parts[i + 1][0];
        const p2 = this.px(b[1], b[0]), p1 = this.px(a[1], a[0]);
        d += `M${p2[0].toFixed(1)} ${p2[1].toFixed(1)}L${p1[0].toFixed(1)} ${p1[1].toFixed(1)}`;
      }
      mk('path', { d, fill: 'none', stroke: '#1f6feb', 'stroke-width': 1.2,
                   opacity: .22, 'stroke-dasharray': '3 5' });
    }
    this.parts.forEach(part => {
      let d = '';
      part.forEach((pt, i) => {
        const [x, y] = this.px(pt[1], pt[0]);
        d += (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
        geoPts.push([x, y]);
      });
      if (d) {
        mk('path', { d, fill: 'none', stroke: '#1f6feb', 'stroke-width': 2.2,
                     'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: .85 });
      }
    });
    // 没有走向：站与站直连（虚线，明说这是示意）
    if (!this.parts.length && this.stops.length > 1) {
      let d = '';
      this.stops.forEach((s, i) => {
        const [x, y] = this.px(s.lat, s.lon);
        d += (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
      });
      mk('path', { d, fill: 'none', stroke: '#93a2b8', 'stroke-width': 1.6,
                   'stroke-dasharray': '5 4' });
    }

    // 站点
    const many = this.stops.length > 34;
    this.stops.forEach((s, i) => {
      const [x, y] = this.px(s.lat, s.lon);
      const on = i === this.selStop;
      mk('circle', { cx: x, cy: y, r: on ? 5.2 : 3.2,
                     fill: on ? '#e8590c' : '#fff', stroke: on ? '#e8590c' : '#0f2d52',
                     'stroke-width': on ? 2 : 1.6 }, this.gStops)
        .addEventListener('click', ev => {
          ev.stopPropagation();
          pickStop(i);
        });
      const show = !many || on || i === 0 || i === this.stops.length - 1 || i % 5 === 0;
      if (show) {
        const t = mk('text', {
          x: x + 6, y: y + 3.4, 'font-size': on ? 11.5 : 10.5,
          fill: on ? '#e8590c' : '#33415c', 'font-weight': on ? 700 : 500,
          stroke: '#fff', 'stroke-width': 3, 'paint-order': 'stroke',
        }, this.gLabels);
        t.textContent = s.name;
      }
    });
  },
};

/* ------------------------------------------------------------------ 腾讯地图 */

const MT = {
  name: 'tmap',
  ok: false, map: null, layers: {},
  stops: [], parts: [], sel: -1,

  async init() {
    if (this.ok) return;
    const key = mapKey();
    if (!key) { const e = new Error('还没配腾讯地图 Key'); e.code = 'NO_KEY'; throw e; }
    await loadScript(`https://map.qq.com/api/gljs?v=1.exp&key=${encodeURIComponent(key)}`);
    if (typeof TMap === 'undefined') throw new Error('腾讯地图 SDK 没就绪');
    this.map = new TMap.Map('ldMap', {
      center: new TMap.LatLng(34.5, 108.9), zoom: 5,
      viewMode: '2D', showControl: false, pitch: 0, rotation: 0,
    });
    this.ok = true;
  },

  /* 底图是 GCJ-02，喂进去的坐标得先纠偏 */
  g(lat, lon) {
    const [lng2, lat2] = GCJ(lon, lat);
    return new TMap.LatLng(lat2, lng2);
  },

  setData(stops, parts) {
    this.stops = (stops || []).filter(s => s.lat != null && s.lon != null);
    this.parts = (parts || []).filter(p => p && p.length);
    this.sel = -1;
    this.clear();
    if (!this.ok) return;
    this.paint();
    this.fit();
  },

  select(i) {
    this.sel = i;
    if (this.ok) this.paint();
  },

  clear() {
    Object.values(this.layers).forEach(l => { try { l.setMap && l.setMap(null); } catch (_) { /* 忽略 */ } });
    this.layers = {};
  },

  paint() {
    if (!this.map) return;
    this.clear();
    const geoPaths = this.parts
      .map(part => part.map(p => this.g(p[1], p[0])))
      .filter(a => a.length > 1);

    if (geoPaths.length) {
      this.layers.line = new TMap.MultiPolyline({
        map: this.map,
        styles: {
          casing: new TMap.PolylineStyle({ color: '#ffffff', width: 9, lineCap: 'round', borderWidth: 0, opacity: .95 }),
          main: new TMap.PolylineStyle({ color: '#1f6feb', width: 5, lineCap: 'round', borderWidth: 0 }),
          gap: new TMap.PolylineStyle({ color: '#1f6feb', width: 2, borderWidth: 0, opacity: .25, dashArray: [4, 6] }),
        },
        geometries: [
          { id: 'casing', styleId: 'casing', paths: geoPaths },
          { id: 'main', styleId: 'main', paths: geoPaths },
        ],
        zIndex: 5,
      });
    } else if (this.stops.length > 1) {
      /* 没走向就站点直连，虚线明说这是示意 */
      this.layers.line = new TMap.MultiPolyline({
        map: this.map,
        styles: { d: new TMap.PolylineStyle({ color: '#93a2b8', width: 2, borderWidth: 0, dashArray: [6, 5] }) },
        geometries: [{ id: 'd', styleId: 'd', paths: this.stops.map(s => this.g(s.lat, s.lon)) }],
        zIndex: 5,
      });
    }

    const dot = (fill, stroke, r) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="${r * 2 + 6}" height="${r * 2 + 6}">
         <circle cx="${r + 3}" cy="${r + 3}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="2"/></svg>`)}`;
    this.layers.stops = new TMap.MultiMarker({
      map: this.map,
      styles: {
        st: new TMap.MarkerStyle({ width: 13, height: 13, anchor: { x: 6.5, y: 6.5 }, src: dot('#ffffff', '#0f2d52', 3.4) }),
        sel: new TMap.MarkerStyle({ width: 17, height: 17, anchor: { x: 8.5, y: 8.5 }, src: dot('#e8590c', '#e8590c', 5.2) }),
      },
      geometries: this.stops.map((s, i) => ({
        id: String(i), styleId: i === this.sel ? 'sel' : 'st', position: this.g(s.lat, s.lon),
      })),
      zIndex: 6,
    });
    this.layers.stops.on('click', evt => pickStop(Number(evt.geometry.id)));

    const n = this.stops.length;
    this.layers.labels = new TMap.MultiLabel({
      map: this.map,
      styles: {
        l: new TMap.LabelStyle({
          color: '#33415c', size: 11, offset: { x: 0, y: -13 },
          alignment: 'center', verticalAlignment: 'bottom',
          backgroundColor: 'rgba(255,255,255,.86)', backgroundPadding: 2,
          borderColor: 'rgba(140,158,180,.45)', borderRadius: 3,
        }),
        sel: new TMap.LabelStyle({
          color: '#ffffff', size: 11, offset: { x: 0, y: -14 },
          alignment: 'center', verticalAlignment: 'bottom',
          backgroundColor: 'rgba(232,89,12,.95)', backgroundPadding: 3, borderRadius: 3,
        }),
      },
      geometries: this.stops.map((s, i) => (labelWanted(i, n, this.sel) ? {
        id: 'L' + i, styleId: i === this.sel ? 'sel' : 'l',
        position: this.g(s.lat, s.lon), content: s.name,
      } : null)).filter(Boolean),
      zIndex: 7,
    });
  },

  fit() {
    if (!this.ok || !this.map) return;
    let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    const add = (lat, lon) => {
      minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat);
      minLon = Math.min(minLon, lon); maxLon = Math.max(maxLon, lon);
    };
    this.stops.forEach(s => add(s.lat, s.lon));
    this.parts.forEach(p => p.forEach(pt => add(pt[1], pt[0])));
    if (!(maxLat > -90)) return;
    if (maxLat - minLat < 0.05 && maxLon - minLon < 0.05) {
      const c = this.stops[0] || { lat: minLat, lon: minLon };
      this.map.easeTo({ center: this.g(c.lat, c.lon), zoom: 11 });
      return;
    }
    const b = new TMap.LatLngBounds(this.g(minLat, minLon), this.g(maxLat, maxLon));
    this.map.fitBounds(b, { padding: { top: 80, bottom: 110, left: 90, right: 90 } });
  },

  zoom(f) {
    if (!this.ok) return;
    this.map.easeTo({ zoom: this.map.getZoom() + (f > 1 ? 1 : -1) });
  },

  resize() {
    /* 腾讯 GL 自己跟着容器走，切回来时补一枪就行 */
    if (this.ok && this.map && this.map.resize) this.map.resize();
  },

  destroy() {
    try { this.clear(); this.map && this.map.destroy && this.map.destroy(); } catch (_) { /* 忽略 */ }
    this.map = null; this.ok = false;
  },
};

/* ------------------------------------------------------------------ OSM 栅格（Leaflet） */

const MO = {
  name: 'osm',
  ok: false, map: null, group: null,
  stops: [], parts: [], sel: -1,

  /* 瓦片源：官方 tile.openstreetmap.org 在境内连不上，CARTO 现在要 Key（返回 API KEY REQUIRED），
   * 用德国社区瓦片（带中文地名）；再不行退法国社区瓦片（只有拼音）。 */
  TILE: 'https://tile.openstreetmap.de/{z}/{x}/{y}.png',
  TILE2: 'https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png',
  ATTR: '© OpenStreetMap contributors · tiles: openstreetmap.de',

  async init() {
    if (this.ok) return;
    if (!window.L) {
      await loadCss('vendor/leaflet/leaflet.css');
      await loadScript('vendor/leaflet/leaflet.js');
    }
    if (!window.L) throw new Error('Leaflet 没就绪');
    this.map = L.map('ldMap', {
      center: [34.5, 108.9], zoom: 4, zoomControl: false,
      attributionControl: true, preferCanvas: true, worldCopyJump: true,
    });
    const layer = L.tileLayer(this.TILE, {
      maxZoom: 19, attribution: this.ATTR, detectRetina: false,
    }).addTo(this.map);
    /* 主瓦片源出问题（被墙 / 429）就整体换法国源顶上，别留一片灰 */
    let err = 0, swapped = false;
    layer.on('tileerror', () => {
      if (swapped || ++err < 4) return;
      swapped = true;
      layer.setUrl(this.TILE2);
      layer.options.attribution = '© OpenStreetMap contributors · tiles: openstreetmap.fr';
      toast('openstreetmap.de 瓦片不可用，已换 openstreetmap.fr');
    });
    /* 缩放按钮我们自己有（右上角），别再塞一套 Leaflet 的，免得跟图例打架 */
    this.ok = true;
  },

  setData(stops, parts) {
    this.stops = (stops || []).filter(s => s.lat != null && s.lon != null);
    this.parts = (parts || []).filter(p => p && p.length);
    this.sel = -1;
    if (!this.ok) return;
    this.map.invalidateSize();
    this.paint();
    this.fit();
  },

  select(i) {
    this.sel = i;
    if (this.ok) this.paint();
  },

  paint() {
    if (!this.map) return;
    if (this.group) { this.group.remove(); this.group = null; }
    const g = L.layerGroup().addTo(this.map);
    this.group = g;

    /* 走向：OSM 几何本身就是 WGS-84，跟瓦片严丝合缝，不用纠偏 */
    if (this.parts.length) {
      this.parts.forEach(part => {
        L.polyline(part.map(p => [p[1], p[0]]), {
          color: '#ffffff', weight: 7, opacity: .9, lineJoin: 'round',
        }).addTo(g);
        L.polyline(part.map(p => [p[1], p[0]]), {
          color: '#1f6feb', weight: 3.5, opacity: .95, lineJoin: 'round',
        }).addTo(g);
      });
      /* 段与段之间的数据断口，用淡虚线串一下，免得看着像铁路断了 */
      if (this.parts.length > 1) {
        for (let i = 0; i < this.parts.length - 1; i++) {
          const b = this.parts[i][this.parts[i].length - 1];
          const a = this.parts[i + 1][0];
          L.polyline([[b[1], b[0]], [a[1], a[0]]], {
            color: '#1f6feb', weight: 1.5, opacity: .3, dashArray: '3 5',
          }).addTo(g);
        }
      }
    } else if (this.stops.length > 1) {
      L.polyline(this.stops.map(s => [s.lat, s.lon]), {
        color: '#93a2b8', weight: 2, dashArray: '6 5',
      }).addTo(g);
    }

    const n = this.stops.length;
    this.stops.forEach((s, i) => {
      const on = i === this.sel;
      const c = L.circleMarker([s.lat, s.lon], {
        radius: on ? 6 : 4, color: on ? '#e8590c' : '#0f2d52',
        weight: on ? 2.5 : 1.6, fillColor: on ? '#e8590c' : '#ffffff', fillOpacity: 1,
      }).addTo(g);
      c.on('click', () => pickStop(i));
      if (on) c.bringToFront();
      if (labelWanted(i, n, this.sel)) {
        c.bindTooltip(s.name, {
          permanent: true, direction: 'right', offset: [5, 0],
          className: on ? 'ld-tip sel' : 'ld-tip',
        });
      }
    });
    if (this.sel >= 0 && this.stops[this.sel]) {
      this.map.panTo([this.stops[this.sel].lat, this.stops[this.sel].lon]);
    }
  },

  fit() {
    if (!this.ok) return;
    const pts = [];
    this.stops.forEach(s => pts.push([s.lat, s.lon]));
    this.parts.forEach(p => p.forEach(pt => pts.push([pt[1], pt[0]])));
    if (!pts.length) return;
    this.map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 12 });
  },

  zoom(f) {
    if (!this.ok) return;
    this.map.setZoom(this.map.getZoom() + (f > 1 ? 1 : -1));
  },

  resize() { if (this.ok && this.map) this.map.invalidateSize(); },

  destroy() {
    try { this.map && this.map.remove(); } catch (_) { /* 忽略 */ }
    this.map = null; this.group = null; this.ok = false;
  },
};

/* ------------------------------------------------------------------ 底图切换 */

const MAP = {
  impl: null, mode: '', stops: [], parts: [],

  async use(mode) {
    mode = mode || basePref() || (mapKey() ? 'tmap' : 'osm');
    const div = $('#ldMap'), svg = $('#ldSvg'), tip = $('#ldBaseTip');

    if (this.impl && this.mode === mode && this.impl.ok) return;
    try { this.impl && this.impl.destroy && this.impl.destroy(); } catch (_) { /* 忽略 */ }

    if (mode === 'svg') {
      div.classList.add('hidden');
      svg.classList.remove('hidden');
      M.init();
      MAP.setData(this.stops, this.parts);
      this.impl = M; this.mode = 'svg';
      tip.textContent = '无底图示意';
    } else {
      svg.classList.add('hidden');
      div.classList.remove('hidden');
      try {
        if (mode === 'osm') await MO.init(); else await MT.init();
        this.impl = mode === 'osm' ? MO : MT;
        this.mode = mode;
        tip.textContent = mode === 'osm' ? 'OSM 栅格（免 Key）' : '腾讯位置服务';
      } catch (e) {
        /* 腾讯要 Key，没配就临时用 OSM 顶上，并把 Key 入口亮出来 */
        if (mode !== 'osm') {
          try {
            await MO.init();
            this.impl = MO; this.mode = 'osm';
            tip.innerHTML = '没配腾讯 Key，先用 OSM · <b id="ldKeyAsk">去填 Key</b>';
            const ask = $('#ldKeyAsk');
            if (ask) ask.onclick = openKeyModal;
            toast(e.code === 'NO_KEY' ? '没配腾讯地图 Key，临时用 OSM 底图' : e.message, true);
          } catch (e2) {
            div.classList.add('hidden');
            svg.classList.remove('hidden');
            M.init(); this.impl = M; this.mode = 'svg';
            tip.textContent = '底图加载失败，退回示意';
          }
        } else {
          div.classList.add('hidden');
          svg.classList.remove('hidden');
          M.init(); this.impl = M; this.mode = 'svg';
          tip.textContent = 'OSM 瓦片加载失败，退回示意';
        }
      }
      this.impl.resize && this.impl.resize();
    }
    this.syncBar();
    this.impl.setData(this.stops, this.parts);
  },

  syncBar() {
    $$('.ld-basebar [data-base]').forEach(b => {
      b.classList.toggle('on', b.dataset.base === this.mode);
    });
  },

  setData(stops, parts) {
    this.stops = (stops || []).filter(s => s && s.lat != null && s.lon != null);
    this.parts = (parts || []).filter(p => p && p.length);
    this.syncOrm();
    if (!this.impl) return;
    this.impl.setData(this.stops, this.parts);
  },

  /* 「ORM 核对」：拿这条线的中心点去 OpenRailwayMap 开一页，
   * 那边是 OSM 数据按铁路专用样式渲染的，走向对不对一眼能看出来 */
  syncOrm() {
    const a = $('#ldOrm');
    if (!a) return;
    const pts = this.stops.map(s => [s.lat, s.lon]);
    this.parts.forEach(p => p.forEach(pt => pts.push([pt[1], pt[0]])));
    if (!pts.length) { a.removeAttribute('href'); a.style.opacity = '.45'; return; }
    let la = 0, lo = 0;
    pts.forEach(([y, x]) => { la += y; lo += x; });
    la /= pts.length; lo /= pts.length;
    a.href = 'https://www.openrailwaymap.org/?style=standard&lat=' +
      la.toFixed(5) + '&lon=' + lo.toFixed(5) + '&zoom=8';
    a.style.opacity = '1';
  },

  select(i) {
    if (this.impl && this.impl.select) this.impl.select(i);
  },

  zoom(f) { if (this.impl && this.impl.zoom) this.impl.zoom(f); },

  fit() {
    if (this.impl && this.impl.fit) this.impl.fit();
    if (this.impl === M) M.draw();
  },

  resize() { if (this.impl && this.impl.resize) this.impl.resize(); },
};

function openKeyModal() {
  openModal('腾讯地图 Key', `
    <p class="muted">地图底图用腾讯位置服务（GL JS）。到
      <a href="https://lbs.qq.com/dev/console/application/mine" target="_blank" rel="noreferrer">腾讯位置服务控制台</a>
      建一个 Web 端 Key 粘到下面，跟主站共用同一个 Key（只存本机 localStorage）。</p>
    <input id="ldKeyInput" placeholder="粘贴腾讯地图 Key" value="${esc(mapKey())}" style="width:100%">
    <p class="muted small">不填也能用：右下角切「OSM」用 OpenStreetMap 栅格底图，或「示意」纯折线。</p>`,
  async () => {
    const v = ($('#ldKeyInput') || {}).value || '';
    if (v.trim()) localStorage.setItem('railcn.mapkey', v.trim());
    else localStorage.removeItem('railcn.mapkey');
    localStorage.setItem('railcn.ldbase', 'tmap');
    await MAP.use('tmap');
    document.querySelectorAll('.modal').forEach(m => m.remove());
  }, '保存并切换');
}

/* ------------------------------------------------------------------ 列表 */

function chipsHtml() {
  const srcs = [['', '全部来源'], ['lib', '底表'], ['custom', '自建'], ['huirui', '资料库'],
                ['ce', 'china-emu'], ['osm', 'OSM']];
  const kinds = [['', '全部类型'], ['gs', '高速'], ['cj', '城际'], ['px', '普速'],
                 ['lj', '联络线'], ['zx', '支线']];
  const sts = [['', '全部状态'], ['draft', '待整理'], ['confirmed', '已确认']];
  const more = [['', '含无站序'], ['1', '只看有站序']];
  const row = (arr, key, cur) => arr.map(([v, t]) =>
    `<button class="chip${cur === v ? ' on' : ''}" data-f="${key}" data-v="${v}">${t}</button>`
  ).join('');
  return `<div style="display:flex;flex-wrap:wrap;gap:4px">
    ${row(srcs, 'source', S.source)}
    ${row(kinds, 'kind', S.kind)}
    ${row(sts, 'status', S.status)}
    ${row(more, 'hasStops', S.hasStops)}
  </div>`;
}

function renderFilters() {
  $('#ldFilters').innerHTML = chipsHtml();
  $$('#ldFilters .chip').forEach(b => {
    b.onclick = () => {
      S[b.dataset.f] = b.dataset.v;
      S.sel = null; S.detail = null;
      loadList();
      renderPanel();
    };
  });
}

function renderList() {
  const box = $('#ldList');
  if (!S.items.length) {
    box.innerHTML = `<div class="ld-empty" style="padding:24px 12px">没有匹配的线路</div>`;
    $('#ldCount').textContent = '';
    return;
  }
  box.innerHTML = S.items.map(it => {    const al = (it.aliases || []).length
      ? `<span class="als">${(it.aliases || []).map(a =>
          `<span class="s" style="background:${SRCBG(a.source)}" title="${esc(a.source)}：${esc(a.name)}"></span>`).join('')}</span>`
      : '<span style="color:#c3cddd">无别名</span>';
    const on = S.detail && S.detail.id === it.id ? ' on' : '';
    const pick = S.picks.has(it.id) ? ' pick' : '';
    const cb = S.merge ? `<span class="cb${S.picks.has(it.id) ? ' on' : ''}"></span>` : '';
    const st = it.status === 'confirmed' ? '<span class="st">已确认</span>' : '<span class="st draft">待整理</span>';
    return `<div class="ld-item${on}${pick}" data-id="${it.id}">
      <div class="nm">${cb}${esc(it.name)}${st}</div>
      <div class="meta">
        <span class="k">${KIND[it.kind] || it.kind || '—'}</span>
        <span>${it.n_stop || 0} 站</span>
        <span>${fmtKm(it.total_km)}</span>
        <span>${al || '<span style="color:#c3cddd">无别名</span>'}</span>
        ${it.n_child ? `<span>· ${it.n_child} 个子段</span>` : ''}
      </div>
    </div>`;
  }).join('');
  if (S.items.length < S.total) {
    box.innerHTML += `<div style="padding:8px 2px 2px">
      <button class="btn sm" id="ldMore">加载更多（还有 ${S.total - S.items.length} 条）</button></div>`;
  }
  $('#ldCount').innerHTML = `共 <b>${S.total}</b> 条，显示 ${S.items.length} 条`;
  const more = $('#ldMore');
  if (more) more.onclick = () => { more.disabled = true; loadList(true); };

  $$('#ldList .ld-item').forEach(el => {
    el.onclick = () => {
      const id = Number(el.dataset.id);
      if (S.merge) {
        if (S.picks.has(id)) S.picks.delete(id); else S.picks.add(id);
        renderList();
        renderMergeBar();
        return;
      }
      selectLine(id);
    };
  });
}

function SRCBG(s) {
  return { lib: '#0f2d52', custom: '#6b46c1', huirui: '#1f6feb', ce: '#0f9d58', osm: '#8b93a5' }[s] || '#8b93a5';
}

function renderMergeBar() {
  let bar = $('#ldMergeBar');
  if (!S.merge) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'ldMergeBar';
    bar.style.cssText = 'display:flex;align-items:center;gap:8px;padding:0 10px 8px;font-size:11.5px';
    $('#ldFilters').after(bar);
  }
  bar.innerHTML = `<span style="color:#77839a">已选 <b>${S.picks.size}</b> 条</span>
    <button class="btn sm primary" id="ldMergeGo" ${S.picks.size < 2 ? 'disabled' : ''}>合并</button>
    <button class="btn sm" id="ldMergeCancel">取消</button>`;
  const go = $('#ldMergeGo');
  if (go) go.onclick = () => openMerge();
  $('#ldMergeCancel').onclick = () => { S.merge = false; S.picks.clear(); renderList(); renderMergeBar(); };
}

async function loadList(append) {
  const p = new URLSearchParams({
    limit: '300', offset: String(append ? S.items.length : 0),
  });
  if (S.q) p.set('q', S.q);
  if (S.source) p.set('source', S.source);
  if (S.kind) p.set('kind', S.kind);
  if (S.status) p.set('status', S.status);
  if (S.hasStops) p.set('hasStops', '1');
  const d = await api('/api/ld/lines?' + p);
  const items = d.items || [];
  S.items = append ? S.items.concat(items) : items;
  S.total = d.total || 0;
  renderList();
  renderMergeBar();
}

/* ------------------------------------------------------------------ 详情 */

async function selectLine(id) {
  S.sel = id;
  $('#ldPanel').innerHTML = `<div class="ld-empty">读取中…</div>`;
  try {
    const d = await api('/api/ld/line?id=' + id);
    S.detail = d;
    renderPanel();
    renderList();
    loadGeom(id);
    loadCandidates(id);
  } catch (e) {
    $('#ldPanel').innerHTML = `<div class="ld-empty">${esc(e.message)}</div>`;
  }
}

async function loadGeom(id) {
  try {
    const g = await api('/api/ld/geom?id=' + id);
    if (S.sel !== id) return;
    S.geom = g;
    MAP.setData(g.stops || [], (g.geom && g.geom.parts) || []);
    renderMapInfo();
  } catch (e) {
    MAP.setData((S.detail && S.detail.stops) || [], []);
  }
}

async function loadCandidates(id) {
  try {
    const d = await api('/api/ld/candidates?id=' + id);
    if (S.sel !== id) return;
    const box = $('#ldCands');
    if (!box) return;
    const items = d.items || [];
    box.innerHTML = items.length
      ? items.map(c => `<div class="cand">
          <span class="s" style="background:${SRCBG(c.source)}"></span>
          <span class="nm">${esc(c.name)}</span>
          <span class="m">${c.n_stop || 0} 站 ${c.totalKm ? '· ' + fmtKm(c.totalKm) : ''}</span>
          <button class="btn sm" data-add="${esc(c.name)}" data-src="${c.source}">收</button>
        </div>`).join('')
      : '<div class="hint">同族写法都收进来了。</div>';
    $$('#ldCands [data-add]').forEach(b => {
      b.onclick = async () => {
        try {
          await api('/api/ld/alias/add', { id: S.sel, source: b.dataset.src, name: b.dataset.add });
          toast('已收作别名');
          selectLine(S.sel);
        } catch (e) { toast(e.message, true); }
      };
    });
  } catch (_) { /* 忽略 */ }
}

function renderMapInfo() {
  const d = S.detail;
  if (!d) { $('#ldMapInfo').innerHTML = '<div class="t" style="color:#77839a">未选线路</div>'; return; }
  const g = (S.geom && S.geom.geom) || null;
  const withC = (d.stops || []).filter(s => s.lat != null).length;
  const geo = g
    ? `走向 <b>${fmtKm(g.km)}</b>（OSM · ${g.nPart} 段${g.ask && g.ask !== d.name ? ' · 用「' + esc(g.ask) + '」' : ''}）`
    : `无 OSM 走向，按站点直连示意`;
  $('#ldMapInfo').innerHTML = `<div class="t">${esc(d.name)}
      <span class="tag">${KIND[d.kind] || '未分'}</span>
      <span class="tag">${d.status === 'confirmed' ? '已确认' : '待整理'}</span></div>
    <div class="r">${(d.stops || []).length} 站 · 有坐标 <b>${withC}</b> / ${(d.stops || []).length}
      · 里程 <b>${fmtKm(d.total_km)}</b><br>${geo}</div>`;
}

function renderPanel() {
  const d = S.detail;
  const box = $('#ldPanel');
  if (!d) {
    box.innerHTML = `<div class="ld-empty"><b>还没挑线路</b>
      左侧点一条线开始整理。<br>
      改规范名、收别名、理顺站序、把分段挂到整条线下面，<br>
      结果都存在独立的 <code>linedesk.db</code> 里。</div>`;
    return;
  }
  const aliases = (d.aliases || []).map(a => `<span class="al">
      <span class="s ${a.source}">${esc(SRC[a.source] || a.source)}</span>
      <span title="${esc(a.name)}">${esc(a.name)}</span>
      <span class="x" data-del="${a.id}" title="删掉这个别名">×</span>
    </span>`).join('');
  const stops = (d.stops || []).map((s, i) => `<div class="st${s.lat == null ? ' nocoord' : ''}" data-i="${i}">
      <span class="i">${i + 1}</span>
      <input class="nm" value="${esc(s.name)}" data-nm="${i}">
      <span class="km">${s.km == null ? '—' : (Math.round(Number(s.km) * 10) / 10)}</span>
      <span class="op">
        <button data-up="${i}" title="上移">↑</button>
        <button data-down="${i}" title="下移">↓</button>
        <button class="del" data-rm="${i}" title="删掉这一站">✕</button>
      </span>
    </div>`).join('');
  const parents = (d.parents || []).map(p => `<div class="rel">
      <span style="color:#77839a">属于</span>
      <a href="#" data-goto="${p.id}">${esc(p.name)}</a>
      <span class="m">${p.kind === 'seg' ? '分段建成' : '岔出'}${p.note ? ' · ' + esc(p.note) : ''}</span>
      <span class="x" data-unrel="${p.id}" title="解除">×</span>
    </div>`).join('');
  const children = (d.children || []).map(c => `<div class="rel">
      <span style="color:#77839a">含</span>
      <a href="#" data-goto="${c.id}">${esc(c.name)}</a>
      <span class="m">${c.n_stop || 0} 站${c.note ? ' · ' + esc(c.note) : ''}</span>
      <span class="x" data-unrel="${c.id}" title="解除">×</span>
    </div>`).join('');
  const fillable = (d.aliases || []).filter(a => a.source !== 'osm');

  box.innerHTML = `
  <div class="sec">
    <h4>基本信息 <span class="r">
      <button class="btn sm" id="ldConfirm">${d.status === 'confirmed' ? '取消确认' : '确认这条'}</button>
      <button class="btn sm danger" id="ldDelete">删除</button></span></h4>
    <div class="frow"><label>规范名</label><input id="ldName" value="${esc(d.name)}"></div>
    <div class="frow"><label>类型</label>
      <select id="ldKind">${Object.entries(KIND).map(([k, v]) =>
        `<option value="${k}"${d.kind === k ? ' selected' : ''}>${v}</option>`).join('')}</select>
      <label style="width:auto;flex:0 0 auto">总里程</label>
      <input id="ldKm" value="${d.total_km == null ? '' : d.total_km}" style="flex:0 0 90px">
    </div>
    <div class="frow"><label>起讫</label>
      <input value="${esc(d.from_sta || '')}" readonly>
      <input value="${esc(d.to_sta || '')}" readonly>
    </div>
    <div class="frow"><label>备注</label><textarea id="ldNote" placeholder="为什么这么归并 / 还差什么">${esc(d.note || '')}</textarea></div>
    <div class="frow" style="justify-content:flex-end;gap:6px">
      <span class="hint" style="margin-right:auto">改完记得存</span>
      <button class="btn primary" id="ldSave">保存</button>
    </div>
  </div>

  <div class="sec">
    <h4>各来源写法 <span class="n">${(d.aliases || []).length}</span>
      <span class="r"><button class="btn sm" id="ldAddAlias">+ 收一条</button></span></h4>
    <div class="tagline">${aliases || '<span class="hint">还没有别名</span>'}</div>
    <div class="hint" style="margin-top:8px">同一个线名在不同来源里写法不同（高铁 / 高速线 / 高速铁路），
      收进来的才算同一条线。</div>
  </div>

  <div class="sec">
    <h4>站序 <span class="n">${(d.stops || []).length}</span>
      <span class="r">
        ${fillable.length ? `<select id="ldFillSel" style="height:26px;font-size:11.5px;border:1px solid #e2e8f0;border-radius:7px">
          ${fillable.map((a, i) => `<option value="${i}">${esc(SRC[a.source])}：${esc(a.name)}</option>`).join('')}
        </select><button class="btn sm" id="ldFill">用它</button>` : ''}
        <button class="btn sm" id="ldAddStop">+ 站</button>
      </span></h4>
    <div class="stops" id="ldStops">${stops || '<div class="hint" style="padding:10px">这条线还没有站序，可以从上面的来源导入</div>'}</div>
    <div class="hint">点地图上的站点会定位到这一站；站名可改，↑↓ 调顺序，✕ 删掉。</div>
  </div>

  <div class="sec">
    <h4>分段 / 全线 <span class="r"><button class="btn sm" id="ldSetParent">挂到父线</button></span></h4>
    ${parents}${children}
    ${(parents || children) ? '' : '<div class="hint">还没跟别的线建立关系。分段建成的线，挂到整条线下面。</div>'}
  </div>

  <div class="sec">
    <h4>同族还没收的写法 <span class="r"><button class="btn sm" id="ldCandReload">刷新</button></span></h4>
    <div id="ldCands"><div class="hint">读取中…</div></div>
  </div>`;

  bindPanel();
}

function bindPanel() {
  const d = S.detail;
  if (!d) return;

  $('#ldSave').onclick = async () => {
    try {
      const body = {
        id: d.id,
        name: $('#ldName').value.trim(),
        kind: $('#ldKind').value,
        total_km: $('#ldKm').value.trim() === '' ? null : Number($('#ldKm').value),
        note: $('#ldNote').value,
      };
      const r = await api('/api/ld/save', body);
      toast('已保存到整理库');
      S.detail = r.line;
      renderPanel(); renderMapInfo();
      loadList();
    } catch (e) { toast(e.message, true); }
  };

  $('#ldConfirm').onclick = async () => {
    try {
      const r = await api('/api/ld/save', {
        id: d.id, status: d.status === 'confirmed' ? 'draft' : 'confirmed',
      });
      S.detail = r.line;
      renderPanel(); renderMapInfo(); loadList();
      toast(r.line.status === 'confirmed' ? '已标记确认' : '改回待整理');
    } catch (e) { toast(e.message, true); }
  };

  $('#ldDelete').onclick = async () => {
    if (!confirm(`删掉「${d.name}」？别名和站序一起删。`)) return;
    try {
      await api('/api/ld/delete', { id: d.id });
      S.detail = null; S.sel = null; S.geom = null;
      MAP.setData([], []);
      renderPanel(); loadList();
      toast('已删除');
    } catch (e) { toast(e.message, true); }
  };

  $$('#ldPanel [data-del]').forEach(x => {
    x.onclick = async () => {
      try {
        const r = await api('/api/ld/alias/del', { aliasId: Number(x.dataset.del) });
        S.detail = r.line; renderPanel();
      } catch (e) { toast(e.message, true); }
    };
  });

  $('#ldAddAlias').onclick = () => openAliasPick();
  $('#ldCandReload').onclick = () => loadCandidates(d.id);

  // 站序
  const stops = () => (S.detail.stops || []).slice();
  const commit = async (arr) => {
    try {
      const r = await api('/api/ld/stops/save', { id: d.id, stops: arr });
      S.detail = r.line;
      renderPanel(); renderMapInfo();
      MAP.setData((r.line.stops || []), (S.geom && S.geom.geom && S.geom.geom.parts) || []);
      loadList();
    } catch (e) { toast(e.message, true); }
  };
  $$('#ldPanel [data-up]').forEach(b => {
    b.onclick = () => {
      const i = Number(b.dataset.up), a = stops();
      if (i <= 0) return;
      [a[i - 1], a[i]] = [a[i], a[i - 1]];
      commit(a);
    };
  });
  $$('#ldPanel [data-down]').forEach(b => {
    b.onclick = () => {
      const i = Number(b.dataset.down), a = stops();
      if (i >= a.length - 1) return;
      [a[i + 1], a[i]] = [a[i], a[i + 1]];
      commit(a);
    };
  });
  $$('#ldPanel [data-rm]').forEach(b => {
    b.onclick = () => {
      const i = Number(b.dataset.rm), a = stops();
      a.splice(i, 1);
      commit(a);
    };
  });
  $$('#ldPanel input[data-nm]').forEach(inp => {
    inp.onchange = () => {
      const i = Number(inp.dataset.nm), a = stops();
      const v = inp.value.trim();
      if (!v) { renderPanel(); return; }
      a[i] = { ...a[i], name: v, lat: null, lon: null };   // 改名后坐标重新查
      commit(a);
    };
  });
  const fillBtn = $('#ldFill');
  if (fillBtn) {
    fillBtn.onclick = async () => {
      const sel = $('#ldFillSel');
      const a = (S.detail.aliases || []).filter(x => x.source !== 'osm')[Number(sel.value)];
      if (!a) return;
      if ((S.detail.stops || []).length &&
          !confirm(`用「${SRC[a.source]}：${a.name}」的站序覆盖现在这 ${S.detail.stops.length} 站？`)) return;
      try {
        const r = await api('/api/ld/stops/fill', { id: d.id, source: a.source, name: a.name });
        S.detail = r.line;
        renderPanel(); renderMapInfo();
        MAP.setData((r.line.stops || []), (S.geom && S.geom.geom && S.geom.geom.parts) || []);
        loadList();
        toast(`已导入 ${(r.line.stops || []).length} 站`);
      } catch (e) { toast(e.message, true); }
    };
  }
  const addStop = $('#ldAddStop');
  if (addStop) {
    addStop.onclick = () => {
      const name = prompt('加哪一站？（写在最后，可以再用 ↑ 调位置）');
      if (!name || !name.trim()) return;
      const a = stops();
      a.push({ name: name.trim(), km: null, lat: null, lon: null, src: 'manual' });
      commit(a);
    };
  }

  // 父子
  $$('#ldPanel [data-goto]').forEach(a => {
    a.onclick = e => { e.preventDefault(); selectLine(Number(a.dataset.goto)); };
  });
  $$('#ldPanel [data-unrel]').forEach(x => {
    x.onclick = async () => {
      const other = Number(x.dataset.unrel);
      try {
        const isParent = (S.detail.parents || []).some(p => p.id === other);
        await api('/api/ld/relation/del', isParent
          ? { parent: other, child: S.detail.id }
          : { parent: S.detail.id, child: other });
        selectLine(S.detail.id);
      } catch (e) { toast(e.message, true); }
    };
  });
  $('#ldSetParent').onclick = () => openParentPick();
}

/* ------------------------------------------------------------------ 弹窗 */

function openModal(title, bodyHtml, onOk, okText) {
  const wrap = document.createElement('div');
  wrap.className = 'modal';
  wrap.innerHTML = `<div class="modal-card">
    <h3>${esc(title)}</h3>
    <div class="modal-body">${bodyHtml}</div>
    <div class="modal-foot">
      <span class="msg" id="mdMsg"></span>
      <button class="btn" id="mdCancel">取消</button>
      <button class="btn primary" id="mdOk">${esc(okText || '确定')}</button>
    </div></div>`;
  document.body.appendChild(wrap);
  const close = () => wrap.remove();
  $('#mdCancel', wrap).onclick = close;
  wrap.addEventListener('click', e => { if (e.target === wrap) close(); });
  $('#mdOk', wrap).onclick = async () => {
    const msg = $('#mdMsg', wrap);
    msg.className = 'msg';
    try {
      await onOk(wrap, msg);
    } catch (e) {
      msg.className = 'msg err';
      msg.textContent = e.message;
    }
  };
  return wrap;
}

async function openParentPick() {
  const d = S.detail;
  const w = openModal('挂到哪条父线下面', `
    <div class="frow"><label>找线</label><input id="ppQ" placeholder="线名，如 京哈高速线"></div>
    <div class="picklist" id="ppList"><div class="hint" style="padding:10px">输入关键字找线</div></div>
    <div class="hint">只在整理库里找。父线是整条线的规范名，分段挂上去之后，
      查整条线能看见它由哪几段组成。</div>`, async (wrap, msg) => {
    const checked = $('#ppList', wrap).querySelector('.pick.on');
    if (!checked) throw new Error('先选一条父线');
    const r = await api('/api/ld/relation', {
      parent: Number(checked.dataset.id), child: d.id, kind: 'seg',
    });
    wrap.remove();
    S.detail = r.line && r.line.id === d.id ? r.line : S.detail;
    selectLine(d.id);
    toast('已挂上');
  }, '挂上');

  const box = $('#ppList', w);
  const search = async () => {
    const q = $('#ppQ', w).value.trim();
    const dd = await api('/api/ld/lines?limit=40' + (q ? '&q=' + encodeURIComponent(q) : ''));
    box.innerHTML = (dd.items || []).filter(x => x.id !== d.id).map(x =>
      `<div class="pick" data-id="${x.id}">${esc(x.name)}
        <span class="m">${x.n_stop || 0} 站 · ${KIND[x.kind] || ''}</span></div>`).join('')
      || '<div class="hint" style="padding:10px">没找到</div>';
    $$('.pick', box).forEach(p => {
      p.onclick = () => {
        $$('.pick', box).forEach(o => o.classList.remove('on'));
        p.classList.add('on');
      };
    });
  };
  $('#ppQ', w).oninput = debounce(search, 220);
  search();
}

async function openMerge() {
  const ids = Array.from(S.picks);
  const dd = await api('/api/ld/lines?limit=300');
  const chosen = (dd.items || []).filter(x => ids.includes(x.id));
  const w = openModal('合并成一条线', `
    <div class="hint" style="margin:0 0 8px">选中的 ${ids.length} 条会并成一条：
      别名都并进来，站序默认留目标线自己的（它没有才借别人的）。</div>
    <div class="picklist" id="mgList">
      ${chosen.map((x, i) => `<div class="pick${i === 0 ? ' on' : ''}" data-id="${x.id}">
        <span>${esc(x.name)}</span><span class="m">${x.n_stop || 0} 站</span></div>`).join('')}
    </div>
    <div class="frow"><label>规范名</label><input id="mgName" value="${esc(chosen[0] ? chosen[0].name : '')}"></div>
    <div class="hint">点上面一条，站序以它为准，名字可以另填。</div>`,
    async (wrap) => {
      const pick = $('#mgList', wrap).querySelector('.pick.on');
      if (!pick) throw new Error('选一条目标线');
      const into = Number(pick.dataset.id);
      const nm = $('#mgName', wrap).value.trim();
      const r = await api('/api/ld/merge', { ids, into, name: nm });
      wrap.remove();
      S.merge = false; S.picks.clear();
      await loadList();
      selectLine(r.line.id);
      toast('已合并');
    }, '合并');

  $$('#mgList .pick', w).forEach(p => {
    p.onclick = () => {
      $$('#mgList .pick', w).forEach(o => o.classList.remove('on'));
      p.classList.add('on');
      $('#mgName', w).value = p.querySelector('span').textContent;
    };
  });
}

async function openAliasPick() {
  const d = S.detail;
  const w = openModal('收一条来源线名', `
    <div class="frow"><label>来源</label>
      <select id="apSrc">${Object.entries(SRC).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
    </div>
    <div class="frow"><label>找线名</label><input id="apQ" placeholder="在五套来源里搜，如 京沪"></div>
    <div class="picklist" id="apList"><div class="hint" style="padding:10px">输入关键字搜来源线名</div></div>
    <div class="hint">默认只搜选中的来源；把来源切到「全部」可以跨来源搜。</div>`,
    async (wrap) => {
      const pick = $('#apList', wrap).querySelector('.pick.on');
      if (!pick) throw new Error('先选一条');
      await api('/api/ld/alias/add', {
        id: d.id, source: pick.dataset.src, name: pick.dataset.name,
      });
      wrap.remove();
      selectLine(d.id);
      toast('已收下');
    }, '收下');

  const box = $('#apList', w);
  const doSearch = async () => {
    const q = $('#apQ', w).value.trim();
    const src = $('#apSrc', w).value;
    const dd = await api('/api/ld/source/search?limit=60' + (q ? '&q=' + encodeURIComponent(q) : ''));
    let items = dd.items || [];
    if (src) items = items.filter(x => x.source === src);
    box.innerHTML = items.map(x => `<div class="pick" data-src="${x.source}" data-name="${esc(x.name)}">
        <span class="s" style="background:${SRCBG(x.source)}"></span>
        <span>${esc(x.name)}</span>
        <span class="m">${x.n_stop || 0} 站</span></div>`).join('')
      || '<div class="hint" style="padding:10px">没找到</div>';
    $$('.pick', box).forEach(p => {
      p.onclick = () => {
        $$('.pick', box).forEach(o => o.classList.remove('on'));
        p.classList.add('on');
      };
    });
  };
  $('#apQ', w).oninput = debounce(doSearch, 250);
  $('#apSrc', w).onchange = doSearch;
  doSearch();
}

function debounce(fn, ms) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ------------------------------------------------------------------ 启动 */

async function loadStats() {
  try {
    const s = await api('/api/ld/stats');
    $('#ldStats').innerHTML = `
      <span>规范线 <b>${s.lines}</b></span>
      <span>已确认 <b>${s.confirmed}</b></span>
      <span>别名 <b>${s.aliases}</b></span>
      <span>有站序 <b>${s.withStops}</b></span>
      <span>父子 <b>${s.relations}</b></span>
      <span style="opacity:.7">初稿 ${esc(s.seededAt || '—')}</span>`;
  } catch (_) { /* 忽略 */ }
}

function bindTop() {
  $('#ldGo').onclick = () => { S.q = $('#ldQ').value.trim(); loadList(); };
  $('#ldQ').onkeydown = e => { if (e.key === 'Enter') { S.q = $('#ldQ').value.trim(); loadList(); } };

  $('#btnMerge').onclick = () => {
    S.merge = !S.merge;
    S.picks.clear();
    $('#btnMerge').classList.toggle('on', S.merge);
    renderList(); renderMergeBar();
  };

  $('#btnNew').onclick = () => {
    const w = openModal('新建一条线路', `
      <div class="frow"><label>线名</label><input id="nlName" placeholder="规范名，如 沪苏湖高铁"></div>
      <div class="hint">只建一条空线，站序可以在右侧「+ 站」补，或者从来源导入。</div>`,
      async (wrap) => {
        const nm = $('#nlName', wrap).value.trim();
        if (!nm) throw new Error('线名不能为空');
        const r = await api('/api/ld/new', { name: nm });
        wrap.remove();
        await loadList(); loadStats();
        selectLine(r.line.id);
      }, '建');
    setTimeout(() => { const i = $('#nlName', w); if (i) i.focus(); }, 30);
  };

  $('#btnRebuild').onclick = async () => {
    if (!confirm('从 rail.db 补新线名（已整理过的不动）。继续？')) return;
    try {
      const r = await api('/api/ld/rebuild', { force: false });
      toast(`补进来 ${r.lines} 条线 / ${r.aliases} 个写法`);
      await loadList(); loadStats();
    } catch (e) { toast(e.message, true); }
  };

  $('#btnExport').onclick = async () => {
    try {
      const d = await api('/api/ld/export');
      const blob = new Blob([JSON.stringify(d, null, 1)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'linedesk-export-' + new Date().toISOString().slice(0, 10) + '.json';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      toast(`导出 ${d.counts.lines} 条线 / ${d.counts.relations} 条关系`);
    } catch (e) { toast(e.message, true); }
  };

  $('#btnLog').onclick = async () => {
    const d = await api('/api/ld/log?limit=80');
    openModal('改动记录', `<div style="font-size:12px;line-height:1.9">
      ${(d.items || []).map(x => `<div><span style="color:#77839a">${esc(x.ts)}</span>
        <b>${esc(x.op)}</b> ${esc(x.target)} <span style="color:#77839a">${esc(x.detail || '')}</span></div>`).join('')
        || '<div class="hint">还没有记录</div>'}</div>`, async () => {}, '关闭');
  };

  $$('.ld-mapctl button').forEach(b => {
    b.onclick = () => {
      const z = b.dataset.zoom;
      if (z === 'in') MAP.zoom(1.35);
      else if (z === 'out') MAP.zoom(1 / 1.35);
      else MAP.fit();
    };
  });

  window.addEventListener('keydown', e => {
    if (e.key === 'Escape' && S.merge) {
      S.merge = false; S.picks.clear();
      $('#btnMerge').classList.remove('on');
      renderList(); renderMergeBar();
    }
  });
}

(async function boot() {
  $$('.ld-basebar [data-base]').forEach(b => {
    b.onclick = async () => {
      const m = b.dataset.base;
      if (m === 'tmap' && !mapKey()) { openKeyModal(); return; }
      localStorage.setItem('railcn.ldbase', m);
      await MAP.use(m);
    };
  });
  await MAP.use();
  bindTop();
  renderFilters();
  renderPanel();
  // ?q= 带关键字、?id= 直接打开某条线（书签 / 从别处跳过来）、?base= 指定底图
  const ps = new URLSearchParams(location.search);
  if (ps.get('base')) await MAP.use(ps.get('base'));
  if (ps.get('q')) { S.q = ps.get('q'); $('#ldQ').value = S.q; }
  await loadList();
  loadStats();
  const openId = Number(ps.get('id') || 0);
  if (openId) selectLine(openId);
})();
