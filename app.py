# -*- coding: utf-8 -*-
"""
DTP 患者服务 · 自助分析 Web App (Streamlit)
上传 销售底表 + 随访任务表 + 项目药房TOP清单(可选)
→ 自动输出：留存率 / 脱落率A·B / DOT分解 / 新患趋势 / 医院·医生·药房维度 / 沟通清单 / Word+Excel报告
详见 README.md。本地运行：streamlit run app.py
"""
import io, os, tempfile
import datetime, inspect
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
    preset = st.selectbox('时间窗预设', ['H1_2026', 'roll1y', 'full', 'custom'], index=0,
                          format_func=lambda x: {'H1_2026': 'H1 2026（2026-01-01~06-30）',
                                                 'roll1y': '回滚1年（末次数据日往前1年）',
                                                 'full': '全量累计（首笔~末笔销售）',
                                                 'custom': '自定义区间…'}[x],
                          help='整个报告的统一时间窗：留存/脱落率B终点/脱落原因/DOT/新患同比 都按此窗计算。'
                               '选「H1 2026」则脱落率B终点=2026-06-30；选「自定义区间」可任意选起止（支持跨年/不规则长度，'
                               '如 2026-01-01~2026-07-31 或 2027 全年），同比=同区间回退1年。')
    custom_start = None
    custom_end = None
    if preset == 'custom':
        _c1, _c2 = st.columns(2)
        with _c1:
            custom_start = st.date_input('自定义·起点', value=datetime.date(2026, 1, 1))
        with _c2:
            custom_end = st.date_input('自定义·终点', value=datetime.date(2026, 7, 31))
        if custom_end < custom_start:
            st.warning('终点早于起点，将自动对调。')
    with_pat = st.checkbox('输出「患者级」跨表关联', value=True,
                           help='按 姓名+药房 复合键匹配销售脱落患者与随访原因；同人不同药房视为不同人(防重名串号)，置信度仍受同名影响，仅供参考')
    st.divider()
    run = st.button('🚀 运行分析', type='primary', use_container_width=True)
    st.caption(f'引擎版本 {getattr(E, "APP_VERSION", "未知(引擎缓存旧版,请Redeploy)")}')

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
    # 引擎签名兜底：若线上引擎为旧缓存版(无 custom_start 形参)，只传它认识的参数，
    # 避免「旧引擎 + 新 app.py」混合导致 TypeError 白屏（此时自定义区间自动退化为默认 H1 窗口）。
    _sig = inspect.signature(E.run_analysis)
    _params = _sig.parameters
    _call = {'max_k': max_k, 'mult': mult, 'with_patient_crossref': with_pat, 'preset': preset}
    if 'custom_start' in _params:
        _call['custom_start'] = custom_start
        _call['custom_end'] = custom_end
    else:
        if preset != 'H1_2026':
            st.warning('线上引擎为旧版缓存(不含自定义区间参数)，已退化为默认 H1 2026 窗口。请 Redeploy 拉取最新引擎。')
        preset = 'H1_2026'
    res = E.run_analysis(sales, followup, **_call)

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
    st.subheader('复购率 A（滚动口径）')
    st.caption('M-2 有购药的患者中，M-1∪M 仍有购药的比例，按月平均；分全量/老患。')
    st.dataframe(res['repurchase_decomposition'], use_container_width=True, height=260)
    st.subheader('复购率 B（结合用药周期 · 首购→二购 on-cycle）')
    st.caption('二购距首购 ≤ 3×用药间隔(品种) → 按周期复购。「已观察%」仅统计首购在终点前 3×间隔内的患者，剔除新近右删失（与脱落率B的 established 逻辑一致）。')
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('**全部首购患者**'); st.dataframe(res['repurchase_B_by_brand'], use_container_width=True)
    with c2:
        st.markdown('**已观察患者（剔除右删失）**'); st.dataframe(res['repurchase_B_established_by_brand'], use_container_width=True)
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
    if 'crossref_brand' in res or 'dropout_reason_detail' in res:
        # ---- 当前时间窗 ----
        wi = res.get('window_info')
        if wi is not None and not wi.empty:
            w = wi.iloc[0]
            st.info(f"当前时间窗预设：**{w.get('时间窗预设')}** ｜ 本期 {w.get('本期窗口起点')} ~ {w.get('本期窗口终点')}"
                     + (f" ｜ 同比 {w.get('上期窗口起点')} ~ {w.get('上期窗口终点')}" if w.get('上期窗口起点') else "（无同比）")
                     + "。脱落率B终点与脱落原因统计均按此窗。")
        # ---- 修正后：脱落原因细分类框架 ----
        st.subheader('脱落原因分类（修正后 · 细分类框架）')
        st.caption('基于 churn_logic：医生相关 → 患者相关 → 其他，统一兜底；「空原因」≠ 无脱落（单独计数，不混入任何细类）。')
        dd = res.get('dropout_reason_detail')
        if dd is not None and not dd.empty:
            st.dataframe(dd, use_container_width=True, height=320)
        dl = res.get('dropout_reason_lvl1')
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('**一级分类汇总（医生相关/患者相关/其他）**')
            if dl is not None and not dl.empty:
                st.dataframe(dl, use_container_width=True)
        with c2:
            st.markdown('**细分原因分布**')
            if dd is not None and not dd.empty:
                st.dataframe(dd[['脱落原因细类', '记录数', '占窗内%']], use_container_width=True)
        dmeta = res.get('dropout_reason_meta')
        if isinstance(dmeta, dict):
            st.info(f"覆盖提示（窗内）：随访记录 {dmeta.get('随访窗内总记录')}；有记载原因 {dmeta.get('窗内有原因记录')} 条；"
                     f"空原因（随访员未填，≠无脱落）{dmeta.get('窗内无原因记录')} 条（占 {dmeta.get('窗内无原因占比%')}%）。")
        # ---- 按品种拆分 ----
        drb = res.get('dropout_reason_by_brand')
        if drb is not None and not drb.empty:
            st.subheader('脱落原因分布 · 按品种拆分汇总')
            st.caption('每个品种的脱落原因细类构成（占该品种%），用于定位某品种主导的脱落原因。')
            st.dataframe(drb, use_container_width=True, height=360)
        # ---- 品牌层面 ----
        if 'crossref_brand' in res:
            st.subheader('跨表关联 · 品牌层面（可靠）')
            st.caption('左：销售各品种脱落B人数/率；右：随访各品种脱落原因按一级分类（医生/患者/其他）构成。')
            st.dataframe(res['crossref_brand'], use_container_width=True)
        # ---- 覆盖率概览 + 低匹配报警 ----
        cov = res.get('crossref_coverage')
        if cov is not None and not cov.empty:
            low = cov.attrs.get('low_match', False)
            if low:
                st.error(f"⚠️ 销售脱落患者与随访表匹配率仅 {cov.attrs.get('match_rate', 0) * 100:.0f}%（<30%）。"
                         "这两张表很可能不是同一批患者（药品/项目/时间段不同），请核对后再引用患者级结论。")
            else:
                st.success(f"✅ 患者级匹配率 {cov.attrs.get('match_rate', 0) * 100:.0f}%（分母=销售脱落B患者）。")
            st.subheader('跨表覆盖率概览')
            st.dataframe(cov, use_container_width=True, height=200)
        # ---- 患者级双视角明细（随访侧） ----
        pld = res.get('patient_level_detail')
        if pld is not None and not pld.empty:
            with st.expander('👥 患者级双视角明细（随访侧：最新一次 vs 最近一次有原因）'):
                st.dataframe(pld, use_container_width=True, height=360)
        # ---- 患者级跨表 ----
        if 'crossref_patient' in res:
            cp = res['crossref_patient']
            st.subheader('跨表关联 · 患者级（姓名+药房 复合键 · 含双视角）')
            st.caption('最新一次=当前状态(可能正常)；最近一次有原因=有记载的脱落原因。两列差值处即"最新正常但曾有脱落原因"。')
            cols = [c for c in ['患者姓名', '品牌', '药房', '关联方式', '最新一次分类',
                                '脱落原因(随访)', '脱落原因分类', '一级分类', '距终点天']
                     if c in cp.columns]
            st.dataframe(cp[cols], use_container_width=True, height=360)
        # ---- 脱落原因 × 药房 × 品种 ----
        if 'dropout_reason_by_pharmacy' in res:
            st.subheader('脱落原因 × 药房 × 品种（汇总 · 已匹配患者）')
            st.caption('仅统计匹配到随访原因的销售脱落患者；用于把改进措施落到具体药房品种与原因细类。')
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
        # 单表：存在且非空才写出（防御式，避免个别 key 缺失导致整页崩溃）
        _single = [
            ('window_info', '0_窗口信息'),
            ('retention_overall', '1_留存率口径1_仅购药_整体'),
            ('retention_by_brand', '2_留存率口径1_仅购药_分品种'),
            ('retention_by_brand_pharmacy', '2c_留存率_分品种×药房'),
            ('retention_cov_overall', '1b_留存率口径2_覆盖_整体'),
            ('retention_cov_by_brand', '2b_留存率口径2_覆盖_分品种'),
            ('dropout_B_by_brand', '5_脱落率B_分品种'),
            ('dropout_B_established_by_brand', '5b_脱落率B_已观察'),
            ('crossref_brand', '6_跨表关联_品牌'),
            ('dropout_reason_detail', '7a_脱落原因_细分类分布'),
            ('dropout_reason_by_pharmacy', '6b_脱落原因_药房×品种'),
            ('crossref_coverage', '7b_跨表覆盖率概览'),
            ('patient_level_detail', '7c_患者级双视角明细'),
            ('crossref_patient', '8_跨表关联_患者级'),
            ('action_map', '8_行动建议'),
            ('dot_decomposition', '10_DOT分解'),
            ('new_patient_monthly', '11_新患月度趋势'),
            ('new_patient_pharmacy_decline', '12_新患下降最大药房'),
            ('old_patient_multi_box', '13_老患多盒行为'),
            ('repurchase_decomposition', '14_复购率分解'),
            ('hospital_dimension', '15_医院维度'),
            ('doctor_top1', '16_医生维度_最大患者量'),
            ('doctor_top5', '17_医生维度_TOP5'),
            ('doctor_low_dot_watch', '18_医生维度_低DOT重点'),
            ('pharmacy_dimension', '19_项目药房_风险'),
            ('drill_hospital_doctor', '20_钻取_异常医院拖后腿医生'),
            ('communication_list', '21_重点关注清单'),
            ('improvement_actions', '22_改进措施与责任'),
        ]
        for _k, _n in _single:
            _df = res.get(_k)
            if isinstance(_df, pd.DataFrame) and not _df.empty:
                _df.to_excel(xw, sheet_name=_n, index=False)
        # 字典型（脱落率A）
        _da = res.get('dropout_A')
        if isinstance(_da, dict):
            if isinstance(_da.get('整体_月度'), pd.DataFrame) and not _da['整体_月度'].empty:
                _da['整体_月度'].to_excel(xw, sheet_name='3_脱落率A_整体', index=False)
            if isinstance(_da.get('分品种_月度'), pd.DataFrame) and not _da['分品种_月度'].empty:
                _da['分品种_月度'].to_excel(xw, sheet_name='4_脱落率A_分品种', index=False)
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
