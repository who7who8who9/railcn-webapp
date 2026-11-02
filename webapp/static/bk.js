/* 备份线路：左列表 + 右预览，挑中了才写进线路库 */
'use strict';

const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const kmText = v => (v == null ? '-' : `${(+v).toFixed(1).replace(/\.0$/, '')} km`);

const ST = {
  tag: '', source: '', q: '', offset: 0, limit: 200, total: 0,
  items: [], counts: {}, sel: null, busy: false,
};

async function api(path, body) {
  const opt = body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : {};
  const res = await fetch(path, opt);
  const txt = await res.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function toast(msg, ms = 3200) {
  const t = $('#bkToast');
  t.innerHTML = msg;
  t.classList.remove('hidden');
  clearTimeout(toast.tm);
  toast.tm = setTimeout(() => t.classList.add('hidden'), ms);
}

/* ---------------- 列表 ---------------- */

async function loadBatches() {
  try {
    const d = await api('/api/backup/lines?limit=1');
    const sel = $('#bkTag');
    sel.innerHTML = '<option value="">全部批次</option>' + (d.batches || []).map(b =>
      `<option value="${esc(b.tag)}">${esc(b.tag)} · ${esc(b.takenAt)} · ${b.n} 条</option>`).join('');
    sel.value = ST.tag;
  } catch (e) { toast('读备份目录失败：' + esc(e.message)); }
}

function chips() {
  const box = $('#bkFilters');
  const all = Object.values(ST.counts || {}).reduce((a, b) => a + b, 0);
  const list = [['', '全部', all]].concat(
    Object.entries(ST.counts || {}).map(([k, v]) => [k, (ST.sources || {})[k] || k, v]));
  box.innerHTML = list.map(([k, label, n]) =>
    `<button class="chip${ST.source === k ? ' on' : ''}" data-src="${esc(k)}">${esc(label)}<span class="n">${n}</span></button>`
  ).join('');
}

function renderList() {
  const box = $('#bkList');
  if (!ST.items.length) {
    box.innerHTML = '<div class="bk-count">没有符合条件的线路</div>';
    return;
  }
  box.innerHTML = ST.items.map((it, i) => `
    <div class="bk-item${ST.sel && ST.sel.tag === it.tag && ST.sel.source === it.source && ST.sel.name === it.name ? ' on' : ''}"
         data-i="${i}">
      <div class="r1">
        <span class="nm">${esc(it.name)}</span>
        <span class="tag">${esc(it.srcCn)}</span>
        ${it.inLib ? '<span class="tag inlib">已在库</span>' : ''}
      </div>
      <div class="r2">
        <span>${it.n} 站</span><span>${kmText(it.km)}</span>
        <span>${esc(it.first)} → ${esc(it.last)}</span>
      </div>
    </div>`).join('');
  $('#bkCount').textContent = `共 ${ST.total} 条，已列 ${ST.items.length} 条`;
  $('#bkMore').classList.toggle('hidden', ST.items.length >= ST.total);
}

async function loadList(reset = true) {
  if (ST.busy) return;
  ST.busy = true;
  try {
    if (reset) { ST.offset = 0; ST.items = []; }
    const qs = new URLSearchParams({
      tag: ST.tag, source: ST.source, q: ST.q,
      limit: String(ST.limit), offset: String(ST.offset),
    });
    const d = await api('/api/backup/lines?' + qs.toString());
    ST.total = d.total;
    ST.counts = d.counts || {};
    ST.sources = d.sources || {};
    ST.items = reset ? d.items : ST.items.concat(d.items);
    chips();
    renderList();
    $('#bkMore').classList.toggle('hidden', ST.items.length >= ST.total);
  } catch (e) {
    toast('读清单失败：' + esc(e.message));
  } finally {
    ST.busy = false;
  }
}

/* ---------------- 预览 ---------------- */

function drawMap(parts, label) {
  // 分段保留：多段走向不能连成一条路径，否则段与段之间会多出直线（看着像锯齿）
  const segs = [];
  for (const seg of parts || []) {
    const pts = (seg || []).filter(p => Array.isArray(p) && p.length >= 2).map(p => [+p[1], +p[0]]);
    if (pts.length >= 2) segs.push(pts);
  }
  if (!segs.length) return '<div class="nogeom">走向没有点</div>';
  let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
  for (const seg of segs) for (const [la, lo] of seg) {
    minLat = Math.min(minLat, la); maxLat = Math.max(maxLat, la);
    minLon = Math.min(minLon, lo); maxLon = Math.max(maxLon, lo);
  }
  const W = 1000, H = 620, pad = 34;
  const mid = (minLat + maxLat) / 2;
  const cos = Math.cos(mid * Math.PI / 180);
  const spanX = Math.max(1e-6, (maxLon - minLon) * cos);
  const spanY = Math.max(1e-6, maxLat - minLat);
  const k = Math.min((W - pad * 2) / spanX, (H - pad * 2) / spanY);
  const ox = (W - spanX * k) / 2, oy = (H - spanY * k) / 2;
  const px = lo => ox + (lo - minLon) * cos * k;
  const py = la => H - (oy + (la - minLat) * k);
  const paths = segs.map(seg => seg.map(([la, lo], i) =>
    `${i ? 'L' : 'M'}${px(lo).toFixed(1)} ${py(la).toFixed(1)}`).join(''));
  const a = segs[0][0], b = segs[segs.length - 1][segs[segs.length - 1].length - 1];
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${paths.map(d => `<path d="${d}" fill="none" stroke="#ffffff" stroke-width="12"
        stroke-linejoin="round" stroke-linecap="round"/>`).join('')}
    ${paths.map(d => `<path d="${d}" fill="none" stroke="#1f6feb" stroke-width="5"
        stroke-linejoin="round" stroke-linecap="round"/>`).join('')}
    <circle cx="${px(a[1]).toFixed(1)}" cy="${py(a[0]).toFixed(1)}" r="9" fill="#0f9d58" stroke="#fff" stroke-width="3"/>
    <circle cx="${px(b[1]).toFixed(1)}" cy="${py(b[0]).toFixed(1)}" r="9" fill="#e8590c" stroke="#fff" stroke-width="3"/>
    <text x="${(px(a[1]) + 14).toFixed(1)}" y="${(py(a[0]) + 6).toFixed(1)}" font-size="20" fill="#0f9d58">首端</text>
    <text x="${(px(b[1]) + 14).toFixed(1)}" y="${(py(b[0]) + 6).toFixed(1)}" font-size="20" fill="#e8590c">尾端</text>
    <text x="16" y="${H - 14}" font-size="17" fill="#77839a">${esc(label || '')}
      ${segs.length > 1 ? ` · ${segs.length} 段` : ''}</text>
  </svg>`;
}

async function openLine(tag, source, name) {
  ST.sel = { tag, source, name };
  renderList();
  $('#bkEmpty').classList.add('hidden');
  const det = $('#bkDetail');
  det.classList.remove('hidden');
  det.innerHTML = '<div class="bk-count">读取站序 …</div>';
  let d;
  try {
    const qs = new URLSearchParams({ tag, source, name });
    d = await api('/api/backup/line?' + qs.toString());
  } catch (e) {
    det.innerHTML = `<div class="warnbox">读不出来：${esc(e.message)}</div>`;
    return;
  }

  // 走向：备份里有就用，没有就试着从 OSM 借一份（只是预览，不入库）
  let parts = [], from = '';
  if (d.geom) {
    try {
      const g = await api('/api/backup/geom?' + new URLSearchParams({ tag, name }).toString());
      parts = g.parts || []; from = `备份里的走向 · ${d.geom.file || ''}`;
    } catch (e) { /* 忽略 */ }
  }
  if (!parts.length) {
    try {
      const g = await api('/api/line/osm?' + new URLSearchParams({ q: name }).toString());
      if (g && g.parts && g.parts.length) { parts = g.parts; from = `走向借自 OSM（备份里没存走向）· ${g.name}`; }
    } catch (e) { /* 没有就没有 */ }
  }

  const stops = d.stops || [];
  det.innerHTML = `
    <div class="bk-head">
      <div>
        <h2>${esc(d.name)}</h2>
        <div class="meta">
          <span class="tag">${esc(d.srcCn)}</span>
          <span>${d.n} 站</span>
          <span>${kmText(d.km)}</span>
          <span>${esc(d.first)} → ${esc(d.last)}</span>
          <span>备份 ${esc(d.tag)}</span>
          ${d.inLib ? '<span class="tag inlib">线路库里已有</span>' : ''}
        </div>
        <div class="bk-hint">看好了再点右边按钮 —— 不点，线路库不会变。</div>
      </div>
      <div class="act">
        <button id="bkDo" class="btn primary">${d.inLib ? '覆盖库里的这条' : '导入到线路库'}</button>
      </div>
    </div>

    <div class="card">
      <h3>走向${from ? ` · <span style="font-weight:400;color:#77839a">${esc(from)}</span>` : ''}</h3>
      <div class="bk-map">${parts.length ? drawMap(parts, d.name) : '<div class="nogeom">备份里只有站序、没有走向数据<br>（这类线导入后地图上按站点直连画）</div>'}</div>
    </div>

    <div class="card">
      <h3>站序 · ${stops.length} 站</h3>
      <div class="stops-wrap">
        <table class="stops">
          <thead><tr><th class="seq">#</th><th>车站</th><th class="km">累计里程</th></tr></thead>
          <tbody>${stops.map((s, i) => `<tr>
            <td class="seq">${i + 1}</td><td>${esc(s.name)}</td>
            <td class="km">${s.km == null ? '-' : kmText(s.km)}</td></tr>`).join('')}</tbody>
        </table>
      </div>
      ${d.note ? `<div class="bk-hint">备份里的备注：${esc(d.note)}</div>` : ''}
    </div>`;

  $('#bkDo').addEventListener('click', () => doRestore(d));
}

async function doRestore(d) {
  const replace = !!d.inLib;
  if (replace && !confirm(`线路库里已经有「${d.name}」。\n用备份里这份（${d.n} 站）覆盖它吗？`)) return;
  const btn = $('#bkDo');
  btn.disabled = true;
  btn.textContent = '导入中…';
  try {
    const r = await api('/api/backup/restore', {
      tag: d.tag, source: d.source, name: d.name, replace,
    });
    toast(`已导入「${esc(r.name)}」：${r.n} 站 / ${kmText(r.km)}`
      + (r.geom ? ' · 走向一起导回来了' : ' · 备份里没走向，地图按站点直连'));
    // 列表里的「已在库」标一下
    for (const it of ST.items) {
      if (it.name === r.name) it.inLib = true;
    }
    renderList();
    d.inLib = true;
    btn.textContent = '覆盖库里的这条';
    openLine(d.tag, d.source, d.name);
  } catch (e) {
    toast('导入失败：' + esc(e.message));
    btn.disabled = false;
    btn.textContent = replace ? '覆盖库里的这条' : '导入到线路库';
  }
}

/* ---------------- 事件 ---------------- */

$('#bkGo').addEventListener('click', () => { ST.q = $('#bkQ').value.trim(); loadList(); });
$('#bkQ').addEventListener('keydown', e => {
  if (e.key === 'Enter') { ST.q = $('#bkQ').value.trim(); loadList(); }
});
$('#bkTag').addEventListener('change', e => {
  ST.tag = e.target.value; ST.sel = null;
  $('#bkDetail').classList.add('hidden'); $('#bkEmpty').classList.remove('hidden');
  loadList();
});
$('#bkFilters').addEventListener('click', e => {
  const b = e.target.closest('.chip');
  if (!b) return;
  ST.source = b.dataset.src || '';
  loadList();
});
$('#bkList').addEventListener('click', e => {
  const it = e.target.closest('.bk-item');
  if (!it) return;
  const rec = ST.items[+it.dataset.i];
  if (!rec) return;
  openLine(rec.tag, rec.source, rec.name);
});
$('#bkMore').addEventListener('click', () => {
  ST.offset = ST.items.length;
  loadList(false);
});

(async () => {
  await loadBatches();
  await loadList();
})();
