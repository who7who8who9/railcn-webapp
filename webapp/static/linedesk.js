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
const SRC = { lib: '底表', custom: '自建', huirui: '资料库', ce: 'china-emu', osm: 'OSM',
              rg: 'rail-globe 走向', hdx: 'OSM 走向', cnrail: 'cnrail 走向' };
const KIND = { gs: '高速', cj: '城际', sj: '市域', lj: '联络线', zx: '支线', px: '普速', '': '未分' };

const S = {
  q: '', source: '', kind: '', status: '', hasStops: '',
  items: [], total: 0,
  sel: null, detail: null, geom: null, ref: null,
  merge: false, picks: new Set(),
  /* ①②里「当前选中的那一条」：line = 'src\u0001name'，stop = {source,name} / {self:true} */
  pick: { line: '', stop: null },
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
function toast(msg, isErr, ms) {
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
  toastT = setTimeout(() => { el.className = 'toast' + (isErr ? ' err' : ''); }, ms || 2200);
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
          cursor: 'pointer',
        }, this.gLabels);
        t.textContent = s.name;
        /* 站名标签（小窗口）也能点：点 = 选中这一站，别穿到底图 */
        t.addEventListener('click', ev => {
          ev.stopPropagation();
          pickStop(i);
        });
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
    /* 站名 label 也要能点选中这一站（否则点击穿到底图） */
    this.layers.labels.on('click', evt => {
      const id = evt && evt.geometry && evt.geometry.id;
      if (id && /^L\d+$/.test(String(id))) pickStop(Number(String(id).slice(1)));
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
        /* interactive: 站名标签是「地图上的小窗口」，默认 pointer-events:none
         * 会穿到底图 —— 要能点，点 = 选中这一站 */
        const tip = c.bindTooltip(s.name, {
          permanent: true, direction: 'right', offset: [5, 0],
          className: on ? 'ld-tip sel' : 'ld-tip', interactive: true,
        }).getTooltip();
        if (tip) tip.on('click', () => pickStop(i));
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

/** 使用规则：这个页面怎么用（顶部「使用说明」） */
function openHelp() {
  const w = openModal('线路整理台 · 使用规则', `
  <div class="md-help">
    <p class="lead">这个页面的活只有一件：<b>把同一条线的各个来源写法，由你判断归并成一条
      「确认线路」</b>。③ 合并保存一步到位：既进主站「车站/线路」，也定为基本线路，
      让数据校对 / 地图 / 选经由都用它。整理成果在独立库 <code>linedesk.db</code>；
      ③ 会把定稿的站序与走向写进 <code>rail.db</code> 的 <code>line_base</code>（可撤销）。</p>

    <h4>一、整理一条线的完整流程（基本信息 → ① → ② + 站序 → ③）</h4>
    <ol>
      <li><b>基本信息</b> —— 规范名 / 类型 / 总里程 / 起讫 / 备注，点「保存基本信息」就
        <b>正式写入整理库</b>，不依赖后面的步骤。线路库里没有的新线，顶栏 <b>+ 新线</b> 或 <b>导入 GeoJSON</b>。</li>
      <li><b>① 线路来源</b> —— <b>只确认并保存这条铁路线的 OSM 值（走向）</b>，
        <b>和线路停靠的车站无关</b>。已收的写法里点一行选中（地图画出它的走向），
        或 <b>导入走向</b>（GeoJSON，只取线形），点「确认线路来源」正式保存。
        <b>只要有走向就能选，哪怕这条来源 0 站</b>（行尾标「0 站 · 仅走向」）。
        这里<b>不新增 / 不改动任何车站</b>，也不改规范名和起讫。</li>
      <li><b>② 站点来源</b> —— <b>只确认并保存这条线的全部车站与位置</b>，
        <b>和 OSM 路线无关</b>。点一行选中再点「用它并确认」把站点和定位搬过来；
        「导入站点」也只取车站。<b>不保存、不改动线路走向</b>。</li>
      <li><b>站序（就在 ② 下面）</b> —— 逐站改：改站名、↑↓ 调顺序、✕ 删站。
        <b>+ 新增车站可以指定插入位置</b>：点某一站行的 <b>+</b> 插在它后面，
        或点顶部「+ 站（可指定位置）」在弹窗里选「最前面 / 第 N 站之后」，
        一次填多个站名会按填写顺序连续插入；新站里程留空、坐标按站名查档案补。
        点 <b>「反向站序」</b>整条线倒过来：里程 = 原末站里程 − 原里程、新起点 0，
        原本缺里程的站仍留空；坐标、总里程、线路走向都不变。<b>改完站序 ② 会自动选中
        「现在的站序」</b>，再点「用它并确认」只确认这份站序、站序不会被来源覆盖；
        想用回来源那份完整站序，点来源行再确认（会覆盖你的改动）。</li>
      <li><b>③ 合并保存为确认线路</b> —— <b>把 ① 和 ② 合并保存，并且同时定为基本线路</b>
        （原来单独的 ④ 已取消，也不再有单独的走向配置）。做完这一步：
        主站「车站/线路」就能查到这条线（含<b>停站与线路路线位置</b>），
        数据校对 / 地图 / 选经由也一起接管。要求：≥2 站且带定位、有真实走向、规范名是正名，缺一样会拦住。
        已定稿的线改了 ① / ② / 站序，点「重新合并保存（覆盖基本线路）」即可，不用先撤销。</li>
    </ol>

    <h4>二、导入 GeoJSON（① 只导走向 / ② 只导站点）</h4>
    <ul>
      <li>顶栏 <b>导入 GeoJSON</b>：可多选文件，每份文件落成一条<b>待整理</b>的新线（站序 + 走向都进来）。</li>
      <li>① <b>导入走向</b>：只写线路走向，<b>车站一个都不动</b>。</li>
      <li>② <b>导入站点</b>：只写车站与定位，<b>线路走向一个字节都不动</b>。</li>
      <li>站序不是按文件里点的先后，而是<b>按走向算</b>：端点建图 → 车站投影切段 →
        主轴排序 + 局部调整 → 站间最短路累里程。文件自带里程且递增时以文件为准。</li>
      <li>导入结果会报「几个车站没落到走向上」「走向断了几处」，跳得多的先看一眼文件质量。</li>
      <li>走向存在整理库 <code>linedesk.db / ld_geom</code>；③ 合并时直接用 ① 定下的那份。</li>
    </ul>

    <h4>三、③ 合并保存后的效力（全面接管）</h4>
    <ul>
      <li>主站「车站/线路」只列这样的确认线路（含<b>停站 + 线路路线位置</b>）；
        其它线路、来源版本、选线都不再显示。</li>
      <li>数据校对的线路下拉只列这一条（排最前），地图用它的走向，选经由用它的站序。</li>
      <li>备份导回来的各来源版本<b>数据仍在库里、只是让位</b>：数据校对 / 备份面板还能查。</li>
      <li>③ 区块右上角有<b>「撤销基本线路」</b>，随时退回原来的取数规则；
        改了内容想覆盖，直接点「重新合并保存」。</li>
    </ul>

    <h4>四、地图操作</h4>
    <ul>
      <li><b>点站点圆点或站名小标签</b> = 选中该站，右侧站序对应行滚动并高亮。</li>
      <li><b>点候选行</b> = 选中这条来源（① 走向 / ② 站点+定位），地图同时画出它的线；
        再点「← 回到本线」回到本线。</li>
      <li>右上 <b>+ / − / 适应</b> 缩放；左下切换 <b>腾讯 / OSM / 示意</b> 底图（腾讯要配 Key）。</li>
      <li>信息窗里随时能看出当前画的是本线还是某个来源的预览。</li>
    </ul>

    <h4>五、规则与注意</h4>
    <ul>
      <li><b>规范名 = 资料库（辉睿）介绍里的中文名称</b>；资料库里没有的线才用别的名字。
        想套用资料库的正式名称 / 起讫，用面板底部「资料库介绍」里的两个按钮手动点（① 不再自动改名）。
        「京沪铁路」和「京沪高铁」是两条线，名字不同就算两条。</li>
      <li><b>规范名不能带来源后缀</b>（「（底表）」「（OSM）」「（线路资料库·82站）」）——
        ③ 合并保存会拦住，面板里给了一键改成正名。</li>
      <li><b>来源一开始是空的</b>：新线、导入的线都不带来源，必须自己收、自己确认。
        换过站序或改过站名，站点来源要重新确认。</li>
      <li>③ 的前提：<b>① ② 都确认</b> + 站序 <b>≥ 2 站</b>且至少 2 站带定位 + 有真实走向。</li>
      <li>「补新线」从 rail.db 同步新出现的线名，已整理过的不动。</li>
      <li>所有改动进顶栏「<b>记录</b>」可追溯；「导出 JSON」可带走整理成果。</li>
    </ul>

    <h4>六、名词</h4>
    <ul>
      <li><b>线路来源（①）</b> —— 这条线用哪一家来源的<b>走向 / OSM 值</b>。一条线只选一条，
        只决定线形，<b>不决定车站</b>。</li>
      <li><b>站点来源（②）</b> —— 站序和<b>每一站的定位</b>从哪来（底表 / 资料库 / china-emu / 自建 /
        GeoJSON / 各走向包）。同样只选一条，只决定车站，<b>不决定线形</b>。</li>
      <li><b>同一条线</b> —— 去掉「（来源）」后缀后同名：「京沪线」「京沪线（底表）」算一条。</li>
      <li><b>确认线路 / 基本线路</b> —— ③ 合并保存的产物，主站与全项目取数都用它。</li>
    </ul>
  </div>`,
    (wrap) => wrap.remove(), '知道了');
  /* 帮助弹窗宽一点，正文要能滚动看完 */
  const card = w.querySelector('.modal-card');
  if (card) card.classList.add('wide');
}

/* ------------------------------------------------------------------ 列表 */

function chipsHtml() {
  const srcs = [['', '全部来源'], ['lib', '底表'], ['custom', '自建'], ['huirui', '资料库'],
                ['ce', 'china-emu'], ['osm', 'OSM'], ['rg', 'rail-globe 走向'],
                ['hdx', 'OSM 走向'], ['cnrail', 'cnrail 走向']];
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
    /* 来源各自确认的进度：线路✓ / 站点✓，一眼看出这条线还缺哪一步 */
    const fl = `<span class="srcf">${it.line_ok ? '线路✓' : '线路○'} ${it.stop_ok ? '站点✓' : '站点○'}</span>`;
    return `<div class="ld-item${on}${pick}" data-id="${it.id}">
      <div class="nm">${cb}${esc(it.name)}${st}</div>
      <div class="meta">
        <span class="k">${KIND[it.kind] || it.kind || '—'}</span>
        <span>${it.n_stop || 0} 站</span>
        <span>${fmtKm(it.total_km)}</span>
        ${fl}
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
  return { lib: '#0f2d52', custom: '#6b46c1', huirui: '#1f6feb', ce: '#0f9d58', osm: '#8b93a5',
           rg: '#0e7490', hdx: '#b45309', cnrail: '#0d9488' }[s] || '#8b93a5';
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
  S.preview = null;                     // 换线了，来源预览作废
  S.pick = { line: '', stop: null };    // ①②的选中状态也清掉
  S.sel = id;
  S.ref = null;
  $('#ldPanel').innerHTML = `<div class="ld-empty">读取中…</div>`;
  try {
    const d = await api('/api/ld/line?id=' + id);
    S.detail = d;
    await refreshRef();
    renderPanel();
    renderList();
    loadGeom(id);
    loadCandidates(id);
  } catch (e) {
    $('#ldPanel').innerHTML = `<div class="ld-empty">${esc(e.message)}</div>`;
  }
}

/** 取这条线的资料库介绍（正式名称 / 起讫站点的口径） */
async function refreshRef() {
  const d = S.detail;
  if (!d) { S.ref = null; return; }
  try {
    const r = await api('/api/ld/ref?name=' + encodeURIComponent(d.name));
    S.ref = r.item || null;
  } catch (_) { S.ref = null; }
}

/** 把资料库的中文名称 / 起止站点应用到这条线 */
async function applyRef(what) {
  const d = S.detail;
  if (!d) return;
  try {
    const r = await api('/api/ld/ref/apply', { id: d.id, what });
    if (r.conflict) toast(r.conflict, true);
    else toast(`已套用资料库的${r.applied.join('、') || '内容'}`);
    S.detail = r.line;
    await refreshRef();
    renderPanel(); renderMapInfo(); loadList();
  } catch (e) { toast(e.message, true); }
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
  /* 正在预览某个来源写法：地图上画的是那条来源的站序，不是本线 */
  if (S.preview) {
    const p = S.preview;
    const geo = !p.hasGeom
      ? '这个来源没有走向，按站点直连示意'
      : p.spineOnly
        ? `走向 <b>${fmtKm(p.geomKm)}</b>（主轴拼接 ${p.nPart} 段，断口按直线补齐）`
        : `走向 <b>${fmtKm(p.geomKm)}</b>（原始 ${p.nPart} 段${
            p.thinned ? ' · 抽稀到 ' + p.nPt + ' 点' : ''}）`;
    $('#ldMapInfo').innerHTML = `<div class="t" style="color:#c47f17">预览来源 · ${esc(p.name)}
        <span class="tag">${esc(SRC[p.src] || p.src)}</span></div>
      <div class="r">${p.n} 站 · 有坐标 <b>${p.coordN}</b> / ${p.n}
        ${p.totalKm != null ? ' · 里程 <b>' + fmtKm(p.totalKm) + '</b>' : ''}<br>${geo}<br>
        <a href="#" id="ldPrevBack" style="color:var(--accent)">← 回到本线「${esc(d.name)}」</a></div>`;
    const back = $('#ldPrevBack');
    if (back) back.onclick = (e) => { e.preventDefault(); restoreSelf(); };
    return;
  }
  const g = (S.geom && S.geom.geom) || null;
  const withC = (d.stops || []).filter(s => s.lat != null).length;
  const geo = g
    ? `走向 <b>${fmtKm(g.km)}</b>（${g.src === 'route'
        ? '合并的来源包走向'
        : (g.src === 'geojson' ? '导入的 GeoJSON' : 'OSM')} · ${g.nPart} 段${
        g.ask && g.ask !== d.name ? ' · 用「' + esc(g.ask) + '」' : ''}）`
    : `无走向，按站点直连示意`;
  $('#ldMapInfo').innerHTML = `<div class="t">${esc(d.name)}
      <span class="tag">${KIND[d.kind] || '未分'}</span>
      <span class="tag">${d.status === 'confirmed' ? '已确认' : '待整理'}</span></div>
    <div class="r">${(d.stops || []).length} 站 · 有坐标 <b>${withC}</b> / ${(d.stops || []).length}
      · 里程 <b>${fmtKm(d.total_km)}</b><br>${geo}</div>`;
}

/** 在地图上预览某个来源写法的站序（各来源写法 / 收一条里搜到的，点了就画） */
async function previewSource(src, name, alEl, kind = 'line') {
  if (S.preview && S.preview.src === src && S.preview.name === name) {
    return restoreSelf();          // 再点一次 = 回到本线
  }
  let dd;
  try {
    dd = await api('/api/ld/source/stops?source=' + encodeURIComponent(src) +
                   '&name=' + encodeURIComponent(name));
  } catch (e) { toast('读不到这条来源：' + e.message, true); return; }
  const stops = dd.stops || [];
  const parts = dd.parts || [];
  /* ① 线路来源只要「有走向」就能用，哪怕这条来源 0 站（line_route 里
     rg / hdx / cnrail 有大把这种，京沪高铁就是）；② 站点来源才必须有站序。 */
  if (kind === 'stop' && !stops.length) {
    toast('这个来源只有走向、没有站序，当不了②站点来源（可以当①线路来源）', true); return;
  }
  if (!stops.length && !parts.length) {
    toast('这个来源既没有站序也没有走向，用不了', true); return;
  }
  S.preview = { src, name, n: stops.length,
                coordN: dd.coordN != null ? dd.coordN : stops.filter(s => s.lat != null).length,
                totalKm: dd.totalKm,
                nPart: dd.nPart || 0, nPt: dd.nPt || 0,
                geomKm: dd.geomKm != null ? dd.geomKm : dd.totalKm,
                hasGeom: parts.length > 0, thinned: !!dd.thinned,
                spineOnly: !!dd.spineOnly };
  /* 新导入的三套走向来源（rail-globe / OSM / cnrail）自带几何，直接画实际走向；
     底表 / 资料库 / china-emu / 自建只有站序，退回点 + 直连示意 */
  MAP.setData(stops, parts);
  renderMapInfo();
  $$('#ldPanel .al').forEach(x => x.classList.remove('on'));
  if (alEl) alEl.classList.add('on');
  toast(!stops.length
    ? `地图上显示「${name}」的走向（${parts.length} 段）；这条来源没有站点，① 只用它的走向`
    : parts.length
      ? `地图上显示来源「${name}」的 ${stops.length} 站 + 实际走向`
      : `地图上显示的是来源「${name}」（${stops.length} 站，这个来源没有走向）`);
}

/** ③ 合并保存前的对照：走向来自哪、站点来自哪，一眼看清合成的是什么 */
function mergeSummary(d) {
  if (!d) return '';
  const alAll = d.aliases || [];
  const lp = alAll.find(a => a.source + '\u0001' + a.name === S.pick.line) || null;
  const lineTxt = lp
    ? `<b>${esc(SRC[lp.source] || lp.source)} · ${esc(lp.name)}</b>`
      + (lp.hasGeom
        ? `（走向 ${lp.km != null ? fmtKm(lp.km) : '有'}${lp.nPart ? ' · ' + lp.nPart + ' 段' : ''}）`
        : '（这来源没有走向，只借名字）')
    : '<span class="warn">还没选</span>';
  const sp = S.pick.stop;
  let stopTxt;
  if (sp && sp.self) {
    stopTxt = `<b>本线现在的站序</b>（${(d.stops || []).length} 站 · 定位 ${d.coordDone || 0}）`;
  } else if (sp) {
    const a = alAll.find(x => x.source === sp.source && x.name === sp.name) || {};
    stopTxt = `<b>${esc(SRC[sp.source] || sp.source)} · ${esc(sp.name)}</b>`
      + `（${a.nStop || 0} 站 · 定位 ${a.coordN || 0}）`;
  } else {
    stopTxt = '<span class="warn">还没选</span>';
  }
  return `<div class="mrow"><label>① 走向</label><span>${lineTxt}</span></div>
    <div class="mrow"><label>② 站点</label><span>${stopTxt}</span></div>
    <div class="mrow"><label>③ 合成</label><span>一条确认线路 + 基本线路：按 ① 走向画线，按 ② 站序与定位落站</span></div>`;
}

/** 回到本线的站序 + 走向 */
function restoreSelf() {
  S.preview = null;
  $$('#ldPanel .al').forEach(x => x.classList.remove('on'));
  const d = S.detail;
  MAP.setData((d && d.stops) || [],
              (S.geom && S.geom.geom && S.geom.geom.parts) || []);
  renderMapInfo();
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
  /* ①② 都是「已收的写法里挑一条」：① 挑走向，② 挑站点和定位。
     每行右边标出体量（有没有走向 / 多少站 / 几个有定位），点一行就选中（单选）。 */
  const alAll = d.aliases || [];
  const keyOf = (a) => a.source + '\u0001' + a.name;
  const confLine = (d.srcLineItems || [])[0];
  if (!S.pick.line || !alAll.some(a => keyOf(a) === S.pick.line)) {
    S.pick.line = confLine ? keyOf(confLine) : '';
  }
  const stopCands = alAll.filter(a => (a.nStop || 0) >= 2);
  const pStop = S.pick.stop;
  const stopPickOk = pStop && (pStop.self ||
    stopCands.some(a => a.source === pStop.source && a.name === pStop.name));
  if (!stopPickOk) {
    const sp = d.stopSrc || {};
    /* 站序被人工删/加/改名/反向过：② 默认选「现在的站序」，
       别自动选回来源行 —— 否则点「用它并确认」会把来源那份完整站序灌回来，
       用户刚删的那一站又冒出来（2026-11-06 实测问题）。 */
    const canSrc = !d.stopsEdited && sp.source && sp.source !== 'geojson'
      && stopCands.some(a => a.source === sp.source && a.name === sp.name);
    S.pick.stop = canSrc ? { source: sp.source, name: sp.name }
      : ((d.stops || []).length >= 2 ? { self: true } : null);
  }
  const rowHtml = (a, kind) => {
    const on = kind === 'line' ? keyOf(a) === S.pick.line
      : !!(S.pick.stop && !S.pick.stop.self
           && S.pick.stop.source === a.source && S.pick.stop.name === a.name);
    const meta = [];
    if (kind === 'line') {
      meta.push(a.hasGeom
        ? `走向 ${a.km != null ? fmtKm(a.km) : '有'}${a.nPart ? ' · ' + a.nPart + ' 段' : ''}`
        : '没走向');
      if (a.nStop) meta.push(`${a.nStop} 站`);
      /* 0 站但有走向：明确标成「能当①用」，别让人以为选不了 */
      else if (a.hasGeom) meta.push('0 站 · 仅走向');
      if (a.ok) meta.push('已确认');
    } else {
      meta.push(`${a.nStop || 0} 站`, `定位 ${a.coordN || 0}`);
      if (a.km != null) meta.push(fmtKm(a.km));
    }
    return `<div class="srcrow ${kind}${on ? ' on' : ''}" data-src="${esc(a.source)}"
        data-name="${esc(a.name)}" data-kind="${kind}"
        title="点这里选中${kind === 'line' ? '它当这条线的走向' : '它当站点来源'}，地图上同时画出它的${kind === 'line' ? '走向' : '站序'}">
      <span class="rd"></span>
      <span class="s ${a.source}">${esc(SRC[a.source] || a.source)}</span>
      <span class="nm">${esc(a.name)}</span>
      <span class="m">${meta.join(' · ')}</span>
      <span class="x" data-del="${a.id}" title="不再收这条写法">×</span>
    </div>`;
  };
  const stopSrc = d.stopSrc;
  const stopLabel = stopSrc
    ? (stopSrc.source === 'geojson'
        ? `GeoJSON · ${esc(stopSrc.name)}`
        : `${esc(SRC[stopSrc.source] || stopSrc.source)} · ${esc(stopSrc.name)}`)
    : '<span class="hint">还没选站点来源</span>';
  const stops = (d.stops || []).map((s, i) => `<div class="st${s.lat == null ? ' nocoord' : ''}" data-i="${i}">
      <span class="i">${i + 1}</span>
      <input class="nm" value="${esc(s.name)}" data-nm="${i}">
      <span class="km">${s.km == null ? '—' : (Math.round(Number(s.km) * 10) / 10)}</span>
      <span class="op">
        <button class="ins" data-ins="${i}" title="在这一站后面插入新站">+</button>
        <button data-up="${i}" title="上移">↑</button>
        <button data-down="${i}" title="下移">↓</button>
        <button class="del" data-rm="${i}" title="删掉这一站">✕</button>
      </span>
    </div>`).join('');
  /* ④「定为基本线路」连同它里面的走向下拉都取消了（2026-11-06）：
     走向只由 ① 确认，③ 合并时直接用 ① 定下的那份，不再二次配置。 */
  /* ③ 现在同时定为基本线路，规范名不能带「（来源）」后缀 —— 提前给个一键改正名 */
  const renameTo = (d.name || '').replace(/[（(](?:底表|OSM|自建|china-emu|GeoJSON|备份|线路资料库)[^）)]*[）)]/g, '')
    .replace(/#\d+$/, '').trim();
  const needRename = renameTo && renameTo !== d.name;
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
  ${(() => {
    /* 2026-11-06 新流程：基本信息 → ① 走向 → ② 站点 + 站序 → ③ 合并即基本线路。
       ④「定为基本线路」独立步骤已取消。 */
    const step = (ok, t) => `<span class="fl${ok ? ' on' : ''}">${ok ? '✓' : '○'} ${t}</span>`;
    return `<div class="ld-flow">
      ${step(!!d.name, '基本信息')}<i>›</i>
      ${step(d.line_ok, '① 走向')}<i>›</i>
      ${step(d.stop_ok, '② 站点 + 站序')}<i>›</i>
      ${step(d.status === 'confirmed', '③ 确认 = 基本线路')}
    </div>`;
  })()}

  <div class="sec">
    <h4>基本信息 <span class="r">
      <button class="btn sm danger" id="ldDelete">删除</button></span></h4>
    <div class="frow"><label>规范名</label><input id="ldName" value="${esc(d.name)}"></div>
    <div class="frow"><label>类型</label>
      <select id="ldKind">${Object.entries(KIND).map(([k, v]) =>
        `<option value="${k}"${d.kind === k ? ' selected' : ''}>${v}</option>`).join('')}</select>
      <label style="width:auto;flex:0 0 auto">总里程</label>
      <input id="ldKm" value="${d.total_km == null ? '' : d.total_km}" style="flex:0 0 90px">
    </div>
    <div class="frow"><label>起讫</label>
      <input id="ldFrom" value="${esc(d.from_sta || '')}" placeholder="起点站">
      <input id="ldTo" value="${esc(d.to_sta || '')}" placeholder="终点站">
    </div>
    <div class="frow"><label>备注</label><textarea id="ldNote" placeholder="为什么这么归并 / 还差什么">${esc(d.note || '')}</textarea></div>
    <div class="frow" style="justify-content:flex-end;gap:6px">
      <span class="hint" style="margin-right:auto">${d.updated_at
        ? '<span style="color:#0f7a45">✓ 已正式保存到整理库</span> · 最近更新 ' + esc(d.updated_at)
        : '改完记得存'}</span>
      <button class="btn primary" id="ldSave">保存基本信息</button>
    </div>
    <div class="hint" style="margin-top:-2px">基本信息点「保存」就正式写入整理库；
      <b>① ② ③ 各自只保存自己那部分</b>，不会回头改这里。</div>
  </div>

  <div class="sec${d.line_ok ? ' ok' : ''}">
    <h4>① 线路来源 <span class="n">已收 ${alAll.length} 条</span>
      <span class="r">
        <button class="btn sm" id="ldGeoGeom" title="只导入线路走向（OSM 值），不改动任何车站">导入走向</button>
        <button class="btn sm" id="ldAddSrc" title="已有的都不合适时，从各来源里再收一条进来">+ 收一条</button>
        <button class="btn sm${d.line_ok ? '' : ' primary'}" id="ldOkSrc">${d.line_ok ? '重新确认' : '确认线路来源'}</button>
      </span></h4>
    <div class="hint">① <b>只确认并保存这条铁路线的 OSM 值（走向）</b>，<b>和线路停靠的车站无关</b> ——
      这里不新增 / 删除 / 改动任何车站，也不改规范名和起讫站点。
      已收的写法里点一行选中（地图上画出它的走向）；都不合适就 <b>+ 收一条</b>，
      或 <b>导入走向</b>（GeoJSON，只取线形）。<b>只要有走向就能用，哪怕这条来源 0 站</b>
      （行尾标「0 站 · 仅走向」）。选好点「确认线路来源」正式保存。</div>
    <div class="srcrows">${alAll.length ? alAll.map(a => rowHtml(a, 'line')).join('')
      : '<div class="hint" style="padding:8px">还没收任何写法，点「+ 收一条」或「导入走向」</div>'}</div>
    <div class="hint">${d.line_ok ? '<span style="color:#0f7a45">✓ 已确认（只存了走向来源）</span>，换了一条要重新点一次' : '○ 还没确认'}</div>
  </div>

  <div class="sec${d.stop_ok ? ' ok' : ''}">
    <h4>② 站点来源 <span class="n">${(d.stops || []).length} 站 · 定位 ${d.coordDone || 0}</span>
      <span class="r">
        <button class="btn sm" id="ldAddStopSrc" title="没有合适的站点来源时，从各来源里再收一条进来">+ 收一条</button>
        <button class="btn sm" id="ldGeoIn" title="只导入车站和定位，不改动线路走向">导入站点</button>
        <button class="btn sm${d.stop_ok ? '' : ' primary'}" id="ldStopUse">用它并确认</button>
      </span></h4>
    <div class="hint">② <b>只确认并保存这条线的全部车站与位置</b>，<b>和 OSM 路线无关</b> ——
      这里<b>不保存、不改动线路走向</b>。点一行选中（地图上画出那家的站序），
      再点「用它并确认」把站点和定位搬到本线；「导入站点」也只取车站。<br>
      <b>改过站序后会默认选中「现在的站序」</b>：这时点「用它并确认」只确认这份站序，
      <b>不会把来源那份完整站序灌回来</b>；要用回来源那份，点来源行再确认（会覆盖改动）。</div>
    <div class="srcrows">${stopCands.map(a => rowHtml(a, 'stop')).join('')}
      ${(d.stops || []).length >= 2 ? `<div class="srcrow stop${
        S.pick.stop && S.pick.stop.self ? ' on' : ''}" data-self="1" data-kind="stop"
        title="用本线现在的站序和定位">
        <span class="rd"></span><span class="s custom">本线</span>
        <span class="nm">现在的站序${d.stopSrc && d.stopSrc.source === 'geojson' ? '（GeoJSON 导入）' : ''}</span>
        <span class="m">${(d.stops || []).length} 站 · 定位 ${d.coordDone || 0}</span></div>` : ''}
    </div>
    <div class="hint">当前：${stopLabel}${d.stop_ok ? '<br><span style="color:#0f7a45">✓ 已确认（只存了站点与位置）</span>' : ''}</div>
  </div>

  <div class="sec">
    <h4>站序 <span class="n">${(d.stops || []).length}</span>
      <span class="r">
        <button class="btn sm" id="ldReverse" title="整条线倒过来，里程按「原末站里程 − 原里程」重算">反向站序</button>
        <button class="btn sm" id="ldAddStop" title="在弹出的窗口里选插到哪一站之后，可一次填多个站名">+ 站（可指定位置）</button>
      </span></h4>
    <div class="stops" id="ldStops">${stops || '<div class="hint" style="padding:10px">这条线还没有站序，可以在上面「② 站点来源」里导入，也可以直接点「+ 站」</div>'}</div>
    <div class="hint">点地图上的站点会定位到这一站；站名可改，↑↓ 调顺序，✕ 删站。
      <b>+ 指定插入位置</b>：点某一站行的 <b>+</b> 就直接插在它后面；点顶部「+ 站（可指定位置）」
      可在弹窗里选「最前面 / 第 N 站之后」，一次填多个站名（换行 / 逗号 / 顿号分隔）按顺序插入。
      新站<b>里程留空、不自动插值</b>，坐标按站名查档案补；原有各站的里程和坐标不动。
      <br><b>反向站序</b> = 整条线倒过来，里程按「原末站里程 − 原里程」重算、新起点 0
      （原本缺里程的站仍留空）；只改站序和里程，不动坐标和总里程。
      改完站序 ② 会自动选中「现在的站序」，再点一次「用它并确认」即可，站序不会被覆盖。</div>
  </div>

  <div class="sec${d.status === 'confirmed' ? ' ok' : ''}">
    <h4>③ 合并保存为确认线路 <span class="n">${d.baseInfo ? '已定为基本线路' : ''}</span>
      ${d.baseInfo ? '<span class="r"><button class="btn sm danger" id="ldUnpublish">撤销基本线路</button></span>' : ''}</h4>
    <div class="hint">把 <b>① 的走向（OSM 值）</b>和 <b>② 的站点 + 位置</b>合并保存成一条确认线路，
      <b>并且同时定为基本线路</b>（数据校对 / 地图 / 选经由一起接管）——
      原来单独的 ④ 已取消，<b>这一步就到位</b>。
      <br>确认要求：≥2 个停站且带定位、有真实走向、规范名是正名，缺一样会拦住。
      <br>① / ② / 站序换过之后要再点一次合并保存，否则用的还是上一次的。</div>
    <div class="mergebox" id="ldMergeBox">${mergeSummary(d)}</div>
    ${d.line_ok && d.stop_ok ? '' : '<div class="hint warn">还差：'
      + [!d.line_ok ? '线路来源' : '', !d.stop_ok ? '站点来源' : ''].filter(Boolean).join('、')
      + '（两边都要先确认）</div>'}
    ${needRename ? `<div class="frow"><label>规范名</label>
        <input value="${esc(d.name)}" readonly style="opacity:.6">
        <button class="btn sm" id="ldRenameBase">改成「${esc(renameTo)}」</button></div>
      <div class="hint warn">规范名还带「（来源）」后缀，合并保存会拦住，先改成正名。</div>` : ''}
    <div class="hint" style="margin-top:-2px">走向就用 <b>① 确认的那份</b>，这里不再单独配置；
      要换走向回 ① 重新确认 / 重新导入，再点一次合并保存。</div>
    ${d.baseInfo ? `<div class="hint" style="color:#0f7a45">✓ 已定稿生效：${d.baseInfo.nStop} 站${
        d.baseInfo.geomName ? ' · 走向 ' + esc(d.baseInfo.geomName) : ' · 走向未配'}。
        改了站序 / 走向直接点下面「重新合并保存」覆盖，不用先撤销。</div>` : ''}
    <div class="frow" style="justify-content:flex-end">
      <button class="btn primary" id="ldFinalize"${d.line_ok && d.stop_ok ? '' : ' disabled'}>
        ${d.status === 'confirmed' ? '重新合并保存（覆盖基本线路）' : '合并保存为确认线路'}</button>
    </div>
  </div>

  <div class="sec">
    <h4>资料库介绍 <span class="n" style="font-weight:400">正式名称口径</span>
      <span class="r">
        <button class="btn sm" id="ldRefName"${S.ref ? '' : ' disabled'}>用它的中文名称</button>
        <button class="btn sm" id="ldRefFromTo"${S.ref ? '' : ' disabled'}>用它的起讫站点</button>
      </span></h4>
    ${S.ref ? `<div class="ref">
      <div class="rl"><b>${esc(S.ref.name)}</b>
        <span class="m">${esc(S.ref.full_name || '')}</span></div>
      <div class="rm">
        ${S.ref.ltype ? `<span>${esc(S.ref.ltype)}</span>` : ''}
        ${S.ref.service ? `<span>${esc(S.ref.service)}</span>` : ''}
        ${S.ref.length_km ? `<span>${esc(S.ref.length_km)} 千米</span>` : ''}
        ${S.ref.open_date ? `<span>${esc(S.ref.open_date)}</span>` : ''}
        ${(S.ref.from_sta || S.ref.to_sta) ? `<span>${esc(S.ref.from_sta)} – ${esc(S.ref.to_sta)}</span>` : ''}
      </div>
      <div class="ri">${esc(S.ref.intro || '')}</div>
      ${S.ref.url ? `<div class="ru"><a href="${esc(S.ref.url)}" target="_blank" rel="noreferrer">原页面</a></div>` : ''}
    </div>` : '<div class="hint">资料库里没有这条线（或名字对不上）。可以从「同族还没收的写法」里收一条资料库写法，或直接改规范名。</div>'}
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
        from_sta: $('#ldFrom').value.trim(),
        to_sta: $('#ldTo').value.trim(),
        note: $('#ldNote').value,
      };
      const r = await api('/api/ld/save', body);
      toast(`基本信息已正式保存到整理库${r.line && r.line.savedAt ? '（' + r.line.savedAt + '）' : ''}`);
      S.detail = r.line;
      renderPanel(); renderMapInfo();
      loadList();
    } catch (e) { toast(e.message, true); }
  };

  /* ---- ① 线路来源：只确认并保存走向（OSM 值），不碰车站 / 基本信息 ---- */
  $('#ldAddSrc').onclick = () => openLineSrcPick();
  $('#ldGeoGeom').onclick = () => pickGeojson(d.id, 'geom');
  $('#ldOkSrc').onclick = async () => {
    const a = (d.aliases || []).find(x => x.source + '\u0001' + x.name === S.pick.line)
      || (d.aliases || []).find(x => x.ok);
    if (!a) { toast('先在上面选一条线路来源（没有合适的就「+ 收一条」或「导入走向」）', true); return; }
    try {
      const r = await api('/api/ld/src/line', {
        id: d.id, items: [{ source: a.source, name: a.name }],
      });
      S.pick.line = a.source + '\u0001' + a.name;
      S.detail = r.line;
      if (r.conflict) toast(r.conflict, true);
      else toast(`① 走向已正式保存：${SRC[a.source] || a.source} · ${a.name}（站点未改动）`);
      await refreshRef();
      renderPanel(); renderMapInfo(); loadList();
    } catch (e) { toast(e.message, true); }
  };

  /* ---- ② 站点来源：只保存站点 + 定位，不碰线路走向 ---- */
  $('#ldAddStopSrc').onclick = () => openLineSrcPick('stop');
  $('#ldGeoIn').onclick = () => pickGeojson(d.id, 'stops');
  const useBtn = $('#ldStopUse');
  if (useBtn) useBtn.onclick = async () => {
    const p = S.pick.stop;
    let src = '', nm = '', fill = true, label = '';
    if (p && p.self) {
      /* 确认「本线现在的站序」：只置已确认，绝不从来源搬站序回来 */
      src = 'manual'; nm = ''; fill = false;
      label = '本线现在的站序';
    } else if (p) {
      src = p.source; nm = p.name;
      label = `${SRC[src] || src} · ${nm}`;
    } else {
      toast('先在上面选一家站点来源（想保留手工改的站序就选「现在的站序」）', true); return;
    }
    const n0 = (d.stops || []).length;
    if (fill) {
      const a = (d.aliases || []).find(x => x.source === src && x.name === nm) || {};
      if (!confirm(`用「${label}」的 ${a.nStop || '?'} 站站点和定位覆盖现在这 ${n0} 站？\n\n`
        + (d.stopsEdited ? '⚠ 你手工改过站序（删/加/改名/反向），这一步会把改动丢掉。\n' : '')
        + '若只想确认当前这份站序，请点「现在的站序」那一行再确认。')) return;
    }
    try {
      const r = await api('/api/ld/src/stop', {
        id: d.id, source: src, name: nm, fill,
      });
      S.detail = r.line;
      renderPanel(); renderMapInfo(); loadList();
      loadGeom(d.id);
      const n = (r.line.stops || []).length;
      toast(`站点来源已确认：${n} 站 · 定位 ${r.line.coordDone || 0}`
        + (fill ? '' : '（本线站序，未改动）'));
    } catch (e) { toast(e.message, true); }
  };

  /* ---- ③ 合并保存为确认线路 = 合并 ①② + 直接定为基本线路（④ 已取消） ---- */
  const fin = $('#ldFinalize');
  if (fin) fin.onclick = async () => {
    if (d.status === 'confirmed' &&
        !confirm(`重新合并保存「${d.name}」？\n\n` +
          '会用 ① 现在的走向 + ② 现在的站点与站序，覆盖已定的基本线路。')) return;
    try {
      const r = await api('/api/ld/finalize', { id: d.id });
      const nl = r.line || {};
      const m = nl.merged || {};
      S.detail = nl;
      S.preview = null;
      renderPanel(); renderMapInfo(); loadList();
      if (m.geom) {
        loadGeom(d.id);
        toast(`已合并：${m.geom.label} 走向`
          + (m.geom.km != null ? ' ' + fmtKm(m.geom.km) : '')
          + (m.geom.nPart ? ' · ' + m.geom.nPart + ' 段' : '')
          + ` + ${m.stops} 站（定位 ${m.coordN}）`);
      } else {
        toast(`已合并保存：${m.stops || 0} 站`);
      }
      if (nl.published) {
        setTimeout(() => toast('③ 已同时定为基本线路：数据校对 / 地图 / 选经由一起接管，'
          + '主站「车站/线路」也能查到（含停站与走向）', false, 6000), 1200);
      } else if (nl.publishError) {
        toast('已合并成确认线路，但定为基本线路失败：' + nl.publishError
          + '（改好后再点一次「重新合并保存」即可）', true, 9000);
      }
      if (m.warn && m.warn.length) toast(m.warn.join('；'), true);
    } catch (e) { toast(e.message, true); }
  };

  /* ---- 资料库中文名称 / 起讫站点 ---- */
  const rn = $('#ldRefName');
  if (rn) rn.onclick = () => applyRef('name');
  const rf = $('#ldRefFromTo');
  if (rf) rf.onclick = () => applyRef('fromto');


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

  /* 一键把「京沪线（OSM）」这种初稿名改成正名「京沪线」 */
  const ren = $('#ldRenameBase');
  if (ren) ren.onclick = async () => {
    const to = (d.name || '').replace(/[（(](?:底表|OSM|自建|china-emu|GeoJSON|备份|线路资料库)[^）)]*[）)]/g, '')
      .replace(/#\d+$/, '').trim();
    if (!to) return;
    try {
      const r = await api('/api/ld/save', { id: d.id, name: to });
      toast(`规范名已改成「${to}」`);
      S.detail = r.line;
      renderPanel(); renderMapInfo(); loadList();
    } catch (e) { toast(e.message, true); }
  };

  /* ④「定为基本线路」及其走向下拉已取消：③ 一步到位，走向只由 ① 决定。
     这里只保留「撤销基本线路」的出口，免得定稿后没法退回。 */
  const unpub = $('#ldUnpublish');
  if (unpub) unpub.onclick = async () => {
    const b = d.baseInfo || {};
    if (!confirm(`撤销基本线路「${b.name || d.name}」？\n\n` +
        `校对 / 地图 / 选经由退回原来的「默认版本 > 底表 > 站数最多」规则。`)) return;
    try {
      await api('/api/ld/unpublish', { name: b.name || d.name });
      S.detail = await api('/api/ld/line?id=' + d.id);   // 重取，baseInfo 变 null
      renderPanel(); loadList();
      toast('已撤销，退回原来的取数规则');
    } catch (e) { toast(e.message, true); }
  };

  $$('#ldPanel [data-del]').forEach(x => {
    x.onclick = async (ev) => {
      ev.stopPropagation();               // 别名整条可点（地图预览），删掉×要单独响应
      try {
        const r = await api('/api/ld/alias/del', { aliasId: Number(x.dataset.del) });
        S.detail = r.line; renderPanel();
      } catch (e) { toast(e.message, true); }
    };
  });
  /* ①② 的候选行：点一行 = 选中（单选）+ 地图上画它的走向 / 站序 */
  $$('#ldPanel .srcrow[data-src]').forEach(x => {
    x.onclick = (ev) => {
      if (ev.target.closest('[data-del]')) return;
      const kind = x.dataset.kind === 'stop' ? 'stop' : 'line';
      if (kind === 'line') {
        S.pick.line = x.dataset.src + '\u0001' + x.dataset.name;
      } else {
        S.pick.stop = { source: x.dataset.src, name: x.dataset.name };
      }
      $$(`#ldPanel .srcrow.${kind}`).forEach(y => y.classList.toggle('on', y === x));
      const same = S.preview && S.preview.src === x.dataset.src
        && S.preview.name === x.dataset.name;
      if (!same) previewSource(x.dataset.src, x.dataset.name, null, kind);
      const box = $('#ldMergeBox');
      if (box) box.innerHTML = mergeSummary(S.detail);
    };
  });
  /* ② 的「本线现在的站序」行 */
  $$('#ldPanel .srcrow[data-self]').forEach(x => {
    x.onclick = () => {
      S.pick.stop = { self: true };
      $$('#ldPanel .srcrow.stop').forEach(y => y.classList.toggle('on', y === x));
      restoreSelf();
      const box = $('#ldMergeBox');
      if (box) box.innerHTML = mergeSummary(S.detail);
    };
  });

  $('#ldCandReload').onclick = () => loadCandidates(d.id);

  /* 反向站序：整条倒过来，里程按「原末站里程 − 原里程」重算，新起点 0，缺的不插值 */
  const rev = $('#ldReverse');
  if (rev) rev.onclick = async () => {
    const a = d.stops || [];
    if (a.length < 2) { toast('至少要有 2 个站才能反向', true); return; }
    if (!confirm(`把「${d.name}」的 ${a.length} 站整条倒过来？\n\n` +
        '· 起点 ↔ 终点 对调，站序反向\n' +
        '· 里程 = 原末站里程 − 原里程，新起点 0；原本没里程的站仍留空\n' +
        '· 站点坐标、总里程、线路走向都不变')) return;
    try {
      const r = await api('/api/ld/stops/reverse', { id: d.id });
      S.detail = r.line;
      S.pick.stop = { self: true };
      renderPanel(); renderMapInfo();
      MAP.setData((r.line.stops || []), (S.geom && S.geom.geom && S.geom.geom.parts) || []);
      loadList();
      toast(`站序已反向：${(r.line.stops || []).length} 站，`
        + `${r.line.from_sta || '?'} → ${r.line.to_sta || '?'}，里程已重算`);
    } catch (e) { toast(e.message, true); }
  };

  // 站序
  const stops = () => (S.detail.stops || []).slice();
  const commit = async (arr) => {
    try {
      const r = await api('/api/ld/stops/save', { id: d.id, stops: arr });
      S.detail = r.line;
      /* 手工改过站序 → ② 默认确认「本线现在的站序」，别再自动选回来源行 */
      S.pick.stop = { self: true };
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
  /* 新增车站：不再用 prompt 一律追加到末尾，改成可以指定插入位置的弹窗。
     - 顶部「+ 站」→ 默认追加到末尾，但弹窗里能改选任意位置；
     - 每一站行的「+」→ 预选「就插在这一站后面」。 */
  const addStop = $('#ldAddStop');
  if (addStop) addStop.onclick = () => openInsertStop(null);
  $$('#ldPanel [data-ins]').forEach(b => {
    b.onclick = () => openInsertStop(Number(b.dataset.ins) + 1);
  });

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

/* -------------------------------------------- 新增车站（可指定插入位置） */
/* pos = 新站插入后的 0-based 起始下标：0 最前、i+1 插在第 i+1 站之后、n 末尾。
   null = 默认末尾；站点行的 + 直接传 i+1，预选「就插在这一站后面」。
   后端 /api/ld/stops/insert 按同一口径落库，前端不用拼整份站序传回去。 */
function openInsertStop(defaultPos) {
  const d = S.detail;
  if (!d || !d.id) return;
  const cur = d.stops || [], n = cur.length;
  const raw = defaultPos == null ? n : Number(defaultPos);
  const pos = Math.max(0, Math.min(isNaN(raw) ? n : raw, n));
  const opts = [`<option value="0"${pos === 0 ? ' selected' : ''}>最前面（作为第 1 站）</option>`]
    .concat(cur.map((s, i) => `<option value="${i + 1}"${pos === i + 1 ? ' selected' : ''}>`
      + `第 ${i + 1} 站「${esc(s.name)}」之后${i === n - 1 ? '（末尾）' : ''}</option>`))
    .join('');
  const w = openModal('新增车站 · 指定插入位置', `
    <div class="frow"><label>站名</label>
      <textarea id="ldInsName" rows="3" placeholder="一次可填多个：换行 / 逗号 / 顿号分隔，按填写顺序插入"></textarea></div>
    <div class="frow"><label>插入位置</label><select id="ldInsPos">${opts}</select></div>
    <div class="hint">当前 ${n} 站。新站<b>里程留空、不自动插值</b>，坐标按站名查档案补；
      原有各站的里程 / 坐标 / 相对顺序都不变。插完 ② 会自动选中「现在的站序」，
      再点一次「用它并确认」即可。</div>`, async (wrap, msg) => {
    const names = $('#ldInsName', wrap).value.trim()
      .split(/[,，;；、\t\r\n]+/).map(s => s.trim()).filter(Boolean);
    if (!names.length) throw new Error('先填要插入的站名');
    const p = Number($('#ldInsPos', wrap).value);
    const r = await api('/api/ld/stops/insert', { id: d.id, names, pos: p });
    wrap.remove();
    S.detail = r.line;
    S.pick.stop = { self: true };     // 手工改过站序，② 别自动选回来源行
    renderPanel(); renderMapInfo();
    MAP.setData((r.line.stops || []), (S.geom && S.geom.geom && S.geom.geom.parts) || []);
    loadList();
    toast(`已在第 ${p + 1} 位插入 ${names.length} 站：${names.join('、')}`);
  }, '插入');
  setTimeout(() => { const el = $('#ldInsName', w); if (el) el.focus(); }, 0);
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
      <select id="apSrc"><option value="">全部</option>${Object.entries(SRC).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
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
        <span class="s" style="background:${SRCBG(x.source)}">${esc(SRC[x.source] || x.source)}</span>
        <span>${esc(x.name)}</span>
        <span class="m">${x.n_stop || 0} 站${x.has_geom ? ' · 有走向' : ''}</span></div>`).join('')
      || '<div class="hint" style="padding:10px">没找到</div>';
    $$('.pick', box).forEach(p => {
      p.onclick = () => {
        $$('.pick', box).forEach(o => o.classList.remove('on'));
        p.classList.add('on');
        /* 点选即在地图上预览这条来源，好判断是不是同一条线 */
        previewSource(p.dataset.src, p.dataset.name);
      };
    });
  };
  $('#apQ', w).oninput = debounce(doSearch, 250);
  $('#apSrc', w).onchange = doSearch;
  doSearch();
}

/* --------------------------------- 收一条：① 走向来源 / ② 站点+定位来源，都是单选 */

async function openLineSrcPick(mode = 'line') {
  const d = S.detail;
  if (!d) return;
  const forStop = mode === 'stop';
  let pick = null;                    // {source, name}
  const w = openModal(forStop ? '收一条站点来源' : '收一条线路来源', `
    <div class="frow"><label>来源</label>
      <select id="lsSrc"><option value="">全部</option>${Object.entries(SRC).map(([k, v]) =>
        `<option value="${k}">${v}</option>`).join('')}</select>
    </div>
    <div class="frow"><label>找线名</label><input id="lsQ" placeholder="在各来源里搜，如 京沪"></div>
    <div class="picklist" id="lsList"><div class="hint" style="padding:10px">输入关键字搜来源线名</div></div>
    <div class="hint">${forStop
      ? '收的是<b>站点和定位</b>：选一家带站序的来源（<b>至少 2 站</b>）。点一行会在地图上画出它的站序，好判断是不是同一条线。'
      : '收的是<b>走向</b>：选一条<b>有走向</b>的来源（rail-globe / OSM 走向 / cnrail 走向）。<b>哪怕 0 站也能用</b>，只要行尾标了「有走向」；点一行会在地图上画出它的走向。'}
      收下后进入「已收未确认」，在主面板里选中它再确认。</div>`,
    async (wrap) => {
      if (!pick) throw new Error('先选一条');
      const r = await api('/api/ld/alias/add', {
        id: d.id, source: pick.source, name: pick.name,
      });
      wrap.remove();
      S.detail = r.line;
      const key = pick.source + '\u0001' + pick.name;
      const al = (r.line.aliases || []).find(a => a.source + '\u0001' + a.name === key);
      if (forStop) {
        if (al && (al.nStop || 0) >= 2) {
          S.pick.stop = { source: pick.source, name: pick.name };
          toast(`已收下「${pick.name}」：${al.nStop} 站 · 定位 ${al.coordN || 0}，点「用它并确认」`);
        } else {
          toast(`「${pick.name}」没有站序，当不了站点来源`, true);
        }
      } else {
        S.pick.line = key;
        toast(`已收下「${pick.name}」，点「确认线路来源」生效`);
      }
      renderPanel(); loadList();
    }, forStop ? '收下并选中' : '收下');

  const box = $('#lsList', w);
  const doSearch = async () => {
    const q = $('#lsQ', w).value.trim();
    const src = $('#lsSrc', w).value;
    const dd = await api('/api/ld/source/search?limit=80' + (q ? '&q=' + encodeURIComponent(q) : ''));
    let items = dd.items || [];
    if (src) items = items.filter(x => x.source === src);
    if (forStop) items = items.filter(x => (x.n_stop || 0) >= 2);
    box.innerHTML = items.map(x => `<div class="pick" data-src="${x.source}"
        data-name="${esc(x.name)}" data-n="${x.n_stop || 0}">
        <span class="s" style="background:${SRCBG(x.source)}">${esc(SRC[x.source] || x.source)}</span>
        <span>${esc(x.name)}</span>
        <span class="m">${x.n_stop || 0} 站${!forStop && x.has_geom ? ' · 有走向' : ''}${x.totalKm ? ' · ' + fmtKm(x.totalKm) : ''}</span></div>`).join('')
      || '<div class="hint" style="padding:10px">没找到'
        + (forStop ? '（要至少 2 站的来源；只有走向、没有站序的当不了站点来源）' : '') + '</div>';
    $$('.pick', box).forEach(p => {
      p.onclick = () => {
        $$('.pick', box).forEach(o => o.classList.remove('on'));
        p.classList.add('on');
        pick = { source: p.dataset.src, name: p.dataset.name };
        /* 点选即在地图上预览这条来源，好判断是不是同一条线 */
        previewSource(p.dataset.src, p.dataset.name, null, forStop ? 'stop' : 'line');
      };
    });
  };
  $('#lsQ', w).oninput = debounce(doSearch, 250);
  $('#lsSrc', w).onchange = doSearch;
  doSearch();
}

/* ------------------------------------------------- 导入 GeoJSON（站序 + 走向） */

function readFileText(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.onerror = () => rej(new Error('读文件失败'));
    fr.readAsText(file, 'utf-8');
  });
}

/** lid 有值 = 导进这条线；没有 = 每份文件新建一条线。
 *  only: 'stops' 只导站点（②）、'geom' 只导走向（①）、'' 两边都导（顶栏建新线）。 */
function pickGeojson(lid, only) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.json,.geojson,application/geo+json,application/json';
  inp.multiple = !lid;
  inp.onchange = async () => {
    const files = Array.from(inp.files || []);
    if (files.length) await importGeojsonFiles(files, lid || 0, only || '');
  };
  inp.click();
}

async function importGeojsonFiles(files, lid, only) {
  let firstId = 0, okN = 0;
  const errs = [];
  for (const f of files) {
    try {
      const text = await readFileText(f);
      const body = { data: text, fileName: f.name };
      if (lid) body.id = lid;
      if (only) body.only = only;
      const r = await api('/api/ld/import-geojson', body);
      okN++;
      if (!firstId) firstId = (r.line && r.line.id) || 0;
      if (r.dropped) errs.push(`${f.name}：${r.dropped} 个车站没落到走向上`);
    } catch (e) {
      errs.push(`${f.name}：${e.message}`);
    }
  }
  await loadList(); loadStats();
  if (firstId) await selectLine(lid || firstId);
  if (okN) {
    if (only === 'stops') {
      toast(`已导入 ${okN} 个文件的站点与定位（线路走向未改动）；记得到 ② 点「用它并确认」`);
    } else if (only === 'geom') {
      toast(`已导入 ${okN} 个文件的线路走向（车站未改动）；记得到 ① 点「确认线路来源」`);
    } else {
      toast(`导入 ${okN} 条线，站序和走向都按文件来；记得到「站点来源」点确认`);
    }
  } else {
    toast('导入失败：' + errs.join('；'), true);
  }
  if (errs.length) console.warn('GeoJSON 导入：', errs);
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
  $('#btnHelp').onclick = openHelp;

  $('#btnMerge').onclick = () => {
    S.merge = !S.merge;
    S.picks.clear();
    $('#btnMerge').classList.toggle('on', S.merge);
    renderList(); renderMergeBar();
  };

  $('#btnGeo').onclick = () => pickGeojson(0);

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
