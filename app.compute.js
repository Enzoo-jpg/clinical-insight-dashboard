/* =========================================================================
 * 临床用药洞察看板 · 计算引擎 (app.compute.js) v2
 * 纯函数，无 DOM 依赖。浏览器 <script src> 与 Node require 双兼容。
 * 口径: 销量用"转化后数量"直接计(不再做支/盒换算); 适应症按关键字归并。
 * ========================================================================= */

/* 日期 -> YYYY-MM (兼容 Date对象 / 'YYYY-MM-DD' / 'YYYY/MM/DD HH:MM:SS') */
function toYM(v) {
  if (v == null) return '';
  if (v instanceof Date) return v.getFullYear() + '-' + String(v.getMonth() + 1).padStart(2, '0');
  const s = String(v);
  const m = s.match(/(\d{4})[-/](\d{1,2})/);
  if (m) return m[1] + '-' + String(+m[2]).padStart(2, '0');
  return s.slice(0, 7);
}
/* 销量直接取数(用户确认: 用转化后数量, 不做单位换算); 容错清洗逗号/空格 */
function toQty(qty) { if (qty == null) return 0; const n = Number(String(qty).replace(/[,\s]/g, '')); return isNaN(n) ? 0 : n; }

function inWindow(ym, startYM, endYM) {
  if (!ym) return false;
  if (startYM && ym < startYM) return false;
  if (endYM && ym > endYM) return false;
  return true;
}
function filterByWindow(rows, startYM, endYM) {
  return rows.filter(r => inWindow(toYM(r.销售日期 != null ? r.销售日期 : r.ym), startYM, endYM));
}
function dedupeKey(row, fields) {
  if (!fields || !fields.length) return String(row.oneid != null ? row.oneid : '');
  return fields.map(f => row[f] == null ? '' : String(row[f])).join('||');
}
function aggregateByDoctor(rows, cfg) {
  const map = new Map();
  for (const r of rows) {
    const key = `${r.医疗单位}|${r.处方科室}|${r.处方医生}`;
    if (!map.has(key)) map.set(key, { hospital: r.医疗单位, dept: r.处方科室, doctor: r.处方医生, boxes: 0, patients: new Set() });
    const agg = map.get(key);
    agg.boxes += toQty(r.销量数量);
    agg.patients.add(dedupeKey(r, cfg.dedupFields));
  }
  const out = [];
  for (const agg of map.values()) {
    const pn = agg.patients.size || 1;
    out.push({ hospital: agg.hospital, dept: agg.dept, doctor: agg.doctor, boxes: +agg.boxes.toFixed(2), patients: agg.patients.size, dot: +(agg.boxes / pn).toFixed(2) });
  }
  return out;
}
function percentile(values, p) {
  const s = [...values].sort((a, b) => a - b);
  if (!s.length) return 0;
  const idx = Math.min(s.length - 1, Math.floor((s.length - 1) * p));
  return s[idx];
}
function classifyQuadrant(doctors, topPct) {
  topPct = topPct || 0.3;
  const pTh = percentile(doctors.map(d => d.patients), 1 - topPct);
  const dTh = percentile(doctors.map(d => d.dot), 1 - topPct);
  for (const d of doctors) {
    const hiP = d.patients >= pTh, hiD = d.dot >= dTh;
    d.highPatients = hiP; d.highDot = hiD;
    d.quadrant = (hiP && hiD) ? '意见领袖' : (hiP && !hiD) ? '学术支持对象' : (!hiP && hiD) ? '潜力医生' : '待评估';
  }
  return doctors;
}
/* 适应症匹配: 支持多选(cfg.indications 数组) 与旧单选(cfg.indication); 均空则视为全部 */
function indMatches(val, cfg) {
  const list = (cfg.indications && cfg.indications.length) ? cfg.indications
    : (cfg.indication ? [cfg.indication] : null);
  if (!list) return true;
  return list.includes(val);
}
function marketAggregate(rows, cfg, includeOwn) {
  const own = cfg.ownProduct;
  const competitors = cfg.competitors || [];
  const dept = cfg.dept || null;
  const map = new Map();
  for (const r of rows) {
    if (!indMatches(r.适应症, cfg)) continue;
    if (dept && r.处方科室 !== dept) continue;
    const isOwn = r.通用名 === own;
    if (isOwn) { if (!includeOwn) continue; }
    else if (competitors.length && !competitors.includes(r.通用名)) continue;
    const key = `${r.医疗单位}|${r.处方科室}|${r.处方医生}`;
    if (!map.has(key)) map.set(key, { hospital: r.医疗单位, dept: r.处方科室, doctor: r.处方医生, boxes: 0, patients: new Set() });
    const a = map.get(key);
    a.boxes += toQty(r.销量数量);
    a.patients.add(dedupeKey(r, cfg.dedupFields));
  }
  const out = [];
  for (const a of map.values()) out.push({ hospital: a.hospital, dept: a.dept, doctor: a.doctor, boxes: +a.boxes.toFixed(2), patients: a.patients.size });
  return out;
}
function mergeTrend(curr, prev) {
  const pmap = new Map(prev.map(d => [d.hospital + '|' + d.dept + '|' + d.doctor, d]));
  const seen = new Set();
  const out = [];
  for (const c of curr) {
    const k = c.hospital + '|' + c.dept + '|' + c.doctor; seen.add(k);
    const p = pmap.get(k); const pBox = p ? p.boxes : 0; const dBox = c.boxes - pBox;
    let trend = '持平';
    if (!p && c.boxes > 0) trend = '新进';
    else if (pBox > 0 && c.boxes === 0) trend = '流失';
    else if (dBox > 0) trend = '上升';
    else if (dBox < 0) trend = '下降';
    out.push({ ...c, prevBoxes: pBox, delta: +dBox.toFixed(2), trend });
  }
  for (const p of prev) {
    const k = p.hospital + '|' + p.dept + '|' + p.doctor;
    if (!seen.has(k)) out.push({ ...p, prevBoxes: p.boxes, delta: -p.boxes, trend: '流失' });
  }
  out.sort((a, b) => b.boxes - a.boxes);
  return out;
}
function summarizeMarket(trendRows, includeOwn, competitors) {
  const up = trendRows.filter(r => r.trend === '上升').length;
  const down = trendRows.filter(r => r.trend === '下降').length;
  const neu = trendRows.filter(r => r.trend === '新进').length;
  const lost = trendRows.filter(r => r.trend === '流失').length;
  const compLabel = competitors && competitors.length ? competitors.join(' / ') : '全部竞品';
  const scope = includeOwn ? `同适应症整体(本品 + ${compLabel})` : `同适应症竞品机会市场(未含本品; 竞品=${compLabel})`;
  return `【${scope}】共 ${trendRows.length} 位医生: 上升 ${up} / 下降 ${down} / 新进 ${neu} / 流失 ${lost}。` +
    (includeOwn ? '本品需关注下降与新进乏力者。' : '未覆盖本品的医生即为可拓展机会, 优先跟进上升且本品缺席者。');
}
function maskName(name) {
  if (!name) return name; const s = String(name); if (s.length <= 1) return s;
  return s[0] + '*'.repeat(Math.min(s.length - 1, 6));
}
function applyMask(rows, on) {
  if (!on) return rows;
  return rows.map(r => ({ ...r, hospital: r.hospital ? maskName(r.hospital) : r.hospital, dept: r.dept ? maskName(r.dept) : r.dept, doctor: r.doctor ? maskName(r.doctor) : r.doctor }));
}
/* 适应症归并: 每行 `关键字=标准名`, # 注释, 含关键字即归并 */
function parseIndMap(text) {
  const map = [];
  (text || '').split('\n').forEach(line => {
    line = line.trim();
    if (!line || line.startsWith('#')) return;
    const i = line.indexOf('=');
    if (i < 0) return;
    map.push([line.slice(0, i).trim(), line.slice(i + 1).trim()]);
  });
  return map;
}
function normalizeIndication(ind, map) {
  if (ind == null || ind === '') return '(未填写)';
  const s = String(ind).toLowerCase();
  for (const [kw, std] of map) if (kw && s.includes(kw.toLowerCase())) return std;
  return String(ind);
}
function parseDeptMap(text) {
  const map = [];
  (text || '').split('\n').forEach(line => {
    line = line.trim();
    if (!line || line.startsWith('#')) return;
    const i = line.indexOf('=');
    if (i < 0) return;
    map.push([line.slice(0, i).trim(), line.slice(i + 1).trim()]);
  });
  return map;
}
function normalizeDept(d, map) {
  if (d == null || d === '') return '(未填写)';
  const s = String(d).toLowerCase();
  for (const [kw, std] of map) if (kw && s.includes(kw.toLowerCase())) return std;
  return String(d);
}
/* 医院 / 医生 归一化：与科室同一套「关键字=标准名」逻辑（字段无关，可复用解析器） */
function normalizeByMap(v, map) {
  if (v == null || v === '') return '(未填写)';
  const s = String(v).toLowerCase();
  for (const [kw, std] of map) if (kw && s.includes(kw.toLowerCase())) return std;
  return String(v);
}
function parseHospMap(text) { return parseDeptMap(text); }
function normalizeHospital(h, map) { return normalizeByMap(h, map); }
function parseDoctorMap(text) { return parseDeptMap(text); }
function normalizeDoctor(d, map) { return normalizeByMap(d, map); }
function windowMonths(start, end) {
  if (!start || !end) return 12;
  const [sy, sm] = start.split('-').map(Number);
  const [ey, em] = end.split('-').map(Number);
  return Math.max(1, (ey - sy) * 12 + (em - sm) + 1);
}
function shiftMonths(ym, delta) {
  const [y, m] = ym.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}
/* 医院 / 品种分析: 尊重时间窗口 + 适应症(多选) + 科室过滤, 展示范围内全部品种(含本品与竞品) */
function hospitalAnalysis(allRows, cfg) {
  const indMap = parseIndMap(cfg.indicationMap || '');
  const deptMap = parseDeptMap(cfg.deptMap || '');
  const hospMap = parseHospMap(cfg.hospMap || '');
  const docMap = parseDoctorMap(cfg.docMap || '');
  const norm = allRows.map(r => ({ ...r, 适应症: normalizeIndication(r.适应症, indMap), 处方科室: normalizeDept(r.处方科室, deptMap), 医疗单位: normalizeHospital(r.医疗单位, hospMap), 处方医生: normalizeDoctor(r.处方医生, docMap) }));
  const cur = filterByWindow(norm, cfg.startYM, cfg.endYM);
  const rows = cur.filter(r => indMatches(r.适应症, cfg) && (!cfg.dept || r.处方科室 === cfg.dept));
  const hMap = new Map(), hpMap = new Map(), pMap = new Map(), dMap = new Map(), iMap = new Map(), ihMap = new Map();
  for (const r of rows) {
    const hk = r.医疗单位;
    if (!hMap.has(hk)) hMap.set(hk, { hospital: hk, boxes: 0, patients: new Set(), doctors: new Set(), products: new Set() });
    const h = hMap.get(hk);
    h.boxes += toQty(r.销量数量); h.patients.add(dedupeKey(r, cfg.dedupFields)); h.doctors.add(r.处方医生); h.products.add(r.通用名);
    const hpk = hk + '|' + r.通用名;
    if (!hpMap.has(hpk)) hpMap.set(hpk, { hospital: hk, product: r.通用名, boxes: 0, patients: new Set() });
    const hp = hpMap.get(hpk);
    hp.boxes += toQty(r.销量数量); hp.patients.add(dedupeKey(r, cfg.dedupFields));
    const pk = r.通用名;
    if (!pMap.has(pk)) pMap.set(pk, { product: pk, boxes: 0, patients: new Set() });
    const p = pMap.get(pk);
    p.boxes += toQty(r.销量数量); p.patients.add(dedupeKey(r, cfg.dedupFields));
    const ik = r.适应症;
    if (!iMap.has(ik)) iMap.set(ik, { indication: ik, boxes: 0, patients: new Set() });
    const ij = iMap.get(ik);
    ij.boxes += toQty(r.销量数量); ij.patients.add(dedupeKey(r, cfg.dedupFields));
    const ihk = hk + '|' + ik;
    if (!ihMap.has(ihk)) ihMap.set(ihk, { hospital: hk, indication: ik, boxes: 0, patients: new Set() });
    const ih = ihMap.get(ihk);
    ih.boxes += toQty(r.销量数量); ih.patients.add(dedupeKey(r, cfg.dedupFields));
    const dk = hk + '|' + r.处方医生 + '|' + r.通用名 + '|' + ik;
    if (!dMap.has(dk)) dMap.set(dk, { hospital: hk, doctor: r.处方医生, product: r.通用名, indication: ik, boxes: 0, patients: new Set() });
    const d = dMap.get(dk);
    d.boxes += toQty(r.销量数量); d.patients.add(dedupeKey(r, cfg.dedupFields));
  }
  const hospitals = [...hMap.values()].map(a => ({
    hospital: a.hospital, boxes: +a.boxes.toFixed(2), patients: a.patients.size,
    dot: a.patients.size ? +(a.boxes / a.patients.size).toFixed(2) : 0,
    doctors: a.doctors.size, products: a.products.size
  }));
  const totalBoxes = [...pMap.values()].reduce((s, a) => s + a.boxes, 0) || 1;
  const products = [...pMap.values()].map(a => ({
    product: a.product, boxes: +a.boxes.toFixed(2), patients: a.patients.size,
    dot: a.patients.size ? +(a.boxes / a.patients.size).toFixed(2) : 0,
    share: +(a.boxes / totalBoxes * 100).toFixed(1)
  }));
  const hospitalProducts = [...hpMap.values()].map(a => ({
    hospital: a.hospital, product: a.product, boxes: +a.boxes.toFixed(2), patients: a.patients.size,
    dot: a.patients.size ? +(a.boxes / a.patients.size).toFixed(2) : 0
  }));
  const byH = new Map();
  hospitalProducts.forEach(a => { const arr = byH.get(a.hospital) || []; arr.push(a); byH.set(a.hospital, arr); });
  byH.forEach(arr => { const tot = arr.reduce((s, a) => s + a.boxes, 0) || 1; arr.forEach(a => a.share = +(a.boxes / tot * 100).toFixed(1)); });
  const indTotal = [...iMap.values()].reduce((s, a) => s + a.boxes, 0) || 1;
  const indications = [...iMap.values()].map(a => ({
    indication: a.indication, boxes: +a.boxes.toFixed(2), patients: a.patients.size,
    dot: a.patients.size ? +(a.boxes / a.patients.size).toFixed(2) : 0, share: +(a.boxes / indTotal * 100).toFixed(1)
  })).sort((a, b) => b.boxes - a.boxes);
  const hospitalIndications = {};
  [...ihMap.values()].forEach(a => { (hospitalIndications[a.hospital] = hospitalIndications[a.hospital] || []).push({ indication: a.indication, boxes: +a.boxes.toFixed(2), patients: a.patients.size, dot: a.patients.size ? +(a.boxes / a.patients.size).toFixed(2) : 0 }); });
  Object.values(hospitalIndications).forEach(arr => { const t = arr.reduce((s, a) => s + a.boxes, 0) || 1; arr.forEach(a => a.share = +(a.boxes / t * 100).toFixed(1)); });
  const detail = [...dMap.values()].map(a => ({
    hospital: a.hospital, doctor: a.doctor, product: a.product, indication: a.indication, boxes: +a.boxes.toFixed(2),
    patients: a.patients.size, dot: a.patients.size ? +(a.boxes / a.patients.size).toFixed(2) : 0
  }));
  detail.sort((a, b) => b.boxes - a.boxes);
  return { hospitals, products, hospitalProducts, indications, hospitalIndications, detail, ownProduct: cfg.ownProduct };
}
function runAnalysis(allRows, cfg) {
  const indMap = parseIndMap(cfg.indicationMap || '');
  const deptMap = parseDeptMap(cfg.deptMap || '');
  const hospMap = parseHospMap(cfg.hospMap || '');
  const docMap = parseDoctorMap(cfg.docMap || '');
  const norm = allRows.map(r => ({ ...r, 适应症: normalizeIndication(r.适应症, indMap), 处方科室: normalizeDept(r.处方科室, deptMap), 医疗单位: normalizeHospital(r.医疗单位, hospMap), 处方医生: normalizeDoctor(r.处方医生, docMap) }));
  const cur = filterByWindow(norm, cfg.startYM, cfg.endYM);
  const prevStart = cfg.prevStartYM || shiftMonths(cfg.startYM, -windowMonths(cfg.startYM, cfg.endYM));
  const prev = filterByWindow(norm, prevStart, shiftMonths(cfg.startYM, -1));
  // 四象限与机会市场口径保持一致: 选了适应症(可多选)则同时按「本品 + 适应症」过滤
  const dept = cfg.dept || null;
  const ownRows = cur.filter(r => r.通用名 === cfg.ownProduct && indMatches(r.适应症, cfg) && (!dept || r.处方科室 === dept));
  const doctors = classifyQuadrant(aggregateByDoctor(ownRows, cfg), cfg.topPct);
  const mCur = marketAggregate(cur, cfg, true);
  const mPrev = marketAggregate(prev, cfg, true);
  const marketIncl = mergeTrend(mCur, mPrev);
  const mCurNo = marketAggregate(cur, cfg, false);
  const mPrevNo = marketAggregate(prev, cfg, false);
  const marketExcl = mergeTrend(mCurNo, mPrevNo);
  return {
    _meta: { total: norm.length, window: cur.length, own: ownRows.length, ownProduct: cfg.ownProduct, indication: (cfg.indications && cfg.indications.length) ? cfg.indications.join(' / ') : (cfg.indication || '全部'), dept: cfg.dept || '全部' },
    doctors, marketIncl, marketExcl,
    summaryIncl: summarizeMarket(marketIncl, true, cfg.competitors || []),
    summaryExcl: summarizeMarket(marketExcl, false, cfg.competitors || []),
    ...hospitalAnalysis(allRows, cfg),
    counts: {
      opinion: doctors.filter(d => d.quadrant === '意见领袖').length,
      support: doctors.filter(d => d.quadrant === '学术支持对象').length,
      potential: doctors.filter(d => d.quadrant === '潜力医生').length,
      low: doctors.filter(d => d.quadrant === '待评估').length
    }
  };
}

/* ---------- 内置 Mock 数据 (演示用) ---------- */
const MOCK_ROWS = (function () {
  const hosp = ['四川省肿瘤医院', '华西医院', '德阳市人民医院', '乐山市人民医院', '西部战区总医院'];
  const dept = ['肿瘤科', '呼吸科', '内科'];
  const prod = { own: '品种E', comp: ['品种A', '品种B', '品种C'] };
  const ind = '肺癌';
  const rows = [];
  const ymList = [];
  for (let i = 0; i < 24; i++) ymList.push(shiftMonths('2024-07', i));
  const docs = [
    { h: hosp[0], d: dept[0], doc: '张医生', own: true,  patients: 20, box: { early: 420, late: 420 } },
    { h: hosp[0], d: dept[0], doc: '李医生', own: true,  patients: 20, box: { early: 120, late: 120 } },
    { h: hosp[1], d: dept[1], doc: '王医生', own: true,  patients: 4,  box: { early: 200, late: 200 } },
    { h: hosp[1], d: dept[1], doc: '赵医生', own: true,  patients: 4,  box: { early: 60,  late: 40  } },
    { h: hosp[2], d: dept[0], doc: '陈医生', own: false, patients: 20, box: { early: 0,   late: 400 } },
    { h: hosp[3], d: dept[2], doc: '刘医生', own: false, patients: 20, box: { early: 200, late: 400 } },
    { h: hosp[4], d: dept[0], doc: '杨医生', own: false, patients: 10, box: { early: 150, late: 0   } }
  ];
  docs.forEach((dc, di) => {
    ymList.forEach((ym, mi) => {
      const phase = mi < 12 ? 'early' : 'late';
      const monthBox = dc.box[phase];
      if (!monthBox) return;
      const prodName = dc.own ? prod.own : prod.comp[(di + mi) % prod.comp.length];
      const perPatient = monthBox / dc.patients;
      for (let p = 0; p < dc.patients; p++) {
        rows.push({ 销售日期: ym + '-15', 医疗单位: dc.h, 处方科室: dc.d, 处方医生: dc.doc, 通用名: prodName, 适应症: ind, 销量数量: +perPatient.toFixed(2), oneid: 'OID' + di + '_' + p, 会员电话: '1380000' + (1000 + p), 会员号: 'M' + p, 开票抬头: dc.h, 会员姓名: dc.doc + p, 药房名称: dc.h });
      }
    });
  });
  return rows;
})();

const _API = { toYM, toQty, filterByWindow, dedupeKey, aggregateByDoctor, percentile, classifyQuadrant, indMatches, marketAggregate, mergeTrend, summarizeMarket, maskName, applyMask, parseIndMap, normalizeIndication, parseDeptMap, normalizeDept, parseHospMap, normalizeHospital, parseDoctorMap, normalizeDoctor, hospitalAnalysis, runAnalysis, shiftMonths, windowMonths, MOCK_ROWS };
if (typeof module !== 'undefined' && module.exports) { module.exports = _API; }
else if (typeof window !== 'undefined') { window.app = _API; }
