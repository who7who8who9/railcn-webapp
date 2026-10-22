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
  staleCode: null,      // 校对页改了坐标，切回车次详情时需要重新拉一次
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
    // lineStops = 经由铁路线上的全部车站（含通过站），老接口没有时退回经停站
    const raw = (route.lineStops && route.lineStops.length > 1)
      ? route.lineStops
      : route.stops.map(s => ({ ...s, stop: true }));
    const pts = raw.filter(s => s.lat != null);

    if (!pts.length) return;

    // 线：外套 + 主线
    const linePts = (route.path && route.path.length > 1) ? route.path : pts;
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

    // 站点：本次停车用实心大蓝点，列车通过的中间站用小空心点
    const stopSvg = encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
        <circle cx="10" cy="10" r="6.5" fill="#1f6feb" stroke="#ffffff" stroke-width="3"/></svg>`
    );
    const passSvg = encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">
        <circle cx="6" cy="6" r="3" fill="#a9b7cc"/></svg>`
    );
    this.layers.stops = new TMap.MultiMarker({
      map: this.map,
      styles: {
        stop: new TMap.MarkerStyle({
          width: 20, height: 20, anchor: { x: 10, y: 10 },
          src: `data:image/svg+xml;charset=utf-8,${stopSvg}`,
        }),
        pass: new TMap.MarkerStyle({
          width: 12, height: 12, anchor: { x: 6, y: 6 },
          src: `data:image/svg+xml;charset=utf-8,${passSvg}`,
        }),
      },
      geometries: pts.map((s, k) => ({
        id: String(k), styleId: s.stop ? 'stop' : 'pass', position: this.g(s.lat, s.lon),
      })),
      zIndex: 6,
    });

    // 站名 + 时刻：只标本次停车站（通过站太密，标了糊成一片）
    const nStop = (route.stops || []).length;
    const labeled = pts.map((s, k) => ({ s, k })).filter(o => o.s.stop);
    this.layers.labels = new TMap.MultiLabel({
      map: this.map,
      styles: {
        l: new TMap.LabelStyle({
          color: '#1a2b45', size: 11, offset: { x: 0, y: -14 },
          alignment: 'center', verticalAlignment: 'bottom',
          backgroundColor: 'rgba(240,244,249,.95)', backgroundPadding: 3,
          borderColor: 'rgba(140,158,180,.55)', borderRadius: 3,
        }),
      },
      geometries: labeled.map(o => ({
        id: 'L' + o.k, styleId: 'l', position: this.g(o.s.lat, o.s.lon),
        content: this.labelText(o.s, nStop),
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

    this.stops = pts;
    this.layer = this.layers;
    // 注意：必须给一个有效初始位置。传 null 会在构造时立刻挂到地图，
    // SDK 内部读 position.x 直接抛 "Cannot read properties of null"。
    this.info = new TMap.InfoWindow({
      map: this.map, position: this.g(pts[0].lat, pts[0].lon),
      offset: { x: 0, y: -14 }, enableCustom: false,
    });
    this.info.close();

    this.layers.stops.on('click', evt => {
      const st = pts[Number(evt.geometry.id)];
      if (!st) return;
      this.info.setPosition(this.g(st.lat, st.lon));
      this.info.setContent(this.stopHtml(st, route));
      this.info.open();
      if (st.stop) highlightStop((st.i || 1) - 1);
    });

    this.fit();
  },

  /** 地图标签：停车站旁边写发车时刻，终到站写到达时刻；没有时刻就只写站名 */
  labelText(st, nStop) {
    const last = (st.i || 0) >= nStop;
    const t = last ? (st.arr || st.dep) : (st.dep || st.arr);
    if (!t) return st.name;
    return `${st.name} ${t}${last ? '到' : '开'}`;
  },

  stopHtml(st, route) {
    if (!st.stop) {
      return `<div class="iw"><b>${st.name}</b>
        <div class="g">通过 · 里程 ${Math.round(st.km)} km</div>
        <div class="g">${route.code} ${route.from} → ${route.to}</div></div>`;
    }
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
    const all = (this.route?.lineStops && this.route.lineStops.length > 1)
      ? this.route.lineStops : (this.route?.stops || []);
    const pts = all.filter(s => s.lat != null);
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
    const all = (route.lineStops && route.lineStops.length > 1)
      ? route.lineStops
      : route.stops.map(s => ({ ...s, stop: true }));
    const pts = all.filter(s => s.lat != null);

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

    // 站点：本次停车实心点 + 站名与时刻，通过站小空心点
    this.stopNodes = {};
    const nStop = (route.stops || []).length;
    pts.forEach((s, i) => {
      const p = px[i];
      const stop = s.stop !== false;
      const c = this.mk('circle', {
        cx: p[0], cy: p[1], r: stop ? 4.2 : 2.4,
        fill: stop ? '#1f6feb' : '#a9b7cc',
        stroke: stop ? '#fff' : 'none',
        'stroke-width': stop ? 2 : 0,
        style: 'cursor:pointer',
      });
      c.addEventListener('click', () => {
        this.infoBox(s);
        if (s.stop) highlightStop((s.i || 1) - 1);
      });
      this.stopNodes[s.i] = c;

      if (stop) {
        const last = (s.i || 0) >= nStop;
        const t = last ? (s.arr || s.dep) : (s.dep || s.arr);
        const label = this.mk('text', {
          x: p[0] + 7, y: p[1] - 6, 'font-size': 11, fill: '#1a2b45',
          'pointer-events': 'none',
        });
        label.textContent = t ? `${s.name} ${t}${last ? '到' : '开'}` : s.name;
        this.gRoot.appendChild(label);
        // 灰色圆角底板垫在文字下面，压在线条和网格上也能看清
        const w = label.getComputedTextLength();
        const bg = this.mk('rect', {
          x: p[0] + 3.5, y: p[1] - 16.5, width: w + 7, height: 15, rx: 3,
          fill: 'rgba(240,244,249,.94)', stroke: '#cfd9e6', 'stroke-width': 1,
          'pointer-events': 'none',
        });
        this.gRoot.insertBefore(bg, label);
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
    let lines;
    if (st.stop === false) {
      lines = [st.name, `通过 · ${Math.round(st.km)} km`];
    } else {
      const arr = st.arr ? `${st.arr}${st.day > 0 ? `(${dayName(st.day)})` : ''}` : '——';
      const dep = st.dep ? `${st.dep}${st.depDay > st.day ? `(${dayName(st.depDay)})` : ''}` : '——';
      lines = [st.name, `第 ${st.i} 站 · ${Math.round(st.km)} km`, `到 ${arr} / 开 ${dep}`];
    }
    const p = this.toPx(this.proj(st.lat, st.lon));
    this.infoTxt.innerHTML = '';
    lines.forEach((t, i) => {
      const ts = this.mk('tspan', { x: 10, dy: i ? 14 : 0 }, this.infoTxt);
      ts.textContent = t;
    });
    this.infoTxt.setAttribute('transform', `translate(${p[0] + 12},${p[1] - 46})`);
    const longest = lines.reduce((m, t) => Math.max(m, t.length), 0);
    const w = Math.max(120, Math.round(longest * 8.4) + 22), h = 20 + lines.length * 14;
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
  setRoute(r) {
    const lg = $('#mapLegend');
    if (lg) lg.classList.toggle('hidden', !(r && r.lineStops && r.lineStops.length));
    this.call('setRoute', r);
  },
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
  if (m === 'manual') {
    const seg = !!(route.manual && route.manual.segKm);
    return `<i class="kmreal" title="${seg
      ? '站间里程手工填写，各站按段累加' : '人工填写的总里程，各站按比例缩放'}">人工里程</i>`;
  }
  if (m === 'real') return `<i class="kmreal" title="${tip}">实测里程</i>`;
  if (m === 'hybrid') return `<i class="kmhyb" title="${tip}">部分实测</i>`;
  return `<i class="kmest" title="${tip}">里程估算</i>`;
}

function renderDetail(route) {
  const el = $('#detail');
  const type = (route.code.match(/^[A-Z]/) ? route.code[0] : 'N');
  const stops = route.stops;
  const segs = route.viaSegs || [];
  const gaps = route.viaUnknown || [];
  const viaHtml = segs.length
    ? `<div class="l3 chips">经由 ${segs.map(s => {
        const span = [s.from, s.to].filter(Boolean).join('–');
        return `<button class="chip" data-line="${esc(s.line)}" title="${esc(span)}">${esc(s.line)}</button>`;
      }).join('')}${route.viaSource === 'manual' ? '<span class="tag manual">人工校正</span>' : ''}</div>`
    : '';
  const gapsHtml = gaps.length
    ? `<div class="l3 gap-line">⚠ 未认出线路：<b>${gaps.map(g => esc(`${g.from}→${g.to}`)).join('、')}</b>
         <button class="linkbtn" data-fixvia="1">去补全</button></div>`
    : '';
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
      ${viaHtml}
      ${gapsHtml}
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

  $$('#detail [data-fixvia]').forEach(b => b.addEventListener('click', () => {
    switchTab('curate');
    cuOpen(route.code);
  }));

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
  S.staleCode = null;               // 这次就是要重新拉，别再触发一遍
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
  if (name !== 'net') { LN.editing = false; lnWide(false); }
  $('#paneTrain').classList.toggle('hidden', name !== 'train');
  $('#paneOD').classList.toggle('hidden', name !== 'od');
  $('#paneNet').classList.toggle('hidden', name !== 'net');
  $('#paneDL').classList.toggle('hidden', name !== 'dl');
  $('#paneHist').classList.toggle('hidden', name !== 'hist');
  $('#paneCurate').classList.toggle('hidden', name !== 'curate');
  // 校对页要把右侧地图区让给编辑器
  const curateMode = name === 'curate';
  document.querySelector('.stage').classList.toggle('hidden', curateMode);
  $('#curateStage').classList.toggle('hidden', !curateMode);
  if (name === 'dl') dlLoad();
  if (name === 'hist') histLoad(true);
  if (name === 'curate') {
    cuLoad();
    const qc = (new URLSearchParams(location.search).get('code') || '').trim().toUpperCase();
    if (qc && qc !== CU.sel) cuOpen(qc);
  }
  // 校对页改过坐标/经由，切回详情时静默重拉一次（此时地图是可见的，重绘才正常）
  if (name === 'train' && S.staleCode && (!S.route || S.route.code === S.staleCode)) {
    const code = S.staleCode;
    S.staleCode = null;
    openTrain(code);
  }
  mapResize();
}

/** 地图在 display:none 之后需要重新量尺寸，否则回来会是一片空白 */
function mapResize() {
  const impl = MapView.impl;
  if (!impl) return;
  try {
    if (impl.resize) impl.resize();
  } catch (e) { /* 降级视图没有 resize */ }
}

/* ============================================================
 * 车站 / 线路（cnrail_data_v2 静态库）
 * ============================================================ */
let netKind = 'station';
let netTimer = 0;

/* ---------- 新增线路 / 车站 ---------- */
const NM = { kind: 'line', rows: [], busy: false, stations: null };

function nmClose() {
  cbClose();
  nmUnKick();
  $('#newModal').classList.add('hidden');
  document.body.classList.remove('modal-open');
}

function nmSetMsg(text, bad) {
  const el = $('#nmMsg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'nm-msg' + (bad ? ' bad' : '');
}

function nmOpen(kind) {
  NM.kind = kind;
  NM.busy = false;
  NM.rows = kind === 'line' ? [{ name: '', km: '' }, { name: '', km: '' }] : [];
  $('#nmTitle').textContent = kind === 'line' ? '新增线路' : '新增车站';
  nmSetMsg('');
  nmRender();
  $('#newModal').classList.remove('hidden');
  document.body.classList.add('modal-open');   // 弹窗期间地图区不接指针事件，避免点穿
  if (kind === 'line') nmLoadStations();
  setTimeout(() => { const el = $('#nmName'); if (el) el.focus(); }, 40);
}

/** 站名库（一次加载 7579 个站名，供新增线路时自绘下拉联想） */
async function nmLoadStations() {
  if (NM.stations) return;
  try {
    const d = await api('/api/stations/names');
    NM.stations = d.items || [];
  } catch (e) { NM.stations = []; }
}

function nmRender() {
  const body = $('#nmBody');
  if (!body) return;

  if (NM.kind === 'line') {
    body.innerHTML = `
      <div class="nm-field">
        <label>线路名</label>
        <input id="nmName" placeholder="比如 沪宁沿江高速线" autocomplete="off">
        <span class="nm-tip">档案里已有的线路不能重名；重名会提示改用「编辑里程」</span>
      </div>
      <div class="nm-sec">
        <div class="nm-sec-h"><b>沿线车站</b>
          <span class="nm-tip">按从上到下的顺序；站名可打字联想或点 ▾ 选择，回车跳下一格；里程填「自起点起算的累计公里数」，不详就留空</span>
          <button class="btn sm ghost" id="nmAddRow">+ 加一站</button>
        </div>
        <div class="nm-rows" id="nmRows"></div>
      </div>
      <div class="nm-field">
        <label>备注</label>
        <input id="nmNote" placeholder="可空，比如：2026 年调图新增">
      </div>`;
    nmRenderRows();
    $('#nmAddRow').addEventListener('click', () => {
      NM.rows.push({ name: '', km: '' });
      nmRenderRows();
      const rows = $$('#nmRows .nm-row');
      if (rows.length) rows[rows.length - 1].querySelector('input.st').focus();
    });
    return;
  }

  body.innerHTML = `
    <div class="nm-field">
      <label>站名</label>
      <input id="nmName" placeholder="比如 新桥镇" autocomplete="off">
      <span class="nm-tip">车站库里已有的站会拒绝新增（改坐标请到「数据校对」）</span>
    </div>
    <div class="nm-field">
      <label>经纬度</label>
      <input id="nmPair" placeholder="可粘贴「经度,纬度」，如 121.523100,31.204800">
      <div class="nm-pair">
        <span>经度</span><input id="nmLon" type="number" step="0.000001" placeholder="121.5231">
        <span>纬度</span><input id="nmLat" type="number" step="0.000001" placeholder="31.2048">
      </div>
    </div>
    <div class="nm-field">
      <label>所属线路（可空，多个用逗号分开）</label>
      <input id="nmLines" placeholder="比如 沪宁沿江高速线">
    </div>
    <div class="nm-two">
      <div class="nm-field"><label>电报码（可空）</label><input id="nmTele"></div>
      <div class="nm-field"><label>路局（可空）</label><input id="nmBureau" placeholder="上海局"></div>
    </div>
    <div class="nm-field"><label>备注</label><input id="nmNote" placeholder="可空"></div>`;

  const pair = $('#nmPair');
  pair.addEventListener('input', () => {
    const p = cuParsePair(pair.value);
    if (!p) return;
    $('#nmLon').value = fmtDeg(p.lon);
    $('#nmLat').value = fmtDeg(p.lat);
  });
}

function nmRenderRows() {
  const box = $('#nmRows');
  if (!box) return;
  box.innerHTML = NM.rows.map((r, i) => `
    <div class="nm-row" data-i="${i}">
      <span class="i">${i + 1}</span>
      <div class="combo">
        <input class="line st" value="${esc(r.name)}" placeholder="车站名，可直接输入"
               autocomplete="off" spellcheck="false">
        <button type="button" class="cb-toggle" tabindex="-1" title="展开车站列表">▾</button>
        <div class="cb-pop"></div>
      </div>
      <input class="km" type="number" step="0.1" min="0" inputmode="decimal"
             value="${esc(r.km)}" placeholder="累计 km（可空）">
      <button class="del" title="删掉这一行">×</button>
    </div>`).join('');

  $$('#nmRows .nm-row').forEach(row => {
    const i = Number(row.dataset.i);
    nmStaBind(row.querySelector('.combo'), i);
    row.querySelector('input.km').addEventListener('input', e => { NM.rows[i].km = e.target.value; });
    row.querySelector('input.km').addEventListener('keydown', e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key !== 'Enter') return;
      e.preventDefault();
      nmStaNext(i);
    });
    row.querySelector('.del').addEventListener('click', () => {
      NM.rows.splice(i, 1);
      nmRenderRows();
    });
  });
}

/* ---------- 新增线路：站名自绘下拉（原生 datalist 会跟中文输入法打架，弃用） ---------- */
let nmKick = null;

function nmUnKick() {
  if (nmKick) {
    window.removeEventListener('scroll', nmKick, true);
    window.removeEventListener('resize', nmKick);
    nmKick = null;
  }
}

function nmStaMatches(q) {
  const all = NM.stations || [];
  const v = (q || '').trim();
  if (!v) return all.slice(0, 200);
  const hits = all.filter(s => s.includes(v));
  hits.sort((a, b) => (b.startsWith(v) - a.startsWith(v)) || a.length - b.length);
  return hits.slice(0, 200);
}

function nmStaPaint(box, q) {
  const pop = box.querySelector('.cb-pop');
  if (!pop) return;
  if (NM.stations == null) {                 // 站名库还没加载完
    pop.innerHTML = '<div class="cb-empty">站名库加载中…</div>';
    CB.items = [];
    CB.idx = -1;
    return;
  }
  const v = (q || '').trim();
  const list = nmStaMatches(v);
  const exact = v && list.includes(v);
  const rows = list.map(n => ({ name: n }));
  if (v && !exact) rows.push({ name: v, custom: true });   // 库外站名也能用
  CB.items = rows;
  const hit = rows.findIndex(r => !r.custom && r.name === v);
  CB.idx = hit >= 0 ? hit : (rows.length ? 0 : -1);
  pop.innerHTML = rows.length
    ? rows.map((r, k) => `
      <div class="cb-item${k === CB.idx ? ' active' : ''}${r.custom ? ' custom' : ''}" data-k="${k}">
        <span class="nm">${esc(r.name)}</span>
        <span class="mt">${r.custom ? '用这个名称' : ''}</span>
      </div>`).join('')
    : '<div class="cb-empty">直接用输入的名字即可</div>';
}

/** 弹窗里的滚动容器会裁剪 absolute 下拉，改为 fixed 贴屏幕坐标 */
function nmStaOpen(box, all) {
  if (CB.box && CB.box !== box) cbClose();
  const inp = box.querySelector('input.line');
  box.classList.add('open');
  CB.box = box;
  nmStaPaint(box, all ? '' : inp.value);
  const pop = box.querySelector('.cb-pop');
  const r = inp.getBoundingClientRect();
  const need = Math.min(pop.scrollHeight || 240, 246);
  pop.classList.toggle('up', r.bottom + need + 16 > window.innerHeight && r.top > need + 16);
  pop.style.position = 'fixed';
  pop.style.left = r.left + 'px';
  pop.style.width = r.width + 'px';
  if (pop.classList.contains('up')) {
    pop.style.top = 'auto';
    pop.style.bottom = (window.innerHeight - r.top + 4) + 'px';
  } else {
    pop.style.bottom = 'auto';
    pop.style.top = (r.bottom + 4) + 'px';
  }
  nmUnKick();
  nmKick = e => {
    if (e && e.target && e.target.closest && e.target.closest('.cb-pop')) return;  // 滚列表自己不关
    cbClose();
    nmUnKick();
  };
  window.addEventListener('scroll', nmKick, true);
  window.addEventListener('resize', nmKick);
}

/** 选定后回车跳下一格；最后一格则自动加一行，连续录入不停手 */
function nmStaNext(i) {
  const rows = $$('#nmRows .nm-row');
  if (i === NM.rows.length - 1) {
    NM.rows.push({ name: '', km: '' });
    nmRenderRows();
    const rs = $$('#nmRows .nm-row');
    rs[rs.length - 1].querySelector('input.line').focus();
  } else if (rows[i + 1]) {
    rows[i + 1].querySelector('input.line').focus();
  }
}

function nmStaPick(box, i, name) {
  box.querySelector('input.line').value = name;
  if (NM.rows[i]) NM.rows[i].name = name;
  cbClose();
  nmUnKick();
  nmStaNext(i);
}

function nmStaBind(box, i) {
  const inp = box.querySelector('input.line');
  const pop = box.querySelector('.cb-pop');
  inp.addEventListener('focus', () => {
    inp.select();                              // 直接打字就是替换，不用先全删
    nmStaOpen(box);
  });
  inp.addEventListener('click', () => { if (CB.box !== box) nmStaOpen(box); });
  inp.addEventListener('input', () => {
    if (NM.rows[i]) NM.rows[i].name = inp.value;
    CB.box = box;
    nmStaPaint(box, inp.value);
    box.classList.add('open');
  });
  inp.addEventListener('keydown', e => {
    if (e.isComposing || e.keyCode === 229) return;    // 输入法组合中的按键不拦截
    const open = box.classList.contains('open');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { nmStaOpen(box); return; }
      const n = CB.items.length;
      if (!n) return;
      CB.idx = (CB.idx + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
      cbHilite(box);
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (open && CB.items[CB.idx]) nmStaPick(box, i, CB.items[CB.idx].name);
      else nmStaNext(i);                       // 没弹下拉时回车=跳下一格
      return;
    }
    if (e.key === 'Escape') { if (open) { e.preventDefault(); cbClose(); } return; }
    if (e.key === 'Tab') { cbClose(); nmUnKick(); }
  });
  box.querySelector('.cb-toggle').addEventListener('mousedown', e => {
    e.preventDefault();                        // 别让输入框失焦
    if (box.classList.contains('open')) { cbClose(); nmUnKick(); return; }
    inp.focus();
    nmStaOpen(box, true);                      // ▾ 展开全部站名
  });
  pop.addEventListener('click', e => {
    const it = e.target.closest('.cb-item');
    if (!it) return;
    const r = CB.items[Number(it.dataset.k)];
    if (r) nmStaPick(box, i, r.name);
  });
}

async function nmSave() {
  if (NM.busy) return;
  const name = ($('#nmName').value || '').trim();
  if (!name) return nmSetMsg('先填名字', true);

  if (NM.kind === 'line') {
    const stations = NM.rows
      .map(r => ({ name: (r.name || '').trim(), km: String(r.km || '').trim() === '' ? null : Number(r.km) }))
      .filter(r => r.name);
    const bad = stations.find(r => r.km != null && (!Number.isFinite(r.km) || r.km < 0));
    if (bad) return nmSetMsg(`${bad.name} 的累计里程不对`, true);
    if (stations.length < 2) return nmSetMsg('至少填两个车站', true);
  } else {
    const lv = ($('#nmLat').value || '').trim(), ov = ($('#nmLon').value || '').trim();
    if (!lv || !ov) return nmSetMsg('经纬度要填齐，或直接粘贴「经度,纬度」', true);
    if (!Number.isFinite(Number(lv)) || !Number.isFinite(Number(ov)))
      return nmSetMsg('经纬度必须是数字', true);
  }

  NM.busy = true;
  nmSetMsg('保存中…');
  try {
    if (NM.kind === 'line') {
      const stations = NM.rows
        .map(r => ({ name: (r.name || '').trim(), km: String(r.km || '').trim() === '' ? null : Number(r.km) }))
        .filter(r => r.name);
      const r = await postJSON('/api/line/new', {
        name, stations, note: ($('#nmNote').value || '').trim(),
      });
      nmClose();
      switchTab('net');
      netKind = 'line';
      $$('.net-toggle .chipsel').forEach(b => b.classList.toggle('active', b.dataset.kind === 'line'));
      $('#netQ').value = r.line.name;
      showLine(r.line.name);
    } else {
      const r = await postJSON('/api/station/new', {
        name,
        lat: Number($('#nmLat').value), lon: Number($('#nmLon').value),
        lines: ($('#nmLines').value || '').trim(),
        tele: ($('#nmTele').value || '').trim(),
        bureau: ($('#nmBureau').value || '').trim(),
        note: ($('#nmNote').value || '').trim(),
      });
      nmClose();
      switchTab('net');
      netKind = 'station';
      $$('.net-toggle .chipsel').forEach(b => b.classList.toggle('active', b.dataset.kind === 'station'));
      $('#netQ').value = r.station.name;
      showStation(r.station.name);
    }
  } catch (e) {
    nmSetMsg(e.message, true);
  } finally {
    NM.busy = false;
  }
}

async function searchNet() {
  const q = $('#netQ').value.trim();
  clearTimeout(netTimer);                       // 防止待触发的联想把下拉框又翻开
  $('#sugNet').classList.add('hidden');
  if (!q) return;
  netKind === 'station' ? await showStation(q) : await showLine(q);
}

async function showStation(q) {
  const box = $('#netList'), det = $('#netDetail');
  $('#paneNet').classList.remove('line-only');
  lnWide(false);
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
          ${d.type ? `<span class="tag">${d.type}</span>` : ''}
          ${d.custom ? '<span class="tag manual">自建</span><button class="btn sm ghost ln-edit" id="stDel">删除</button>' : ''}</div>
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
    const stDel = $('#stDel');
    if (stDel) {
      let armed = false;
      stDel.addEventListener('click', async () => {
        if (!armed) {
          armed = true;
          stDel.textContent = '确认删除？';
          setTimeout(() => { armed = false; stDel.textContent = '删除'; }, 3000);
          return;
        }
        try {
          await postJSON('/api/station/delete', { name: d.name });
          det.classList.remove('show');
          box.innerHTML = `<div class="empty">已删除自建车站「${d.name}」</div>`;
        } catch (e) { stDel.textContent = '删除失败'; }
      });
    }
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

/* ---------- 线路里程：人工校正 ---------- */
const LN = { data: null, segKm: {}, totalKm: '', editing: false, busy: false, mode: 'seg' };

const lnKmText = v => (v == null ? '-' : `${v} km`);

/** 档案区间里程（相邻两站相减），不详为 null */
function lnSegBase(d) {
  const st = (d && d.stations) || [];
  return st.slice(0, -1).map((p, i) => {
    const a = p.kmBase;                 // 接口一定给 kmBase（档案原值），null 就是不详
    const b = st[i + 1].kmBase;
    if (a == null || b == null || b < a) return null;
    return Math.round((b - a) * 10) / 10;
  });
}

/** 档案累计里程（只看档案值，不受手填影响），不详为 null */
function lnCumBase() {
  const seg = lnSegBase(LN.data);
  const st = (LN.data && LN.data.stations) || [];
  const cum = [st.length && st[0].kmBase != null ? st[0].kmBase : 0];
  for (let i = 0; i < seg.length; i++) {
    cum[i + 1] = (cum[i] == null || seg[i] == null)
      ? null : Math.round((cum[i] + seg[i]) * 10) / 10;
  }
  return cum;
}

/** 与后端 cnrail._rebuild_km 同规则，算出「当前输入下」的区间与累计里程 */
function lnEffective() {
  const d = LN.data, seg = lnSegBase(d);
  Object.keys(LN.segKm).forEach(k => {
    const i = Number(k);
    if (i >= 0 && i < seg.length && LN.segKm[k] != null) seg[i] = LN.segKm[k];
  });
  const total = Number(LN.totalKm);
  if (LN.totalKm !== '' && Number.isFinite(total) && total > 0) {
    const manual = Object.keys(LN.segKm).filter(k => LN.segKm[k] != null);
    const manualSum = manual.reduce((s, k) => s + LN.segKm[k], 0);
    const rest = seg.map((val, i) => i).filter(i => seg[i] != null && !manual.includes(String(i)));
    const restSum = rest.reduce((s, i) => s + seg[i], 0);
    const target = total - manualSum;
    if (rest.length && restSum > 0 && target > 0) {
      const r = target / restSum;
      rest.forEach(i => { seg[i] = Math.round(seg[i] * r * 10) / 10; });
    }
  }
  const st = (d && d.stations) || [];
  const cum = [];
  if (st.length) {
    const first = st[0].kmBase;
    cum[0] = first == null ? 0 : first;
  }
  for (let i = 0; i < seg.length; i++) {
    cum[i + 1] = (cum[i] == null || seg[i] == null) ? null : Math.round((cum[i] + seg[i]) * 10) / 10;
  }
  return { seg, cum };
}

/** 编辑里程时把侧栏放宽（390 → 560），收起来还原 */
function lnWide(on) {
  const side = $('.side');
  if (!side) return;
  const had = side.classList.contains('wide');
  side.classList.toggle('wide', !!on);
  if (had === !!on) return;
  mapResize();
  setTimeout(() => { MapView.fit && MapView.fit(); }, 60);
}

function lnSetDirty(msg) {
  const el = $('#lnMsg');
  if (!el) return;
  el.textContent = msg || '';
  el.classList.toggle('bad', /失败/.test(msg || ''));
}

function lnRenderSegs() {
  const boxEl = $('#lnSegs');
  if (!boxEl) return;
  if (LN.mode === 'cum') return lnRenderCum();

  const d = LN.data, base = lnSegBase(d), st = d.stations || [];
  boxEl.innerHTML = st.slice(0, -1).map((p, i) => {
    const a = base[i];
    const has = Object.prototype.hasOwnProperty.call(LN.segKm, i) && LN.segKm[i] != null;
    const v = has ? LN.segKm[i] : null;
    const delta = has && a != null ? Math.round((v - a) * 10) / 10 : null;
    const dtxt = delta == null ? '' : (Math.abs(delta) < 0.05 ? '=' : (delta > 0 ? '+' : '') + delta.toFixed(1));
    return `<div class="seg-row${has ? ' manual' : ''}" data-i="${i}">
      <span class="i">${i + 1}</span>
      <span class="od" title="${esc(p.name)} → ${esc(st[i + 1].name)}">${esc(p.name)}<em>→</em>${esc(st[i + 1].name)}</span>
      <input class="km" type="number" step="0.1" min="0" max="5000" inputmode="decimal"
             value="${has ? v : ''}" placeholder="${a == null ? '-' : a.toFixed(1)}"
             title="${a == null ? '档案没有这一段的里程，可直接填' : '留空沿用档案值 ' + a.toFixed(1) + ' km'}">
      <span class="u">km</span>
      <button class="seg-clr" title="恢复这一段的档案值"${has ? '' : ' disabled'}>↺</button>
      <span class="auto">${dtxt}</span>
    </div>`;
  }).join('');

  $$('#lnSegs .seg-row').forEach(row => {
    const i = Number(row.dataset.i);
    const inp = row.querySelector('input.km');
    const clr = row.querySelector('.seg-clr');
    const apply = () => {
      const raw = inp.value.trim();
      const n = Number(raw);
      if (raw === '' || !Number.isFinite(n) || n <= 0) delete LN.segKm[i];
      else LN.segKm[i] = n;
      const on = Object.prototype.hasOwnProperty.call(LN.segKm, i);
      row.classList.toggle('manual', on);
      clr.disabled = !on;
      const base = lnSegBase(LN.data)[i];
      const aEl = row.querySelector('.auto');
      if (aEl) {
        const dd = on && base != null ? Math.round((LN.segKm[i] - base) * 10) / 10 : null;
        aEl.textContent = dd == null ? '' : (Math.abs(dd) < 0.05 ? '=' : (dd > 0 ? '+' : '') + dd.toFixed(1));
      }
      lnRefreshPreview();
      lnSetDirty('有未保存的修改');
    };
    inp.addEventListener('input', apply);
    inp.addEventListener('blur', apply);
    inp.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const next = row.nextElementSibling;
      if (next) next.querySelector('input.km').focus(); else inp.blur();
    });
    clr.addEventListener('click', () => {
      inp.value = '';
      delete LN.segKm[i];
      row.classList.remove('manual');
      clr.disabled = true;
      const aEl = row.querySelector('.auto');
      if (aEl) aEl.textContent = '';
      lnRefreshPreview();
      lnSetDirty('有未保存的修改');
    });
  });
}

/** 累计模式：直接填「自起点站到这一站的公里数」，内部反算成段距保存 */
function lnRenderCum() {
  const boxEl = $('#lnSegs');
  const d = LN.data, st = (d && d.stations) || [];
  if (st.length < 2) {
    boxEl.innerHTML = '<div class="ve-note">这条线只有一站，没有可填的里程。</div>';
    return;
  }
  const eff = lnEffective(), baseCum = lnCumBase();
  const first = st[0].name;
  boxEl.innerHTML = st.slice(1).map((p, k) => {
    const i = k + 1;
    const has = LN.segKm[i - 1] != null;
    const prev = eff.cum[i - 1], cur = eff.cum[i], base = baseCum[i];
    const locked = prev == null;
    const dd = (cur != null && base != null) ? Math.round((cur - base) * 10) / 10 : null;
    const dtxt = dd == null ? '' : (Math.abs(dd) < 0.05 ? '=' : (dd > 0 ? '+' : '') + dd.toFixed(1));
    return `<div class="seg-row cum${has ? ' manual' : ''}${locked ? ' lock' : ''}" data-i="${i}">
      <span class="i">${i + 1}</span>
      <span class="od" title="自 ${esc(first)} 起算">${esc(p.name)}</span>
      <input class="km" type="number" step="0.1" min="0" max="20000" inputmode="decimal"
             value="${has && cur != null ? cur : ''}" placeholder="${cur == null ? '-' : cur.toFixed(1)}"
             ${locked ? 'disabled' : ''}
             title="${locked ? '前面有里程不详的区间，先切到「按段距」补齐' : `留空沿用当前累计 ${cur == null ? '-' : cur.toFixed(1)} km`}">
      <span class="u">km</span>
      <button class="seg-clr" title="恢复档案值"${has ? '' : ' disabled'}>↺</button>
      <span class="auto">${dtxt}</span>
    </div>`;
  }).join('');

  $$('#lnSegs .seg-row').forEach(row => {
    const i = Number(row.dataset.i);
    const inp = row.querySelector('input.km');
    const clr = row.querySelector('.seg-clr');
    const apply = () => {
      const raw = inp.value.trim();
      const c = Number(raw);
      if (raw === '' || !Number.isFinite(c) || c <= 0) {
        delete LN.segKm[i - 1];
        row.classList.remove('manual', 'bad');
        clr.disabled = true;
      } else {
        const prev = lnEffective().cum[i - 1];
        const dv = prev == null ? null : Math.round((c - prev) * 10) / 10;
        if (dv == null || dv <= 0) {
          row.classList.add('bad');
          lnSetDirty(prev == null
            ? '前面有里程不详的区间，先切到「按段距」补齐'
            : `第 ${i + 1} 站的累计要大于前一站的 ${prev.toFixed(1)} km`);
          return;
        }
        row.classList.remove('bad');
        row.classList.add('manual');
        clr.disabled = false;
        LN.segKm[i - 1] = dv;
      }
      lnRefreshCumRows();
      lnRefreshPreview();
      lnSetDirty('有未保存的修改');
    };
    inp.addEventListener('input', apply);
    inp.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const next = row.nextElementSibling;
      if (next) next.querySelector('input.km').focus(); else inp.blur();
    });
    clr.addEventListener('click', () => {
      delete LN.segKm[i - 1];
      lnRenderSegs();
      lnRefreshPreview();
      lnSetDirty('有未保存的修改');
    });
  });
}

/** 累计模式下只刷新数值（不重建输入框，免得输入时丢焦点） */
function lnRefreshCumRows() {
  const eff = lnEffective(), baseCum = lnCumBase();
  $$('#lnSegs .seg-row.cum').forEach(row => {
    const i = Number(row.dataset.i);
    const inp = row.querySelector('input.km');
    const has = LN.segKm[i - 1] != null;
    if (document.activeElement !== inp) {
      inp.value = has && eff.cum[i] != null ? eff.cum[i] : '';
      inp.placeholder = eff.cum[i] == null ? '-' : eff.cum[i].toFixed(1);
    }
    const dd = (eff.cum[i] != null && baseCum[i] != null)
      ? Math.round((eff.cum[i] - baseCum[i]) * 10) / 10 : null;
    const el = row.querySelector('.auto');
    if (el) el.textContent = dd == null ? '' : (Math.abs(dd) < 0.05 ? '=' : (dd > 0 ? '+' : '') + dd.toFixed(1));
  });
}

/** 只重画里程数字：合计 + 各站累计（不碰输入框，避免丢焦点） */
function lnRefreshPreview() {
  const { cum } = lnEffective();
  const st = (LN.data && LN.data.stations) || [];
  const typed = Number(LN.totalKm);
  const hasTyped = LN.totalKm !== '' && Number.isFinite(typed) && typed > 0;
  // 已知里程：最后一个算得出来的累计（尾部有缺口时，末站是 None）
  let known = null;
  for (let i = cum.length - 1; i >= 0; i--) if (cum[i] != null) { known = cum[i]; break; }
  const sum = $('#lnSum');
  if (sum) {
    sum.textContent = hasTyped
      ? `合计 ${lnKmText(typed)}`
      : `已知 ${lnKmText(known)}`;
  }
  const list = $('#lnStops');
  if (list) {
    $$('#lnStops .stop').forEach((li, i) => {
      const k = li.querySelector('.km');
      if (k) k.textContent = lnKmText(cum[i]);
    });
  }
  const nManual = Object.keys(LN.segKm).filter(k => LN.segKm[k] != null).length;
  const warn = $('#lnWarn');
  if (warn) {
    const decl = hasTyped ? typed : (LN.data && LN.data.totalKm);
    warn.textContent = (known != null && decl != null && known > decl + 1)
      ? `已知 ${known} km 已超过总里程 ${decl} km` : '';
  }
  const tag = $('#lnFoot');
  if (tag) {
    tag.textContent = [
      `${st.length} 站`,
      nManual ? `手填 ${nManual} 段` : '',
      LN.totalKm !== '' ? `总里程 ${LN.totalKm} km` : '',
    ].filter(Boolean).join(' · ');
  }
}

async function lnSave() {
  if (LN.busy || !LN.data) return;
  LN.busy = true;
  lnSetDirty('保存中…');
  try {
    const r = await postJSON('/api/line/km', {
      name: LN.data.name,
      segKm: LN.segKm,
      totalKm: LN.totalKm === '' ? null : Number(LN.totalKm),
    });
    LN.data = r.line;
    LN.segKm = { ...((r.line.override && r.line.override.segKm) || {}) };
    LN.totalKm = r.line.override && r.line.override.totalKm != null ? String(r.line.override.totalKm) : '';
    lnRenderHead();
    lnRenderEditor();
    lnSetDirty('已保存');
  } catch (e) {
    lnSetDirty('保存失败：' + e.message);
  } finally {
    LN.busy = false;
  }
}

async function lnResetAll() {
  if (LN.busy || !LN.data) return;
  LN.busy = true;
  lnSetDirty('正在恢复档案值…');
  try {
    const r = await postJSON('/api/line/km/clear', { name: LN.data.name });
    LN.data = r.line;
    LN.segKm = {};
    LN.totalKm = '';
    lnRenderHead();
    lnRenderEditor();
    lnSetDirty('已恢复档案值');
  } catch (e) {
    lnSetDirty('恢复失败：' + e.message);
  } finally {
    LN.busy = false;
  }
}

function lnRenderHead() {
  const d = LN.data, el = $('#lnHead');
  if (!el || !d) return;
  el.innerHTML = `
    <div class="l1"><span class="code">${esc(d.name)}</span>
      <span class="tag">${d.n} 站</span>
      <span class="tag${d.totalKm == null ? ' warn' : ''}">${lnKmText(d.totalKm)}</span>
      ${d.kmManual ? '<span class="tag manual">人工里程</span>' : ''}
      ${d.kmComplete ? '' : '<span class="tag warn">里程不完整</span>'}
      ${d.custom ? '<span class="tag manual">自建</span>' : ''}
      <span class="ln-acts">
        ${d.custom ? '<button class="btn sm ghost" id="lnDel">删除</button>' : ''}
        <button class="btn sm${LN.editing ? ' on' : ''}" id="lnEditBtn">${LN.editing ? '收起编辑' : '编辑里程'}</button>
      </span>
    </div>
    <div class="l2">坐标覆盖 ${d.coordCoverage}% · 点击站点查看所属线路 · <span id="lnFoot"></span></div>`;
  $('#lnEditBtn').addEventListener('click', () => {
    LN.editing = !LN.editing;
    lnWide(LN.editing);
    lnRenderHead();
    lnRenderEditor();
  });
  const del = $('#lnDel');
  if (del) {
    let armed = false;
    del.addEventListener('click', async () => {
      if (!armed) {
        armed = true;
        del.textContent = '确认删除？';
        setTimeout(() => { armed = false; del.textContent = '删除'; }, 3000);
        return;
      }
      try {
        await postJSON('/api/line/delete', { name: d.name });
        LN.data = null; LN.segKm = {}; LN.totalKm = ''; LN.editing = false;
        lnWide(false);
        $('#netDetail').classList.remove('show');
        $('#paneNet').classList.remove('line-only');
        $('#netList').innerHTML = `<div class="empty">已删除自建线路「${d.name}」</div>`;
      } catch (e) {
        del.textContent = '删除失败';
      }
    });
  }
  lnRefreshPreview();
}

function lnRenderEditor() {
  const host = $('#lnKmBox');
  if (!host) return;
  host.hidden = !LN.editing;
  if (!LN.editing) { host.innerHTML = ''; return; }
  const d = LN.data;
  const segs = lnSegBase(d);
  const known = segs.filter(v => v != null).length;
  host.innerHTML = `
    <div class="lnkm-head">
      <b>线路里程</b>
      <div class="segmode" id="lnMode">
        <button data-mode="seg" class="${LN.mode === 'seg' ? 'on' : ''}" title="填相邻两站之间的公里数">按段距</button>
        <button data-mode="cum" class="${LN.mode === 'cum' ? 'on' : ''}" title="填自起点站算起的累计公里数">按累计</button>
      </div>
      <span class="lnkm-hint">${LN.mode === 'seg'
        ? '灰字是档案值，留空沿用；填了就锁住这一段，不详的段显示 -'
        : '填「从起点站到这一站」的累计公里数，不详的显示 -'}</span>
      <div class="lnkm-btns">
        <button class="btn sm primary" id="lnSave">保存</button>
        <button class="btn sm" id="lnResetAll">全部恢复档案</button>
        <button class="btn sm ghost" id="lnCancel">取消</button>
      </div>
    </div>
    <div class="seg-list" id="lnSegs"></div>
    <div class="lnkm-foot">
      <label for="lnTotal">线路总里程</label>
      <input id="lnTotal" type="number" step="0.1" min="1" max="20000" inputmode="decimal"
             value="${LN.totalKm === '' ? '' : LN.totalKm}"
             placeholder="${d.totalKmBase != null ? d.totalKmBase : '-'}">
      <span class="u">km</span>
      <button class="btn sm ghost" id="lnTotalClr"${LN.totalKm === '' ? ' disabled' : ''}>恢复档案</button>
      <span class="lnkm-sum" id="lnSum"></span>
      <span class="lnkm-warn" id="lnWarn"></span>
      <span class="lnkm-seg" id="lnMsg"></span>
    </div>
    <div class="ve-note">档案里 ${segs.length} 段，其中 ${known} 段有里程；填了总里程时，只有没手填的段会等比缩放到「总里程 − 手填段之和」。保存后车次详情、地图、时间轴全部同步。</div>`;

  $$('#lnMode button').forEach(b => b.addEventListener('click', () => {
    if (LN.mode === b.dataset.mode) return;
    LN.mode = b.dataset.mode;
    $$('#lnMode button').forEach(x => x.classList.toggle('on', x.dataset.mode === LN.mode));
    lnRenderEditor();
  }));

  lnRenderSegs();
  $('#lnSave').addEventListener('click', lnSave);
  $('#lnResetAll').addEventListener('click', lnResetAll);
  $('#lnCancel').addEventListener('click', () => {
    LN.editing = false;
    LN.segKm = { ...((LN.data.override && LN.data.override.segKm) || {}) };
    LN.totalKm = LN.data.override && LN.data.override.totalKm != null ? String(LN.data.override.totalKm) : '';
    lnWide(false);
    lnRenderHead();
    lnRenderEditor();
  });
  const tot = $('#lnTotal');
  tot.addEventListener('input', () => {
    LN.totalKm = tot.value.trim();
    $('#lnTotalClr').disabled = LN.totalKm === '';
    lnRefreshPreview();
    lnSetDirty('有未保存的修改');
  });
  $('#lnTotalClr').addEventListener('click', () => {
    LN.totalKm = '';
    tot.value = '';
    $('#lnTotalClr').disabled = true;
    lnRefreshPreview();
    lnSetDirty('有未保存的修改');
  });
  lnRefreshPreview();
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
    LN.data = d;
    LN.editing = false;
    lnWide(false);
    LN.segKm = { ...((d.override && d.override.segKm) || {}) };
    LN.totalKm = d.override && d.override.totalKm != null ? String(d.override.totalKm) : '';
    det.innerHTML = `
      <div class="dhead" id="lnHead"></div>
      <div class="lnkm" id="lnKmBox" hidden></div>
      <ul class="stops" id="lnStops">
        ${d.stations.map((p, i) => `<li class="stop${i === 0 ? ' start' : ''}${i === d.stations.length - 1 ? ' end' : ''}" data-i="${i}">
          <div class="t"><div class="arr">${i + 1}</div></div>
          <div class="rail"><span class="dot"></span></div>
          <div class="name"><span class="n">${esc(p.name)}</span>${p.lat == null ? '<span class="s">缺坐标</span>' : ''}</div>
          <div class="km">${lnKmText(p.km)}</div>
        </li>`).join('')}
      </ul>`;
    det.classList.add('show');
    $('#paneNet').classList.add('line-only');   // 线路详情独占侧栏高度
    lnRenderHead();
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
      `<div class="sug-item" data-v="${it.name}">${it.name} <i>${netKind === 'station'
        ? '' : `${it.n} 站 · ${lnKmText(it.totalKm)}`}</i></div>`).join('');
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
  // 新增线路 / 车站
  $('#btnNewLine') && $('#btnNewLine').addEventListener('click', () => nmOpen('line'));
  $('#btnNewStation') && $('#btnNewStation').addEventListener('click', () => nmOpen('station'));
  $('#nmSave').addEventListener('click', nmSave);
  $('#nmCancel').addEventListener('click', nmClose);
  $('#newModal').addEventListener('click', e => { if (e.target.id === 'newModal') nmClose(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('#newModal').classList.contains('hidden')) nmClose();
  });

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
    document.body.classList.add('modal-open');
  });
  $('#keyCancel').addEventListener('click', () => {
    $('#keyModal').classList.add('hidden');
    document.body.classList.remove('modal-open');
  });
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
  if (['train', 'od', 'net', 'dl', 'hist', 'curate'].includes(h)) switchTab(h);
}

/* ==========================================================================
   数据校对：补全经由 / 里程 / 坐标
   ========================================================================== */
const ISSUE_CN = { via: '经由待补', km: '里程估算', coord: '缺坐标' };
const CU_FILTERS = [
  ['todo', '待补全'], ['via0', '经由全无'], ['via', '经由缺口'], ['km', '里程估算'],
  ['coord', '缺坐标'], ['manual', '已校正'], ['clean', '无问题'], ['all', '全部'],
];
const CU = {
  filter: 'todo', q: '', items: [], total: 0, limit: 60,
  sel: null, detail: null, via: [], segKm: {}, lines: null, poll: null,
  coordQ: '', coordOnlyMiss: false, mode: 'seg',
};

const esc = s => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({ error: '响应解析失败' }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function cuWire() {
  $('#cuScan').addEventListener('click', async () => {
    $('#cuProg').textContent = '正在启动扫描…';
    try {
      await postJSON('/api/curate/scan', {});
      cuPoll();
    } catch (e) {
      $('#cuProg').textContent = '启动失败：' + e.message;
    }
  });
  $('#cuGo').addEventListener('click', () => { CU.q = $('#cuQ').value.trim(); cuLoad(); });
  $('#cuQ').addEventListener('keydown', e => {
    if (e.key === 'Enter') { CU.q = e.target.value.trim(); cuLoad(); }
    if (e.key === 'Escape') { e.target.value = ''; CU.q = ''; cuLoad(); }
  });
}

async function cuLoad() {
  try {
    cuRenderStats(await api('/api/curate/stats'));
  } catch (e) { /* 统计失败不影响列表 */ }
  const box = $('#cuList');
  if (!CU.items.length) box.innerHTML = '<div class="empty">正在载入校对清单…</div>';
  try {
    const d = await api('/api/curate/list', { filter: CU.filter, q: CU.q, limit: CU.limit });
    CU.items = d.items || [];
    CU.total = d.total || 0;
    cuRenderFilters();
    cuRenderList();
  } catch (e) {
    box.innerHTML = `<div class="empty">清单载入失败：${esc(e.message)}<br><br>`
      + '如果还没扫过，点上面「重新扫描」生成清单。</div>';
  }
}

function cuRenderStats(st) {
  const c = st.counts || {};
  $('#cuStats').innerHTML = [
    ['待补全', c.todo, 'warn'], ['经由全无', c.via0, 'warn'], ['经由缺口', c.via, ''],
    ['里程', c.km, ''], ['缺坐标', c.coord, ''], ['已校正', c.manual, 'ok'],
    ['已扫描', c.scanned, ''],
  ].map(([k, v, cls]) => `<span class="cu-stat ${cls}">${k}<b>${nfmt(v || 0)}</b></span>`).join('');

  const p = $('#cuProg');
  const btn = $('#cuScan');
  if (st.running) {
    p.textContent = `${st.message || '扫描中'}（${nfmt(st.done)}/${nfmt(st.total)}）`;
    btn.textContent = '扫描中…';
    btn.disabled = true;
    cuPoll();
  } else {
    btn.textContent = '重新扫描';
    btn.disabled = false;
    if (st.error) p.textContent = '出错：' + st.error;
    else if (c.lastScan) p.textContent = `上次扫描 ${fmtMD(c.lastScan * 1000)} ${fmtClock(c.lastScan * 1000)}`;
    else p.textContent = '还没扫过，点「重新扫描」生成清单';
    if (CU.poll) { clearTimeout(CU.poll); CU.poll = null; }
  }
}

function cuPoll() {
  if (CU.poll) return;
  CU.poll = setTimeout(async () => {
    CU.poll = null;
    try {
      const st = await api('/api/curate/stats');
      cuRenderStats(st);
      if (!st.running) cuLoad();
    } catch (e) { cuPoll(); }
  }, 1500);
}

function cuRenderFilters() {
  $('#cuFilters').innerHTML = CU_FILTERS.map(([k, label]) =>
    `<button class="cu-filter${k === CU.filter ? ' active' : ''}" data-f="${k}">${label}</button>`).join('');
  $$('#cuFilters .cu-filter').forEach(b => b.addEventListener('click', () => {
    CU.filter = b.dataset.f;
    CU.limit = 60;
    cuLoad();
  }));
}

function cuRenderList() {
  const box = $('#cuList');
  if (!CU.items.length) {
    box.innerHTML = `<div class="empty">${CU.q ? '没有匹配的车次' : '这个分类下没有车次'}</div>`;
    $('#cuFoot').innerHTML = '';
    return;
  }
  box.innerHTML = CU.items.map(it => {
    const flags = (it.issues || []).map(x => {
      const label = (x === 'via' && it.viaCoverage < 1) ? '经由全无' : (ISSUE_CN[x] || x);
      return `<span class="cu-flag ${x}">${label}</span>`;
    }).join('');
    const manual = (it.viaSource === 'manual' || it.kmMode === 'manual')
      ? '<span class="cu-flag manual">已校正</span>' : '';
    const via = it.via ? esc(it.via) : '<span class="warn">经由未识别</span>';
    const cov = it.viaSource === 'manual' ? '' : ` · ${Math.round(it.viaCoverage || 0)}%`;
    const gap = it.gaps ? `<span class="warn"> · 缺 ${it.gaps} 段</span>` : '';
    const miss = it.missing ? ` · 缺 ${it.missing} 站坐标` : '';
    return `<div class="cu-row${it.code === CU.sel ? ' active' : ''}" data-code="${esc(it.code)}">
      <div class="r1"><span class="code">${esc(it.code)}</span>
        <span class="od">${esc(it.from || '—')} → ${esc(it.to || '—')}</span></div>
      <div class="r2">${via}${cov}${gap} · ${it.totalKm} km${miss}</div>
      <div class="flags">${manual}${flags}</div>
    </div>`;
  }).join('');

  $$('#cuList .cu-row').forEach(r => r.addEventListener('click', () => cuOpen(r.dataset.code)));

  $('#cuFoot').innerHTML = `<span>共 ${nfmt(CU.total)} 趟 · 已显示 ${CU.items.length}</span>`
    + (CU.items.length < CU.total ? '<button id="cuMore" class="btn sm">再加载 60</button>' : '');
  const more = $('#cuMore');
  if (more) more.addEventListener('click', () => { CU.limit += 60; cuLoad(); });
}

async function cuOpen(code) {
  CU.sel = code;
  CU.detail = null;
  $('#cuEditor').innerHTML = `<div class="cu-empty">正在载入 <b>${esc(code)}</b> …</div>`;
  cuRenderList();
  try {
    const d = await api('/api/curate/detail', { code });
    CU.detail = d;
    if (!d.exists) {
      $('#cuEditor').innerHTML = `<div class="cu-empty">${esc(d.error || '这趟车在本地库里没有数据')}</div>`;
      return;
    }
    if (!CU.lines) cuLoadLineNames();
    CU.via = d.override
      ? (d.override.via || []).map(x => ({ line: x.line || '', from: x.from || '', to: x.to || '' }))
      : cuRowsFromAuto(d.auto || {});
    CU.segKm = {};
    const ovSeg = (d.override && d.override.segKm) || {};
    Object.keys(ovSeg).forEach(k => {
      const i = Number(k);
      const v = Number(ovSeg[k]);
      if (Number.isFinite(i) && i >= 0 && Number.isFinite(v) && v > 0) CU.segKm[i] = v;
    });
    cuRenderEditor();
  } catch (e) {
    $('#cuEditor').innerHTML = `<div class="cu-empty">载入失败：${esc(e.message)}</div>`;
  }
}

async function cuLoadLineNames() {
  try {
    const d = await api('/api/curate/lineNames');
    CU.lines = (d.items || []).map(x => (typeof x === 'string'
      ? { name: x, n: null, totalKm: null } : x));
    CU.lineMap = new Map(CU.lines.map(l => [l.name, l]));
  } catch (e) { CU.lines = []; CU.lineMap = new Map(); }
}

/** 站点自动补全用的 datalist（每趟车单独一套站名） */
function cuBindStations(names) {
  let dl = $('#cuStaList');
  if (!dl) {
    dl = document.createElement('datalist');
    dl.id = 'cuStaList';
    document.body.appendChild(dl);
  }
  dl.innerHTML = (names || []).map(n => `<option value="${esc(n)}">`).join('');
}

function cuChainHtml(segs, gaps) {
  // 区间与缺口都带 i0（在经停对里的序号），按运行顺序合并，缺口就落在正确位置
  const items = [];
  (segs || []).forEach(s => items.push({ i0: s.i0 == null ? 0 : s.i0, seg: s }));
  (gaps || []).forEach(g => items.push({ i0: g.i0 == null ? 0 : g.i0, gap: g }));
  items.sort((a, b) => a.i0 - b.i0);
  const parts = [];
  items.forEach((it, i) => {
    if (i) parts.push('<span class="varrow">→</span>');
    if (it.seg) {
      const span = [it.seg.from, it.seg.to].filter(Boolean).join('–');
      const cls = it.seg.src === 'manual' ? 'manual' : '';
      parts.push(`<span class="vseg ${cls}">${esc(it.seg.line)}<em>${esc(span)}</em></span>`);
    } else {
      parts.push(`<span class="vseg gap">未识别 <em>${esc(it.gap.from)} → ${esc(it.gap.to)}</em></span>`);
    }
  });
  return parts.length ? parts.join('') : '<span class="ve-note">— 没有推断出任何线路 —</span>';
}

/** 自动结果 → 编辑行；缺口变成「线路名留空」的行，用户只需补线路名 */
function cuRowsFromAuto(auto) {
  const items = [];
  (auto.viaSegs || []).forEach(s => items.push({
    i0: s.i0 == null ? 0 : s.i0, line: s.line || '', from: s.from || '', to: s.to || '',
  }));
  (auto.viaUnknown || []).forEach(g => items.push({
    i0: g.i0 == null ? 0 : g.i0, line: '', from: g.from || '', to: g.to || '',
  }));
  items.sort((a, b) => a.i0 - b.i0);
  return items.map(x => ({ line: x.line, from: x.from, to: x.to }));
}

function cuRenderEditor() {
  const d = CU.detail;
  const a = d.auto || {};
  const ov = d.override;
  const isManual = !!(ov && ((ov.via || []).length || ov.totalKm
    || Object.keys(ov.segKm || {}).length));
  const kmModeCn = { manual: '人工填写', real: '线路实测', hybrid: '部分实测', estimate: '直线估算' }[a.kmMode] || a.kmMode;
  const covText = a.viaSource === 'none' || !(a.viaSegs || []).length
    ? '整趟都没认出线路'
    : `覆盖 ${a.viaCoverage}%（${(a.viaUnknown || []).length ? (a.viaUnknown || []).length + ' 段未识别' : '全部识别'}）`;
  const stops = d.stops || [];
  const coord = d.coordSummary || {
    total: stops.length,
    missing: stops.filter(s => s.lat == null).length,
    manual: stops.filter(s => s.coordSource === 'manual').length,
  };
  const coordSum = `共 ${coord.total} 站 · 缺坐标 ${coord.missing} · 已人工修正 ${coord.manual}`;

  $('#cuEditor').innerHTML = `
    <div class="ce-head">
      <span class="ce-code">${esc(d.code)}</span>
      <span class="ce-od">${esc(d.from || '—')} → ${esc(d.to || '—')}</span>
      <span class="ce-badge">${esc(d.date || '')}</span>
      ${isManual ? '<span class="ce-badge manual">已人工校正</span>' : ''}
      ${(a.viaUnknown || []).length ? '<span class="ce-badge warn">经由有缺口</span>' : ''}
      <span class="ce-spacer"></span>
      <button id="ceGoTrain" class="btn sm ghost">在车次详情中打开</button>
    </div>

    <div class="ce-actions">
      <button id="ceSave" class="btn primary">保存校正</button>
      <button id="ceReset" class="btn ghost">撤销校正</button>
      <span id="ceMsg" class="ce-msg"></span>
    </div>

    <div class="ce-card">
      <h4>自动推断的经由
        <span class="hint">${covText} · 总里程 ${a.totalKm} km（${kmModeCn}）· 线路里程配对 ${esc(a.kmMatched)}</span>
      </h4>
      <div class="via-chain">${cuChainHtml(a.viaSegs, a.viaUnknown)}</div>
    </div>

    <div class="ce-card">
      <h4>手工填写经由
        <span class="hint">按运行顺序；站名可留空，只写线路名也能保存</span>
        <span class="right">
          <button id="ceFill" class="btn sm ghost">按自动结果填充</button>
          <button id="ceClear" class="btn sm ghost">清空</button>
        </span>
      </h4>
      <div class="via-edit" id="ceViaRows"></div>
      <div class="ve-actions"><button id="ceAdd" class="btn sm">+ 添加一段</button></div>
      <div class="ve-note" id="ceViaCheck"></div>
    </div>

    <div class="ce-card">
      <h4>${CU.mode === 'cum' ? '累计里程' : '站间里程'}
        <span class="segmode" id="ceSegMode">
          <button data-mode="seg" class="${CU.mode === 'seg' ? 'on' : ''}" title="填相邻两站之间的公里数">按段距</button>
          <button data-mode="cum" class="${CU.mode === 'cum' ? 'on' : ''}" title="填自始发站算起的累计公里数">按累计</button>
        </span>
        <span class="hint">${CU.mode === 'cum'
          ? '从始发站算起的累计公里数；空白框里的灰字是当前值，右侧显示与自动累计的差'
          : '相邻两站之间的公里数；灰字是自动值，留空就沿用，右侧显示与自动值的差'}</span>
        <span class="right">
          <button id="ceSegClear" class="btn sm ghost">全部恢复自动</button>
        </span>
      </h4>
      <div class="seg-list" id="ceSegList"></div>
      <div class="ve-note seg-note" id="ceSegNote"></div>
    </div>

    <div class="ce-card">
      <h4>里程与备注</h4>
      <div class="ce-grid">
        <div class="ce-field">
          <label>总里程 km（可留空，自动 ${a.totalKm} km）</label>
          <input id="ceKm" type="number" step="0.1" min="1" max="20000"
                 value="${ov && ov.totalKm ? ov.totalKm : ''}" placeholder="${a.totalKm}">
        </div>
        <div class="ce-field">
          <label>备注</label>
          <input id="ceNote" value="${esc((ov && ov.note) || '')}" placeholder="比如：该段按 2026 年调图核对">
        </div>
      </div>
      <div class="ve-note">填了站间里程：手填段按手填值，其余段沿用自动值，各站累计实时重算；
        若同时又填了总里程，则只把「没手填的段」按比例缩放到 总里程 − 手填段之和。
        保存后车次详情、地图、时间轴同步生效。</div>
    </div>

    <div class="ce-card">
      <h4>车站坐标 <span class="hint" id="cuCoordSum">${coordSum}</span>
        <span class="right">
          <input id="cuCoordQ" class="cu-q" placeholder="筛选站名" value="${esc(CU.coordQ)}">
          <label class="cu-mini"><input type="checkbox" id="cuCoordOnlyMiss"${CU.coordOnlyMiss ? ' checked' : ''}> 只看缺坐标</label>
        </span>
      </h4>
      <div class="cu-coord-list" id="cuCoordList">${cuCoordRowsHtml()}</div>
      <div class="ve-note">经纬度按 <b>WGS-84</b>（与 OSM 一致）填写，页面会自行换算成腾讯地图坐标系。
        也可以直接把 <code class="inline">经度,纬度</code> 粘到任一格里（如
        <code class="inline">115.67135293489208,37.32526706163806</code>），会自动拆成两格并校验顺序。
        保存后地图定位、线路走向、里程插值立即重算；「还原」回到车站档案里的原值。</div>
    </div>

    <details class="ce-card st-card">
      <summary>经停站与里程（${stops.length} 站）</summary>
      <div class="st-list" id="cuStList"></div>
    </details>`;


  cuBindStations(d.stations);
  cuRenderViaRows();
  cuRenderSegKm();
  cuRenderStops();

  $('#ceGoTrain').addEventListener('click', () => {
    S.date = d.date || S.date;
    const di = $('#date'); if (di && d.date) di.value = d.date;
    S.staleCode = null;             // 下面自己会重拉，别让 switchTab 再触发一次
    switchTab('train');
    openTrain(d.code, d.date, { cacheOnly: true });
  });
  $('#ceAdd').addEventListener('click', () => {
    const last = CU.via[CU.via.length - 1];
    CU.via.push({ line: '', from: last ? last.to : (d.from || ''), to: '' });
    cuRenderViaRows();
  });
  $('#ceClear').addEventListener('click', () => { CU.via = []; cuRenderViaRows(); });
  $('#ceFill').addEventListener('click', () => {
    CU.via = cuRowsFromAuto(a);
    cuRenderViaRows();
  });
  $('#ceKm').addEventListener('input', () => {
    cuSegSummary();
    cuRenderStops();
    cuCheck();
  });
  $$('#ceSegMode button').forEach(b => b.addEventListener('click', () => {
    if (CU.mode === b.dataset.mode) return;
    CU.mode = b.dataset.mode;
    cuRenderEditor();
  }));
  $('#ceSegClear').addEventListener('click', () => {
    CU.segKm = {};
    cuRenderSegKm();
    cuRenderStops();
    cuCheck();
    cuMsg('', '站间里程已全部恢复自动值');
  });
  $('#ceSave').addEventListener('click', cuSave);
  $('#ceReset').addEventListener('click', cuReset);

  $('#cuCoordQ').addEventListener('input', e => {
    CU.coordQ = e.target.value;
    $('#cuCoordList').innerHTML = cuCoordRowsHtml();
    cuBindCoordRows();
  });
  $('#cuCoordOnlyMiss').addEventListener('change', e => {
    CU.coordOnlyMiss = e.target.checked;
    $('#cuCoordList').innerHTML = cuCoordRowsHtml();
    cuBindCoordRows();
  });
  cuBindCoordRows();
}

/* ---------- 车站坐标编辑 ---------- */
/** 「经度,纬度」成对输入：也接受中文逗号 / 分号 / 空格，并做顺序自检 */
function cuParsePair(text) {
  const s = String(text).trim();
  const m = s.match(/^([-+]?\d+(?:\.\d+)?)\s*[,，;；\s]\s*([-+]?\d+(?:\.\d+)?)$/);
  if (!m) return null;
  const a = Number(m[1]), b = Number(m[2]);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;

  const inLat = v => Math.abs(v) <= 90;
  const inLon = v => Math.abs(v) <= 180;
  let lon, lat;
  if (!inLat(b) && inLat(a)) {
    lat = a; lon = b;                       // 第二值超出 ±90，它只能是经度 → 粘的是「纬度,经度」
  } else if (!inLat(a) && inLat(b)) {
    lon = a; lat = b;                       // 第一值超出 ±90，它只能是经度 → 「经度,纬度」
  } else if (a >= 73 && a <= 136 && b >= 3 && b <= 54) {
    lon = a; lat = b;                       // 中国境内：先经度、后纬度
  } else if (b >= 73 && b <= 136 && a >= 3 && a <= 54) {
    lon = b; lat = a;                       // 反过来写的也认
  } else {
    lon = a; lat = b;                       // 兜底按「经度,纬度」
  }
  if (!inLon(lon) || !inLat(lat)) return null;
  return { lat, lon };
}

const fmtDeg = v => String(Number(Number(v).toFixed(6)));

function cuCoordRowHtml(s, i) {
  const src = s.coordSource === 'manual' ? 'manual' : (s.lat == null ? 'none' : 'base');
  const srcCn = { manual: '人工', base: '档案', none: '缺失' }[src];
  const lat = s.lat == null ? '' : fmtDeg(s.lat);
  const lon = s.lon == null ? '' : fmtDeg(s.lon);
  const tip = s.lat == null
    ? '档案里没有这个站的坐标'
    : `档案原值 ${fmtDeg(s.baseLat)}, ${fmtDeg(s.baseLon)}`;
  return `<div class="coord-row ${src}" data-nm="${esc(s.name)}" data-i="${i}">
    <span class="nm" title="${esc(s.name)}"><i>${i + 1}</i>${esc(s.name)}</span>
    <input class="lat" value="${lat}" placeholder="纬度" title="纬度 -90 ~ 90｜也可粘贴「经度,纬度」一次填两格">
    <input class="lon" value="${lon}" placeholder="经度" title="经度 -180 ~ 180｜也可粘贴「经度,纬度」一次填两格">
    <span class="src ${src}" title="${esc(tip)}">${srcCn}</span>
    <button class="btn sm coord-save">保存</button>
    <button class="btn sm ghost coord-clear"${s.coordSource === 'manual' ? '' : ' disabled'}>还原</button>
  </div>`;
}

function cuCoordRowsHtml() {
  const d = CU.detail;
  if (!d) return '';
  const q = (CU.coordQ || '').trim();
  let list = (d.stops || []).map((s, i) => ({ s, i }));
  if (CU.coordOnlyMiss) list = list.filter(x => x.s.lat == null);
  if (q) list = list.filter(x => x.s.name.includes(q));
  if (!list.length) {
    return `<div class="ve-note">${CU.coordOnlyMiss ? '这趟车所有车站都有坐标了。' : '没有匹配的车站。'}</div>`;
  }
  return list.map(x => cuCoordRowHtml(x.s, x.i)).join('');
}

function cuBindCoordRows() {
  $$('#cuCoordList .coord-save').forEach(b => b.addEventListener('click', () => {
    const row = b.closest('.coord-row');
    cuSaveCoord(row.dataset.nm, row.querySelector('.lat').value,
      row.querySelector('.lon').value, b);
  }));
  $$('#cuCoordList .coord-clear').forEach(b => b.addEventListener('click', () => {
    cuClearCoord(b.closest('.coord-row').dataset.nm);
  }));
  // 输完直接回车保存；任何一个格子里粘「经度,纬度」都自动拆成两格
  $$('#cuCoordList .coord-row input').forEach(inp => {
    inp.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      e.target.closest('.coord-row').querySelector('.coord-save').click();
    });
    inp.addEventListener('input', e => {
      const pair = cuParsePair(e.target.value);
      if (!pair) return;
      const row = e.target.closest('.coord-row');
      row.querySelector('.lat').value = fmtDeg(pair.lat);
      row.querySelector('.lon').value = fmtDeg(pair.lon);
      cuMsg('ok', `${row.dataset.nm}：已按「经度, 纬度」拆开 → 经度 ${fmtDeg(pair.lon)}、纬度 ${fmtDeg(pair.lat)}，按回车保存`);
    });
  });
}

/** 把一次坐标变动同步到编辑器各处（列表徽标 / 汇总 / 经停站表 / 左侧清单） */
function cuApplyCoord(name, r) {
  const d = CU.detail;
  if (!d) return;
  const st = (d.stops || []).find(s => s.name === name);
  if (st) {
    st.lat = r.lat;
    st.lon = r.lon;
    st.baseLat = r.baseLat;
    st.baseLon = r.baseLon;
    st.coordSource = r.source || (r.lat == null ? 'none' : 'manual');
  }
  const stops = d.stops || [];
  d.missingCoords = stops.filter(s => s.lat == null).map(s => s.name);
  d.coordSummary = {
    total: stops.length,
    missing: d.missingCoords.length,
    manual: stops.filter(s => s.coordSource === 'manual').length,
  };
  const sumEl = $('#cuCoordSum');
  if (sumEl) {
    sumEl.textContent = `共 ${d.coordSummary.total} 站 · 缺坐标 ${d.coordSummary.missing} · 已人工修正 ${d.coordSummary.manual}`;
  }
  const list = $('#cuCoordList');
  if (list) {
    // 只重画这一行，别把其它行里正在输入的内容冲掉
    const old = list.querySelector(`.coord-row[data-nm="${CSS.escape(name)}"]`);
    if (old && st) old.outerHTML = cuCoordRowHtml(st, Number(old.dataset.i));
    cuBindCoordRows();
  }
  const stList = $('#cuStList');
  if (stList) cuRenderStops();
  S.staleCode = d.code || null;     // 切回车次详情时重新拉一次，地图才会用新坐标
  cuLoad();
}

/* ---------- 线路下拉：可搜索、可键盘选，也允许填库外线路名 ---------- */
const CB = { box: null, items: [], idx: -1 };

function cbLineList(q) {
  const all = CU.lines || [];
  const v = (q || '').trim();
  if (!v) return all.slice(0, 500);
  const hits = all.filter(l => l.name.includes(v));
  hits.sort((a, b) => {
    const ap = a.name.startsWith(v) ? 0 : 1;
    const bp = b.name.startsWith(v) ? 0 : 1;
    return ap - bp || a.name.length - b.name.length;
  });
  return hits.slice(0, 200);
}

function cbClose() {
  if (CB.box) CB.box.classList.remove('open');
  CB.box = null;
  CB.items = [];
  CB.idx = -1;
}

function cbHilite(box) {
  const pop = box.querySelector('.cb-pop');
  if (!pop) return;
  $$('.cb-item', pop).forEach((el, k) => el.classList.toggle('active', k === CB.idx));
  const act = pop.querySelector('.cb-item.active');
  if (!act) return;
  // 只滚面板自己，别把整页带着动
  const top = act.offsetTop - pop.offsetTop;
  if (top < pop.scrollTop) pop.scrollTop = top - 4;
  else if (top + act.offsetHeight > pop.scrollTop + pop.clientHeight) {
    pop.scrollTop = top + act.offsetHeight - pop.clientHeight + 4;
  }
}

/** 重建下拉内容：有匹配就先列匹配，非精确匹配时把「用当前输入」放在最后一项 */
function cbPaintList(box, q) {
  const pop = box.querySelector('.cb-pop');
  if (!pop) return;
  const v = (q || '').trim();
  const list = cbLineList(v);
  const exact = list.some(l => l.name === v);
  const rows = list.slice();
  if (v && !exact) rows.push({ name: v, custom: true });
  CB.items = rows;
  // 命中只认库里的线路名；自定义项跟输入值同名，不能算「命中」
  const hit = list.findIndex(l => l.name === v);
  CB.idx = hit >= 0 ? hit : (rows.length ? 0 : -1);
  pop.innerHTML = rows.length
    ? rows.map((r, k) => {
      const mt = r.custom ? '用这个名称'
        : [r.n ? `${r.n} 站` : '', lnKmText(r.totalKm), r.kmManual ? '人工' : '']
          .filter(Boolean).join(' · ');
      return `<div class="cb-item${k === CB.idx ? ' active' : ''}${r.custom ? ' custom' : ''}"
        data-k="${k}"><span class="nm">${esc(r.name)}</span><span class="mt">${esc(mt)}</span></div>`;
    }).join('')
    : '<div class="cb-empty">线路库里没有匹配项 —— 直接输入的字就是自定义线路名</div>';
}

function cbOpen(box, i, all) {
  if (CB.box && CB.box !== box) cbClose();
  const inp = box.querySelector('input.line');
  box.classList.add('open');
  CB.box = box;
  cbPaintList(box, all ? '' : inp.value);
  if (all) {                                  // 看全部时仍把当前值标出来
    const k = CB.items.findIndex(r => r.name === inp.value.trim());
    if (k >= 0) { CB.idx = k; cbHilite(box); }
  }
  const pop = box.querySelector('.cb-pop');
  // 下面空间不够就往上翻
  const r = inp.getBoundingClientRect();
  const need = Math.min(pop.scrollHeight || 240, 246);
  pop.classList.toggle('up', r.bottom + need + 16 > window.innerHeight && r.top > need + 16);
}

function cbPick(box, i, name) {
  const inp = box.querySelector('input.line');
  inp.value = name;
  if (i != null && CU.via[i]) CU.via[i].line = name;
  cbClose();
  cuCheck();
}

function cuBindLineCombo(box, i) {
  const inp = box.querySelector('input.line');
  const pop = box.querySelector('.cb-pop');
  inp.addEventListener('focus', () => {
    inp.select();                              // 直接打字就是替换，不用先全删
    cbOpen(box, i);
  });
  inp.addEventListener('click', () => { if (CB.box !== box) cbOpen(box, i); });
  inp.addEventListener('input', () => {
    if (i != null && CU.via[i]) CU.via[i].line = inp.value;
    CB.box = box;
    cbPaintList(box, inp.value);
    box.classList.add('open');
    cuCheck();
  });
  inp.addEventListener('keydown', e => {
    if (e.isComposing || e.keyCode === 229) return;   // 输入法组合中的按键不拦截
    const open = box.classList.contains('open');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { cbOpen(box, i); return; }
      const n = CB.items.length;
      if (!n) return;
      CB.idx = (CB.idx + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
      cbHilite(box);
      return;
    }
    if (e.key === 'Enter') {
      if (open && CB.items[CB.idx]) { e.preventDefault(); cbPick(box, i, CB.items[CB.idx].name); }
      else cbClose();
      return;
    }
    if (e.key === 'Escape') { if (open) { e.preventDefault(); cbClose(); } return; }
    if (e.key === 'Tab') cbClose();
  });
  box.querySelector('.cb-toggle').addEventListener('mousedown', e => {
    e.preventDefault();                       // 别让输入框失焦
    if (box.classList.contains('open')) { cbClose(); return; }
    inp.focus();
    cbOpen(box, i, true);                     // ▾ 展开全部线路
  });
  pop.addEventListener('click', e => {
    const it = e.target.closest('.cb-item');
    if (!it) return;
    const r = CB.items[Number(it.dataset.k)];
    if (r) cbPick(box, i, r.name);
  });
}

document.addEventListener('pointerdown', e => {
  if (CB.box && !CB.box.contains(e.target)) cbClose();
}, true);

function cuRenderViaRows() {
  const box = $('#ceViaRows');
  if (!box) return;
  if (!CU.via.length) {
    box.innerHTML = '<div class="ve-note">还没有填写。点「+ 添加一段」，或「按自动结果填充」。</div>';
    cbClose();
    cuCheck();
    return;
  }
  box.innerHTML = CU.via.map((r, i) => `
    <div class="ve-row" data-i="${i}">
      <div class="combo">
        <input class="line" value="${esc(r.line)}" placeholder="点这里选线路，或直接输入"
               autocomplete="off" spellcheck="false">
        <button type="button" class="cb-toggle" tabindex="-1" title="展开线路列表">▾</button>
        <div class="cb-pop"></div>
      </div>
      <input class="from" list="cuStaList" value="${esc(r.from)}" placeholder="起始站">
      <input class="to" list="cuStaList" value="${esc(r.to)}" placeholder="终止站">
      <button class="del" title="删除这一段">×</button>
    </div>`).join('');

  $$('#ceViaRows .ve-row').forEach(row => {
    const i = Number(row.dataset.i);
    cuBindLineCombo(row.querySelector('.combo'), i);
    const fr = row.querySelector('input.from');
    fr.addEventListener('input', () => { CU.via[i].from = fr.value; cuCheck(); });
    const to = row.querySelector('input.to');
    to.addEventListener('input', () => { CU.via[i].to = to.value; cuCheck(); });
    row.querySelector('.del').addEventListener('click', () => {
      CU.via.splice(i, 1);
      cuRenderViaRows();
    });
  });
  cbClose();
  cuCheck();
}

/* ---------- 站间里程：手填段 + 自动段 → 各站累计（规则与后端一致） ---------- */
/** 编辑器基线里的自动相邻段里程 */
function cuAutoDiffs() {
  const stops = (CU.detail && CU.detail.stops) || [];
  const out = [];
  for (let i = 0; i + 1 < stops.length; i++) {
    out.push(Math.max(0, Number(stops[i + 1].km || 0) - Number(stops[i].km || 0)));
  }
  return out;
}

function cuEffectiveKm() {
  const auto = cuAutoDiffs();
  const diffs = auto.slice();
  const fixed = {};
  Object.keys(CU.segKm || {}).forEach(k => {
    const i = Number(k), v = Number(CU.segKm[k]);
    if (i >= 0 && i < diffs.length && v > 0) { fixed[i] = v; diffs[i] = v; }
  });
  const kmEl = $('#ceKm');
  const totalRaw = kmEl && kmEl.value.trim() !== '' ? Number(kmEl.value) : null;
  const free = diffs.map((_, i) => i).filter(i => !(i in fixed));
  if (totalRaw && totalRaw > 0 && free.length) {
    const base = free.reduce((s, i) => s + auto[i], 0);
    const target = totalRaw - Object.keys(fixed).reduce((s, k) => s + fixed[k], 0);
    if (base > 0 && target > 0) {
      const ratio = target / base;
      free.forEach(i => { diffs[i] *= ratio; });
    }
  }
  const cum = [0];
  for (let i = 0; i < diffs.length; i++) cum.push(cum[i] + diffs[i]);
  return { diffs, cum, auto, fixed, total: cum[cum.length - 1] || 0 };
}

function cuRenderSegKm() {
  const box = $('#ceSegList');
  const d = CU.detail;
  if (!box || !d) return;
  const stops = d.stops || [];
  const auto = cuAutoDiffs();
  if (stops.length < 2) {
    box.innerHTML = '<div class="ve-note">这趟车只有一站，没有站间里程可填。</div>';
    cuSegSummary();
    return;
  }
  if (CU.mode === 'cum') return cuRenderSegCum();

  box.innerHTML = stops.slice(0, -1).map((s, i) => {
    const a = auto[i] || 0;
    const v = CU.segKm[i];
    const has = v != null;
    const delta = has ? v - a : null;
    const dtxt = delta == null ? '' : (Math.abs(delta) < 0.05 ? '=' : (delta > 0 ? '+' : '') + delta.toFixed(1));
    return `<div class="seg-row${has ? ' manual' : ''}" data-i="${i}">
      <span class="i">${i + 1}</span>
      <span class="od" title="${esc(s.name)} → ${esc(stops[i + 1].name)}">${esc(s.name)}<em>→</em>${esc(stops[i + 1].name)}</span>
      <input class="km" type="number" step="0.1" min="0" max="5000" inputmode="decimal"
             value="${has ? v : ''}" placeholder="${a.toFixed(1)}"
             title="留空沿用自动值 ${a.toFixed(1)} km">
      <span class="u">km</span>
      <button class="seg-clr" title="恢复这一段的自动值"${has ? '' : ' disabled'}>↺</button>
      <span class="auto" title="与自动值的差">${dtxt}</span>
    </div>`;
  }).join('');

  $$('#ceSegList .seg-row').forEach(row => {
    const i = Number(row.dataset.i);
    const inp = row.querySelector('input.km');
    const clr = row.querySelector('.seg-clr');
    const apply = () => {
      const raw = inp.value.trim();
      const n = Number(raw);
      if (raw === '' || !Number.isFinite(n) || n <= 0) delete CU.segKm[i];
      else CU.segKm[i] = n;
      const on = Object.prototype.hasOwnProperty.call(CU.segKm, i);
      row.classList.toggle('manual', on);
      clr.disabled = !on;
      // 最右列：与自动值的差（placeholder 就是自动值）
      const dEl = row.querySelector('.auto');
      if (dEl) {
        const a = Number(inp.placeholder) || 0;
        const dd = on ? CU.segKm[i] - a : null;
        dEl.textContent = dd == null ? '' : (Math.abs(dd) < 0.05 ? '=' : (dd > 0 ? '+' : '') + dd.toFixed(1));
      }
      cuSegSummary();
      cuRenderStops();
      cuCheck();
    };
    inp.addEventListener('input', apply);
    inp.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const next = row.nextElementSibling;
      if (next) next.querySelector('input.km').focus();
      else inp.blur();
    });
    clr.addEventListener('click', () => {
      inp.value = '';
      row.classList.remove('manual');
      delete CU.segKm[i];
      clr.disabled = true;
      const dEl = row.querySelector('.auto');
      if (dEl) dEl.textContent = '';
      cuSegSummary();
      cuRenderStops();
      cuCheck();
    });
  });
  cuSegSummary();
}

/** 累计模式：填「从始发站到这一站」的累计公里数，内部反算成段距 */
function cuRenderSegCum() {
  const box = $('#ceSegList');
  const stops = (CU.detail && CU.detail.stops) || [];
  const eff = cuEffectiveKm();
  const first = stops.length ? stops[0].name : '';
  box.innerHTML = stops.slice(1).map((s, k) => {
    const i = k + 1;
    const has = CU.segKm[i - 1] != null;
    const cur = eff.cum[i];
    const base = Number(s.km || 0);
    const dd = Math.round((cur - base) * 10) / 10;
    const dtxt = Math.abs(dd) < 0.05 ? '=' : (dd > 0 ? '+' : '') + dd.toFixed(1);
    return `<div class="seg-row cum${has ? ' manual' : ''}" data-i="${i}">
      <span class="i">${i + 1}</span>
      <span class="od" title="自 ${esc(first)} 起算">${esc(s.name)}</span>
      <input class="km" type="number" step="0.1" min="0" max="20000" inputmode="decimal"
             value="${has ? cur.toFixed(1) : ''}" placeholder="${cur.toFixed(1)}"
             title="留空沿用当前累计 ${cur.toFixed(1)} km">
      <span class="u">km</span>
      <button class="seg-clr" title="恢复自动值"${has ? '' : ' disabled'}>↺</button>
      <span class="auto" title="与自动累计的差">${dtxt}</span>
    </div>`;
  }).join('');

  $$('#ceSegList .seg-row').forEach(row => {
    const i = Number(row.dataset.i);
    const inp = row.querySelector('input.km');
    const clr = row.querySelector('.seg-clr');
    const apply = () => {
      const raw = inp.value.trim();
      const c = Number(raw);
      if (raw === '' || !Number.isFinite(c) || c <= 0) {
        delete CU.segKm[i - 1];
        row.classList.remove('manual', 'bad');
        clr.disabled = true;
      } else {
        const prev = cuEffectiveKm().cum[i - 1];
        const dv = Math.round((c - prev) * 10) / 10;
        if (dv <= 0) {
          row.classList.add('bad');
          cuMsg('bad', `第 ${i + 1} 站的累计要大于前一站的 ${prev.toFixed(1)} km`);
          return;
        }
        row.classList.remove('bad');
        row.classList.add('manual');
        clr.disabled = false;
        CU.segKm[i - 1] = dv;
      }
      cuRefreshCumRows();
      cuSegSummary();
      cuRenderStops();
      cuCheck();
    };
    inp.addEventListener('input', apply);
    inp.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const next = row.nextElementSibling;
      if (next) next.querySelector('input.km').focus(); else inp.blur();
    });
    clr.addEventListener('click', () => {
      delete CU.segKm[i - 1];
      cuRenderSegKm();
      cuRenderStops();
      cuCheck();
    });
  });
  cuSegSummary();
}

/** 累计模式刷新数值（不重建输入框，避免丢焦点） */
function cuRefreshCumRows() {
  const stops = (CU.detail && CU.detail.stops) || [];
  const eff = cuEffectiveKm();
  $$('#ceSegList .seg-row.cum').forEach(row => {
    const i = Number(row.dataset.i);
    const inp = row.querySelector('input.km');
    const has = CU.segKm[i - 1] != null;
    const cur = eff.cum[i];
    if (document.activeElement !== inp) {
      inp.value = has ? cur.toFixed(1) : '';
      inp.placeholder = cur.toFixed(1);
    }
    const base = Number((stops[i] || {}).km || 0);
    const dd = Math.round((cur - base) * 10) / 10;
    const el = row.querySelector('.auto');
    if (el) el.textContent = Math.abs(dd) < 0.05 ? '=' : (dd > 0 ? '+' : '') + dd.toFixed(1);
  });
}

function cuSegSummary() {
  const el = $('#ceSegNote');
  if (!el) return;
  const d = CU.detail;
  if (!d || (d.stops || []).length < 2) { el.textContent = ''; return; }
  const eff = cuEffectiveKm();
  const autoSum = eff.auto.reduce((a, b) => a + b, 0);
  const keys = Object.keys(eff.fixed).map(Number).sort((a, b) => a - b);
  const fixedSum = keys.reduce((s, k) => s + eff.fixed[k], 0);
  const bits = [`合计 <b>${eff.total.toFixed(1)}</b> km`];
  if (keys.length) {
    bits.push(`手填 ${keys.length} 段 / ${fixedSum.toFixed(1)} km`);
    const dc = eff.total - autoSum;
    if (Math.abs(dc) > 0.5) bits.push(`比自动值 ${dc > 0 ? '+' : ''}${dc.toFixed(1)} km`);
  } else {
    bits.push('全部沿用自动值');
  }
  el.innerHTML = bits.join(' · ');
}

/** 经停站表：显示按当前填写算出来的累计里程 */
function cuRenderStops() {
  const box = $('#cuStList');
  const d = CU.detail;
  if (!box || !d) return;
  const stops = d.stops || [];
  const eff = cuEffectiveKm();
  box.innerHTML = stops.map((s, i) => {
    const km = i < eff.cum.length ? eff.cum[i] : Number(s.km || 0);
    const base = Number(s.km || 0);
    const changed = Math.abs(km - base) > 0.05;
    return `<div class="st-line${s.lat == null ? ' miss' : ''}">
      <span class="i">${i + 1}</span><span>${esc(s.name)}</span>
      <span class="k">${km.toFixed(1)} km${changed
        ? `<em class="chg" title="自动值 ${base.toFixed(1)} km">调整</em>` : ''}${s.lat == null ? ' · 缺坐标' : ''}</span></div>`;
  }).join('');
}

function cuCheck() {
  const d = CU.detail;
  const el = $('#ceViaCheck');
  if (!d || !el) return;
  const rows = CU.via.filter(r => (r.line || '').trim() || (r.from || '').trim() || (r.to || '').trim());
  const known = new Set(d.stations || []);
  const bad = [];
  const note = [];

  rows.forEach((r, i) => {
    if (!(r.line || '').trim()) bad.push(`第 ${i + 1} 段没填线路名`);
    [r.from, r.to].forEach(n => {
      const v = (n || '').trim();
      if (v && !known.has(v)) bad.push(`「${v}」不在这趟车的 ${known.size} 个经停站里`);
    });
  });
  for (let i = 1; i < rows.length; i++) {
    const prev = (rows[i - 1].to || '').trim();
    const cur = (rows[i].from || '').trim();
    if (prev && cur && prev !== cur) bad.push(`第 ${i} 段到「${prev}」，第 ${i + 1} 段却从「${cur}」起，中间断开`);
  }
  const first = (rows[0] || {}).from || '';
  const last = (rows[rows.length - 1] || {}).to || '';
  if (first && d.from && first.trim() !== d.from) note.push(`首段起点「${first}」≠ 始发站「${d.from}」`);
  if (last && d.to && last.trim() !== d.to) note.push(`末段终点「${last}」≠ 终到站「${d.to}」`);

  const kmEl = $('#ceKm');
  if (kmEl && kmEl.value.trim()) {
    const km = Number(kmEl.value);
    const auto = Number((d.auto || {}).totalKm || 0);
    if (Number.isFinite(km) && auto > 0 && Math.abs(km - auto) / auto > 0.25) {
      note.push(`比自动值 ${auto} km 差 ${Math.round((km - auto) / auto * 100)}%，确认无误再存`);
    }
  }

  // 手填站间里程：与自动段值差太远的，提醒核对（不影响保存）
  const segAuto = cuAutoDiffs();
  Object.keys(CU.segKm || {}).forEach(k => {
    const i = Number(k), v = Number(CU.segKm[k]);
    const a = segAuto[i] || 0;
    if (a > 0 && v > 0 && (v > a * 3 || v < a / 3)) {
      note.push(`第 ${i + 1} 段手填 ${v} km（自动 ${a.toFixed(1)} km），差得比较多，确认一下`);
    }
  });

  $$('#ceViaRows .ve-row').forEach((row, i) => {
    const r = rows[i];
    if (!r) return;
    row.classList.toggle('invalid', !(r.line || '').trim());
  });

  el.innerHTML = bad.length
    ? bad.map(m => `<div class="bad">· ${esc(m)}</div>`).join('')
      + (note.length ? note.map(m => `<div>· ${esc(m)}</div>`).join('') : '')
    : (rows.length
      ? (note.length ? note.map(m => `<div>· ${esc(m)}</div>`).join('') : '<div class="good">✓ 区间首尾相接，站名都在经停表里</div>')
      : '');
}

function cuMsg(cls, text) {
  const el = $('#ceMsg');
  if (!el) return;
  el.className = 'ce-msg ' + (cls || '');
  el.textContent = text || '';
}

async function cuSave() {
  const d = CU.detail;
  if (!d) return;
  const via = CU.via
    .map(r => ({ line: (r.line || '').trim(), from: (r.from || '').trim(), to: (r.to || '').trim() }))
    .filter(r => r.line);
  const kmRaw = ($('#ceKm').value || '').trim();
  const note = ($('#ceNote').value || '').trim();
  const segKm = {};
  Object.keys(CU.segKm || {}).forEach(k => {
    const v = Number(CU.segKm[k]);
    if (Number.isFinite(v) && v > 0) segKm[k] = v;
  });
  cuMsg('', '保存中…');
  try {
    await postJSON('/api/override', {
      code: d.code, via, totalKm: kmRaw ? Number(kmRaw) : null, note, segKm,
    });
    await cuOpen(d.code);
    cuMsg('ok', '已保存 · 车次详情和地图立即生效');
    cuLoad();
  } catch (e) {
    cuMsg('bad', '保存失败：' + e.message);
  }
}

async function cuReset() {
  const d = CU.detail;
  if (!d) return;
  if (!confirm(`撤销 ${d.code} 的全部人工校正，回到自动推断结果？`)) return;
  cuMsg('', '撤销中…');
  try {
    await postJSON('/api/override', { code: d.code, via: [], totalKm: null, note: '' });
    await cuOpen(d.code);
    cuMsg('ok', '已撤销，恢复自动结果');
    cuLoad();
  } catch (e) {
    cuMsg('bad', '撤销失败：' + e.message);
  }
}

async function cuSaveCoord(name, latv, lonv, btn) {
  const lat = Number(latv), lon = Number(lonv);
  if (String(latv).trim() === '' || String(lonv).trim() === ''
      || !Number.isFinite(lat) || !Number.isFinite(lon)) {
    cuMsg('bad', `${name}：纬度和经度都要填数字`);
    btn.classList.add('shake');
    setTimeout(() => btn.classList.remove('shake'), 400);
    return;
  }
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    cuMsg('bad', `${name}：纬度要在 -90 ~ 90、经度要在 -180 ~ 180 之间`);
    return;
  }
  if (lat === 0 && lon === 0) {
    cuMsg('bad', `${name}：(0, 0) 多半是填错了，请核对`);
    return;
  }
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const r = await postJSON('/api/coord', { name, lat, lon });
    cuApplyCoord(name, r);
    cuMsg('ok', `${name} 坐标已保存为 ${fmtDeg(r.lat)}, ${fmtDeg(r.lon)}${r.refreshed ? `（重算 ${r.refreshed} 趟车）` : ''}`);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '保存';
    cuMsg('bad', '坐标保存失败：' + e.message);
  }
}

async function cuClearCoord(name) {
  if (!confirm(`把「${name}」的坐标还原成车站档案里的原值？`)) return;
  const btn = document.querySelector(`#cuCoordList .coord-row[data-nm="${CSS.escape(name)}"] .coord-clear`);
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await postJSON('/api/coord/clear', { name });
    cuApplyCoord(name, r);
    cuMsg('ok', r.source === 'none'
      ? `${name} 已还原（车站档案里也没有这个站的坐标）`
      : `${name} 已还原为档案值 ${fmtDeg(r.lat)}, ${fmtDeg(r.lon)}`);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '还原'; }
    cuMsg('bad', '还原失败：' + e.message);
  }
}

cuWire();

boot();
