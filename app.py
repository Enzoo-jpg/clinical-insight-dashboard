# -*- coding: utf-8 -*-
"""
临床用药洞察看板 · Streamlit 版
==========================================================================
服务器端计算（pandas 向量化），适合几万~十万行数据快速分析。
流程：上传明细 → 数据清洗映射（适应症/医院/科室/医生）→ 计算配置(含适应症范围选取)
→ 医生四象限 / 机会市场 / 医院与品种·适应症分析 → Excel 导出。列名自动识别，无需手动映射列。
==========================================================================
"""
import io
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

import compute as C

st.set_page_config(page_title="临床用药洞察看板", layout="wide", page_icon="📊")

QUAD_COLORS = {'意见领袖': '#16a34a', '学术支持对象': '#d97706', '潜力医生': '#2563eb', '待评估': '#94a3b8'}
PIE_PALETTE = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#65a30d', '#ea580c', '#475569']
IND_PALETTE = ['#7c3aed', '#0891b2', '#db2777', '#65a30d', '#ea580c', '#475569', '#2563eb', '#16a34a', '#d97706', '#dc2626']
OWN_RED = '#dc2626'


# ----------------------------------------------------------- session 初始化
def _init_state():
    ss = st.session_state
    ss.setdefault('raw_df', None)
    ss.setdefault('map_ind', C.DEFAULT_IND_MAP)
    ss.setdefault('map_org', DEFAULT_ORG_MAP)


# ----------------------------------------------------------- 计算缓存
@st.cache_data(show_spinner="正在计算…", max_entries=8)
def cached_run(df_std_bytes, cfg_key, cfg):
    df = pd.read_pickle(io.BytesIO(df_std_bytes))
    return C.run_analysis(df, cfg)


def _import_all_values(df_std, std_col, cur_text, is_ind=False):
    """把数据中该列全部值中，尚未被现有规则覆盖的，补成 `值=值` 追加到映射文本"""
    if df_std is None or std_col not in df_std.columns:
        return cur_text
    mapping = C.parse_map(cur_text)
    std_set = {std for _, std in mapping}
    existing_kw = {kw for kw, _ in mapping}
    vals = [v for v in df_std[std_col].dropna().astype(str).unique() if v.strip() != '']
    add = []
    for v in vals:
        norm = C._normalize_value(v, mapping, is_indication=is_ind)
        if norm == v and v not in std_set and v not in existing_kw:
            add.append(f"{v}={v}")
    if not add:
        return cur_text
    base = cur_text.rstrip('\n')
    return (base + '\n' + '\n'.join(add)) if base else '\n'.join(add)


# 医院 / 科室 / 医生 合并清洗框：[医院] [科室] [医生] 分段，段内 关键字=标准名
def split_org_map(text):
    """合并框文本 -> (医院映射文本, 科室映射文本, 医生映射文本)"""
    sec = {'医院': [], '科室': [], '医生': []}
    cur = None
    for line in (text or '').split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('[') and s.endswith(']'):
            name = s[1:-1].strip()
            cur = name if name in sec else None
            continue
        if cur is None:
            continue
        sec[cur].append(line.rstrip())
    return '\n'.join(sec['医院']), '\n'.join(sec['科室']), '\n'.join(sec['医生'])


def join_org_map(hosp, dept, doc):
    """三段映射文本 -> 带 [医院]/[科室]/[医生] 分段的合并框文本"""
    def block(title, body):
        lines = [f"[{title}]"]
        if body and body.strip():
            lines.append(body.rstrip('\n'))
        return '\n'.join(lines)
    return '\n\n'.join([block('医院', hosp), block('科室', dept), block('医生', doc)])


DEFAULT_ORG_MAP = (
    "# 医院 / 科室 / 医生 清洗映射：用 [医院] [科室] [医生] 分段，每段内写 关键字=标准名（每行一条）\n"
    "[医院]\n\n[科室]\n\n[医生]"
)

_init_state()


# ----------------------------------------------------------- 标题
st.title("📊 临床用药洞察看板")
st.caption("导入药房销售明细 → 医生分层 + 机会市场 + 医院/品种/适应症多维分析。服务器端 pandas 计算，几万行秒级完成。")

# ========================================================= 1. 数据导入
st.header("1. 数据导入")
up = st.file_uploader("上传销售明细（Excel / CSV）", type=['xlsx', 'xls', 'csv'])

if up is not None:
    try:
        if up.name.lower().endswith('.csv'):
            raw = pd.read_csv(up)
        else:
            raw = pd.read_excel(up)
        prev = st.session_state.get('raw_df')
        # 仅当表结构变化时重置自动导入，避免覆盖用户已编辑的映射
        if prev is None or list(prev.columns) != list(raw.columns):
            st.session_state.pop('_auto_imported', None)
        st.session_state.raw_df = raw
    except Exception as e:
        st.error(f"读取失败：{e}")

raw_df = st.session_state.raw_df
if raw_df is None:
    st.info("请上传销售明细（Excel / CSV）。数据仅在本次会话的服务器内存中计算，不会外发。")
    st.stop()

st.success(f"已读取 {len(raw_df):,} 行 · 列：{'、'.join(map(str, raw_df.columns))}")

# ========================================================= 2. 数据清洗映射
st.header("2. 数据清洗映射（先清洗，再选适应症范围）")
auto = C.auto_map_columns(list(raw_df.columns))
st.caption("系统已按列名自动识别标准字段（销售日期/医疗单位/处方科室/处方医生/通用名/适应症/销量数量），无需手动映射列。"
           "请在下方对原始值做归并清洗：**关键字=标准名，每行一条**；同一标准名可有多条关键字。"
           "把同一适应症的多种写法归到一个标准名，医院 / 科室 / 医生同理（医生按 医院+医生 区分同名）。")

missing = [r for r in C.REQ if r not in auto]
if missing:
    st.error("自动识别未找到以下必填字段，请核对上传文件的列名：" + "、".join(missing)
             + "\n当前列：" + "、".join(map(str, raw_df.columns))
             + "\n建议列名：销售日期、医疗单位、处方科室、处方医生、通用名、适应症、销量数量（或含别名如 销售时间/医院/科室/医生 等）。")
    st.stop()

df_std = C.apply_mapping(raw_df, auto)

# 首次载入自动导入数据出现的全部值（仅补未覆盖）：适应症单独，机构/人员按分段写入合并框
if not st.session_state.get('_auto_imported'):
    st.session_state.map_ind = _import_all_values(df_std, '适应症', st.session_state.map_ind, is_ind=True)
    _h0, _d0, _c0 = split_org_map(st.session_state.map_org)
    _h1 = _import_all_values(df_std, '医疗单位', _h0)
    _d1 = _import_all_values(df_std, '处方科室', _d0)
    _c1 = _import_all_values(df_std, '处方医生', _c0)
    st.session_state.map_org = join_org_map(_h1, _d1, _c1)
    st.session_state['_auto_imported'] = True

mc = st.columns(2)
with mc[0]:
    st.markdown("**适应症归并映射**（如 皮科/关节/消化 → 标准适应症）")
    st.session_state.map_ind = st.text_area(
        "适应症映射", value=st.session_state.map_ind, height=320, label_visibility="collapsed",
        help="关键字=标准名，每行一条；同一标准名可有多条关键字。")
    if st.button("重置适应症映射为默认", key="reset_ind"):
        st.session_state.map_ind = C.DEFAULT_IND_MAP
        st.rerun()
with mc[1]:
    st.markdown("**医院 / 科室 / 医生 清洗映射**（用 `[医院]` `[科室]` `[医生]` 分段，"
                "每段内写 关键字=标准名，每行一条；医生按 医院+医生 区分同名，如 张伟(主任) = 张伟）")
    st.session_state.map_org = st.text_area(
        "机构与人员映射", value=st.session_state.map_org, height=320, label_visibility="collapsed")
    if st.button("重置机构/人员映射为默认", key="reset_org"):
        st.session_state.map_org = DEFAULT_ORG_MAP
        st.rerun()

# 清洗后规模预览（随上方映射实时更新）
_org_h, _org_d, _org_c = split_org_map(st.session_state.map_org)
_im, _dm, _hm, _cm = (C.parse_map(st.session_state.map_ind), C.parse_map(_org_d),
                      C.parse_map(_org_h), C.parse_map(_org_c))
_n_ind = df_std['适应症'].map(lambda v: C._normalize_value(v, _im, True)).nunique()
_n_hosp = df_std['医疗单位'].map(lambda v: C._normalize_value(v, _hm)).nunique()
_n_dept = df_std['处方科室'].map(lambda v: C._normalize_value(v, _dm)).nunique()
_n_doc = df_std['处方医生'].map(lambda v: C._normalize_value(v, _cm)).nunique()
st.caption(f"清洗后规模预览：适应症 **{_n_ind}** 类 · 医院 **{_n_hosp}** 个 · 科室 **{_n_dept}** 个 · 医生 **{_n_doc}** 位（随上方映射实时更新）。"
           "确认无误后，下一步的「适应症范围」会自动基于清洗后的标准名刷新。")

# ========================================================= 3. 计算配置
st.header("3. 计算配置")

# 归一化后的候选值（供下拉/多选）
ind_map = _im
dept_map = _dm
ind_vals = sorted({C._normalize_value(v, ind_map, is_indication=True)
                   for v in df_std['适应症'].dropna().astype(str).unique() if str(v).strip() != ''})
dept_vals = sorted({C._normalize_value(v, dept_map)
                    for v in df_std['处方科室'].dropna().astype(str).unique() if str(v).strip() != ''})
prod_vals = sorted({str(v) for v in df_std['通用名'].dropna().astype(str).unique() if str(v).strip() != ''})

cc = st.columns(5)
with cc[0]:
    start_ym = st.text_input("起始年月 (YYYY-MM)", value="2026-01")
with cc[1]:
    end_ym = st.text_input("结束年月 (YYYY-MM)", value="2026-06")
with cc[2]:
    own_product = st.selectbox("分析品种（本品）", prod_vals, index=0 if prod_vals else None)
with cc[3]:
    dept_filter = st.selectbox("科室范围", ['全部科室'] + dept_vals)
with cc[4]:
    top_pct = st.number_input("高值分位阈值(%)", min_value=5, max_value=50, value=30, step=5)

inds_sel = st.multiselect("适应症范围（归并后标准名，可多选；不选=全部）", ind_vals, default=[])
comp_candidates = [p for p in prod_vals if p != own_product]
comps_sel = st.multiselect("竞品范围（机会市场参考品种，默认全部竞品）", comp_candidates, default=comp_candidates)
dedup_sel = st.multiselect("去重患者字段（组合键 AND：所选字段全部相同才算同一人）", C.DEDUP_OPTS, default=['oneid'])
mask_on = st.checkbox("对医生 / 科室 / 医院名称脱敏（对外展示用）", value=False)

if not dedup_sel:
    st.error("请至少选择一个去重患者字段。")
    st.stop()

cfg = {
    'startYM': start_ym.strip(), 'endYM': end_ym.strip(),
    'ownProduct': own_product,
    'indications': inds_sel,
    'topPct': top_pct / 100.0,
    'dedupFields': dedup_sel,
    'competitors': comps_sel,
    'indicationMap': st.session_state.map_ind,
    'deptMap': _org_d,
    'hospMap': _org_h,
    'docMap': _org_c,
    'dept': None if dept_filter == '全部科室' else dept_filter,
}

run = st.button("▶ 运行分析", type="primary", width='stretch')
if not run and not st.session_state.get('_has_run'):
    st.stop()
st.session_state['_has_run'] = True

# 计算（缓存：df + cfg 不变则不重算）
buf = io.BytesIO()
df_std.to_pickle(buf)
cfg_key = str(sorted([(k, str(v)) for k, v in cfg.items()]))
try:
    res = cached_run(buf.getvalue(), cfg_key, cfg)
except Exception as e:
    st.error(f"计算失败：{e}")
    st.stop()


def mask_df(df, cols):
    if not mask_on or df.empty:
        return df
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = df[c].map(C.mask_name)
    return df


# ========================================================= 4. 医生分层
m = res['_meta']
st.header("4. 医生分层（本品 · 四象限）")
st.caption(f"诊断：读取 {m['total']:,} 行 → 窗口内 {m['window']:,} 行 → 本品({m['ownProduct']}) {m['own']:,} 行 "
           f"→ 去重后医生 {len(res['doctors'])} 人（适应症：{m['indication']} · 科室：{m['dept']}）")

cnt = res['counts']
kc = st.columns(4)
kc[0].metric("意见领袖", cnt['opinion'])
kc[1].metric("学术支持对象", cnt['support'])
kc[2].metric("潜力医生", cnt['potential'])
kc[3].metric("待评估", cnt['low'])

docs = res['doctors']
if not docs.empty:
    docs_disp = mask_df(docs, ['hospital', 'dept', 'doctor'])
    fig = go.Figure()
    for q, color in QUAD_COLORS.items():
        sub = docs_disp[docs_disp['quadrant'] == q]
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub['patients'], y=sub['dot'], mode='markers', name=q,
            marker=dict(color=color, size=10, opacity=0.7),
            text=[f"{r.doctor}<br>{r.hospital} / {r.dept}<br>患者 {r.patients} 人 · DOT {r.dot}"
                  for r in sub.itertuples()],
            hovertemplate="%{text}<extra></extra>",
        ))
    fig.update_layout(
        xaxis_title="患者数", yaxis_title="DOT（人均盒数）",
        height=440, legend=dict(orientation="h", yanchor="bottom", y=1.02),
        margin=dict(l=50, r=30, t=40, b=40),
    )
    st.plotly_chart(fig, width='stretch')

    tbl = docs_disp.sort_values('patients', ascending=False)[['doctor', 'hospital', 'dept', 'patients', 'dot', 'quadrant']]
    tbl.columns = ['医生', '医院', '科室', '患者数', 'DOT', '分层']
    st.dataframe(tbl, width='stretch', hide_index=True)
else:
    st.info("当前筛选下无本品医生数据。")

# ========================================================= 5. 机会市场
st.header("5. 机会市场（同适应症）")
scope = st.radio("范围", ['含本品整体（本品+所选竞品）', '未含本品（仅所选竞品）'], horizontal=True, label_visibility="collapsed")
excl = scope.startswith('未含')
mk = res['marketExcl'] if excl else res['marketIncl']
st.info(res['summaryExcl'] if excl else res['summaryIncl'])
if not mk.empty:
    mk_disp = mask_df(mk, ['hospital', 'dept', 'doctor'])[
        ['doctor', 'hospital', 'dept', 'boxes', 'patients', 'prevBoxes', 'delta', 'trend']]
    mk_disp.columns = ['医生', '医院', '科室', '盒数', '患者', '上期盒', '变化', '趋势']
    st.dataframe(mk_disp, width='stretch', hide_index=True)

# ========================================================= 6. 医院与品种分析
st.header("6. 医院与品种分析")
st.caption("尊重当前时间窗口 / 适应症 / 科室筛选，展示范围内全部品种（含本品与竞品）的医院与医生分布。")

hospitals = res['hospitals']
if hospitals.empty:
    st.info("当前筛选下无医院数据。")
else:
    hc = st.columns([1, 1, 2])
    with hc[0]:
        topn = st.selectbox("TOP 医院数量", [3, 5, 10, 9999], format_func=lambda x: '全部' if x == 9999 else str(x))
    with hc[1]:
        sort_key = st.selectbox("排序维度", ['patients', 'boxes', 'dot'],
                                format_func=lambda x: {'patients': '患者数', 'boxes': '销量(盒)', 'dot': 'DOT'}[x])
    with hc[2]:
        hosp_names = list(hospitals['hospital'])
        drill_disp = st.selectbox("下钻医院（医生×品种明细 + 该院占比）",
                                  ['全部医院'] + [C.mask_name(h) if mask_on else h for h in hosp_names])
    # 还原下钻真实名
    drill = None
    if drill_disp != '全部医院':
        for h in hosp_names:
            if (C.mask_name(h) if mask_on else h) == drill_disp:
                drill = h
                break

    sort_name = {'patients': '患者数', 'boxes': '销量', 'dot': 'DOT'}[sort_key]
    sorted_h = hospitals.sort_values(sort_key, ascending=False)
    top = sorted_h.head(topn)
    top_set = set(top['hospital'])

    sc = st.columns(4)
    sc[0].metric("医院数", len(hospitals))
    sc[1].metric(f"TOP{topn if topn != 9999 else '全'} 患者合计", int(top['patients'].sum()))
    sc[2].metric(f"TOP{topn if topn != 9999 else '全'} 销量合计", round(float(top['boxes'].sum()), 2))
    sc[3].metric("品种数(范围)", len(res['products']))

    gcol = st.columns([2, 1])
    with gcol[0]:
        disp_names = [C.mask_name(h) if mask_on else h for h in top['hospital']]
        barfig = go.Figure(go.Bar(x=disp_names, y=list(top[sort_key]), marker_color='#2563eb',
                                  text=list(top[sort_key]), textposition='outside'))
        barfig.update_layout(title=f"TOP 医院（按{sort_name}）", height=380, yaxis_title=sort_name,
                             margin=dict(l=40, r=20, t=50, b=80), xaxis_tickangle=-30)
        st.plotly_chart(barfig, width='stretch')
    with gcol[1]:
        if drill:
            hp = res['hospitalProducts']
            prod_rows = hp[hp['hospital'] == drill].copy()
            tot = prod_rows['boxes'].sum() or 1
            prod_rows['share'] = (prod_rows['boxes'] / tot * 100).round(1)
            prod_title = drill_disp
        else:
            prod_rows = res['products'].copy()
            prod_title = '全部范围'
        prod_rows = prod_rows.sort_values('boxes', ascending=False)
        colors = [OWN_RED if p == res['ownProduct'] else PIE_PALETTE[i % len(PIE_PALETTE)]
                  for i, p in enumerate(prod_rows['product'])]
        piefig = go.Figure(go.Pie(labels=list(prod_rows['product']), values=list(prod_rows['boxes']),
                                  hole=0.4, marker=dict(colors=colors)))
        piefig.update_layout(title=f"品种占比 · {prod_title}", height=380, margin=dict(l=10, r=10, t=50, b=30))
        st.plotly_chart(piefig, width='stretch')

    pt = prod_rows[['product', 'boxes', 'share', 'patients', 'dot']].copy()
    pt['product'] = pt['product'].map(lambda p: f"{p}（本品）" if p == res['ownProduct'] else p)
    pt.columns = ['品种', '销量(盒)', '占比%', '患者数', 'DOT']
    st.dataframe(pt, width='stretch', hide_index=True)

    # 适应症占比
    st.subheader("各适应症占比分布")
    if drill:
        hi = res['hospitalIndications']
        ind_rows = hi[hi['hospital'] == drill].copy()
        ind_title = drill_disp
    else:
        ind_rows = res['indications'].copy()
        ind_title = '全部范围'
    ind_rows = ind_rows.sort_values('boxes', ascending=False)
    if not ind_rows.empty:
        icol = st.columns([1, 1])
        with icol[0]:
            icolors = [IND_PALETTE[i % len(IND_PALETTE)] for i in range(len(ind_rows))]
            ipie = go.Figure(go.Pie(labels=list(ind_rows['indication']), values=list(ind_rows['boxes']),
                                    hole=0.4, marker=dict(colors=icolors)))
            ipie.update_layout(title=f"适应症占比 · {ind_title}", height=360, margin=dict(l=10, r=10, t=50, b=30))
            st.plotly_chart(ipie, width='stretch')
        with icol[1]:
            it = ind_rows[['indication', 'boxes', 'share', 'patients', 'dot']].copy()
            it.columns = ['适应症', '销量(盒)', '占比%', '患者数', 'DOT']
            st.dataframe(it, width='stretch', hide_index=True)

    # 明细
    st.subheader("医院 - 医生 - 品种 - 适应症 明细")
    detail = res['detail']
    if drill:
        dd = detail[detail['hospital'] == drill]
        detail_title = f"医院：{drill_disp}"
    else:
        dd = detail[detail['hospital'].isin(top_set)]
        detail_title = f"TOP {topn if topn != 9999 else '全部'} 医院合计"
    dd_disp = mask_df(dd, ['hospital', 'doctor'])
    limit = 1000
    st.caption(f"{detail_title} · 共 {len(dd_disp):,} 行" + (f"（仅显示前 {limit} 行）" if len(dd_disp) > limit else ""))
    show = dd_disp.head(limit)[['hospital', 'doctor', 'product', 'indication', 'boxes', 'patients', 'dot']].copy()
    show.columns = ['医院', '医生', '品种', '适应症', '销量(盒)', '患者数', 'DOT']
    st.dataframe(show, width='stretch', hide_index=True)

# ========================================================= 导出
st.divider()


def build_excel():
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='xlsxwriter') as w:
        d = res['doctors']
        if not d.empty:
            dd = d[['doctor', 'hospital', 'dept', 'patients', 'dot', 'quadrant']].copy()
            dd.columns = ['医生', '医院', '科室', '患者数', 'DOT', '分层']
            dd.to_excel(w, sheet_name='医生分层', index=False)
        mi = res['marketIncl'].copy()
        me = res['marketExcl'].copy()
        parts = []
        for label, dfp in [('含本品', mi), ('未含本品', me)]:
            if not dfp.empty:
                t = dfp[['doctor', 'hospital', 'dept', 'boxes', 'patients', 'prevBoxes', 'delta', 'trend']].copy()
                t.insert(0, '范围', label)
                parts.append(t)
        if parts:
            mk = pd.concat(parts, ignore_index=True)
            mk.columns = ['范围', '医生', '医院', '科室', '盒数', '患者', '上期盒', '变化', '趋势']
            mk.to_excel(w, sheet_name='机会市场', index=False)
        if not res['hospitals'].empty:
            h = res['hospitals'][['hospital', 'patients', 'boxes', 'dot', 'doctors', 'products']].copy()
            h.columns = ['医院', '患者数', '销量盒', 'DOT', '医生数', '品种数']
            h.to_excel(w, sheet_name='医院排行', index=False)
        if not res['products'].empty:
            p = res['products'][['product', 'boxes', 'share', 'patients', 'dot']].copy()
            p.columns = ['品种', '销量盒', '占比', '患者数', 'DOT']
            p.to_excel(w, sheet_name='品种占比', index=False)
        if not res['indications'].empty:
            ind = res['indications'][['indication', 'boxes', 'share', 'patients', 'dot']].copy()
            ind.columns = ['适应症', '销量盒', '占比', '患者数', 'DOT']
            ind.to_excel(w, sheet_name='适应症占比', index=False)
        if not res['detail'].empty:
            hdp = res['detail'][['hospital', 'doctor', 'product', 'indication', 'boxes', 'patients', 'dot']].copy()
            hdp.columns = ['医院', '医生', '品种', '适应症', '销量盒', '患者数', 'DOT']
            hdp.to_excel(w, sheet_name='医院医生品种适应症', index=False)
    return out.getvalue()


st.download_button("⬇ 导出结果 (Excel)", data=build_excel(),
                   file_name="临床用药洞察_结果.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   width='stretch')
