/* railcn 前端：车次查询 + 地图行程 + 位置推算 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const pad = n => String(n).padStart(2, '0');
const fmtClock = ts => {
  const d = new Date(ts);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const fmtDay = ts => {
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()}`;
};
const WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const fmtMD = ts => {
  const d = new Date(ts);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const fmtWD = ts => WD[new Date(ts).getDay()];
const fmtStamp = ts => `${fmtMD(ts)} ${fmtClock(ts)}`;
/* 相对始发日的自然日序号：0 当日 / 1 次日 / 2 第三日… */
const dayName = n => (n <= 0 ? '当日' : n === 1 ? '次日' : n === 2 ? '第三日'
  : n === 3 ? '第四日' : n === 4 ? '第五日' : `第${n + 1}日`);
const dayDiff = (isoA, isoB) => {
  const a = new Date(isoA), b = new Date(isoB);
  return Math.round(
    (new Date(b.getFullYear(), b.getMonth(), b.getDate()) -
     new Date(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
};
const startOfNextDay = ts => {
  const d = new Date(ts);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime();
};
const daySegments = (a, b) => {
  const segs = [];
  let cur = a, guard = 0;
  while (cur < b && guard++ < 40) {
    const nxt = Math.min(b, startOfNextDay(cur + 1000));
    segs.push({ t: cur, ms: Math.max(1, nxt - cur) });
    cur = nxt;
  }
  return segs;
};
const fmtDur = min => {
  if (min == null || isNaN(min)) return '—';
  const m = Math.max(0, Math.round(min));
  return m >= 60 ? `${Math.floor(m / 60)}h${pad(m % 60)}m` : `${m}m`;
};
const todayStr = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

async function api(path, params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  });
  const base = path.startsWith('/api') ? path : `/api${path}`;
  const res = await fetch(`${base}?${qs.toString()}`);
  const data = await res.json().catch(() => ({ error: '响应解析失败' }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ============================================================
 * 坐标：WGS-84 -> GCJ-02（腾讯地图/高德 使用 GCJ-02）
 * ============================================================ */
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

/* ============================================================
 * 状态
 * ============================================================ */
const S = {
  date: todayStr(),
  route: null,
  simT: null,
  playing: false,
  speed: 60,
  followNow: true,
  lastTick: 0,
  nowStopIdx: -1,
};

const mapKey = () => localStorage.getItem('railcn.mapkey') || '';

/* ============================================================
 * 地图后端：TMap（腾讯 GL JS）
 * ============================================================ */
const TMapView = {
  name: 'tmap',
  ok: false,
  map: null,
  layers: {},
  route: null,
  pts: [],

  async init() {
    const key = mapKey();
    const sdk = key
      ? `https://map.qq.com/api/gljs?v=1.exp&key=${encodeURIComponent(key)}`
      : 'https://map.qq.com/api/gljs?v=1.exp';
    if (!key) {
      const host = 'http://127.0.0.1:__WB_HTTP_PORT__/_TMapService/_wbt/__WB_TMAP_SECRET__';
      if (host.includes('__WB')) {
        const err = new Error('no-key');
        err.code = 'NO_KEY';
        throw err;
      }
      window._TMapSecurityConfig = { serviceHost: host };
    }
    await new Promise((res, rej) => {
      const el = document.createElement('script');
      el.src = sdk;
      el.onload = res;
      el.onerror = () => rej(new Error('地图 SDK 加载失败（检查网络或 Key）'));
      document.head.appendChild(el);
    });
    if (typeof TMap === 'undefined') throw new Error('地图 SDK 未就绪');

    this.map = new TMap.Map('map', {
      center: new TMap.LatLng(34.5, 108.9),
      zoom: 5,
      viewMode: '2D',
      showControl: false,
      pitch: 0,
      rotation: 0,
    });
    this.ok = true;
  },

  g(lat, lon) {
    const [lng2, lat2] = GCJ(lon, lat);
    return new TMap.LatLng(lat2, lng2);
  },

  setRoute(route) {
    this.route = route;
    this.clear();
    const withCoord = route.stops.filter(s => s.lat != null);

    if (!withCoord.length) return;

    // 线：外套 + 主线
    const linePts = (route.path && route.path.length > 1) ? route.path : withCoord;
    const path = linePts.map(s => this.g(s.lat, s.lon));
    this.layers.line = new TMap.MultiPolyline({
      map: this.map,
      styles: {
        casing: new TMap.PolylineStyle({
          color: '#ffffff', width: 9, lineCap: 'round', borderWidth: 0, opacity: .95,
        }),
        main: new TMap.PolylineStyle({
          color: '#1f6feb', width: 5, lineCap: 'round', borderWidth: 0,
        }),
      },
      geometries: [
        { id: 'casing', styleId: 'casing', paths: path },
        { id: 'main', styleId: 'main', paths: path },
      ],
      zIndex: 5,
    });

    // 站点点
    const dotSvg = encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">
        <circle cx="11" cy="11" r="6.5" fill="#ffffff" stroke="#1f6feb" stroke-width="2.5"/></svg>`
    );
    this.layers.stops = new TMap.MultiMarker({
      map: this.map,
      styles: {
        s: new TMap.MarkerStyle({
          width: 11, height: 11, anchor: { x: 5.5, y: 5.5 },
          src: `data:image/svg+xml;charset=utf-8,${dotSvg}`,
        }),
      },
      geometries: withCoord.map(s => ({
        id: String(s.i), styleId: 's', position: this.g(s.lat, s.lon),
      })),
      zIndex: 6,
    });

    // 站名（点太多时只显示主要站）
    const labelStep = withCoord.length > 26 ? Math.ceil(withCoord.length / 22) : 1;
    const labeled = withCoord.filter((s, idx) =>
      idx === 0 || idx === withCoord.length - 1 || idx % labelStep === 0);
    this.layers.labels = new TMap.MultiLabel({
      map: this.map,
      styles: {
        l: new TMap.LabelStyle({
          color: '#26324a', size: 11, offset: { x: 0, y: -13 },
          alignment: 'center', verticalAlignment: 'bottom',
          backgroundColor: 'rgba(255,255,255,.82)', backgroundPadding: 2,
          borderColor: 'rgba(255,255,255,0)', borderRadius: 3,
        }),
      },
      geometries: labeled.map(s => ({
        id: 'L' + s.i, styleId: 'l', position: this.g(s.lat, s.lon), content: s.name,
      })),
      zIndex: 7,
    });

    // 列车
    const trainSvg = encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="46" height="46">
        <circle cx="23" cy="23" r="20" fill="rgba(232,89,12,.16)"/>
        <circle cx="23" cy="23" r="13" fill="#e8590c" stroke="#fff" stroke-width="3"/>
        <text x="23" y="28" font-size="13" text-anchor="middle" fill="#fff">🚄</text></svg>`
    );
    this.layers.train = new TMap.MultiMarker({
      map: this.map,
      styles: {
        t: new TMap.MarkerStyle({
          width: 46, height: 46, anchor: { x: 23, y: 23 },
          src: `data:image/svg+xml;charset=utf-8,${trainSvg}`,
        }),
      },
      geometries: [],
      zIndex: 9,
    });

    this.stops = withCoord;
    this.layer = this.layers;
    // 注意：必须给一个有效初始位置。传 null 会在构造时立刻挂到地图，
    // SDK 内部读 position.x 直接抛 "Cannot read properties of null"。
    this.info = new TMap.InfoWindow({
      map: this.map, position: this.g(withCoord[0].lat, withCoord[0].lon),
      offset: { x: 0, y: -14 }, enableCustom: false,
    });
    this.info.close();

    this.layers.stops.on('click', evt => {
      const idx = Number(evt.geometry.id);
      const st = route.stops[idx - 1];
      if (!st) return;
      this.info.setPosition(this.g(st.lat, st.lon));
      this.info.setContent(this.stopHtml(st, route));
      this.info.open();
      highlightStop(idx - 1);
    });

    this.fit();
  },

  stopHtml(st, route) {
    const arr = st.arr ? `${st.arr}${st.day > 0 ? `（${dayName(st.day)}）` : ''}` : '——';
    const dep = st.dep ? `${st.dep}${st.depDay > st.day ? `（${dayName(st.depDay)}）` : ''}` : '——';
    return `<div class="iw"><b>${st.name}</b>
      <div class="g">第 ${st.i} 站 · 里程 ${Math.round(st.km)} km</div>
      <div>到 ${arr} · 开 ${dep}${st.stayMin ? ` · 停 ${st.stayMin}分` : ''}</div>
      <div class="g">${route.code} ${route.from} → ${route.to}</div></div>`;
  },

  setTrain(pt) {
    if (!this.ok || !this.layers.train) return;
    this.layers.train.setGeometries([{ id: 'train', styleId: 't', position: this.g(pt.lat, pt.lon) }]);
  },

  focusStop(idx, zoom = 11) {
    const st = this.route?.stops[idx];
    if (!st || st.lat == null) return;
    this.map.easeTo({ center: this.g(st.lat, st.lon), zoom });
  },

  fit() {
    const pts = this.route?.stops.filter(s => s.lat != null);
    if (!pts || !pts.length) return;
    let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    pts.forEach(s => {
      minLat = Math.min(minLat, s.lat); maxLat = Math.max(maxLat, s.lat);
      minLon = Math.min(minLon, s.lon); maxLon = Math.max(maxLon, s.lon);
    });
    if (maxLat - minLat < 0.05 && maxLon - minLon < 0.05) {
      this.map.easeTo({ center: this.g(pts[0].lat, pts[0].lon), zoom: 12 });
      return;
    }
    const b = new TMap.LatLngBounds(this.g(minLat, minLon), this.g(maxLat, maxLon));
    this.map.fitBounds(b, { padding: { top: 90, bottom: 110, left: 90, right: 90 } });
  },

  clear() {
    Object.values(this.layers).forEach(l => { try { l.setMap && l.setMap(null); } catch (e) { } });
    this.layers = {};
  },

  resize() { this.map && this.map.resize && this.map.resize(); },
};

/* ============================================================
 * 地图后端：SVG 示意（无底图，无 Key 时使用）
 * ============================================================ */
const SvgView = {
  name: 'svg',
  ok: true,
  svg: null,
  view: null,
  W: 0, H: 0,
  route: null,
  drag: null,

  init() {
    this.svg = $('#fallback');
    if (this.gRoot && this.svg.contains(this.gRoot)) {   // 幂等：降级时可能再次调用
      this.svg.classList.remove('hidden');
      return;
    }
    this.svg.classList.remove('hidden');
    this.W = this.svg.clientWidth || 900;
    this.H = this.svg.clientHeight || 600;
    this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
    this.gRoot = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    this.svg.appendChild(this.gRoot);
    this.bindPanZoom();
    this.ok = true;
  },

  mk(tag, attrs = {}, parent) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    (parent || this.gRoot).appendChild(el);
    return el;
  },

  proj(lat, lon) {
    const x = (lon + 180) / 360;
    const s = Math.sin(lat * Math.PI / 180);
    const y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
    return [x, y];
  },

  setRoute(route) {
    this.route = route;
    this.W = this.svg.clientWidth || 900;
    this.H = this.svg.clientHeight || 600;
    this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
    this.gRoot.innerHTML = '';
    const pts = route.stops.filter(s => s.lat != null);
    if (!pts.length) return;

    const xy = pts.map(s => ({ s, p: this.proj(s.lat, s.lon) }));
    let minX = 1, maxX = 0, minY = 1, maxY = 0;
    xy.forEach(o => {
      minX = Math.min(minX, o.p[0]); maxX = Math.max(maxX, o.p[0]);
      minY = Math.min(minY, o.p[1]); maxY = Math.max(maxY, o.p[1]);
    });
    const padX = 0.12 * (maxX - minX || 0.02), padY = 0.12 * (maxY - minY || 0.02);
    this.view = {
      x0: minX - padX, x1: maxX + padX,
      y0: minY - padY, y1: maxY + padY,
      zoom: 1, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2,
    };
    this.render();
  },

  scale() {
    const v = this.view;
    const w = (v.x1 - v.x0), h = (v.y1 - v.y0);
    const base = Math.min(this.W / w, this.H / h);
    return base * v.zoom;
  },

  toPx(p) {
    const v = this.view, k = this.scale();
    return [this.W / 2 + (p[0] - v.cx) * k, this.H / 2 + (p[1] - v.cy) * k];
  },

  render() {
    const route = this.route;
    if (!route) return;
    this.gRoot.innerHTML = '';
    const v = this.view;
    const pts = route.stops.filter(s => s.lat != null);

    // 网格底纹
    const grid = this.mk('g', { opacity: .5 });
    for (let i = 0; i <= 6; i++) {
      this.mk('line', {
        x1: 0, y1: (this.H / 6) * i, x2: this.W, y2: (this.H / 6) * i,
        stroke: '#dbe4ef', 'stroke-width': 1,
      }, grid);
      this.mk('line', {
        y1: 0, x1: (this.W / 8) * i, y2: this.H, x2: (this.W / 8) * i,
        stroke: '#dbe4ef', 'stroke-width': 1,
      }, grid);
    }

    const px = pts.map(s => this.toPx(this.proj(s.lat, s.lon)));
    const lineArr = (route.path && route.path.length > 1) ? route.path : pts;
    const lpx = lineArr.map(s => this.toPx(this.proj(s.lat, s.lon)));
    // 线路：外套 + 主线
    const d = lpx.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    this.mk('path', { d, fill: 'none', stroke: '#ffffff', 'stroke-width': 9, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
    this.mk('path', { d, fill: 'none', stroke: '#1f6feb', 'stroke-width': 4.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });

    // 站点
    const step = pts.length > 30 ? Math.ceil(pts.length / 24) : 1;
    this.stopNodes = {};
    pts.forEach((s, i) => {
      const p = px[i];
      const isEnd = i === 0 || i === pts.length - 1;
      const c = this.mk('circle', {
        cx: p[0], cy: p[1], r: isEnd ? 5 : 3.6,
        fill: isEnd ? '#1f6feb' : '#fff', stroke: isEnd ? '#fff' : '#1f6feb', 'stroke-width': 2,
        style: 'cursor:pointer',
      });
      c.addEventListener('click', () => {
        this.infoBox(s);
        highlightStop(s.i - 1);
      });
      this.stopNodes[s.i] = c;

      if (isEnd || i % step === 0) {
        this.mk('text', {
          x: p[0] + 8, y: p[1] - 7, 'font-size': 11.5, fill: '#26324a',
          stroke: '#fff', 'stroke-width': 3, 'paint-order': 'stroke', 'pointer-events': 'none',
        }).textContent = s.name;
      }
    });

    // 列车标记
    this.trainNode = this.mk('g', {});
    const halo = this.mk('circle', { r: 14, fill: 'rgba(232,89,12,.20)', class: 'halo' }, this.trainNode);
    this.mk('circle', { r: 7.5, fill: '#e8590c', stroke: '#fff', 'stroke-width': 2.5 }, this.trainNode);
    this.trainNode.setAttribute('opacity', 0);

    this.infoNode = this.mk('g', { opacity: 0 });
    this.infoRect = this.mk('rect', { rx: 8, fill: '#fff', stroke: '#e2e8f0', 'stroke-width': 1 }, this.infoNode);
    this.infoTxt = this.mk('text', { 'font-size': 11.5, fill: '#26324a' }, this.infoNode);
  },

  infoBox(st) {
    const arr = st.arr ? `${st.arr}${st.day > 0 ? `(${dayName(st.day)})` : ''}` : '——';
    const dep = st.dep ? `${st.dep}${st.depDay > st.day ? `(${dayName(st.depDay)})` : ''}` : '——';
    const lines = [`${st.name}`, `第 ${st.i} 站 · ${Math.round(st.km)} km`, `到 ${arr} / 开 ${dep}`];
    const p = this.toPx(this.proj(st.lat, st.lon));
    this.infoTxt.innerHTML = '';
    lines.forEach((t, i) => {
      const ts = this.mk('tspan', { x: 10, dy: i ? 14 : 0 }, this.infoTxt);
      ts.textContent = t;
    });
    this.infoTxt.setAttribute('transform', `translate(${p[0] + 12},${p[1] - 46})`);
    const w = 120, h = 50;
    this.infoRect.setAttribute('x', p[0] + 4);
    this.infoRect.setAttribute('y', p[1] - 62);
    this.infoRect.setAttribute('width', w);
    this.infoRect.setAttribute('height', h);
    this.infoNode.setAttribute('opacity', 1);
  },

  setTrain(pt) {
    if (!this.trainNode || !this.view) return;
    const p = this.toPx(this.proj(pt.lat, pt.lon));
    this.trainNode.setAttribute('transform', `translate(${p[0]},${p[1]})`);
    this.trainNode.setAttribute('opacity', 1);
  },

  focusStop(idx, zoom = 1.8) {
    const st = this.route?.stops[idx];
    if (!st || st.lat == null || !this.view) return;
    const p = this.proj(st.lat, st.lon);
    this.view.cx = p[0]; this.view.cy = p[1];
    this.view.zoom = Math.min(12, zoom);
    this.render();
    this.lastPt && this.setTrain(this.lastPt);
  },

  fit() {
    if (!this.route) return;
    this.setRoute(this.route);
  },

  clear() {
    this.gRoot && (this.gRoot.innerHTML = '');
    this.svg && this.svg.classList.add('hidden');
  },

  resize() {
    if (!this.svg || this.svg.classList.contains('hidden')) return;
    const oldW = this.W;
    this.W = this.svg.clientWidth || this.W;
    this.H = this.svg.clientHeight || this.H;
    this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
    if (this.route && Math.abs(oldW - this.W) > 20) this.render();
  },

  bindPanZoom() {
    const svg = this.svg;
    svg.addEventListener('wheel', e => {
      if (!this.view) return;
      e.preventDefault();
      const k = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      this.view.zoom = clamp(this.view.zoom * k, 0.6, 16);
      this.render();
      this.lastPt && this.setTrain(this.lastPt);
    }, { passive: false });

    svg.addEventListener('pointerdown', e => {
      this.drag = { x: e.clientX, y: e.clientY, cx: this.view.cx, cy: this.view.cy };
      svg.setPointerCapture(e.pointerId);
    });
    svg.addEventListener('pointermove', e => {
      if (!this.drag || !this.view) return;
      const k = this.scale();
      this.view.cx = this.drag.cx - (e.clientX - this.drag.x) / k;
      this.view.cy = this.drag.cy - (e.clientY - this.drag.y) / k;
      this.render();
      this.lastPt && this.setTrain(this.lastPt);
    });
    svg.addEventListener('pointerup', () => { this.drag = null; });
  },
};

/* 统一入口 */
const MapView = {
  impl: null,
  async init() {
    try {
      await TMapView.init();
      this.impl = TMapView;
      $('#fallback').classList.add('hidden');
      $('#map').style.visibility = 'visible';
    } catch (e) {
      this.impl = SvgView;
      $('#map').style.visibility = 'hidden';
      try {
        SvgView.init();
      } catch (e2) {
        console.error('SVG 视图初始化失败', e2);
      }
      const hint = $('#mapHint');
      hint.classList.remove('hidden');
      hint.innerHTML = e.code === 'NO_KEY'
        ? `当前为<b>无底图示意模式</b>（未配置腾讯地图 Key）。<br>点击右上角「地图」填入 Key 后即可显示真实地图底图。`
        : `地图 SDK 加载失败：${e.message}<br>已切换为无底图示意模式。`;
    }
    window.addEventListener('resize', () => this.impl && this.impl.resize && this.impl.resize());
  },
  /* 地图后端出问题时降级为示意模式，不把整个详情页拖垮 */
  degrade(e) {
    if (this.impl === SvgView) return false;
    console.error('[railcn] 地图渲染失败，已降级为示意模式：', e);
    try { this.impl.clear && this.impl.clear(); } catch (_) { /* 忽略 */ }
    this.impl = SvgView;
    $('#map').style.visibility = 'hidden';
    try { SvgView.init(); } catch (_) { /* 忽略 */ }
    const hint = $('#mapHint');
    hint.classList.remove('hidden');
    hint.innerHTML = `地图渲染异常（${e.message}），已切换为<b>无底图示意模式</b>。`;
    return true;
  },
  call(name, arg) {
    if (!this.impl) return;
    try {
      this.impl[name](arg);
    } catch (e) {
      if (this.degrade(e) && this.impl) {
        try { this.impl[name](arg); } catch (_) { /* 忽略 */ }
      }
    }
  },
  setRoute(r) { this.call('setRoute', r); },
  setTrain(p) { this.call('setTrain', p); },
  focusStop(i) { this.call('focusStop', i); },
  fit() { this.call('fit'); },
};

/* ============================================================
 * 位置推算
 * ============================================================ */
function coordOf(st) {
  return st && st.lat != null ? { lat: st.lat, lon: st.lon } : null;
}

/* 沿展开后的折线按里程取点 */
function pointAtKm(path, km) {
  if (!path || path.length < 2) return null;
  if (km <= path[0].km) return { lat: path[0].lat, lon: path[0].lon };
  const last = path[path.length - 1];
  if (km >= last.km) return { lat: last.lat, lon: last.lon };
  let lo = 0, hi = path.length - 1;
  while (hi - lo > 1) {                      // 二分找所在小段
    const mid = (lo + hi) >> 1;
    if (path[mid].km <= km) lo = mid; else hi = mid;
  }
  const a = path[lo], b = path[hi];
  const span = b.km - a.km;
  const f = span > 0 ? (km - a.km) / span : 0;
  return { lat: a.lat + (b.lat - a.lat) * f, lon: a.lon + (b.lon - a.lon) * f };
}

function posAt(route, t) {
  const stops = route.stops;
  const n = stops.length;
  const T = s => Date.parse(s.iso);
  const D = s => Date.parse(s.depIso || s.iso);
  const first = stops[0];
  if (t < D(first)) {
    return { phase: 'before', point: coordOf(first), stop: first, idx: 0, progress: 0, km: 0 };
  }
  for (let i = 0; i < n; i++) {
    const s = stops[i];
    const arr = T(s), dep = D(s);
    if (i === 0 && t <= dep) {
      return { phase: 'before', point: coordOf(s), stop: s, idx: 0, progress: 0, km: 0 };
    }
    if (t < arr) {
      const p = stops[i - 1];
      if (!p) break;
      const span = arr - D(p);
      const f = span > 0 ? clamp((t - D(p)) / span) : 0;
      const pa = coordOf(p), pb = coordOf(s);
      const km = (p.km || 0) + ((s.km || 0) - (p.km || 0)) * f;
      const point = (route.path && route.path.length > 1 && pointAtKm(route.path, km))
        || (pa && pb
          ? { lat: pa.lat + (pb.lat - pa.lat) * f, lon: pa.lon + (pb.lon - pa.lon) * f }
          : (pb || pa));
      return { phase: 'run', from: p, to: s, frac: f, point, idx: i, km, spanMin: span / 60000 };
    }
    if (t <= dep && !(i === n - 1 && t >= arr)) {
      return { phase: 'stop', stop: s, point: coordOf(s), idx: i, km: s.km || 0 };
    }
  }
  const last = stops[n - 1];
  return { phase: 'arrived', point: coordOf(last), stop: last, idx: n - 1, km: last.km || 0, progress: 1 };
}

/* ============================================================
 * 渲染：车次详情
 * ============================================================ */
function kmBadge(route) {
  const m = route.kmMode || (route.kmReal ? 'real' : 'estimate');
  const tip = `按线路里程表匹配 ${route.kmMatched} 段；站点直线累计 ${route.straightKm} km；弯曲系数 ×${route.kmFactor}`;
  if (m === 'real') return `<i class="kmreal" title="${tip}">实测里程</i>`;
  if (m === 'hybrid') return `<i class="kmhyb" title="${tip}">部分实测</i>`;
  return `<i class="kmest" title="${tip}">里程估算</i>`;
}

function renderDetail(route) {
  const el = $('#detail');
  const type = (route.code.match(/^[A-Z]/) ? route.code[0] : 'N');
  const stops = route.stops;
  const startT = Date.parse(route.startIso), endT = Date.parse(route.endIso);
  const firstDep = stops[0].dep || fmtClock(startT);
  const lastArr = stops[stops.length - 1].arr || fmtClock(endT);
  const span = route.daySpan || (dayDiff(route.startIso, route.endIso) + 1);
  const over = span > 1;

  const head = `
    <div class="dhead">
      <div class="l1">
        <span class="code">${route.code}</span>
        <span class="tag type">${route.trainClass || type + ' 次'}</span>
        <span class="tag">${route.date}</span>
        ${over ? `<span class="tag day">跨 ${span} 日</span>` : ''}
        ${route.source === 'cache' ? '<span class="tag">本地缓存</span>' : ''}
      </div>
      <div class="l2">${route.from} → ${route.to}</div>
      <div class="journey">
        <div class="jcell"><span class="k">始发${over ? ' · 第 1 日' : ''}</span>
          <b>${fmtMD(startT)} ${fmtWD(startT)} ${firstDep}</b></div>
        <span class="jarrow">→</span>
        <div class="jcell"><span class="k">终到${over ? ` · 第 ${span} 日（${dayName(span - 1)}）` : ''}</span>
          <b>${fmtMD(endT)} ${fmtWD(endT)} ${lastArr}</b></div>
      </div>
      <div class="l3">
        <span>历时 ${fmtDur(route.durationMin)}</span>
        <span>${stops.length} 站 · ${route.totalKm} km${kmBadge(route)}</span>
        ${over ? `<span>${route.startDate} → ${route.endDate}，跨 ${span} 个自然日</span>` : ''}
      </div>
      ${(route.viaLines && route.viaLines.length)
        ? `<div class="l3 chips">经由 ${route.viaLines.map(l => `<button class="chip" data-line="${l}">${l}</button>`).join('')}</div>` : ''}
      ${route.missingCoords && route.missingCoords.length
      ? `<div class="l3" style="color:#b45309">⚠ ${route.missingCoords.length} 站缺坐标：${route.missingCoords.slice(0, 6).join('、')}${route.missingCoords.length > 6 ? '…' : ''}</div>` : ''}
    </div>`;

  let lastDay = 0;
  const rows = stops.map((s, i) => {
    const cls = ['stop'];
    if (i === 0) cls.push('start');
    if (i === stops.length - 1) cls.push('end');
    const stay = s.stayMin ? `停 ${s.stayMin} 分`
      : (i === 0 ? '始发' : (i === stops.length - 1 ? '终到' : ''));
    const day = s.day != null ? s.day : dayDiff(route.startIso, s.iso);
    let sep = '';
    if (i > 0 && day !== lastDay) {
      const dt = Date.parse(s.iso);
      sep = `<li class="daysep">第 ${day + 1} 日 · ${fmtMD(dt)} ${fmtWD(dt)}
        <em>跨零点（${dayName(day)}）</em></li>`;
    }
    lastDay = day;

    const arrBadge = (s.arr && day > 0) ? `<em class="dnum">+${day}</em>` : '';
    const depDay = s.depDay != null ? s.depDay : day;
    const depBadge = (s.dep && depDay > day) ? `<em class="dnum dep">+${depDay}</em>` : '';

    const arrLine = i === 0 ? '' :
      `<div class="arr"><i class="lbl">到</i>${s.arr || '——'}${arrBadge}</div>`;
    const depLine = i === stops.length - 1 ? '' :
      `<div class="dep"><i class="lbl">开</i>${s.dep || '——'}${depBadge}</div>`;

    return `${sep}<li class="${cls.join(' ')}" data-i="${i}">
      <div class="t">${arrLine}${depLine}</div>
      <div class="rail"><span class="dot"></span></div>
      <div class="name"><span class="n">${s.name}</span><span class="s">${stay}</span></div>
      <div class="km">${s.km ? Math.round(s.km) + ' km' : ''}</div>
    </li>`;
  }).join('');

  el.innerHTML = head + `<ul class="stops">${rows}</ul>`;
  el.classList.add('show');
  $('#trainList').classList.add('hidden');
  $$('#detail .chip[data-line]').forEach(b =>
    b.addEventListener('click', () => { switchTab('net'); showLine(b.dataset.line); }));

  $$('#detail .stop').forEach(li => {
    li.addEventListener('click', () => {
      const i = Number(li.dataset.i);
      MapView.focusStop(i);
      highlightStop(i);
    });
  });
}

function highlightStop(i) {
  $$('#detail .stop').forEach(el => el.classList.toggle('isnow', Number(el.dataset.i) === i));
  const el = $(`#detail .stop[data-i="${i}"]`);
  if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* ============================================================
 * 渲染：HUD / 时间轴
 * ============================================================ */
function renderHud(pos) {
  const route = S.route;
  if (!route) return;
  const st = $('#hudState');
  const km = pos.km || 0;
  const total = route.totalKm || 1;
  const pct = clamp(km / total);

  $('#hudCode').textContent = route.code;
  $('#hudRoute').textContent = `${route.from} → ${route.to}`;
  $('#hudBar').style.width = (pct * 100).toFixed(1) + '%';

  const nowT = S.simT;
  const endT = Date.parse(route.endIso);

  if (pos.phase === 'before') {
    st.innerHTML = `尚未发车 · <span class="em">${fmtStamp(Date.parse(route.startIso))}</span> 从 ${route.from} 始发`;
    $('#hudNext').textContent = route.stops[1]
      ? `${route.stops[1].name} ${fmtStamp(Date.parse(route.stops[1].iso))}` : route.to;
    $('#hudLeft').textContent = fmtDur((endT - nowT) / 60000);
    $('#hudSpeed').textContent = '0';
  } else if (pos.phase === 'arrived') {
    st.innerHTML = `已终到 <span class="em">${route.to}</span>（${fmtStamp(endT)}）`;
    $('#hudNext').textContent = '—';
    $('#hudLeft').textContent = '0m';
    $('#hudSpeed').textContent = '0';
  } else if (pos.phase === 'stop') {
    st.innerHTML = `停靠 <span class="em">${pos.stop.name}</span> · ${fmtStamp(nowT)} 开`;
    const nxt = route.stops[pos.idx + 1];
    $('#hudNext').textContent = nxt ? `${nxt.name} ${fmtStamp(Date.parse(nxt.iso))}` : '—';
    $('#hudLeft').textContent = fmtDur((endT - nowT) / 60000);
    $('#hudSpeed').textContent = '0';
  } else {
    const from = pos.from, to = pos.to;
    st.innerHTML = `运行中 · <span class="em">${from.name} → ${to.name}</span>（${Math.round(pos.frac * 100)}%）`;
    $('#hudNext').textContent = `${to.name} ${fmtStamp(Date.parse(to.iso))}`;
    const left = (endT - nowT) / 60000;
    $('#hudLeft').textContent = fmtDur(left);
    const segKm = (to.km || 0) - (from.km || 0);
    const spd = pos.spanMin > 0 ? segKm / (pos.spanMin / 60) : 0;
    $('#hudSpeed').textContent = spd > 0 ? `${Math.round(spd)} km/h` : '—';
  }
  $('#hudKm').textContent = `${Math.round(km)} km`;
}

/* ============================================================
 * 时间轴（自绘，不用原生 range —— Chrome / Edge 下显示一致）
 * ============================================================ */
const TL = {
  a: 0, b: 0, drag: false,

  init() {
    this.el = $('#tl');
    this.fill = $('#tlFill');
    this.knob = $('#tlKnob');
    this.ticks = $('#tlTicks');
    this.tip = $('#tlTip');
    this.band = $('#tlBand');
    const el = this.el;

    el.addEventListener('pointerdown', e => {
      if (!S.route) return;
      this.drag = true;
      el.classList.add('drag');
      try { el.setPointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
      this.seekAt(e.clientX);
      e.preventDefault();
    });
    el.addEventListener('pointermove', e => {
      if (!S.route) return;
      if (this.drag) this.seekAt(e.clientX);
      else this.showTip(e.clientX);
    });
    const done = e => {
      if (!this.drag) return;
      this.drag = false;
      el.classList.remove('drag');
      try { el.releasePointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
    };
    el.addEventListener('pointerup', done);
    el.addEventListener('pointercancel', done);
    el.addEventListener('keydown', e => {
      if (!S.route) return;
      const step = (e.shiftKey ? 5 : 0.5) / 100;
      let f = this.frac();
      if (e.key === 'ArrowRight') f = clamp(f + step);
      else if (e.key === 'ArrowLeft') f = clamp(f - step);
      else if (e.key === 'Home') f = 0;
      else if (e.key === 'End') f = 1;
      else return;
      e.preventDefault();
      this.setFrac(f, true);
    });
  },

  frac() {
    if (!S.route) return 0;
    const a = Date.parse(S.route.startIso), b = Date.parse(S.route.endIso);
    return clamp((S.simT - a) / Math.max(1, b - a));
  },

  seekAt(clientX) {
    const r = this.el.getBoundingClientRect();
    this.setFrac(clamp((clientX - r.left) / Math.max(1, r.width)), true);
  },

  showTip(clientX) {
    const r = this.el.getBoundingClientRect();
    const f = clamp((clientX - r.left) / Math.max(1, r.width));
    this.tip.textContent = fmtStamp(this.a + (this.b - this.a) * f);
  },

  setFrac(f, seek) {
    if (seek) {
      S.followNow = false;
      S.playing = false;
      $('#btnPlay').textContent = '▶';
      S.simT = this.a + (this.b - this.a) * f;
    }
    this.render(f);
  },

  render(f) {
    if (!this.fill) return;
    const p = (f === undefined ? this.frac() : f) * 100;
    this.fill.style.width = `${p}%`;
    this.knob.style.left = `${p}%`;
    this.el.setAttribute('aria-valuenow', p.toFixed(1));
    this.tip.textContent = fmtStamp(this.a + (this.b - this.a) * (p / 100));
  },

  setup(route) {
    const a = Date.parse(route.startIso), b = Date.parse(route.endIso);
    this.a = a;
    this.b = b;

    // 零点刻度
    this.ticks.innerHTML = '';
    let guard = 0;
    for (let t = startOfNextDay(a); t < b && guard++ < 40; t = startOfNextDay(t + 1000)) {
      const d = document.createElement('i');
      d.className = 'tl-tick';
      d.style.left = `${(((t - a) / Math.max(1, b - a)) * 100).toFixed(2)}%`;
      d.title = `跨零点 ${fmtMD(t)} 00:00`;
      this.ticks.appendChild(d);
    }

    // 自然日分段（跨日车次一眼看出占几天）
    const segs = daySegments(a, b);
    if (segs.length > 1) {
      this.band.classList.remove('hidden');
      this.band.innerHTML = segs.map(s => {
        const pct = s.ms / Math.max(1, b - a) * 100;
        const label = pct < 6 ? '' : (pct < 13 ? fmtMD(s.t) : `${fmtMD(s.t)} ${fmtWD(s.t)}`);
        return `<div class="seg" style="flex:0 0 ${pct.toFixed(2)}%;width:${pct.toFixed(2)}%">${
          label}</div>`;
      }).join('');
    } else {
      this.band.classList.add('hidden');
      this.band.innerHTML = '';
    }
    this.render(0);
  },
};

function setupDock(route) {
  const a = Date.parse(route.startIso), b = Date.parse(route.endIso);
  TL.setup(route);
  $('#labA').textContent = fmtStamp(a);
  $('#labB').textContent = fmtStamp(b);
  $('#labNow').textContent = fmtStamp(S.simT);
  $('#dock').classList.remove('hidden');
  $('#hud').classList.remove('hidden');
}

function syncSlider() {
  if (!S.route) return;
  TL.render();
  $('#labNow').textContent = fmtStamp(S.simT);
}

/* ============================================================
 * 主循环
 * ============================================================ */
function tick() {
  const now = performance.now();
  const dt = S.lastTick ? (now - S.lastTick) : 0;
  S.lastTick = now;

  if (S.route) {
    const a = Date.parse(S.route.startIso), b = Date.parse(S.route.endIso);
    if (S.followNow) {
      S.simT = Date.now();
    } else if (S.playing) {
      S.simT += dt * S.speed;
      if (S.simT >= b) { S.simT = b; S.playing = false; $('#btnPlay').textContent = '▶'; }
    }
    S.simT = clamp(S.simT, a, b);

    const pos = posAt(S.route, S.simT);
    if (pos.point) {
      MapView.setTrain(pos.point);
      if (SvgView.lastPt !== pos.point) SvgView.lastPt = pos.point;
    }
    renderHud(pos);
    syncSlider();

    const nowIdx = pos.phase === 'run' ? pos.idx - 1 : pos.idx;
    if (nowIdx !== S.nowStopIdx) {
      S.nowStopIdx = nowIdx;
      highlightStopSafe(nowIdx);
    }
  }
  requestAnimationFrame(tick);
}

let hlTimer = 0;
function highlightStopSafe(i) {
  if (!S.route || i < 0) return;
  $$('#detail .stop').forEach(el => el.classList.toggle('isnow', Number(el.dataset.i) === i));
}

setInterval(() => { if (S.followNow) { /* 位置每秒刷新，与时刻表对齐 */ } }, 1000);

/* ============================================================
 * 交互
 * ============================================================ */
async function openTrain(code, date, opts = {}) {
  if (!code) return;
  const useDate = date || S.date;
  const cacheOnly = !!opts.cacheOnly;
  const tip = cacheOnly
    ? `正在读取本地已下载的 <b>${code}</b>（${useDate}）…`
    : `正在查询 <b>${code}</b>（${useDate}）的经停站…<br>首次查询会向 12306 请求，约 1~3 秒。`;
  $('#detail').innerHTML = `<div class="empty">${tip}</div>`;
  $('#detail').classList.add('show');
  $('#trainList').classList.add('hidden');
  $('#dock').classList.add('hidden');
  $('#hud').classList.add('hidden');
  try {
    const route = await api('/api/route', { code, date: useDate, cache: cacheOnly ? 1 : '' });
    S.route = route;
    S.followNow = true;
    S.simT = clamp(Date.now(), Date.parse(route.startIso), Date.parse(route.endIso));
    S.nowStopIdx = -1;
    renderDetail(route);
    MapView.setRoute(route);
    setupDock(route);
    const pos = posAt(route, S.simT);
    if (pos.point) MapView.setTrain(pos.point);
  } catch (e) {
    $('#detail').innerHTML = cacheOnly
      ? `<div class="empty">查询失败：${e.message}<br><br>
         · 该日期可能还没下载这个车次的经停站<br>
         · 到「数据下载」补齐该日期，或在「历史数据」换个日期看<br><br>
         <span class="muted small">提示：历史数据页只读本地库，不会去请求 12306。</span></div>`
      : `<div class="empty">查询失败：${e.message}<br><br>可能原因：<br>· 车次当天不开行（换一个日期试试）<br>· 12306 接口限流，稍后重试<br>· 车次号输入有误</div>`;
  }
}

async function searchTrain() {
  const q = $('#q').value.trim();
  if (!q) return;
  $('#sugg').classList.add('hidden');

  // 看起来像车次号：直接开
  if (/^[A-Za-z]?\d+(\/\d+)?$/.test(q)) {
    return openTrain(q.toUpperCase());
  }
  // 否则按车站检索
  try {
    const data = await api('/api/trains', { q, limit: 60 });
    renderTrainList(data.items, `“${q}” 相关车次 ${data.count} 条`);
  } catch (e) {
    $('#trainList').innerHTML = `<div class="empty">搜索失败：${e.message}</div>`;
  }
}

function renderTrainList(items, title) {
  $('#detail').classList.remove('show');
  const list = $('#trainList');
  list.classList.remove('hidden');
  if (!items.length) {
    list.innerHTML = `<div class="empty">没有匹配的车次。<br>试试：<b>G1</b>、<b>G1033</b>，或直接输入车站名筛选。</div>`;
    return;
  }
  list.innerHTML = `<div class="listhdr">${title || ''}</div>` +
    items.map(t => `
      <div class="row" data-code="${t.code}">
        <span class="tcode">${t.code}</span>
        <div class="main">
          <div class="od">${t.start_sta || '?'} → ${t.end_sta || '?'}</div>
          <div class="meta">${t.stop_count || '—'} 站 · ${t.date || ''}</div>
        </div>
        <div class="right"><div class="time">${t.type || ''}</div></div>
      </div>`).join('');
  $$('#trainList .row').forEach(r => r.addEventListener('click', () => {
    $$('#trainList .row').forEach(x => x.classList.remove('active'));
    r.classList.add('active');
    openTrain(r.dataset.code);
  }));
}

async function searchOD() {
  const from = $('#odFrom').value.trim();
  const to = $('#odTo').value.trim();
  if (!from || !to) return;
  const box = $('#odList');
  box.innerHTML = `<div class="empty">正在查询 ${from} → ${to} …</div>`;
  try {
    const data = await api('/api/tickets', { from, to, date: $('#odDate').value || S.date });
    if (!data.trains.length) {
      box.innerHTML = `<div class="empty">${data.from} → ${data.to} 当日无直达车次。</div>`;
      return;
    }
    box.innerHTML = `<div class="listhdr">${data.from} → ${data.to} · ${data.date} · ${data.count} 趟</div>` +
      data.trains.map(t => {
        const seats = t.seats.slice(0, 3).map(s => {
          const ok = s.num && s.num !== '无' && s.num !== '--' && s.num !== '';
          const txt = /^\d+$/.test(s.num || '') ? `${s.num}张` : (s.num === '有' ? '有' : (s.num === '候补' ? '候补' : '无'));
          return `<span class="seat${ok ? ' ok' : ''}">${s.name} <b>¥${Math.round(s.price)}</b> <i>${txt}</i></span>`;
        }).join('');
        const eff = Math.round(Number(t.lishi.slice(0, 2)) * 60 + Number(t.lishi.slice(3)));
        return `<div class="orow" data-code="${t.code}">
          <div class="l1">
            <span class="code">${t.code}</span>
            <span class="times">${t.dep}<i>→</i>${t.arr}</span>
            <span class="dur">${fmtDur(eff)}</span>
            <span class="flags">${t.flags.slice(0, 2).map(f => `<em>${f}</em>`).join('')}</span>
          </div>
          <div class="l2">${seats}</div>
        </div>`;
      }).join('');
    $$('#odList .orow').forEach(r => r.addEventListener('click', () => {
      openTrain(r.dataset.code);
      switchTab('train');
    }));
  } catch (e) {
    box.innerHTML = `<div class="empty">查询失败：${e.message}</div>`;
  }
}

function switchTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('#paneTrain').classList.toggle('hidden', name !== 'train');
  $('#paneOD').classList.toggle('hidden', name !== 'od');
  $('#paneNet').classList.toggle('hidden', name !== 'net');
  $('#paneDL').classList.toggle('hidden', name !== 'dl');
  $('#paneHist').classList.toggle('hidden', name !== 'hist');
  if (name === 'dl') dlLoad();
  if (name === 'hist') histLoad(true);
  MapView.impl && MapView.impl.resize && MapView.impl.resize();
}

/* ============================================================
 * 车站 / 线路（cnrail_data_v2 静态库）
 * ============================================================ */
let netKind = 'station';
let netTimer = 0;

async function searchNet() {
  const q = $('#netQ').value.trim();
  clearTimeout(netTimer);                       // 防止待触发的联想把下拉框又翻开
  $('#sugNet').classList.add('hidden');
  if (!q) return;
  netKind === 'station' ? await showStation(q) : await showLine(q);
}

async function showStation(q) {
  const box = $('#netList'), det = $('#netDetail');
  det.classList.remove('show');
  box.innerHTML = `<div class="empty">查询 ${q} …</div>`;
  try {
    const d = await api('/api/station', { q });
    if (!d.found) { box.innerHTML = `<div class="empty">车站库里没有「${q}」</div>`; return; }
    const chips = (d.lines || []).map(l => `<button class="chip" data-line="${l}">${l}</button>`).join('');
    const svc = (d.services || []).map(s => `<span class="chip flat">${s}</span>`).join('');
    box.innerHTML = '';
    det.innerHTML = `
      <div class="dhead">
        <div class="l1"><span class="code">${d.name}</span>
          ${d.tele ? `<span class="tag">电报码 ${d.tele}</span>` : ''}
          ${d.type ? `<span class="tag">${d.type}</span>` : ''}</div>
        <div class="l2">${[d.province, d.city, d.district].filter(Boolean).join(' / ') || d.loc || ''}</div>
        <div class="l3">
          ${d.bureau ? `<span>${d.bureau}</span>` : ''}
          ${d.opened ? `<span>${d.opened} 启用</span>` : ''}
          <span>${(d.lat || 0).toFixed(4)}, ${(d.lon || 0).toFixed(4)}</span>
        </div>
        ${svc ? `<div class="l3">${svc}</div>` : ''}
        <div class="l3" style="margin-top:6px">连接线路 ${(d.lines || []).length} 条（点击可在地图查看）</div>
        <div class="l3 chips">${chips || '<span style="color:#9aa6ba">无</span>'}</div>
      </div>
      <ul class="stops">
        <li class="stop"><div class="t"></div><div class="rail"><span class="dot"></span></div>
          <div class="name"><span class="n">本站始发车次</span><span class="s">本地库 ${d.trainsFrom} 条</span></div>
          <div class="km"></div></li>
        <li class="stop"><div class="t"></div><div class="rail"><span class="dot"></span></div>
          <div class="name"><span class="n">本站终到车次</span><span class="s">本地库 ${d.trainsTo} 条</span></div>
          <div class="km"></div></li>
      </ul>`;
    det.classList.add('show');
    $$('#netDetail .chip[data-line]').forEach(b =>
      b.addEventListener('click', () => showLine(b.dataset.line)));
    if (d.lat != null) {
      MapView.setRoute({ code: d.name, stops: [{ i: 1, name: d.name, lat: d.lat, lon: d.lon }] });
      MapView.fit();
    }
    $('#hud').classList.add('hidden');
    $('#dock').classList.add('hidden');
  } catch (e) {
    box.innerHTML = `<div class="empty">查询失败：${e.message}</div>`;
  }
}

async function showLine(q) {
  const box = $('#netList'), det = $('#netDetail');
  try {
    det.classList.remove('show');
    box.innerHTML = `<div class="empty">查询线路 ${q} …</div>`;
    const d = await api('/api/line', { q });
    const pts = d.stations.filter(p => p.lat != null);
    MapView.setRoute({
      code: d.name,
      stops: pts.map((p, i) => ({ i: i + 1, name: p.name, lat: p.lat, lon: p.lon })),
    });
    MapView.fit();
    $('#hud').classList.add('hidden');
    $('#dock').classList.add('hidden');
    box.innerHTML = '';
    det.innerHTML = `
      <div class="dhead">
        <div class="l1"><span class="code">${d.name}</span>
          <span class="tag">${d.n} 站</span>
          ${d.totalKm != null ? `<span class="tag">${d.totalKm} km</span>` : ''}
          ${d.kmComplete ? '' : '<span class="tag warn">里程不完整</span>'}</div>
        <div class="l2">坐标覆盖 ${d.coordCoverage}% · 点击站点查看所属线路</div>
      </div>
      <ul class="stops">
        ${d.stations.map((p, i) => `<li class="stop${i === 0 ? ' start' : ''}${i === d.stations.length - 1 ? ' end' : ''}" data-i="${i}">
          <div class="t"><div class="arr">${i + 1}</div></div>
          <div class="rail"><span class="dot"></span></div>
          <div class="name"><span class="n">${p.name}</span>${p.lat == null ? '<span class="s">缺坐标</span>' : ''}</div>
          <div class="km">${p.km != null ? p.km + ' km' : '—'}</div>
        </li>`).join('')}
      </ul>`;
    det.classList.add('show');
    $$('#netDetail .stop').forEach(li => li.addEventListener('click', () => {
      MapView.focusStop(Number(li.dataset.i));
    }));
  } catch (e) {
    box.innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

async function suggestNet() {
  const q = $('#netQ').value.trim();
  const box = $('#sugNet');
  if (!q) { box.classList.add('hidden'); return; }
  try {
    const d = netKind === 'station'
      ? await api('/api/stations', { q, limit: 10 })
      : await api('/api/lines', { q, limit: 10 });
    const items = netKind === 'station' ? d.items : d.items;
    if (!items.length) { box.classList.add('hidden'); return; }
    box.innerHTML = items.map(it =>
      `<div class="sug-item" data-v="${netKind === 'station' ? it.name : it.name}">${
        netKind === 'station' ? it.name : it.name} <i>${netKind === 'station' ? '' : (it.totalKm != null ? it.totalKm + ' km' : '')}</i></div>`).join('');
    box.classList.remove('hidden');
    $$('#sugNet .sug-item').forEach(el => el.addEventListener('click', () => {
      $('#netQ').value = el.dataset.v;
      box.classList.add('hidden');
      searchNet();
    }));
  } catch (_) { box.classList.add('hidden'); }
}

/* 站名联想 */
function bindSuggest(inputSel, sugSel) {
  const input = $(inputSel), box = $(sugSel);
  let timer = 0;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { box.classList.add('hidden'); return; }
    timer = setTimeout(async () => {
      try {
        const d = await api('/api/stations', { q, limit: 10 });
        if (!d.items.length) { box.classList.add('hidden'); return; }
        box.innerHTML = d.items.map(it =>
          `<div class="si${it.hasCoord ? '' : ' nocoord'}" data-v="${it.name}">
             <span>${it.name}</span><span class="c">${it.city || ''}</span></div>`).join('');
        box.classList.remove('hidden');
        $$('.si', box).forEach(si => si.addEventListener('click', () => {
          input.value = si.dataset.v;
          box.classList.add('hidden');
        }));
      } catch (e) { box.classList.add('hidden'); }
    }, 160);
  });
  document.addEventListener('click', e => {
    if (!box.contains(e.target) && e.target !== input) box.classList.add('hidden');
  });
}

/* ============================================================
 * 启动
 * ============================================================ */
/* ============================================================
 * 数据下载（按日期批量拉取车次 + 经停站）
 * ============================================================ */
const DL = { dates: [], sel: null, timer: 0, loading: false };

const nfmt = v => (v == null ? '—' : Number(v).toLocaleString('zh-CN'));

function dlBadge(s) {
  if (s.status === 'running') {
    return ['run', (s.phase || '').startsWith('enum') ? '枚举车次' : '下载停站'];
  }
  if (s.status === 'paused') return ['part', '已暂停'];
  if (s.status === 'error') return ['err', '出错'];
  if (s.trains && !s.pending) return ['done', '已完成'];
  if (s.stops > 0) return ['part', '部分完成'];
  return ['idle', '未下载'];
}

async function dlLoad() {
  if (DL.loading) return;
  DL.loading = true;
  try {
    const d = await api('/api/dates');
    DL.dates = d.items;
    if (!DL.sel) DL.sel = DL.dates[0] && DL.dates[0].date;
    dlRenderList();
    dlRenderDetail();
    const busy = DL.dates.some(x => x.status === 'running');
    clearInterval(DL.timer);
    if (busy) DL.timer = setInterval(dlLoad, 2500);
  } catch (e) {
    $('#dlList').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
  } finally {
    DL.loading = false;
  }
}

function dlRenderList() {
  const el = $('#dlList');
  if (!el) return;
  el.innerHTML = DL.dates.map(s => {
    const [bc, bt] = dlBadge(s);
    const running = s.status === 'running';
    const pct = Math.max(0, Math.min(100, s.percent || 0));
    return `<div class="drow${DL.sel === s.date ? ' active' : ''}" data-date="${s.date}">
      <div class="dl-date">${s.date.slice(5)}<span class="wd">${s.weekday}</span>${
        s.isToday ? '<span class="today">今天</span>' : ''}</div>
      <div class="dl-r">
        <span class="dl-badge ${bc}">${bt}</span>
        ${running
          ? '<button class="btn sm" data-act="stop">暂停</button>'
          : `<button class="btn sm${s.pending || !s.trains ? ' primary' : ''}" data-act="start">${
              s.trains && !s.pending ? '校验' : '下载'}</button>`}
      </div>
      <div class="dl-meta">开行 ${nfmt(s.trains)} 趟${
        s.pending ? ` · 待处理 ${nfmt(s.pending)}` : ''}${
        s.skipped ? ` · 不开行 ${nfmt(s.skipped)}` : ''}</div>
      <div class="dl-bar${running ? ' run' : (pct >= 100 ? ' done' : '')}">
        <i style="width:${pct}%"></i></div>
    </div>`;
  }).join('');

  $$('#dlList .drow').forEach(r => r.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-act]');
    if (btn) { dlAct(btn.dataset.act, r.dataset.date); return; }
    DL.sel = r.dataset.date;
    dlRenderList();
    dlRenderDetail();
  }));
}

function dlRenderDetail() {
  const el = $('#dlDetail');
  if (!el) return;
  const s = DL.dates.find(x => x.date === DL.sel);
  if (!s) { el.innerHTML = ''; return; }
  const running = s.status === 'running';
  const [bc, bt] = dlBadge(s);
  const errs = (s.errors || []).length
    ? `<div class="dl-errs">最近错误：${s.errors.slice(-4).join('<br>')}</div>` : '';
  el.innerHTML = `
    <div class="dl-d-head"><b>${s.date}</b>
      <span class="muted">${s.weekday}${s.isToday ? ' · 今天' : ''}</span>
      <span class="dl-badge ${bc}" style="margin-left:auto">${bt}</span></div>
    <div class="dl-grid">
      <div><span class="k">已确认开行</span><b>${nfmt(s.trains)} 趟</b></div>
      <div><span class="k">待处理（车次全集 ${nfmt(s.universe)}）</span><b>${nfmt(s.pending)}</b></div>
      <div><span class="k">当天不开行</span><b>${nfmt(s.skipped || 0)}</b></div>
      <div><span class="k">失败</span><b>${nfmt(s.failed)}</b></div>
      <div><span class="k">完成度</span><b>${s.percent || 0}%</b></div>
      <div><span class="k">${running ? '本轮进度' : '并发 / 间隔'}</span><b>${
        running
          ? `${nfmt((s.done || 0) + (s.skipped || 0) + (s.failed || 0))} / ${nfmt(s.total)}`
          : `${s.workers || 6} 线程 · ${s.delay || 0.45}s`}</b></div>
    </div>
    <div class="dl-actions">
      ${running
        ? '<button class="btn sm" data-act="stop">⏸ 暂停（进度保留）</button>'
        : `<button class="btn sm primary" data-act="start">${
            s.trains ? (s.pending ? `▶ 继续补 ${nfmt(s.pending)} 趟` : '↻ 重新校验') : '▶ 开始下载'}</button>`}
      ${running ? '' : '<button class="btn sm" data-act="reenum">刷新全集</button>'}
      <button class="btn sm" data-act="export-json">⬇ JSON</button>
      <button class="btn sm" data-act="export-csv">⬇ CSV</button>
    </div>
    ${s.message ? `<div class="dl-msg">${s.message}</div>` : ''}
    ${errs}
    <div class="dl-h2" style="margin-top:10px">导出文件同时保存在 <code>webapp\\data\\exports\\</code>，
      原始 JSON 长期保留在 <code>rail.db</code>，重复下载只补缺失。</div>`;

  $$('#dlDetail [data-act]').forEach(b =>
    b.addEventListener('click', () => dlAct(b.dataset.act, s.date)));
}

async function dlAct(act, date) {
  const sel = $('#dlWorkers');
  const workers = sel ? Number(sel.value) : 6;
  try {
    if (act === 'start') {
      await api('/api/collect/start', { date, workers });
    } else if (act === 'reenum') {
      await api('/api/collect/start', { date, workers, enum: 1 });
    } else if (act === 'stop') {
      await api('/api/collect/stop', { date });
    } else if (act === 'export-json') {
      location.href = `/api/collect/download?date=${date}&fmt=json`;
      return;
    } else if (act === 'export-csv') {
      location.href = `/api/collect/download?date=${date}&fmt=csv`;
      return;
    }
  } catch (e) {
    alert('操作失败：' + e.message);
    return;
  }
  await dlLoad();
}

/* ============================================================
 * 历史数据（已落库，长期保留）
 * ============================================================ */
const HT = { dates: [], sel: null, q: '', items: [], total: 0, limit: 60, loading: false };

async function histLoad(keepSel) {
  if (HT.loading) return;
  HT.loading = true;
  try {
    const d = await api('/api/history');
    HT.dates = d.dates || [];
    const sumRoutes = HT.dates.reduce((a, x) => a + (x.routes || 0), 0);
    if (!HT.dates.length) {
      $('#histDates').innerHTML = '';
      $('#histSum').textContent = '暂无数据';
      $('#histList').innerHTML = `<div class="empty">本地库还没有已下载的数据。<br><br>
        先到「数据下载」选一个日期开始下载，<br>下载过的日期会自动出现在这里。</div>`;
      $('#histFoot').innerHTML = '';
      return;
    }
    $('#histSum').textContent = `${HT.dates.length} 个日期 · ${nfmt(sumRoutes)} 趟已存`;
    if (!keepSel || !HT.dates.some(x => x.date === HT.sel)) {
      // 默认选最近一个「有经停站」的日期，避免打开就是空列表
      HT.sel = (HT.dates.find(x => x.routes > 0) || HT.dates[0]).date;
    }
    histRenderDates();
    await histLoadTrains(false);
  } catch (e) {
    $('#histList').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
  } finally {
    HT.loading = false;
  }
}

function histRenderDates() {
  const box = $('#histDates');
  box.innerHTML = HT.dates.map(d => `
    <button class="hchip${d.date === HT.sel ? ' active' : ''}${d.isPast ? ' past' : ''}${d.routes ? '' : ' nodata'}"
            data-date="${d.date}" title="${d.date} ${d.weekday} · 已存 ${nfmt(d.routes)} 趟 / ${nfmt(d.stops)} 条停站记录">
      ${d.date.slice(5)} <span class="c">${d.weekday.replace('周', '')} · ${
        d.routes ? nfmt(d.routes) : '无停站'}</span>
    </button>`).join('');
  $$('#histDates .hchip').forEach(b => b.addEventListener('click', () => {
    HT.sel = b.dataset.date;
    histRenderDates();
    histLoadTrains(false);
  }));
}

async function histLoadTrains(append) {
  if (!HT.sel) return;
  const offset = append ? HT.items.length : 0;
  const box = $('#histList');
  if (!append) box.innerHTML = `<div class="empty">读取 ${HT.sel} 的车次…</div>`;
  try {
    const d = await api('/api/history/trains', {
      date: HT.sel, q: HT.q, limit: HT.limit, offset,
    });
    HT.total = d.total;
    HT.items = append ? HT.items.concat(d.items) : d.items;
    histRenderList();
  } catch (e) {
    box.innerHTML = `<div class="empty">读取失败：${e.message}</div>`;
  }
}

function histRenderList() {
  const box = $('#histList');
  const day = HT.dates.find(x => x.date === HT.sel) || {};
  if (!HT.items.length) {
    box.innerHTML = `<div class="empty">${HT.q ? `该日没有匹配「${HT.q}」的车次` : '该日没有车次记录'}</div>`;
  } else {
    box.innerHTML = `<div class="listhdr">${HT.sel} ${day.weekday || ''}${
      day.isPast ? ' · 历史' : ''}${day.isToday ? ' · 今天' : ''} · ${
      HT.q ? `匹配 ${nfmt(HT.total)} 条` : `共 ${nfmt(HT.total)} 趟`}</div>` +
      HT.items.map(t => `<div class="hrow" data-code="${t.code}">
        <div class="h1"><span class="code">${t.code}</span>
          <span class="od">${t.start_sta || '?'} → ${t.end_sta || '?'}</span></div>
        <div class="h2">${t.type || ''}${t.type ? ' · ' : ''}${
          t.stops ? `${t.stops} 站` : (t.stop_count ? `${t.stop_count} 站` : '站数未知')}</div>
        <span class="st ${t.has_route ? 'ok' : 'no'}">${t.has_route ? '已下载' : '未下载'}</span>
      </div>`).join('');
    $$('#histList .hrow').forEach(r => r.addEventListener('click', () => {
      openTrain(r.dataset.code, HT.sel, { cacheOnly: true });
      switchTab('train');
      $$('#histList .hrow').forEach(x => x.classList.remove('active'));
      r.classList.add('active');
    }));
  }

  const rest = HT.total - HT.items.length;
  $('#histFoot').innerHTML = `
    <button class="btn sm" data-act="json">⬇ JSON</button>
    <button class="btn sm" data-act="csv">⬇ CSV</button>
    ${rest > 0 ? `<button class="btn sm" data-act="more">加载更多（剩 ${nfmt(rest)}）</button>` : ''}
    <span style="margin-left:auto">导出目录 <code class="inline">webapp\\data\\exports</code></span>`;
  $$('#histFoot [data-act]').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.act === 'more') return histLoadTrains(true);
    location.href = `/api/collect/download?date=${HT.sel}&fmt=${b.dataset.act}`;
  }));
}


async function boot() {
  $('#date').value = S.date;
  $('#odDate').value = S.date;
  $('#date').addEventListener('change', e => {
    S.date = e.target.value || todayStr();
    if (S.route) openTrain(S.route.code);
  });
  $('#btnSearch').addEventListener('click', searchTrain);
  $('#q').addEventListener('keydown', e => {
    if (e.key === 'Enter') searchTrain();
    if (e.key === 'Escape') $('#sugg').classList.add('hidden');
  });
  $('#btnOD').addEventListener('click', searchOD);
  $('#odFrom').addEventListener('keydown', e => e.key === 'Enter' && searchOD());
  $('#odTo').addEventListener('keydown', e => e.key === 'Enter' && searchOD());
  $('#btnSwap').addEventListener('click', () => {
    const a = $('#odFrom'), b = $('#odTo');
    [a.value, b.value] = [b.value, a.value];
  });
  $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
  bindSuggest('#odFrom', '#sugFrom');
  bindSuggest('#odTo', '#sugTo');

  // 车站 / 线路
  $('#btnNet').addEventListener('click', searchNet);
  $('#netQ').addEventListener('keydown', e => e.key === 'Enter' && searchNet());
  $('#netQ').addEventListener('input', () => { clearTimeout(netTimer); netTimer = setTimeout(suggestNet, 220); });
  $$('.chipsel').forEach(b => b.addEventListener('click', () => {
    $$('.chipsel').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    netKind = b.dataset.kind;
    $('#netQ').placeholder = netKind === 'station' ? '北京南' : '京沪高速';
    if ($('#netQ').value.trim()) searchNet();
  }));

  // dock
  $('#btnPlay').addEventListener('click', () => {
    S.playing = !S.playing;
    if (S.playing) S.followNow = false;
    $('#btnPlay').textContent = S.playing ? '❚❚' : '▶';
  });
  $('#btnNow').addEventListener('click', () => {
    S.followNow = true; S.playing = false;
    $('#btnPlay').textContent = '▶';
    S.simT = Date.now();
  });
  $$('.btn.speed').forEach(b => b.addEventListener('click', () => {
    $$('.btn.speed').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    S.speed = Number(b.dataset.speed);
  }));
  TL.init();

  // map key modal
  $('#btnKey').addEventListener('click', () => {
    $('#keyInput').value = mapKey();
    $('#keyModal').classList.remove('hidden');
  });
  $('#keyCancel').addEventListener('click', () => $('#keyModal').classList.add('hidden'));
  $('#keySave').addEventListener('click', () => {
    const v = $('#keyInput').value.trim();
    if (v) localStorage.setItem('railcn.mapkey', v); else localStorage.removeItem('railcn.mapkey');
    location.reload();
  });

  try {
    const st = await api('/api/stats');
    const cn = st.cnrail || {};
    $('#stats').textContent = cn.available
      ? `车次 ${st.trains} · 车站库 ${cn.stations} · 线路 ${cn.lines} · 缓存 ${st.routesCached} 趟`
      : `车次 ${st.trains} · 缓存 ${st.routesCached} 趟 · 坐标 ${st.coordCoverage}%`;
  } catch (e) { }

  await MapView.init().catch(e => console.error('地图初始化失败', e));
  requestAnimationFrame(tick);

  $('#trainList').innerHTML = `<div class="empty">
    输入车次号后回车，即可查看行程与实时推算位置。<br><br>
    <b>试试：</b> G1（京沪标杆）、G1033、D2287、Z21<br>
    <b>站站查询：</b> 切到「站站查询」查余票并直接看图。</div>`;

  // 数据下载页
  $('#dlRefresh').addEventListener('click', dlLoad);

  // 历史数据页
  $('#histRefresh').addEventListener('click', () => histLoad(true));
  $('#histGo').addEventListener('click', () => {
    HT.q = $('#histQ').value.trim();
    histLoadTrains(false);
  });
  $('#histQ').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      HT.q = e.target.value.trim();
      histLoadTrains(false);
    }
    if (e.key === 'Escape') { e.target.value = ''; HT.q = ''; histLoadTrains(false); }
  });

  // 默认展示一趟标杆车（支持 ?code=K3&date=2026-10-22 直接打开，便于收藏/分享）
  const qs = new URLSearchParams(location.search);
  const qCode = (qs.get('code') || '').trim().toUpperCase();
  const qDate = (qs.get('date') || '').trim();
  if (qCode) {
    if (qDate) { S.date = qDate; $('#date').value = qDate; }
    openTrain(qCode, S.date, { cacheOnly: qs.get('cache') === '1' });
  } else {
    openTrain('G1');
  }

  const h = (location.hash || '').replace('#', '');
  if (['train', 'od', 'net', 'dl', 'hist'].includes(h)) switchTab(h);
}

boot();
