# -*- coding: utf-8 -*-
"""
DTP 患者服务 · 自助分析 Web App (Streamlit)
上传 销售底表 + 随访任务表 + 项目药房TOP清单(可选)
→ 自动输出：留存率 / 脱落率A·B / DOT分解 / 新患趋势 / 医院·医生·药房维度 / 沟通清单 / Word+Excel报告
详见 README.md。本地运行：streamlit run app.py
"""
import io, os, tempfile
import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import dtp_engine as E

st.set_page_config(page_title='DTP 患者服务 · 完整复盘分析', layout='wide')
st.title('💊 DTP 患者服务 · 完整复盘分析')
st.caption('销售底表 + 随访任务表 → 留存率 · 脱落率 · DOT · 新患 · 医院/医生/药房维度 · 沟通清单 · Word/Excel报告')

# ---------------- 侧边栏参数 ----------------
with st.sidebar:
    st.header('⚙️ 分析参数')
    max_k = st.slider('留存追踪月数 (M1~Mk)', 3, 18, 12, help='追踪新患 cohort 后续多少个月的留存')
    mult = st.slider('脱落率B 倍数 (×用药间隔)', 2, 6, 3, help='末次购药距终点 > N×用药间隔 判为真停药；填3≈药吃完后再空2个周期')
    with_pat = st.checkbox('输出「近似患者级」跨表关联', value=True,
                           help='按 姓名+品种+首购月 近似匹配销售脱落患者与随访原因；同名歧义大，置信度低，仅供参考')
    st.divider()
    run = st.button('🚀 运行分析', type='primary', use_container_width=True)

# ---------------- 文件上传 ----------------
col1, col2, col3 = st.columns(3)
with col1:
    sales_file = st.file_uploader('① 销售底表（xlsx/xls）',
                                  type=['xlsx', 'xls'], help='必须含 商品名称/销售时间/销售数量/患者ID/药房名称/处方医生/医疗单位')
with col2:
    followup_files = st.file_uploader('② 随访任务表（xls，可多选）',
                                      type=['xlsx', 'xls'], accept_multiple_files=True,
                                      help='患者随访系统导出的历史任务，用于取脱落原因')
with col3:
    top_file = st.file_uploader('③ 项目药房TOP清单（可选）',
                                type=['xlsx', 'xls'],
                                help='含 药房名称 + 各品种TOP/项目/重点列 + 城市；不上传则默认全部药房为项目')

if not sales_file:
    st.info('请先上传「销售底表」，再点击「运行分析」。随访表与TOP清单可选。')
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
top_path = save_tmp(top_file) if top_file else None

if not run:
    st.stop()

# ---------------- 运行 ----------------
with st.spinner('计算中…'):
    try:
        tier = E.load_pharmacy_tier(top_path) if top_path else None
        sales = E.load_sales(sales_path, tier=tier)
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

sales_info = (f'销售 {len(sales):,} 行 / 患者 {sales["患者ID"].nunique():,} / '
              f'{sales["销售时间"].min().date()}~{sales["销售时间"].max().date()}'
              + (f' ｜ 随访 {len(followup):,} 行' if followup is not None else ' ｜ 无随访表'))
st.success(sales_info)

# ---------------- 标签页展示 ----------------
tabs = st.tabs(['📈 留存率', '🔁 脱落率A', '💤 脱落率B', '📊 DOT与新患',
                '🏥 医院维度', '👨‍⚕️ 医生维度', '🏪 项目药房', '🔗 跨表原因',
                '🎯 沟通改进', '📋 说明'])

# ===== 留存率 =====
with tabs[0]:
    st.subheader('新患留存率 Cohort（按首购月分群）· 两套口径')
    cal = st.radio('留存口径', ['口径1｜仅看购药时间', '口径2｜结合说明书盒数覆盖'],
                   horizontal=True, key='ret_cal')
    if cal.startswith('口径1'):
        st.caption('Mk留存% = 该首购月新患中，在第 k 月「当月有购药」的占比。多盒购买会使曲线非单调递减；近期 cohort 窗口不足显示为空。')
        r = res['retention_overall']; rb = res['retention_by_brand']
    else:
        st.caption('Mk留存% = 每次购药覆盖=销售数量×每盒天数(说明书) 天，覆盖区间与第 k 月有交集即算留存。曲线更平滑更单调，更贴近真实在治率。')
        r = res['retention_cov_overall']; rb = res['retention_cov_by_brand']
    st.dataframe(r, use_container_width=True, height=420)
    st.subheader('分品种留存率')
    st.dataframe(rb, use_container_width=True, height=300)
    st.subheader('分品种 × 药房 留存率（定位差药房）')
    rbp = res.get('retention_by_brand_pharmacy')
    if rbp is not None and not rbp.empty:
        brands = sorted(rbp['品牌'].unique())
        selb = st.selectbox('选择品种查看各药房留存率（口径1）', brands, key='ret_pharm')
        sub = rbp[rbp['品牌'] == selb].copy()
        if 'M3留存%' in sub.columns:
            sub = sub.sort_values('M3留存%')
        st.caption('按 M3 留存率升序，越靠上 = 该品种下留存越差的药房，优先排查。')
        st.dataframe(sub, use_container_width=True, height=360)
    else:
        st.info('销售底表无「药房名称」列，无法分药房拆分。')
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
        st.markdown('**已观察患者（剔除右删失）**'); st.dataframe(res['dropout_B_established_by_brand'], use_container_width=True)

# ===== DOT与新患 =====
with tabs[3]:
    st.subheader('DOT 分解：老患 vs 新患窗口 vs 重叠患者')
    st.caption('DOT = 窗口内购买总盒数 / 去重患者数（盒/人）。本期窗口 vs 同比窗口，区分真实持续性改善与患者构成污染。')
    st.dataframe(res['dot_decomposition'], use_container_width=True, height=360)
    c1, c2 = st.columns(2)
    with c1:
        st.subheader('新患月度趋势')
        st.line_chart(res['new_patient_monthly'].set_index('月份'))
    with c2:
        st.subheader('老患多盒行为')
        st.dataframe(res['old_patient_multi_box'], use_container_width=True, height=300)
    st.subheader('复购率分解')
    st.dataframe(res['repurchase_decomposition'], use_container_width=True, height=260)
    st.subheader('项目药房新患变化（本期 vs 上期）')
    st.dataframe(res['new_patient_pharmacy_decline'], use_container_width=True, height=300)

# ===== 医院维度 =====
with tabs[4]:
    st.subheader('医院维度（医疗单位）')
    st.caption('按品种+医院聚合患者数、DOT、复购率、脱落率A、新患占比，识别高脱落/低DOT医院。')
    st.dataframe(res['hospital_dimension'], use_container_width=True, height=420)

# ===== 医生维度 =====
with tabs[5]:
    st.subheader('医生维度')
    st.caption('各项目药房下处方医生的患者量与 DOT；低DOT重点医生已按风险判定标注。')
    st.markdown('**低 DOT 重点医生**')
    st.dataframe(res['doctor_low_dot_watch'], use_container_width=True, height=320)
    with st.expander('各药房最大患者量医生'):
        st.dataframe(res['doctor_top1'], use_container_width=True, height=300)
    with st.expander('各药房 TOP5 医生'):
        st.dataframe(res['doctor_top5'], use_container_width=True, height=300)

# ===== 项目药房 =====
with tabs[6]:
    st.subheader('项目药房维度')
    st.caption('识别大店低DOT、小店低DOT、正常门店。')
    st.dataframe(res['pharmacy_dimension'], use_container_width=True, height=420)
    st.subheader('钻取：异常医院拖后腿医生')
    st.dataframe(res['drill_hospital_doctor'], use_container_width=True, height=300)

# ===== 跨表原因 =====
with tabs[7]:
    if 'crossref_brand' in res:
        st.subheader('跨表关联 · 品牌层面（可靠）')
        st.caption('左：销售算出的各品种脱落B人数/率；右：随访表各品种脱落原因的可控占比。')
        st.dataframe(res['crossref_brand'], use_container_width=True)
        if 'crossref_patient' in res:
            cp = res['crossref_patient']
            m = cp.attrs.get('matched', 0); t = cp.attrs.get('total', 0)
            st.subheader(f'跨表关联 · 近似患者级（置信度低，匹配 {m}/{t} = {m / max(t, 1) * 100:.0f}%）')
            st.caption('按 姓名+品种+首购月 近似匹配；同名歧义大，仅供线索排查。')
            st.dataframe(cp, use_container_width=True, height=360)
        if 'dropout_reason_by_pharmacy' in res:
            st.subheader('脱落原因 × 药房 × 品种（汇总·已匹配患者）')
            st.caption('仅统计成功匹配到随访原因的脱落患者；原因类=可控/不可控/其他。用于把改进措施落到具体药房品种。')
            st.dataframe(res['dropout_reason_by_pharmacy'], use_container_width=True, height=320)
    else:
        st.info('未上传随访表，无跨表脱落原因。')

# ===== 沟通改进 =====
with tabs[8]:
    st.subheader('半年最该沟通清单')
    st.dataframe(res['communication_list'], use_container_width=True, height=360)
    st.subheader('改进措施与责任')
    st.dataframe(res['improvement_actions'], use_container_width=True, height=300)

# ===== 说明 =====
with tabs[9]:
    st.subheader('口径与边界说明')
    st.markdown('''
    - **留存率（两套口径）**：按首购月分群（=新患队列）。
      - *口径1 仅看购药时间*：Mk = 第 k 月当月有购药占比。
      - *口径2 结合说明书盒数覆盖*：每次购药覆盖=销售数量×每盒天数，覆盖区间与第 k 月相交即算留存。
    - **DOT**：窗口内购买总盒数 ÷ 去重患者数（盒/人），不换算月数；老患=窗口起始前首购，新患窗口=首购落在窗口内。
    - **品牌识别（自丰富映射表）**：药名先按品牌字匹配，未含品牌字则查 `brand_aliases.json`（通用名→品牌），并从数据自动学习新别名。
    - **脱落率A（滚动）**：基准月 M-2 有购药、M-1∪M 无购药即脱落。
    - **脱落率B（累计沉默）**：末次购药距数据终点 > N×用药间隔 → 真停药。新近患者免判，已观察% 更干净。
    - **跨表关联**：两表**无共同患者主键**，品牌层面关联为主；近似患者级仅作线索。
    - **项目药房判定**：优先读取上传的 TOP 清单；无则使用销售底表「角色」列；均无则默认全部药房为项目。
    ''')

# ---------------- 下载 Excel ----------------
st.divider()
dcol1, dcol2 = st.columns(2)
with dcol1:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as xw:
        res['window_info'].to_excel(xw, sheet_name='0_窗口信息', index=False)
        res['retention_overall'].to_excel(xw, sheet_name='1_留存率口径1_仅购药_整体', index=False)
        res['retention_by_brand'].to_excel(xw, sheet_name='2_留存率口径1_仅购药_分品种', index=False)
        res['retention_cov_overall'].to_excel(xw, sheet_name='1b_留存率口径2_覆盖_整体', index=False)
        res['retention_cov_by_brand'].to_excel(xw, sheet_name='2b_留存率口径2_覆盖_分品种', index=False)
        res['retention_by_brand_pharmacy'].to_excel(xw, sheet_name='2c_留存率_分品种×药房', index=False)
        res['dropout_A']['整体_月度'].to_excel(xw, sheet_name='3_脱落率A_整体', index=False)
        if '分品种_月度' in res['dropout_A']:
            res['dropout_A']['分品种_月度'].to_excel(xw, sheet_name='4_脱落率A_分品种', index=False)
        res['dropout_B_by_brand'].to_excel(xw, sheet_name='5_脱落率B_分品种', index=False)
        res['dropout_B_established_by_brand'].to_excel(xw, sheet_name='5b_脱落率B_已观察', index=False)
        if 'crossref_brand' in res:
            res['crossref_brand'].to_excel(xw, sheet_name='6_跨表关联_品牌', index=False)
        if 'dropout_reason_by_pharmacy' in res:
            res['dropout_reason_by_pharmacy'].to_excel(xw, sheet_name='6b_脱落原因_药房×品种', index=False)
        if 'crossref_patient' in res:
            res['crossref_patient'].to_excel(xw, sheet_name='7_跨表关联_患者级', index=False)
        res['action_map'].to_excel(xw, sheet_name='8_行动建议', index=False)
        res['dot_decomposition'].to_excel(xw, sheet_name='10_DOT分解', index=False)
        res['new_patient_monthly'].to_excel(xw, sheet_name='11_新患月度趋势', index=False)
        res['new_patient_pharmacy_decline'].to_excel(xw, sheet_name='12_新患下降最大药房', index=False)
        res['old_patient_multi_box'].to_excel(xw, sheet_name='13_老患多盒行为', index=False)
        res['repurchase_decomposition'].to_excel(xw, sheet_name='14_复购率分解', index=False)
        res['hospital_dimension'].to_excel(xw, sheet_name='15_医院维度', index=False)
        res['doctor_top1'].to_excel(xw, sheet_name='16_医生维度_最大患者量', index=False)
        res['doctor_top5'].to_excel(xw, sheet_name='17_医生维度_TOP5', index=False)
        res['doctor_low_dot_watch'].to_excel(xw, sheet_name='18_医生维度_低DOT重点', index=False)
        res['pharmacy_dimension'].to_excel(xw, sheet_name='19_项目药房_风险', index=False)
        res['drill_hospital_doctor'].to_excel(xw, sheet_name='20_钻取_异常医院拖后腿医生', index=False)
        res['communication_list'].to_excel(xw, sheet_name='21_重点关注清单', index=False)
        res['improvement_actions'].to_excel(xw, sheet_name='22_改进措施与责任', index=False)
    buf.seek(0)
    st.download_button('⬇️ 下载完整 Excel 报告', buf,
                       'DTP_完整复盘分析.xlsx',
                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

with dcol2:
    word_buf = E.generate_word_report(res, sales_info)
    if word_buf:
        st.download_button('⬇️ 下载 Word 复盘报告', word_buf,
                           'DTP_复盘报告.docx',
                           'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    else:
        st.info('Word 报告需 python-docx，如不可用请检查 requirements.txt')
