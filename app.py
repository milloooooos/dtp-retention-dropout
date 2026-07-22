# -*- coding: utf-8 -*-
"""
DTP 患者服务 · 自助分析 Web App (Streamlit)
上传 销售底表 + 随访任务表 → 自动计算 留存率 / 脱落率A / 脱落率B / 跨表脱落原因。
详见 README.md。本地运行：streamlit run app.py
"""
import io, os, tempfile
import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import dtp_engine as E

st.set_page_config(page_title='DTP 留存率/脱落率 自助分析', layout='wide')
st.title('💊 DTP 患者服务 · 留存率 / 脱落率 自助分析')
st.caption('销售底表 + 随访任务表 → 留存率Cohort · 脱落率A(滚动) · 脱落率B(累计沉默) · 跨表脱落原因(可控/不可控)')

# ---------------- 侧边栏参数 ----------------
with st.sidebar:
    st.header('⚙️ 分析参数')
    max_k = st.slider('留存追踪月数 (M1~Mk)', 3, 18, 12, help='追踪新患 cohort 后续多少个月的留存')
    mult = st.slider('脱落率B 倍数 (×用药间隔)', 2, 6, 3, help='末次购药距终点 > N×用药间隔 判为真停药')
    with_pat = st.checkbox('输出「近似患者级」跨表关联', value=True,
                           help='按 姓名+品种+首购月 近似匹配销售脱落患者与随访原因；同名歧义大，置信度低，仅供参考')
    st.divider()
    run = st.button('🚀 运行分析', type='primary', use_container_width=True)

# ---------------- 文件上传 ----------------
sales_file = st.file_uploader('① 销售底表（xlsx/xls，含 商品名称/销售时间/销售数量/患者ID 等）',
                              type=['xlsx', 'xls'], help='DTP 药房购药流水')
followup_files = st.file_uploader('② 随访任务表（xls，可多选：2025H1 + 2026H1）',
                                   type=['xlsx', 'xls'], accept_multiple_files=True,
                                   help='患者随访系统导出的历史任务，用于取脱落原因')

if not sales_file:
    st.info('请先上传「销售底表」，再点击「运行分析」。随访表可选——不上传也能算留存率与脱落率，只是没有跨表原因。')
    st.stop()

# ---------------- 保存临时文件 ----------------
def save_tmp(uploaded):
    suffix = os.path.splitext(uploaded.name)[1]
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'wb') as f:
        f.write(uploaded.getbuffer())
    return path

sales_path = save_tmp(sales_file)
followup_paths = [save_tmp(f) for f in followup_files] if followup_files else None

if not run:
    st.stop()

# ---------------- 运行 ----------------
with st.spinner('计算中…'):
    try:
        sales = E.load_sales(sales_path)
    except Exception as e:
        st.error(f'销售底表读取失败：{e}\n请检查列名是否含 商品名称/销售时间/销售数量/患者ID（或等价别名）。')
        st.stop()
    followup = None
    if followup_paths:
        try:
            followup = E.load_followup(followup_paths)
        except Exception as e:
            st.warning(f'随访表读取失败，将仅输出留存率/脱落率（无跨表原因）：{e}')
            followup = None
    res = E.run_analysis(sales, followup, max_k=max_k, mult=mult, with_patient_crossref=with_pat)

st.success(f'✅ 销售 {len(sales):,} 行 / 患者 {sales["患者ID"].nunique():,} / '
           f'{sales["销售时间"].min().date()}~{sales["销售时间"].max().date()}'
           + (f' ｜ 随访 {len(followup):,} 行' if followup is not None else ' ｜ 无随访表'))

# ---------------- 标签页展示 ----------------
tabs = st.tabs(['📈 留存率', '🔁 脱落率A', '💤 脱落率B', '🔗 跨表原因', '🎯 行动建议', '📋 说明'])

# ===== 留存率 =====
def _retention_heatmap(r, title):
    cols = [c for c in r.columns if '留存' in c]
    fig, ax = plt.subplots(figsize=(min(2 + len(cols), 16), max(4, len(r) * 0.28)))
    data = r[cols].fillna(np.nan).values.astype(float)
    im = ax.imshow(data, aspect='auto', cmap='YlGnBu', vmin=0, vmax=100)
    ax.set_xticks(range(len(cols))); ax.set_xticklabels(cols, rotation=45, ha='right')
    ax.set_yticks(range(len(r))); ax.set_yticklabels(r['首购月'])
    for i in range(len(r)):
        for j in range(len(cols)):
            v = data[i, j]
            if not np.isnan(v):
                ax.text(j, i, f'{v:.0f}', ha='center', va='center', fontsize=7,
                        color='white' if v < 50 else 'black')
    ax.set_title(title)
    fig.colorbar(im, ax=ax, fraction=0.025)
    return fig

with tabs[0]:
    st.subheader('新患留存率 Cohort（按首购月分群）· 两套口径')
    cal = st.radio('留存口径', ['口径1｜仅看购药时间', '口径2｜结合说明书盒数覆盖'],
                   horizontal=True, key='ret_cal')
    if cal.startswith('口径1'):
        st.caption('Mk留存% = 该首购月新患中，在第 k 月「当月有购药」的占比。多盒购买会使曲线非单调递减（囤3盒者次月不买、第3月才回）；近期 cohort 窗口不足显示为空。')
        r = res['retention_overall']; rb = res['retention_by_brand']; ttl = '留存率(仅购药时间) 热力图 (%)'
    else:
        st.caption('Mk留存% = 每次购药覆盖=销售数量×每盒天数(说明书) 天，覆盖区间与第 k 月有交集即算留存。把囤药者的“在治”计入，曲线更平滑更单调，更贴近真实在治率。')
        r = res['retention_cov_overall']; rb = res['retention_cov_by_brand']; ttl = '留存率(盒数覆盖) 热力图 (%)'
    st.pyplot(_retention_heatmap(r, ttl))
    st.dataframe(r, use_container_width=True, height=420)
    st.subheader('分品种留存率')
    st.dataframe(rb, use_container_width=True, height=300)
    with st.expander('📊 两套口径差值（口径2−口径1，差越大=囤药/断续购药越明显）'):
        a = res['retention_overall'].set_index('首购月')
        b = res['retention_cov_overall'].set_index('首购月')
        common = [c for c in a.columns if c in b.columns and '留存' in c]
        diff = (b[common] - a[common]).round(1).reset_index()
        st.dataframe(diff, use_container_width=True, height=300)

# ===== 脱落率A =====
with tabs[1]:
    st.subheader('脱落率 A（滚动口径）')
    st.caption('基准月 M-2 有购药、观察窗 M-1∪M 无购药 → 脱落。月度口径。')
    da = res['dropout_A']['整体_月度']
    da = da.dropna(subset=['脱落率A'])
    chart = da.copy(); chart['脱落率A%'] = (chart['脱落率A'] * 100).round(1)
    st.line_chart(chart.set_index('基准月')['脱落率A%'])
    st.dataframe(da.assign(脱落率A=(da['脱落率A'] * 100).round(1)), use_container_width=True)
    if '分品种_月度' in res['dropout_A']:
        st.subheader('分品种 · 月度脱落率A')
        st.dataframe(res['dropout_A']['分品种_月度'].assign(脱落率A=(res['dropout_A']['分品种_月度']['脱落率A'] * 100).round(1)),
                     use_container_width=True, height=300)

# ===== 脱落率B =====
with tabs[2]:
    st.subheader('脱落率 B（累计沉默口径）')
    st.caption('末次购药距数据终点 > 3×用药间隔 → 真停药。「已观察%」仅统计首购在终点前 3×间隔内的患者，剔除新近右删失。')
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('**全部患者**'); st.dataframe(res['dropout_B_by_brand'], use_container_width=True)
    with c2:
        st.markdown('**已观察患者（剔除右删失）'); st.dataframe(res['dropout_B_established_by_brand'], use_container_width=True)

# ===== 跨表原因 =====
with tabs[3]:
    if 'crossref_brand' in res:
        st.subheader('跨表关联 · 品牌层面（可靠）')
        st.caption('左：销售算出的各品种脱落B人数/率；右：随访表各品种脱落原因的可控占比。两表无共同患者主键，品牌层面关联为主。')
        st.dataframe(res['crossref_brand'], use_container_width=True)
        if 'crossref_patient' in res:
            cp = res['crossref_patient']
            m = cp.attrs.get('matched', 0); t = cp.attrs.get('total', 0)
            st.subheader(f'跨表关联 · 近似患者级（置信度低，匹配 {m}/{t} = {m / max(t, 1) * 100:.0f}%）')
            st.caption('按 姓名+品种+首购月 近似匹配；同名歧义大，仅供线索排查，不作为统计依据。')
            st.dataframe(cp, use_container_width=True, height=360)
    else:
        st.info('未上传随访表，无跨表脱落原因。上传随访任务表后可看可控/不可控构成。')

# ===== 行动建议 =====
with tabs[4]:
    st.subheader('可控脱落 → 行动建议（参考映射）')
    st.dataframe(res['action_map'], use_container_width=True)

# ===== 说明 =====
with tabs[5]:
    st.subheader('口径与边界说明')
    st.markdown('''
    - **留存率（两套口径）**：按首购月分群（=新患队列）。
      - *口径1 仅看购药时间*：Mk = 第 k 月当月有购药占比。多盒购买可使其非单调递减；近期 cohort 窗口不足为空（右删失）。
      - *口径2 结合说明书盒数覆盖*：每次购药覆盖=销售数量×每盒天数，覆盖区间与第 k 月相交即算留存。曲线更平滑单调，反映真实在治率；两套差值≈囤药/断续购药强度。
    - **品牌识别（自丰富映射表）**：药名先按品牌字匹配，未含品牌字则查 `brand_aliases.json`（通用名→品牌），并从数据自动学习新别名写回；仍识别不出的记入 unresolved 供人工补录。
    - **脱落率A（滚动）**：基准月 M-2 有购药、M-1∪M 无购药即脱落。月度口径，适合看趋势。
    - **脱落率B（累计沉默）**：末次购药距数据终点 > N×用药间隔 → 真停药。新近患者（首购在终点前 N×间隔内）免判，已观察% 更干净。
    - **跨表关联**：销售脱落患者 ↔ 随访脱落原因（可控/不可控）。两表**无共同患者主键**（会员号格式不同），故：
      ① 品牌层面关联为主（可靠）；② 近似患者级（姓名+品种+首购月）仅作线索，同名歧义大。
    - **脱落映射**：原因文本 → 可控/不可控 关键词分类（见 dtp_engine.classify_reason）。
    - **数据边界**：随访表若无某品种（如本例优赫得=0行），跨表该品种原因空，属覆盖缺口非错误。
    ''')
    st.info('分析引擎：dtp_engine.py ｜ 本页所有数字均可经「下载Excel」导出复核。')

# ---------------- 下载 Excel ----------------
st.divider()
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine='openpyxl') as xw:
    res['retention_overall'].to_excel(xw, sheet_name='1_留存率口径1_仅购药_整体', index=False)
    res['retention_by_brand'].to_excel(xw, sheet_name='2_留存率口径1_仅购药_分品种', index=False)
    res['retention_cov_overall'].to_excel(xw, sheet_name='1b_留存率口径2_覆盖_整体', index=False)
    res['retention_cov_by_brand'].to_excel(xw, sheet_name='2b_留存率口径2_覆盖_分品种', index=False)
    res['dropout_A']['整体_月度'].to_excel(xw, sheet_name='3_脱落率A_整体', index=False)
    if '分品种_月度' in res['dropout_A']:
        res['dropout_A']['分品种_月度'].to_excel(xw, sheet_name='4_脱落率A_分品种', index=False)
    res['dropout_B_by_brand'].to_excel(xw, sheet_name='5_脱落率B_分品种', index=False)
    res['dropout_B_established_by_brand'].to_excel(xw, sheet_name='5b_脱落率B_已观察', index=False)
    if 'crossref_brand' in res:
        res['crossref_brand'].to_excel(xw, sheet_name='6_跨表关联_品牌', index=False)
    if 'crossref_patient' in res:
        res['crossref_patient'].to_excel(xw, sheet_name='7_跨表关联_患者级', index=False)
    res['action_map'].to_excel(xw, sheet_name='8_行动建议', index=False)
buf.seek(0)
st.download_button('⬇️ 下载完整 Excel 报告', buf, 'DTP_留存率与脱落分析.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
