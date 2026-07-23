# -*- coding: utf-8 -*-
"""
临床用药洞察看板 · 计算引擎 (compute.py)
==========================================================================
纯 pandas 实现，无 UI 依赖。口径与原前端 app.compute.js 逐项对齐：
  - 销量用"转化后数量"直接计（不做支/盒换算）
  - 适应症 / 科室 / 医院 / 医生 按「关键字=标准名」归并归一化
  - 患者去重用复合键 AND（所选字段全部相同才算同一人）
  - 医生四象限：患者数 × DOT 分位阈值分层
  - 机会市场：本期 vs 上一等长窗口的趋势
  - 医院与品种分析：医院 / 品种 / 适应症 / 医院医生品种明细
性能：用 pandas groupby + nunique 向量化，几万~十万行秒级完成。
==========================================================================
"""
import math
import re
import pandas as pd
import numpy as np

STD = ['销售日期', '医疗单位', '处方科室', '处方医生', '通用名', '适应症', '销量数量',
       'oneid', '会员电话', '会员号', '开票抬头', '会员姓名', '药房名称']
REQ = ['销售日期', '医疗单位', '处方科室', '处方医生', '通用名', '适应症', '销量数量']
DEDUP_OPTS = ['oneid', '会员电话', '会员号', '开票抬头', '会员姓名', '药房名称']

# 列名自动识别别名（底表列 -> 标准字段）
COL_ALIASES = {
    '销售日期': ['销售时间', '日期', '时间'],
    '医疗单位': ['医疗单位', '医院'],
    '处方科室': ['处方科室', '科室'],
    '处方医生': ['处方医生', '医生'],
    '通用名': ['商品名称', '通用名', '产品'],
    '适应症': ['适应症', '诊断'],
    '销量数量': ['转化后数量', '销售数量', '销量'],
    'oneid': ['oneid'],
    '会员电话': ['会员电话'],
    '会员号': ['会员号'],
    '开票抬头': ['开票抬头'],
    '会员姓名': ['会员姓名'],
    '药房名称': ['药房名称'],
}

DEFAULT_IND_MAP = """特应性=特应性皮炎
AD=特应性皮炎
银屑=银屑病
斑块=银屑病
类风湿=类风湿关节炎
RA=类风湿关节炎
强直=强直性脊柱炎
AS=强直性脊柱炎
关节炎=关节炎
鼻窦=过敏性鼻炎/鼻窦炎
鼻炎=过敏性鼻炎/鼻窦炎
过敏=过敏性鼻炎/鼻窦炎
哮喘=哮喘/COPD
COPD=哮喘/COPD
慢阻肺=哮喘/COPD
阻塞=哮喘/COPD
克罗恩=炎症性肠病
溃结=炎症性肠病
结肠=炎症性肠病
类天疱疮=大疱性皮肤病
天疱疮=大疱性皮肤病
荨麻疹=荨麻疹
皮炎=特应性皮炎"""

DEFAULT_DEPT_MAP = """皮肤=皮肤科
性病=皮肤科
风湿=风湿免疫科
免疫=风湿免疫科
消化=消化内科
呼吸=呼吸内科"""

DEFAULT_HOSP_MAP = ""
DEFAULT_DOC_MAP = ""


# ---------------------------------------------------------------- 基础工具
def to_ym(v):
    """日期 -> 'YYYY-MM'，兼容 Timestamp / 'YYYY-MM-DD' / 'YYYY/MM/DD HH:MM:SS'"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ''
    if isinstance(v, (pd.Timestamp,)):
        return f"{v.year}-{v.month:02d}"
    try:
        import datetime
        if isinstance(v, (datetime.datetime, datetime.date)):
            return f"{v.year}-{v.month:02d}"
    except Exception:
        pass
    s = str(v)
    m = re.search(r'(\d{4})[-/](\d{1,2})', s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return s[:7]


def to_qty(v):
    """销量直接取数；容错清洗逗号/空格"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return 0.0
    try:
        n = float(re.sub(r'[,\s]', '', str(v)))
        return 0.0 if math.isnan(n) else n
    except Exception:
        return 0.0


def parse_map(text):
    """解析「关键字=标准名」映射文本，# 开头为注释"""
    out = []
    for line in (text or '').split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        i = line.find('=')
        if i < 0:
            continue
        out.append((line[:i].strip(), line[i + 1:].strip()))
    return out


def _normalize_value(v, mapping, is_indication=False):
    if v is None or v == '' or (isinstance(v, float) and math.isnan(v)):
        return '(未填写)' if is_indication else '(未填写)'
    s = str(v).lower()
    for kw, std in mapping:
        if kw and kw.lower() in s:
            return std
    return str(v)


def normalize_series(series, mapping, is_indication=False):
    """对整列做归一化：先按唯一值建缓存，再 map，几万行也快"""
    uniq = series.astype(object).where(series.notna(), None).unique()
    cache = {u: _normalize_value(u, mapping, is_indication) for u in uniq}
    return series.astype(object).where(series.notna(), None).map(cache)


# ---------------------------------------------------------------- 组合映射（医院+科室+医生 整体）
COMBO_SEP = '\t'  # 组合内 医院/科室/医生 的分隔符
DEFAULT_COMBO_MAP = (
    "# 医院+科室+医生 组合映射：底表里所有 医院|科室|医生 唯一组合会列在下面（用 Tab 分隔）\n"
    "# 同一组变体：把右侧改成目标标准组合即可整体归并\n"
    "# 例如：\n"
    "# 成都市第一人民医院\\t风湿免疫科\\t雷丽华 = 成都市第一人民医院\\t血液风湿免疫科\\t雷丽华\n"
)


def parse_combo_map(text):
    """解析 `医院\\t科室\\t医生 = 医院\\t科室\\t医生` 格式 → {(h0,d0,c0): (h1,d1,c1)}"""
    out = {}
    for line in (text or '').split('\n'):
        s = line.rstrip('\r\n').strip(' ')  # 只去换行/首尾空格，保留 tab（末尾空段是通配写法的一部分）
        if not s or s.startswith('#') or '=' not in s:
            continue
        left, right = s.split('=', 1)
        lk = tuple(x.strip() for x in left.split(COMBO_SEP))
        rk = tuple(x.strip() for x in right.split(COMBO_SEP))
        if len(lk) == 3 and len(rk) == 3:
            out[lk] = rk
    return out


def _apply_combo_map(d, combo_map):
    """把 (医院, 科室, 医生) 三列按 combo_map 整体归一；无匹配则原样。

    规则支持「通配」：左键任一段留空(``) 表示「该段任意值均匹配」，
    右键对应段留空表示「保留原值」。例如：
      `成都市第一人民医院\t\t = 成都市第一人民医院(天府院区)\t\t`
      会把该医院的所有组合统一改名，无需逐条写。
    匹配优先级：完全匹配 > 通配符更少者 > 通配符更多者。
    """
    if not combo_map:
        return d
    keys = list(zip(d['医疗单位'].astype(str), d['处方科室'].astype(str), d['处方医生'].astype(str)))
    uniq = set(keys)
    exact = {}
    wild = []
    for lk, rk in combo_map.items():
        n = sum(1 for x in lk if x == '')
        if n == 0:
            exact[lk] = rk
        else:
            wild.append((lk, rk, n))
    wild.sort(key=lambda t: t[2])  # 通配符越少越精确，优先匹配

    def resolve(k):
        if k in exact:
            return exact[k]
        for lk, rk, _ in wild:
            if all(lk[i] == '' or lk[i] == k[i] for i in range(3)):
                return tuple(rk[i] if rk[i] != '' else k[i] for i in range(3))
        return k

    cache = {k: resolve(k) for k in uniq}
    norm = [cache[k] for k in keys]
    d = d.copy()
    d['医疗单位'] = [k[0] for k in norm]
    d['处方科室'] = [k[1] for k in norm]
    d['处方医生'] = [k[2] for k in norm]
    return d


def shift_months(ym, delta):
    y, m = [int(x) for x in ym.split('-')]
    idx = (y * 12 + (m - 1)) + delta
    ny, nm = idx // 12, idx % 12
    return f"{ny}-{nm + 1:02d}"


def window_months(start, end):
    if not start or not end:
        return 12
    sy, sm = [int(x) for x in start.split('-')]
    ey, em = [int(x) for x in end.split('-')]
    return max(1, (ey - sy) * 12 + (em - sm) + 1)


def mask_name(name):
    if not name:
        return name
    s = str(name)
    if len(s) <= 1:
        return s
    return s[0] + '*' * min(len(s) - 1, 6)


def js_round(x, n):
    """贴近 JS Number.toFixed 的四舍五入（round half away from zero）"""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0
    factor = 10 ** n
    return math.floor(abs(x) * factor + 0.5) / factor * (1 if x >= 0 else -1)


def js_round_series(s, n):
    """向量化版 js_round，作用于 pandas Series / numpy 数组"""
    a = np.asarray(s, dtype='float64')
    a = np.nan_to_num(a, nan=0.0)
    factor = 10 ** n
    r = np.floor(np.abs(a) * factor + 0.5) / factor
    return r * np.where(a >= 0, 1.0, -1.0)


def _dot_series(boxes, patients):
    """DOT = 销量/患者数（患者为0时按0；恒有行故患者>=1），向量化"""
    boxes = np.asarray(boxes, dtype='float64')
    patients = np.asarray(patients, dtype='float64')
    safe = np.where(patients > 0, patients, 1.0)
    dot = np.where(patients > 0, boxes / safe, 0.0)
    return js_round_series(dot, 2)


def percentile(values, p):
    """复刻 JS percentile：sort 后 idx = min(len-1, floor((len-1)*p))"""
    s = sorted(values)
    if not s:
        return 0
    idx = min(len(s) - 1, int(math.floor((len(s) - 1) * p)))
    return s[idx]


def ind_matches_list(cfg):
    inds = cfg.get('indications')
    if inds:
        return list(inds)
    single = cfg.get('indication')
    if single:
        return [single]
    return None  # None = 全部


# ---------------------------------------------------------------- 归一化 + 去重列
def _prep_dataframe(df, cfg):
    """归一化四字段 + 生成 ym 列 + 去重复合键列 + 数值销量列"""
    d = df.copy()
    ind_map = parse_map(cfg.get('indicationMap', ''))
    dept_map = parse_map(cfg.get('deptMap', ''))
    hosp_map = parse_map(cfg.get('hospMap', ''))
    doc_map = parse_map(cfg.get('docMap', ''))
    d['适应症'] = normalize_series(d['适应症'], ind_map, is_indication=True)
    d['处方科室'] = normalize_series(d['处方科室'], dept_map)
    d['医疗单位'] = normalize_series(d['医疗单位'], hosp_map)
    d['处方医生'] = normalize_series(d['处方医生'], doc_map)
    # 组合级归一（在单字段归一之后、_pkey 之前）
    d = _apply_combo_map(d, parse_combo_map(cfg.get('comboMap', '')))
    d['_ym'] = d['销售日期'].map(to_ym)
    d['_qty'] = d['销量数量'].map(to_qty)
    dedup = cfg.get('dedupFields') or ['oneid']
    dedup = [f for f in dedup if f in d.columns]
    if not dedup:
        dedup = ['oneid'] if 'oneid' in d.columns else []
    if len(dedup) == 1:
        d['_pkey'] = d[dedup[0]].fillna('').astype(str)
    elif len(dedup) > 1:
        parts = [d[f].fillna('').astype(str) for f in dedup]
        pk = parts[0]
        for p in parts[1:]:
            pk = pk.str.cat(p, sep='||')
        d['_pkey'] = pk
    else:
        d['_pkey'] = ''
    return d


def _in_window_mask(d, start_ym, end_ym):
    m = d['_ym'].astype(str) != ''
    if start_ym:
        m &= d['_ym'].astype(str) >= start_ym
    if end_ym:
        m &= d['_ym'].astype(str) <= end_ym
    return m


# ---------------------------------------------------------------- 医生聚合 + 四象限
def aggregate_by_doctor(d):
    if d.empty:
        return pd.DataFrame(columns=['hospital', 'dept', 'doctor', 'boxes', 'patients', 'dot'])
    g = d.groupby(['医疗单位', '处方科室', '处方医生'], sort=False)
    agg = g.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    agg = agg.rename(columns={'医疗单位': 'hospital', '处方科室': 'dept', '处方医生': 'doctor'})
    agg['boxes'] = js_round_series(agg['boxes'], 2)
    agg['dot'] = _dot_series(agg['boxes'], agg['patients'])
    return agg


def classify_quadrant(doctors, top_pct):
    top_pct = top_pct or 0.3
    if doctors.empty:
        for col in ['highPatients', 'highDot', 'quadrant']:
            doctors[col] = []
        return doctors
    p_th = percentile(doctors['patients'].tolist(), 1 - top_pct)
    d_th = percentile(doctors['dot'].tolist(), 1 - top_pct)
    doctors = doctors.copy()
    hi_p = doctors['patients'] >= p_th
    hi_d = doctors['dot'] >= d_th
    doctors['highPatients'] = hi_p
    doctors['highDot'] = hi_d
    doctors['quadrant'] = np.select(
        [hi_p & hi_d, hi_p & ~hi_d, ~hi_p & hi_d],
        ['意见领袖', '学术支持对象', '潜力医生'],
        default='待评估')
    return doctors


# ---------------------------------------------------------------- 机会市场
def market_aggregate(d, cfg, include_own):
    own = cfg.get('ownProduct')
    competitors = cfg.get('competitors') or []
    dept = cfg.get('dept') or None
    inds = ind_matches_list(cfg)
    m = pd.Series(True, index=d.index)
    if inds is not None:
        m &= d['适应症'].isin(inds)
    if dept:
        m &= d['处方科室'] == dept
    sub = d[m]
    is_own = sub['通用名'] == own
    if include_own:
        keep_own = is_own
    else:
        keep_own = pd.Series(False, index=sub.index)
    if competitors:
        keep_comp = (~is_own) & sub['通用名'].isin(competitors)
    else:
        keep_comp = (~is_own)
    sub = sub[keep_own | keep_comp]
    if sub.empty:
        return pd.DataFrame(columns=['hospital', 'dept', 'doctor', 'boxes', 'patients'])
    g = sub.groupby(['医疗单位', '处方科室', '处方医生'], sort=False)
    agg = g.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    agg = agg.rename(columns={'医疗单位': 'hospital', '处方科室': 'dept', '处方医生': 'doctor'})
    agg['boxes'] = js_round_series(agg['boxes'], 2)
    return agg


def merge_trend(curr, prev):
    """本期 vs 上期趋势，向量化 outer merge 实现（口径同原 JS）"""
    cols = ['hospital', 'dept', 'doctor', 'boxes', 'patients']
    if curr.empty and prev.empty:
        return pd.DataFrame(columns=cols + ['prevBoxes', 'delta', 'trend'])
    c = (curr[cols] if not curr.empty else pd.DataFrame(columns=cols)).copy()
    p = (prev[cols] if not prev.empty else pd.DataFrame(columns=cols)).copy()
    p = p.rename(columns={'boxes': 'prevBoxes', 'patients': 'prevPatients'})
    merged = c.merge(p, on=['hospital', 'dept', 'doctor'], how='outer', indicator=True)
    has_prev = merged['prevBoxes'].notna()
    only_prev = (merged['_merge'] == 'right_only').to_numpy()
    # 仅上期出现（流失）：boxes/patients 沿用上期值（同原 JS ...p）；仅当有流失行时才赋值
    if only_prev.any():
        merged.loc[only_prev, 'boxes'] = merged.loc[only_prev, 'prevBoxes']
        merged.loc[only_prev, 'patients'] = merged.loc[only_prev, 'prevPatients']
    merged['boxes'] = merged['boxes'].fillna(0.0)
    merged['patients'] = merged['patients'].fillna(0).astype(int)
    merged['prevBoxes'] = merged['prevBoxes'].fillna(0.0)
    box = merged['boxes'].to_numpy(dtype='float64')
    pbox = merged['prevBoxes'].to_numpy(dtype='float64')
    delta = box - pbox
    # 覆盖顺序 = 原 JS if-elif 的逆序（优先级最高的最后写，保证不被覆盖）
    trend = np.full(len(merged), '持平', dtype=object)
    trend[delta < 0] = '下降'
    trend[delta > 0] = '上升'
    trend[(pbox > 0) & (box == 0)] = '流失'
    trend[(~has_prev.to_numpy()) & (box > 0)] = '新进'
    # 只在上期出现：box 沿用上期值(>0)，delta=-prev, trend=流失（与原逻辑一致）
    if only_prev.any():
        trend[only_prev] = '流失'
        delta[only_prev] = -pbox[only_prev]
    merged['prevBoxes'] = js_round_series(pbox, 2)
    merged['delta'] = js_round_series(delta, 2)
    merged['trend'] = trend
    res = merged[['hospital', 'dept', 'doctor', 'boxes', 'patients', 'prevBoxes', 'delta', 'trend']]
    if not res.empty:
        res = res.sort_values('boxes', ascending=False).reset_index(drop=True)
    return res


def summarize_market(trend_rows, include_own, competitors):
    if trend_rows.empty:
        up = down = neu = lost = 0
        n = 0
    else:
        up = int((trend_rows['trend'] == '上升').sum())
        down = int((trend_rows['trend'] == '下降').sum())
        neu = int((trend_rows['trend'] == '新进').sum())
        lost = int((trend_rows['trend'] == '流失').sum())
        n = len(trend_rows)
    comp_label = ' / '.join(competitors) if competitors else '全部竞品'
    scope = (f"同适应症整体(本品 + {comp_label})" if include_own
             else f"同适应症竞品机会市场(未含本品; 竞品={comp_label})")
    tail = ('本品需关注下降与新进乏力者。' if include_own
            else '未覆盖本品的医生即为可拓展机会, 优先跟进上升且本品缺席者。')
    return f"【{scope}】共 {n} 位医生: 上升 {up} / 下降 {down} / 新进 {neu} / 流失 {lost}。" + tail


# ---------------------------------------------------------------- 医院与品种分析
def hospital_analysis(d_all, cfg):
    d = _prep_dataframe(d_all, cfg)
    cur = d[_in_window_mask(d, cfg.get('startYM'), cfg.get('endYM'))]
    inds = ind_matches_list(cfg)
    m = pd.Series(True, index=cur.index)
    if inds is not None:
        m &= cur['适应症'].isin(inds)
    if cfg.get('dept'):
        m &= cur['处方科室'] == cfg['dept']
    rows = cur[m]

    empty_cols_h = ['hospital', 'boxes', 'patients', 'dot', 'doctors', 'products']
    if rows.empty:
        return {
            'hospitals': pd.DataFrame(columns=empty_cols_h),
            'products': pd.DataFrame(columns=['product', 'boxes', 'patients', 'dot', 'share']),
            'hospitalProducts': pd.DataFrame(columns=['hospital', 'product', 'boxes', 'patients', 'dot', 'share']),
            'indications': pd.DataFrame(columns=['indication', 'boxes', 'patients', 'dot', 'share']),
            'hospitalIndications': pd.DataFrame(columns=['hospital', 'indication', 'boxes', 'patients', 'dot', 'share']),
            'detail': pd.DataFrame(columns=['hospital', 'doctor', 'product', 'indication', 'boxes', 'patients', 'dot']),
            'ownProduct': cfg.get('ownProduct'),
        }

    # 医院级
    gh = rows.groupby('医疗单位', sort=False)
    hospitals = gh.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique'),
                       doctors=('处方医生', 'nunique'), products=('通用名', 'nunique')).reset_index()
    hospitals = hospitals.rename(columns={'医疗单位': 'hospital'})
    hospitals['boxes'] = js_round_series(hospitals['boxes'], 2)
    hospitals['dot'] = _dot_series(hospitals['boxes'], hospitals['patients'])
    hospitals = hospitals[empty_cols_h]

    # 品种级
    gp = rows.groupby('通用名', sort=False)
    products = gp.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    products = products.rename(columns={'通用名': 'product'})
    products['boxes'] = js_round_series(products['boxes'], 2)
    products['dot'] = _dot_series(products['boxes'], products['patients'])
    total_boxes = products['boxes'].sum() or 1
    products['share'] = js_round_series(products['boxes'] / total_boxes * 100, 1)
    products = products.sort_values('boxes', ascending=False).reset_index(drop=True)

    # 医院×品种
    ghp = rows.groupby(['医疗单位', '通用名'], sort=False)
    hp = ghp.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    hp = hp.rename(columns={'医疗单位': 'hospital', '通用名': 'product'})
    hp['boxes'] = js_round_series(hp['boxes'], 2)
    hp['dot'] = _dot_series(hp['boxes'], hp['patients'])
    hp['_htot'] = hp.groupby('hospital')['boxes'].transform('sum').replace(0, 1)
    hp['share'] = js_round_series(hp['boxes'] / hp['_htot'] * 100, 1)
    hospital_products = hp.drop(columns=['_htot'])

    # 适应症级
    gi = rows.groupby('适应症', sort=False)
    indications = gi.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    indications = indications.rename(columns={'适应症': 'indication'})
    indications['boxes'] = js_round_series(indications['boxes'], 2)
    indications['dot'] = _dot_series(indications['boxes'], indications['patients'])
    ind_total = indications['boxes'].sum() or 1
    indications['share'] = js_round_series(indications['boxes'] / ind_total * 100, 1)
    indications = indications.sort_values('boxes', ascending=False).reset_index(drop=True)

    # 医院×适应症
    gih = rows.groupby(['医疗单位', '适应症'], sort=False)
    hi = gih.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    hi = hi.rename(columns={'医疗单位': 'hospital', '适应症': 'indication'})
    hi['boxes'] = js_round_series(hi['boxes'], 2)
    hi['dot'] = _dot_series(hi['boxes'], hi['patients'])
    hi['_htot'] = hi.groupby('hospital')['boxes'].transform('sum').replace(0, 1)
    hi['share'] = js_round_series(hi['boxes'] / hi['_htot'] * 100, 1)
    hospital_indications = hi.drop(columns=['_htot'])

    # 医院×医生×品种×适应症 明细
    gd = rows.groupby(['医疗单位', '处方医生', '通用名', '适应症'], sort=False)
    detail = gd.agg(boxes=('_qty', 'sum'), patients=('_pkey', 'nunique')).reset_index()
    detail = detail.rename(columns={'医疗单位': 'hospital', '处方医生': 'doctor', '通用名': 'product', '适应症': 'indication'})
    detail['boxes'] = js_round_series(detail['boxes'], 2)
    detail['dot'] = _dot_series(detail['boxes'], detail['patients'])
    detail = detail.sort_values('boxes', ascending=False).reset_index(drop=True)

    return {
        'hospitals': hospitals,
        'products': products,
        'hospitalProducts': hospital_products,
        'indications': indications,
        'hospitalIndications': hospital_indications,
        'detail': detail,
        'ownProduct': cfg.get('ownProduct'),
    }


# ---------------------------------------------------------------- 总入口
def run_analysis(df, cfg):
    cfg = dict(cfg or {})
    d = _prep_dataframe(df, cfg)
    total = len(d)

    # 时间窗口缺省时从数据自动推导：endYM=数据最大月, startYM=回溯 (窗口-1) 个月
    if not cfg.get('startYM') or not cfg.get('endYM'):
        yms = sorted(y for y in d['_ym'].astype(str).unique() if y)
        if yms:
            cfg.setdefault('endYM', yms[-1])
            span = cfg.get('windowMonths') or 12
            cfg.setdefault('startYM', shift_months(cfg['endYM'], -(span - 1)))

    start_ym, end_ym = cfg.get('startYM'), cfg.get('endYM')
    cur = d[_in_window_mask(d, start_ym, end_ym)]
    prev_start = cfg.get('prevStartYM') or (
        shift_months(start_ym, -window_months(start_ym, end_ym)) if start_ym else None)
    prev_end = shift_months(start_ym, -1) if start_ym else None
    prev = d[_in_window_mask(d, prev_start, prev_end)]

    dept = cfg.get('dept') or None
    inds = ind_matches_list(cfg)
    own_mask = cur['通用名'] == cfg.get('ownProduct')
    if inds is not None:
        own_mask &= cur['适应症'].isin(inds)
    if dept:
        own_mask &= cur['处方科室'] == dept
    own_rows = cur[own_mask]

    doctors = classify_quadrant(aggregate_by_doctor(own_rows), cfg.get('topPct'))

    m_cur = market_aggregate(cur, cfg, True)
    m_prev = market_aggregate(prev, cfg, True)
    market_incl = merge_trend(m_cur, m_prev)
    m_cur_no = market_aggregate(cur, cfg, False)
    m_prev_no = market_aggregate(prev, cfg, False)
    market_excl = merge_trend(m_cur_no, m_prev_no)

    ha = hospital_analysis(df, cfg)

    ind_label = (' / '.join(cfg['indications']) if cfg.get('indications')
                 else (cfg.get('indication') or '全部'))

    def qcount(q):
        return int((doctors['quadrant'] == q).sum()) if not doctors.empty else 0

    result = {
        '_meta': {
            'total': total, 'window': len(cur), 'own': len(own_rows),
            'ownProduct': cfg.get('ownProduct'), 'indication': ind_label,
            'dept': cfg.get('dept') or '全部',
        },
        'doctors': doctors,
        'marketIncl': market_incl,
        'marketExcl': market_excl,
        'summaryIncl': summarize_market(market_incl, True, cfg.get('competitors') or []),
        'summaryExcl': summarize_market(market_excl, False, cfg.get('competitors') or []),
        'counts': {
            'opinion': qcount('意见领袖'), 'support': qcount('学术支持对象'),
            'potential': qcount('潜力医生'), 'low': qcount('待评估'),
        },
    }
    result.update(ha)
    return result


# ---------------------------------------------------------------- 列名自动识别
def auto_map_columns(headers):
    """底表列 -> 标准字段的自动映射。返回 {标准字段: 底表列}"""
    mapping = {}
    low = {h: str(h).strip().lower() for h in headers}
    for std, aliases in COL_ALIASES.items():
        found = ''
        # 先精确/包含匹配别名
        for a in aliases:
            al = a.lower()
            for h in headers:
                if low[h] == al:
                    found = h
                    break
            if found:
                break
        if not found:
            for a in aliases:
                al = a.lower()
                for h in headers:
                    if al in low[h]:
                        found = h
                        break
                if found:
                    break
        if found:
            mapping[std] = found
    return mapping


def apply_mapping(df, mapping):
    """按 mapping 把底表列改名到标准字段，并补齐缺失的可选列为 None"""
    out = pd.DataFrame()
    for std in STD:
        col = mapping.get(std)
        if col is not None and col in df.columns:
            out[std] = df[col]
        else:
            out[std] = None
    return out


# ---------------------------------------------------------------- 内置示例数据
def mock_rows():
    hosp = ['四川省肿瘤医院', '华西医院', '德阳市人民医院', '乐山市人民医院', '西部战区总医院']
    dept = ['肿瘤科', '呼吸科', '内科']
    own, comp = '品种E', ['品种A', '品种B', '品种C']
    ind = '肺癌'
    ym_list = [shift_months('2024-07', i) for i in range(24)]
    docs = [
        {'h': hosp[0], 'd': dept[0], 'doc': '张医生', 'own': True, 'patients': 20, 'early': 420, 'late': 420},
        {'h': hosp[0], 'd': dept[0], 'doc': '李医生', 'own': True, 'patients': 20, 'early': 120, 'late': 120},
        {'h': hosp[1], 'd': dept[1], 'doc': '王医生', 'own': True, 'patients': 4, 'early': 200, 'late': 200},
        {'h': hosp[1], 'd': dept[1], 'doc': '赵医生', 'own': True, 'patients': 4, 'early': 60, 'late': 40},
        {'h': hosp[2], 'd': dept[0], 'doc': '陈医生', 'own': False, 'patients': 20, 'early': 0, 'late': 400},
        {'h': hosp[3], 'd': dept[2], 'doc': '刘医生', 'own': False, 'patients': 20, 'early': 200, 'late': 400},
        {'h': hosp[4], 'd': dept[0], 'doc': '杨医生', 'own': False, 'patients': 10, 'early': 150, 'late': 0},
    ]
    rows = []
    for di, dc in enumerate(docs):
        for mi, ym in enumerate(ym_list):
            phase = 'early' if mi < 12 else 'late'
            month_box = dc[phase]
            if not month_box:
                continue
            prod_name = own if dc['own'] else comp[(di + mi) % len(comp)]
            per = month_box / dc['patients']
            for p in range(dc['patients']):
                rows.append({
                    '销售日期': ym + '-15', '医疗单位': dc['h'], '处方科室': dc['d'],
                    '处方医生': dc['doc'], '通用名': prod_name, '适应症': ind,
                    '销量数量': round(per, 2), 'oneid': f'OID{di}_{p}',
                    '会员电话': '1380000' + str(1000 + p), '会员号': f'M{p}',
                    '开票抬头': dc['h'], '会员姓名': dc['doc'] + str(p), '药房名称': dc['h'],
                })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 自测
if __name__ == '__main__':
    df = mock_rows()
    cfg = {
        'startYM': '2025-07', 'endYM': '2026-06', 'ownProduct': '品种E',
        'indications': [], 'topPct': 0.3, 'dedupFields': ['oneid'],
        'competitors': ['品种A', '品种B', '品种C'],
        'indicationMap': DEFAULT_IND_MAP, 'deptMap': DEFAULT_DEPT_MAP,
        'hospMap': '', 'docMap': '',
    }
    res = run_analysis(df, cfg)
    print('总行数', res['_meta']['total'])
    print('窗口内', res['_meta']['window'])
    print('本品行', res['_meta']['own'])
    print('医生数', len(res['doctors']))
    print('四象限计数', res['counts'])
    print('医院数', len(res['hospitals']))
    print('品种数', len(res['products']))
    print('适应症数', len(res['indications']))
    print('明细行', len(res['detail']))
    print(res['summaryIncl'])
    print(res['summaryExcl'])
    print('--- 医生分层 ---')
    print(res['doctors'][['doctor', 'hospital', 'patients', 'dot', 'quadrant']].to_string(index=False))
    print('--- 品种占比 ---')
    print(res['products'].to_string(index=False))
