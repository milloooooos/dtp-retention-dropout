# -*- coding: utf-8 -*-
"""
DTP 患者服务 · 自助分析引擎（留存率 / 脱落率A / 脱落率B / 跨表脱落原因）
=============================================================================
设计目标：把"销售底表 + 随访任务表"两份数据，统一算：
  1) 留存率 Cohort（按首购月分群 = 新患队列，追踪第 N 月仍在购药比例）
  2) 脱落率 A（滚动：基准月 M-2 有购药、观察窗 M-1∪M 无购药 → 脱落）
  3) 脱落率 B（累计沉默：末次购药距数据终点 > 3×用药间隔 → 真停药）
  4) 跨表关联：销售算出的脱落患者 → 随访表的脱落原因（可控/不可控映射）
口径说明见 README。本模块被 run_dtp.py（本地批量）与 app.py（Streamlit）共用。
"""
import pandas as pd, numpy as np, re, os, json

# ---------------- 品种与用药间隔（每盒天数，用于口径B阈值） ----------------
BRANDS = ['泰瑞沙', '优赫得', '利普卓', '英飞凡', '沃瑞沙', '凡舒卓', '荃科得']
# 每盒天数（= 一次用药间隔，口径B阈值 = 3 × 此值；也用于留存率覆盖口径）
DAYS_PER_BOX = {'泰瑞沙': 30, '利普卓': 14, '沃瑞沙': 7, '英飞凡': 28,
                '优赫得': 21, '凡舒卓': 28, '荃科得': 30}

# ================= 自丰富品牌映射表（通用名/别名 → 品牌） =================
# 说明：随访表写法为「品牌-(通用名)」、销售表写法为「通用名(品牌)」，两者都含品牌字，
# 但未来若某文件只给通用名（无品牌字），需靠映射表识别。本表会：
#   ① 从数据自动学习（凡是同时含品牌字的记录，抽出其通用名核心词→品牌，写回映射表）；
#   ② 对纯通用名记录用映射表回退匹配；③ 仍识别不出的写入 unresolved 供人工补录。
ALIAS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'brand_aliases.json')

def _seed_aliases():
    # 种子映射均来自本项目真实数据抽取，非主观假设
    return {
        '甲磺酸奥希替尼片': '泰瑞沙', '奥希替尼': '泰瑞沙',
        '注射用德曲妥珠单抗': '优赫得', '德曲妥珠单抗': '优赫得',
        '奥拉帕利片': '利普卓', '奥拉帕利': '利普卓', '奥拉帕尼': '利普卓',
        '度伐利尤单抗注射液': '英飞凡', '度伐利尤单抗': '英飞凡',
        '赛沃替尼片': '沃瑞沙', '赛沃替尼': '沃瑞沙',
        '本瑞利珠单抗注射液': '凡舒卓', '本瑞利珠单抗': '凡舒卓',
        '卡匹色替片': '荃科得', '卡匹色替': '荃科得',
    }

def load_alias_map():
    seed = _seed_aliases()
    if os.path.exists(ALIAS_FILE):
        try:
            with open(ALIAS_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            seed.update(d.get('aliases', {}))
            return {'aliases': seed, 'unresolved': list(d.get('unresolved', []))}
        except Exception:
            pass
    return {'aliases': seed, 'unresolved': []}

def save_alias_map(am):
    try:
        payload = {'aliases': am.get('aliases', {}),
                   'unresolved': sorted(set(am.get('unresolved', [])))}
        with open(ALIAS_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _core_token(s):
    """去掉括号/连字符/空格等分隔符，得到核心药名串。"""
    return re.sub(r'[()（）\-—\s、,，/]+', '', str(s))

def learn_aliases(series, am):
    """从数据自动学习：凡记录同时含品牌字，抽出其通用名核心词 → 该品牌，写回映射表。"""
    aliases = am['aliases']
    for v in pd.Series(series).dropna().astype(str).unique():
        b = next((br for br in BRANDS if br in v), None)
        if not b:
            continue
        g = _core_token(v).replace(b, '')
        if g and g not in aliases:
            aliases[g] = b
    return am

def resolve_brand(x, am):
    """识别顺序：① 直接含品牌字 → ② 映射表通用名子串命中 → ③ 记入 unresolved 返回'其他'。"""
    s = str(x)
    b = next((br for br in BRANDS if br in s), None)
    if b:
        return b
    core = _core_token(s)
    for g, br in am['aliases'].items():
        if g and g in core:
            return br
    if s and s not in ('nan', 'NaN', '') and s not in am['unresolved']:
        am['unresolved'].append(s)
    return '其他'

# 模块级单例：整个进程共享一份映射表，随每次 load 学习并落盘
_ALIAS = load_alias_map()

# ---------------- 用药周期状态 三分类（随访表） ----------------
DROPOUT = {'停药----脱落', '持续用药中--流失', '随访失败', '当期未复购',
           '转院内购药', '当期转渠道', '换药'}
RISK = {'未按计划持续用药-不依从', '推迟购药', '推迟用药', '延迟用药',
        '未规范用药（拉长用药间隔）'}
CONT = {'按计划持续用药', '改变用药剂量', '改变用药间隔时间', '停药后复购', '其它'}

def status_class(s):
    if pd.isna(s):
        return '缺失'
    s = str(s).strip()
    if s in DROPOUT:
        return '脱落'
    if s in RISK:
        return '风险'
    if s in CONT:
        return '持续'
    return '其他'

# ---------------- 脱落原因 → 可控/不可控 映射（复用此前"脱落映射"逻辑） ----------------
def classify_reason(r):
    if pd.isna(r):
        return None
    s = str(r).strip()
    for k in ['经济', '慈善援助', '慈善赠药']:
        if k in s:
            return '可控'
    for k in ['自主停药', '自行', '观念不强', '依从性差', '不规律', '竞争', '其他药房',
              '本地医院', '换药店', '换医院', '异地', '回本市', '回到其他城市', '换城市',
              '回医保', '不再服务', '渠道', '未拨通', '挂断', '联系不上', '拒绝随访',
              '随访失败', '延迟', '暂缓', '推迟', '未及时购药', '正常用药', '需门店', '其它原因']:
        if k in s:
            return '可控'
    for k in ['去世', '疾病进展', '耐药', '更换方案', '换药', '遵医嘱', '医生建议', '病情',
              '疗程结束', '已结束疗程', '康复好转', '疾病康复', '不良反应', '减药', '延长用药间隔']:
        if k in s:
            return '不可控'
    return '其他/未分类'

def reason_of(row):
    for c in ['未按计划持续用药原因', '本月未购药的原因', '脱落/流失原因', '其他原因']:
        v = row.get(c)
        if pd.notna(v) and str(v).strip() not in ('', 'nan', 'NaN'):
            return str(v).strip()
    return None

# 可控脱落 → 行动建议（参考映射）
ACTION_MAP = [
    ['经济负担/支付', '经济负担、因经济原因永久停药、报销不方便',
     '在所有脱落中占可控多数', '推进慈善援助/医保报销落地、援助患者转介、支付方案宣导', '门店+AZ代表'],
    ['渠道分流', '转竞争药房/其他药房/回医院/换城市/异地/配送不便',
     '新患高脱落品牌突出', '院外联动锁客：首购即绑定复购提醒、就近取药/配送、竞品药房回流跟进', '门店运营'],
    ['随访执行', '联系不上/拒绝随访/未拨通/挂断/随访失败',
     '可控且纯执行侧', '优化随访触达：多时段多次呼叫、企微/短信、家属协同', '门店随访'],
    ['观念/依从', '观念不强/自主停药/不规律/自行延长间隔',
     '需医生侧配合', '院内外联动：医生观念沟通 + 患者依从性教育 + 续方提醒', '门店+医生'],
    ['援助药分流', '领取慈善援助药/赠药',
     '可控但临床仍留存', '区分"真流失"与"援助分流"，记录援助渠道避免重复计脱落', '门店'],
]

# ---------------- 列名自动识别（兼容不同导出命名） ----------------
SALES_COL_ALIASES = {
    'brand_raw': ['商品名称', '产品名称', '药品名称'],
    'time': ['销售时间', '开单时间', '购药日期', '单据日期'],
    'qty': ['销售数量', '开单数量', '购药盒数', '数量'],
    'pid': ['患者ID', '患者编码', '会员号', '开票抬头'],
    'name': ['会员姓名', '患者姓名', '开票抬头', '患者'],
    'pharmacy': ['药房名称', '药店名称', '门店'],
    'hospital': ['医疗单位', '开方医院', '医院'],
    'doctor': ['处方医生', '医生'],
}
FOLLOWUP_COL_ALIASES = {
    'brand_raw': ['药品名称', '商品名称', '产品名称'],
    'name': ['患者', '患者姓名', '会员姓名'],
    'status': ['用药周期状态'],
    'create': ['创建日期'],
    'first_buy': ['患者首购日期', '首诊后首次用药时间'],
    'last_buy': ['末次购药日期'],
    'reason1': ['未按计划持续用药原因'],
    'reason2': ['本月未购药的原因'],
    'reason3': ['脱落/流失原因'],
    'reason_other': ['其他原因'],
    'task_type': ['任务类型'],
    'pharmacy': ['药店名称', '门店', '当前购药药店'],
}

def _pick(df, aliases):
    for a in aliases:
        if a in df.columns:
            return a
    return None

def brand_of(x):
    """兼容旧调用：走自丰富映射表识别（直接品牌字 → 通用名别名 → 其他）。"""
    return resolve_brand(x, _ALIAS)

# ---------------- 加载销售底表 ----------------
def load_sales(path, sheet='底表'):
    if isinstance(path, str) and path.lower().endswith('.xls'):
        df = pd.read_excel(path, sheet_name=sheet, engine='xlrd')
    else:
        df = pd.read_excel(path, sheet_name=sheet) if not isinstance(path, pd.DataFrame) else path
    df.columns = [str(c).strip() for c in df.columns]
    bcol = _pick(df, SALES_COL_ALIASES['brand_raw']) or '商品名称'
    tcol = _pick(df, SALES_COL_ALIASES['time']) or '销售时间'
    qcol = _pick(df, SALES_COL_ALIASES['qty']) or '销售数量'
    pcol = _pick(df, SALES_COL_ALIASES['pid']) or '患者ID'
    ncol = _pick(df, SALES_COL_ALIASES['name']) or pcol
    df = df.rename(columns={bcol: '品牌原始', tcol: '销售时间', qcol: '销售数量',
                            pcol: '患者ID', ncol: '患者姓名'}).copy()
    df['销售时间'] = pd.to_datetime(df['销售时间'], errors='coerce')
    df = df.dropna(subset=['销售时间', '患者ID'])
    learn_aliases(df['品牌原始'], _ALIAS)                       # 自动学习
    df['品牌'] = df['品牌原始'].map(lambda x: resolve_brand(x, _ALIAS))
    save_alias_map(_ALIAS)                                      # 落盘丰富
    df['ym'] = df['销售时间'].dt.to_period('M')
    df['首购月'] = df.groupby('患者ID')['销售时间'].transform('min').dt.to_period('M')
    df['末次月'] = df.groupby('患者ID')['销售时间'].transform('max').dt.to_period('M')
    df['是否新患'] = df['ym'] == df['首购月']
    return df

# ---------------- 加载随访任务表 ----------------
def load_followup(paths):
    if isinstance(paths, (str,)):
        paths = [paths]
    frames = []
    for p in paths:
        try:
            frames.append(pd.read_excel(p, engine='xlrd'))
        except Exception:
            frames.append(pd.read_excel(p))
    df = pd.concat(frames, ignore_index=True, sort=False)
    if '任务编号' in df.columns:
        df = df.drop_duplicates(subset=['任务编号'], keep='first')
    df.columns = [str(c).strip() for c in df.columns]
    bcol = _pick(df, FOLLOWUP_COL_ALIASES['brand_raw']) or '药品名称'
    ncol = _pick(df, FOLLOWUP_COL_ALIASES['name']) or '患者'
    scol = _pick(df, FOLLOWUP_COL_ALIASES['status']) or '用药周期状态'
    ccol = _pick(df, FOLLOWUP_COL_ALIASES['create']) or '创建日期'
    fcol = _pick(df, FOLLOWUP_COL_ALIASES['first_buy']) or '患者首购日期'
    lcol = _pick(df, FOLLOWUP_COL_ALIASES['last_buy']) or '末次购药日期'
    df = df.rename(columns={bcol: '品牌原始', ncol: '患者姓名', scol: '用药周期状态',
                            ccol: '创建日期', fcol: '患者首购日期', lcol: '末次购药日期'}).copy()
    for c in ['创建日期', '患者首购日期', '末次购药日期']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    learn_aliases(df['品牌原始'], _ALIAS)                       # 自动学习
    df['品牌'] = df['品牌原始'].map(lambda x: resolve_brand(x, _ALIAS))
    save_alias_map(_ALIAS)                                      # 落盘丰富
    df['状态类'] = df['用药周期状态'].map(status_class)
    df['原因原始'] = df.apply(reason_of, axis=1)
    df['原因类'] = df['原因原始'].map(classify_reason)
    df['患者姓名'] = df['患者姓名'].astype(str).str.strip()
    return df

# ============================ 1. 留存率 Cohort ============================
def compute_retention(sales, max_k=12):
    """按首购月分群（=新患队列），追踪第 k 月仍在购药的比例。"""
    fp = sales.groupby('患者ID')['销售时间'].min()
    cohorts = fp.apply(lambda x: x.to_period('M'))
    sales = sales.copy()
    sales['cohort'] = sales['患者ID'].map(cohorts)
    max_month = sales['销售时间'].max().to_period('M')
    rows = []
    for cm in sorted(cohorts.unique()):
        grp = sales[sales['cohort'] == cm]
        base = set(grp['患者ID'].unique())
        n0 = len(base)
        rec = {'首购月': str(cm), '新患数': n0}
        for k in range(1, max_k + 1):
            target = cm + k
            if target > max_month:
                rec[f'M{k}留存%'] = None
                continue
            purch = set(sales[sales['ym'] == target]['患者ID'].unique())
            retained = base & purch
            rec[f'M{k}留存%'] = round(len(retained) / n0 * 100, 1) if n0 else None
        rows.append(rec)
    return pd.DataFrame(rows)

def compute_retention_by_brand(sales, max_k=12):
    out = []
    for b, g in sales.groupby('品牌'):
        if b == '其他':
            continue
        r = compute_retention(g, max_k)
        r.insert(0, '品牌', b)
        out.append(r)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()

# -------- 留存率 口径2：结合说明书每盒天数的「覆盖期」判断 --------
def compute_retention_coverage(sales, max_k=12):
    """留存率(覆盖口径)：结合说明书『每盒天数×购买盒数』的药品覆盖期。
    每次购药覆盖 = 销售数量 × 每盒天数 天。患者在首购后第 k 个自然月，只要有
    任一购药的覆盖区间与该月有交集，即视为『仍在药品覆盖内=留存』。
    与口径1(仅看当月是否有购药)相比：一次囤多盒的患者在断购月仍算留存，
    曲线更贴近真实『在治』状态、更单调，剔除了多盒囤药造成的锯齿。"""
    s = sales.copy()
    s['dpb'] = s['品牌'].map(lambda b: DAYS_PER_BOX.get(b, 30))
    qty = pd.to_numeric(s['销售数量'], errors='coerce').fillna(1).clip(lower=1)
    s['cover_end'] = s['销售时间'] + pd.to_timedelta(qty * s['dpb'], unit='D')
    fp = s.groupby('患者ID')['销售时间'].min()
    cohorts = fp.apply(lambda x: x.to_period('M'))
    s['cohort'] = s['患者ID'].map(cohorts)
    max_month = s['销售时间'].max().to_period('M')
    rows = []
    for cm in sorted(cohorts.unique()):
        grp = s[s['cohort'] == cm]
        base = set(grp['患者ID'].unique())
        n0 = len(base)
        sub = s[s['患者ID'].isin(base)]
        rec = {'首购月': str(cm), '新患数': n0}
        for k in range(1, max_k + 1):
            target = cm + k
            if target > max_month:
                rec[f'M{k}留存%'] = None
                continue
            tstart = target.to_timestamp(how='start')
            tend = target.to_timestamp(how='end')
            covered = set(sub[(sub['销售时间'] <= tend) & (sub['cover_end'] >= tstart)]['患者ID'].unique())
            rec[f'M{k}留存%'] = round(len(covered) / n0 * 100, 1) if n0 else None
        rows.append(rec)
    return pd.DataFrame(rows)

def compute_retention_coverage_by_brand(sales, max_k=12):
    out = []
    for b, g in sales.groupby('品牌'):
        if b == '其他':
            continue
        r = compute_retention_coverage(g, max_k)
        r.insert(0, '品牌', b)
        out.append(r)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()

# ============================ 2. 脱落率 A（滚动） ============================
def _monthly_dropout_A(sales):
    months = sorted(sales['ym'].unique())
    rows = []
    for i, m in enumerate(months):
        if i < 2:
            continue
        base_m, w1, w2 = months[i - 2], months[i - 1], months[i]
        base_p = set(sales[sales['ym'] == base_m]['患者ID'].unique())
        win_p = set(sales[sales['ym'].isin([w1, w2])]['患者ID'].unique())
        drop = base_p - win_p
        rows.append({'基准月': str(base_m), '基准患者数': len(base_p),
                     '脱落数': len(drop),
                     '脱落率A': round(len(drop) / len(base_p), 4) if base_p else None})
    return pd.DataFrame(rows)

def compute_dropout_A(sales):
    overall = _monthly_dropout_A(sales)
    res = {'整体_月度': overall}
    # 分品种
    brand_rows = []
    for b, g in sales.groupby('品牌'):
        if b == '其他':
            continue
        d = _monthly_dropout_A(g)
        d.insert(0, '品牌', b)
        brand_rows.append(d)
    if brand_rows:
        res['分品种_月度'] = pd.concat(brand_rows, ignore_index=True)
    # 分药房
    pharm_rows = []
    pcol = '药房名称' if '药房名称' in sales.columns else None
    if pcol:
        for ph, g in sales.groupby(pcol):
            d = _monthly_dropout_A(g)
            if d.empty:
                continue
            d.insert(0, '药房', ph)
            pharm_rows.append(d)
        if pharm_rows:
            res['分药房_月度'] = pd.concat(pharm_rows, ignore_index=True)
    return res

# ============================ 3. 脱落率 B（累计沉默） ============================
def compute_dropout_B(sales, mult=3):
    """患者级：末次购药距数据终点 > mult×用药间隔 → 真停药（脱落B）。
    同时标记 established：首购在「终点−mult×间隔」之前，才有足够观察期判断真停药；
    新近患者（首购太靠近终点）免除右删失，不应判定脱落。"""
    end = sales['销售时间'].max()
    last = sales.groupby('患者ID').agg(
        末次购药=('销售时间', 'max'), 品牌=('品牌', 'last'),
        首购=('销售时间', 'min'), 患者姓名=('患者姓名', 'last')).copy()
    last['距终点天'] = (end - last['末次购药']).dt.days
    last['阈值天'] = last['品牌'].map(lambda b: mult * DAYS_PER_BOX.get(b, 30))
    last['脱落B'] = last['距终点天'] > last['阈值天']
    last['观察窗起点'] = last['品牌'].map(lambda b: end - pd.Timedelta(days=mult * DAYS_PER_BOX.get(b, 30)))
    last['established'] = last['首购'] <= last['观察窗起点']
    return last.reset_index()

# ============================ 4. 跨表关联：销售脱落 ↔ 随访原因 ============================
def crossref_brand(sales_last, followup):
    """品牌层面关联：销售各品种脱落B人数 ↔ 随访各品种脱落原因构成。"""
    s = sales_last.groupby('品牌')['脱落B'].agg(脱落患者数='sum', 患者总数='count')
    s['销售脱落率B%'] = (s['脱落患者数'] / s['患者总数'] * 100).round(1)
    fd = followup[followup['状态类'] == '脱落'].copy()
    if fd.empty:
        reason = pd.DataFrame(columns=['品牌', '可控数', '不可控数', '未分类数', '可控占比%'])
    else:
        grp = fd.groupby('品牌')['原因类'].value_counts().unstack(fill_value=0)
        for c in ['可控', '不可控', '其他/未分类']:
            if c not in grp.columns:
                grp[c] = 0
        reason = grp.reset_index()
        reason['可控占比%'] = (reason['可控'] / (reason[['可控', '不可控', '其他/未分类']].sum(axis=1)) * 100).round(1)
        reason = reason.rename(columns={'可控': '可控数', '不可控': '不可控数', '其他/未分类': '未分类数'})
    out = s.reset_index().merge(reason, on='品牌', how='left')
    return out

def crossref_patient(sales_last, followup):
    """近似患者级关联：销售脱落患者(姓名+品牌+首购月) ↔ 随访脱落记录(姓名+品牌+首购月)。
    注意：两表无共同患者主键，姓名存在大量同名歧义，本结果为近似关联，置信度有限。"""
    ch = sales_last[sales_last['脱落B']].copy()
    ch['首购月'] = ch['首购'].dt.to_period('M')
    ch['key'] = ch['患者姓名'].astype(str) + '|' + ch['品牌'] + '|' + ch['首购月'].astype(str)
    fd = followup[followup['状态类'] == '脱落'].copy()
    fd['首购月'] = fd['患者首购日期'].dt.to_period('M')
    fd['key'] = fd['患者姓名'].astype(str) + '|' + fd['品牌'] + '|' + fd['首购月'].astype(str)
    # 每个 key 取第一条随访原因
    fmap = fd.drop_duplicates('key').set_index('key')[['原因原始', '原因类', '用药周期状态']].to_dict('index')
    rows = []
    matched = 0
    for _, r in ch.iterrows():
        m = fmap.get(r['key'])
        if m:
            matched += 1
            rows.append({'患者姓名': r['患者姓名'], '品牌': r['品牌'], '首购月': str(r['首购月']),
                         '末次购药': r['末次购药'].date(), '距终点天': int(r['距终点天']),
                         '脱落原因(随访)': m['原因原始'], '原因类': m['原因类'],
                         '随访状态': m['用药周期状态'], '关联方式': '姓名+品种+首购月(近似)'})
        else:
            rows.append({'患者姓名': r['患者姓名'], '品牌': r['品牌'], '首购月': str(r['首购月']),
                         '末次购药': r['末次购药'].date(), '距终点天': int(r['距终点天']),
                         '脱落原因(随访)': None, '原因类': None,
                         '随访状态': None, '关联方式': '无随访匹配'})
    out = pd.DataFrame(rows)
    out.attrs['matched'] = matched
    out.attrs['total'] = len(ch)
    return out

# ============================ 编排 ============================
def run_analysis(sales, followup=None, max_k=12, mult=3, with_patient_crossref=True):
    result = {}
    # 留存率 口径1：仅看购药时间（当月是否有购药）
    result['retention_overall'] = compute_retention(sales, max_k)
    result['retention_by_brand'] = compute_retention_by_brand(sales, max_k)
    # 留存率 口径2：结合说明书盒数覆盖期
    result['retention_cov_overall'] = compute_retention_coverage(sales, max_k)
    result['retention_cov_by_brand'] = compute_retention_coverage_by_brand(sales, max_k)
    result['dropout_A'] = compute_dropout_A(sales)
    last = compute_dropout_B(sales, mult)
    result['dropout_B_patient'] = last
    # 脱落率B 分品种（全部患者）
    bbrand = last.groupby('品牌')['脱落B'].agg(脱落患者数='sum', 患者总数='count')
    bbrand['脱落率B%'] = (bbrand['脱落患者数'] / bbrand['患者总数'] * 100).round(1)
    result['dropout_B_by_brand'] = bbrand.reset_index().sort_values('脱落率B%', ascending=False)
    # 脱落率B 分品种（已观察患者，剔除新近右删失）
    est = last[last['established']]
    ebrand = est.groupby('品牌')['脱落B'].agg(脱落患者数_已观察='sum', 患者总数_已观察='count')
    ebrand['脱落率B_已观察%'] = (ebrand['脱落患者数_已观察'] / ebrand['患者总数_已观察'] * 100).round(1)
    result['dropout_B_established_by_brand'] = ebrand.reset_index().sort_values('脱落率B_已观察%', ascending=False)
    result['action_map'] = pd.DataFrame(ACTION_MAP, columns=['可控类型', '典型原因', '现状', '建议动作', '责任方'])
    if followup is not None and not followup.empty:
        result['crossref_brand'] = crossref_brand(last, followup)
        if with_patient_crossref:
            result['crossref_patient'] = crossref_patient(last, followup)
    return result

if __name__ == '__main__':
    import sys
    SRC = r'F:/厂家项目/阿斯利康/2026/半年复盘/英飞凡、利普卓、泰瑞沙、优赫得6.26.xlsx'
    F25 = 'E:/下载内容/历史任务 - 2026-07-22T105636.443.xls'
    F26 = 'E:/下载内容/历史任务 - 2026-07-22T105440.252.xls'
    sales = load_sales(SRC)
    followup = load_followup([F25, F26])
    res = run_analysis(sales, followup)
    print('留存率-口径1(仅购药时间) 整体:')
    print(res['retention_overall'].head(8).to_string(index=False))
    print('\n留存率-口径2(结合盒数覆盖) 整体:')
    print(res['retention_cov_overall'].head(8).to_string(index=False))
    print('\n自丰富映射表 aliases 条数:', len(_ALIAS['aliases']), ' unresolved:', _ALIAS['unresolved'])
    print('\n脱落率A 整体月度:')
    print(res['dropout_A']['整体_月度'].tail(8).to_string(index=False))
    print('\n脱落率B 分品种:')
    print(res['dropout_B_by_brand'].to_string(index=False))
    if 'crossref_brand' in res:
        print('\n跨表关联(品牌层面):')
        print(res['crossref_brand'].to_string(index=False))
    if 'crossref_patient' in res:
        cp = res['crossref_patient']
        print('\n近似患者级关联匹配率: {}/{} = {:.1f}%'.format(
            cp.attrs.get('matched', 0), cp.attrs.get('total', 0),
            cp.attrs.get('matched', 0) / max(cp.attrs.get('total', 1), 1) * 100))
