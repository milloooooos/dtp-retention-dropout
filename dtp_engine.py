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
import pandas as pd, numpy as np, re, os, json, io

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

# ---------------- 项目药房判定（TOP 清单） ----------------
TOP_COL_PATTERNS = ['TOP', '项目', '重点']

def load_pharmacy_tier(path):
    """读取项目药房 TOP 清单，返回 {药房全称: {品牌: '项目'/'非项目', '城市': str}}。
    支持列名：'对应的药店名称'/'药房名称'/'药店名称' + 各品种 TOP/项目/重点 列 + '城市'。"""
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    ph_col = next((c for c in df.columns if c in ['对应的药店名称', '药房名称', '药店名称', '门店']), None)
    city_col = next((c for c in df.columns if c in ['城市', '地市', '城市名']), None)
    if ph_col is None:
        return {}
    brand_cols = {}
    for c in df.columns:
        up = c.upper()
        for b in BRANDS:
            if b in c and any(p in up for p in TOP_COL_PATTERNS):
                brand_cols[b] = c
                break
    tier = {}
    for _, r in df.iterrows():
        ph = str(r[ph_col]).strip() if pd.notna(r[ph_col]) else None
        if not ph or ph in ('nan', 'NaN'):
            continue
        entry = {'城市': str(r[city_col]).strip() if city_col and pd.notna(r[city_col]) else ''}
        for b, col in brand_cols.items():
            v = r[col]
            if pd.notna(v) and str(v).strip() not in ('', 'nan', 'NaN'):
                txt = str(v).strip()
                entry[b] = '项目' if any(k in txt for k in ['项目', '重点', 'TOP', '是', 'Y', '√']) else '非项目'
        tier[ph] = entry
    return tier

def _normalize_pharmacy_name(s):
    """把常见简称统一成全称风格，便于和 TOP 清单匹配。"""
    if pd.isna(s):
        return ''
    return str(s).strip()

def apply_tier(sales, tier):
    """给销售底表加 角色/城市 列。如未提供 tier 且底表无角色列，默认全部为项目。"""
    sales = sales.copy()
    if '角色' not in sales.columns:
        sales['角色'] = '项目'
    if '城市' not in sales.columns:
        sales['城市'] = ''
    if not tier:
        return sales
    # 建立全称→tier 的查找（同时保留原名）
    norm_tier = {_normalize_pharmacy_name(k): v for k, v in tier.items()}
    roles = []
    cities = []
    for _, r in sales.iterrows():
        ph = _normalize_pharmacy_name(r.get('药房名称', ''))
        brand = r.get('品牌', '其他')
        entry = norm_tier.get(ph)
        # 尝试用子串匹配兜底
        if entry is None:
            for k, v in norm_tier.items():
                if k and (k in ph or ph in k):
                    entry = v
                    break
        if entry and brand in entry:
            roles.append(entry[brand])
        else:
            roles.append('非项目')
        cities.append(entry.get('城市', '') if entry else '')
    sales['角色'] = roles
    sales['城市'] = cities
    return sales

# ---------------- 加载销售底表 ----------------
def load_sales(path, sheet='底表', tier=None):
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
    # 兼容 build_report 底表已有的药房/医院/医生列
    if '药房名称' not in df.columns:
        df['药房名称'] = df.get('门店', '未识别')
    if '医疗单位' not in df.columns:
        df['医疗单位'] = df.get('医院', '未识别')
    if '处方医生' not in df.columns:
        df['处方医生'] = df.get('医生', '')
    df = apply_tier(df, tier)
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

def compute_retention_by_brand_pharmacy(sales, max_k=12):
    """留存率：分 品牌 × 药房。对每个(品牌,药房)子群跑 compute_retention（口径1：仅购药时间）。
    样本<3 的子群剔除，避免小样本噪声。用于定位『哪个药房哪个品种留存差』。"""
    pcol = '药房名称' if '药房名称' in sales.columns else None
    if not pcol:
        return pd.DataFrame()
    out = []
    for (b, ph), g in sales.groupby(['品牌', pcol]):
        if b == '其他' or g['患者ID'].nunique() < 3:
            continue
        r = compute_retention(g, max_k)
        r.insert(0, '药房', ph)
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
    if '药房名称' in sales.columns:
        last['药房'] = sales.groupby('患者ID')['药房名称'].last()
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
            rows.append({'患者姓名': r['患者姓名'], '品牌': r['品牌'], '药房': r.get('药房'),
                         '首购月': str(r['首购月']),
                         '末次购药': r['末次购药'].date(), '距终点天': int(r['距终点天']),
                         '脱落原因(随访)': m['原因原始'], '原因类': m['原因类'],
                         '随访状态': m['用药周期状态'], '关联方式': '姓名+品种+首购月(近似)'})
        else:
            rows.append({'患者姓名': r['患者姓名'], '品牌': r['品牌'], '药房': r.get('药房'),
                         '首购月': str(r['首购月']),
                         '末次购药': r['末次购药'].date(), '距终点天': int(r['距终点天']),
                         '脱落原因(随访)': None, '原因类': None,
                         '随访状态': None, '关联方式': '无随访匹配'})
    out = pd.DataFrame(rows)
    out.attrs['matched'] = matched
    out.attrs['total'] = len(ch)
    return out

# ============================ 5. DOT / 新患 / 复购 / 维度分析 ============================
def compute_windows(sales):
    """根据销售底表最大日期，自动确定回滚1年窗口及其同比窗口。"""
    end = sales['销售时间'].max()
    end = pd.Timestamp(end.year, end.month, 1) + pd.offsets.MonthEnd(0)
    start = (end - pd.DateOffset(years=1)) + pd.Timedelta(days=1)
    start = pd.Timestamp(start.year, start.month, 1)
    end_prev = start - pd.Timedelta(days=1)
    start_prev = (end_prev - pd.DateOffset(years=1)) + pd.Timedelta(days=1)
    start_prev = pd.Timestamp(start_prev.year, start_prev.month, 1)
    return start, end, start_prev, end_prev

def _dot_snap(sub, wstart, wend, pids=None):
    """DOT = 窗口内购买总盒数 / 去重患者数（盒/人，不换算月数）。"""
    if pids is not None:
        sub = sub[sub['患者ID'].isin(pids)]
    win = sub[(sub['销售时间'] >= wstart) & (sub['销售时间'] <= wend)]
    if win.empty:
        return np.nan
    return win.groupby('患者ID')['销售数量'].sum().mean()

def _rolling_repurchase(sub, months, fm):
    """滚动复购率：M-2 有购药的患者中，M-1∪M 仍有购药的占比，按月平均。"""
    pm = sub.groupby(['患者ID', 'ym']).size().reset_index()
    have = set(sub['ym'].unique())
    rates = []
    for m in months:
        if (m - 2) not in have or m not in have:
            continue
        bp = set(pm[pm['ym'] == m - 2]['患者ID'])
        if not bp:
            continue
        ret = set(pm[pm['ym'].isin([m - 1, m])]['患者ID'])
        rates.append(len(bp & ret) / len(bp))
    return np.mean(rates) if rates else np.nan

def dot_decomposition(sales):
    """DOT 分解：全量 / 老患 / 新患窗口 / 重叠患者，同比。"""
    wstart, wend, wstart_prev, wend_prev = compute_windows(sales)
    fm = sales.groupby('患者ID')['销售时间'].min()
    old = set(fm[fm < wstart].index)
    old_prev = set(fm[fm < wstart_prev].index)
    new = set(fm[(fm >= wstart) & (fm <= wend)].index)
    new_prev = set(fm[(fm >= wstart_prev) & (fm <= wend_prev)].index)
    active = sales[(sales['销售时间'] >= wstart) & (sales['销售时间'] <= wend)]['患者ID'].unique()
    active_prev = sales[(sales['销售时间'] >= wstart_prev) & (sales['销售时间'] <= wend_prev)]['患者ID'].unique()
    new = new & set(active)
    new_prev = new_prev & set(active_prev)
    overlap = set(active) & set(active_prev)
    months = list(pd.period_range(wstart, wend, freq='M'))
    months_prev = list(pd.period_range(wstart_prev, wend_prev, freq='M'))
    rows = []
    for b in BRANDS:
        sub = sales[sales['品牌'] == b]
        if sub.empty:
            continue
        d_all = _dot_snap(sub, wstart, wend)
        d_all_prev = _dot_snap(sub, wstart_prev, wend_prev)
        d_old = _dot_snap(sub, wstart, wend, old)
        d_old_prev = _dot_snap(sub, wstart_prev, wend_prev, old_prev)
        d_new = _dot_snap(sub, wstart, wend, new)
        d_new_prev = _dot_snap(sub, wstart_prev, wend_prev, new_prev)
        d_ov = _dot_snap(sub, wstart, wend, overlap)
        d_ov_prev = _dot_snap(sub, wstart_prev, wend_prev, overlap)
        r_all = _rolling_repurchase(sub, months, fm)
        r_all_prev = _rolling_repurchase(sub, months_prev, fm)
        r_old = _rolling_repurchase(sub[sub['患者ID'].isin(old)], months, fm)
        r_old_prev = _rolling_repurchase(sub[sub['患者ID'].isin(old_prev)], months_prev, fm)
        rows.append({
            '品种': b, '患者数': sub['患者ID'].nunique(),
            'DOT_本期_全': round(d_all, 2) if pd.notna(d_all) else '',
            'DOT_同比_全': round(d_all_prev, 2) if pd.notna(d_all_prev) else '',
            'DOT全_Δ': round(d_all - d_all_prev, 2) if pd.notna(d_all) and pd.notna(d_all_prev) else '',
            'DOT_本期_老患': round(d_old, 2) if pd.notna(d_old) else '',
            'DOT_同比_老患': round(d_old_prev, 2) if pd.notna(d_old_prev) else '',
            'DOT老患_Δ': round(d_old - d_old_prev, 2) if pd.notna(d_old) and pd.notna(d_old_prev) else '',
            'DOT_本期_新患窗口': round(d_new, 2) if pd.notna(d_new) else '',
            'DOT_同比_新患窗口': round(d_new_prev, 2) if pd.notna(d_new_prev) else '',
            'DOT_本期_重叠患者': round(d_ov, 2) if pd.notna(d_ov) else '',
            'DOT_同比_重叠患者': round(d_ov_prev, 2) if pd.notna(d_ov_prev) else '',
            '复购率_本期_全': round(r_all, 3) if pd.notna(r_all) else '',
            '复购率_同比_全': round(r_all_prev, 3) if pd.notna(r_all_prev) else '',
            '复购率_本期_老患': round(r_old, 3) if pd.notna(r_old) else '',
            '复购率_同比_老患': round(r_old_prev, 3) if pd.notna(r_old_prev) else '',
        })
    return pd.DataFrame(rows)

def new_patient_monthly(sales):
    """新患月度趋势（按首购品种、首购月）。"""
    fm = sales.groupby('患者ID').agg(首购月=('ym', 'min'), 首购品种=('品牌', lambda x: x.iloc[0]))
    out = pd.DataFrame()
    for b in BRANDS:
        s = fm[fm['首购品种'] == b]
        vc = s['首购月'].value_counts().sort_index()
        out[b] = vc
    out.index.name = '月份'
    out = out.fillna(0).astype(int)
    # 取最近 18 个月
    min_m = out.index.min()
    if pd.notna(min_m):
        out = out[out.index >= (out.index.max() - 17)]
    return out.reset_index()

def new_patient_pharmacy_decline(sales):
    """项目药房新患同比变化（本期 vs 上期，按品种+药房）。"""
    wstart, wend, wstart_prev, wend_prev = compute_windows(sales)
    fm = sales.groupby('患者ID').agg(首购月=('ym', 'min'), 首购品种=('品牌', lambda x: x.iloc[0]),
                                      首购药房=('药房名称', lambda x: x.iloc[0]))
    proj = sales[sales['角色'] == '项目'] if '角色' in sales.columns else sales
    rows = []
    for b in BRANDS:
        s = fm[fm['首购品种'] == b]
        for ph, g in s.groupby('首购药房'):
            if '角色' in sales.columns and role_of(ph, b, sales) != '项目':
                continue
            n_now = int(((g['首购月'] >= wstart.to_period('M')) & (g['首购月'] <= wend.to_period('M'))).sum())
            n_prev = int(((g['首购月'] >= wstart_prev.to_period('M')) & (g['首购月'] <= wend_prev.to_period('M'))).sum())
            if n_prev == 0 and n_now == 0:
                continue
            rows.append({'品种': b, '药房': ph, '城市': sales[sales['药房名称'] == ph]['城市'].dropna().iloc[0] if '城市' in sales.columns and not sales[sales['药房名称'] == ph]['城市'].dropna().empty else '',
                         '新患_本期': n_now, '新患_上期': n_prev, '变化': n_now - n_prev,
                         '同比': round((n_now - n_prev) / n_prev, 2) if n_prev else np.nan})
    return pd.DataFrame(rows).sort_values(['品种', '同比'])

def role_of(ph, b, sales):
    """从 sales 的角色列反查药房在某品种下的角色（项目/非项目）。"""
    sub = sales[(sales['药房名称'] == ph) & (sales['品牌'] == b)]
    if sub.empty or '角色' not in sub.columns:
        return '项目'
    return sub['角色'].iloc[0]

def old_patient_multi_box(sales):
    """老患购药行为：单次购买盒数分布、购药间隔。"""
    wstart, wend, _, _ = compute_windows(sales)
    fm = sales.groupby('患者ID')['销售时间'].min()
    old = set(fm[fm < wstart].index)
    proj = sales[sales['角色'] == '项目'] if '角色' in sales.columns else sales
    rows = []
    for b in BRANDS:
        sub = proj[(proj['品牌'] == b) & (proj['患者ID'].isin(old))].copy()
        if sub.empty:
            continue
        visit = sub.groupby(['患者ID', 'ym'])['销售数量'].sum().reset_index()
        box_dist = visit['销售数量'].value_counts().sort_index()
        intervals = []
        for pid, g in sub.sort_values('销售时间').groupby('患者ID'):
            ts = g['销售时间'].sort_values().tolist()
            for i in range(1, len(ts)):
                intervals.append((ts[i] - ts[i - 1]).days)
        intervals = pd.Series(intervals)
        total = box_dist.sum()
        rows.append({
            '品种': b, '老患人数': sub['患者ID'].nunique(),
            '单次1盒占比': round(box_dist.get(1, 0) / total, 2) if total else '',
            '单次>=2盒占比': round(box_dist[box_dist.index >= 2].sum() / total, 2) if total else '',
            '单次>=3盒占比': round(box_dist[box_dist.index >= 3].sum() / total, 2) if total else '',
            '中位间隔天': round(intervals.median(), 0) if len(intervals) else '',
            '间隔>45天占比': round((intervals > 45).mean(), 2) if len(intervals) else ''
        })
    return pd.DataFrame(rows)

def repurchase_decomposition(sales):
    """复购率分解：全量 vs 老患，并估算新患稀释占比。"""
    wstart, wend, _, _ = compute_windows(sales)
    fm = sales.groupby('患者ID')['销售时间'].min()
    old = set(fm[fm < wstart].index)
    months = list(pd.period_range(wstart, wend, freq='M'))
    rows = []
    for b in BRANDS:
        sub = sales[sales['品牌'] == b]
        if sub.empty:
            continue
        r_all = _rolling_repurchase(sub, months, fm)
        pm = sub.groupby(['患者ID', 'ym']).size().reset_index()
        have = set(sub['ym'].unique())
        dil = []
        for m in months:
            if (m - 2) not in have or m not in have:
                continue
            bp = pm[pm['ym'] == m - 2]
            if bp.empty:
                continue
            fms = bp['患者ID'].map(fm).dt.to_period('M')
            dil.append((fms == m - 2).mean())
        dilution = np.mean(dil) if dil else np.nan
        r_old = _rolling_repurchase(sub[sub['患者ID'].isin(old)], months, fm)
        rows.append({
            '品种': b,
            '复购率_全_本期': round(r_all, 3) if pd.notna(r_all) else '',
            '新患稀释占比(估算)': round(dilution, 3) if pd.notna(dilution) else '',
            '老患占比': round(1 - dilution, 2) if pd.notna(dilution) else '',
            '复购率_老患_本期': round(r_old, 3) if pd.notna(r_old) else ''
        })
    return pd.DataFrame(rows)

def _split_doc(x):
    if pd.isna(x):
        return []
    return [p.strip() for p in str(x).replace('、', '|').replace('，', '|').replace(',', '|').split('|') if p.strip()]

def _newp_ratio(sub, fm, months):
    if sub.empty:
        return np.nan
    ids = [i for i in sub['患者ID'].unique() if pd.notna(i)]
    if not ids:
        return np.nan
    return fm.loc[ids].dt.to_period('M').isin(set(months)).mean()

def hospital_dimension(sales):
    """医院维度：按医疗单位聚合 DOT、复购率、新患占比、主要来源药房。"""
    wstart, wend, _, _ = compute_windows(sales)
    months = list(pd.period_range(wstart, wend, freq='M'))
    fm = sales.groupby('患者ID')['销售时间'].min()
    proj = sales[sales['角色'] == '项目'] if '角色' in sales.columns else sales
    rows = []
    for b in BRANDS:
        sub = proj[proj['品牌'] == b]
        if sub.empty:
            continue
        for hosp, g in sub.groupby('医疗单位'):
            n = g['患者ID'].nunique()
            if n < 5:
                continue
            d26 = _dot_snap(g, wstart, wend)
            r26 = _rolling_repurchase(g, months, fm)
            ph_break = g.groupby('药房名称')['患者ID'].nunique().sort_values(ascending=False)
            contrib = ', '.join(f"{p}({nn})" for p, nn in ph_break.head(3).items())
            rows.append({
                '品种': b, '医疗单位': hosp, '患者数': n,
                'DOT_本期': round(d26, 2) if pd.notna(d26) else np.nan,
                '复购率_本期': round(r26, 3) if pd.notna(r26) else np.nan,
                '脱落率A': round(1 - r26, 3) if pd.notna(r26) else np.nan,
                '新患占比': round(_newp_ratio(g, fm, months), 3),
                '主要来源项目药房': ph_break.index[0] if len(ph_break) else '',
                '主要药房患者数': int(ph_break.iloc[0]) if len(ph_break) else '',
                '贡献项目药房明细': contrib
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    href = {b: df[df['品种'] == b]['DOT_本期'].median() for b in BRANDS}
    df['品种参考DOT'] = df['品种'].map(href)
    def flag(r):
        low = pd.notna(r['DOT_本期']) and r['DOT_本期'] < 0.5 * r['品种参考DOT']
        high_drop = pd.notna(r['脱落率A']) and r['脱落率A'] >= 0.4
        if high_drop and low:
            return '高脱落+低DOT-重点沟通'
        if high_drop:
            return '高脱落-重点沟通'
        if low and r['新患占比'] >= 0.5:
            return '新患致低DOT(结论)'
        if low:
            return '老患低DOT-真风险'
        if pd.notna(r['DOT_本期']) and r['DOT_本期'] < r['品种参考DOT']:
            return '低DOT-观察'
        return '正常'
    df['风险等级'] = df.apply(flag, axis=1)
    def action(r):
        if r['风险等级'] == '新患致低DOT(结论)':
            return '结论：低DOT由高新患占比稀释；动作：稳住新患+提升DOT；新患不足→反馈药企代表拓新'
        if '高脱落' in r['风险等级']:
            return '动作：院内外联动，针对该医院重点医生做观念沟通+续方管理'
        if '真风险' in r['风险等级']:
            return '动作：老患者依从性管理/续方提醒，核查外流'
        return '观察'
    df['结论与动作'] = df.apply(action, axis=1)
    return df.sort_values(['品种', '患者数'], ascending=[True, False])

def doctor_dimension(sales):
    """医生维度：各项目药房下处方医生的患者量与 DOT。"""
    wstart, wend, _, _ = compute_windows(sales)
    proj = sales[sales['角色'] == '项目'] if '角色' in sales.columns else sales
    proj2 = proj.copy()
    proj2['医生列表'] = proj2['处方医生'].map(_split_doc)
    proj2 = proj2.explode('医生列表')
    proj2 = proj2[proj2['医生列表'] != '']
    records = []
    for b in BRANDS:
        for ph in sorted(proj2[proj2['品牌'] == b]['药房名称'].unique()):
            sub = proj2[(proj2['品牌'] == b) & (proj2['药房名称'] == ph)]
            if sub.empty:
                continue
            st = sub.groupby('医生列表').agg(
                患者数=('患者ID', 'nunique'),
                主要医院=('医疗单位', lambda x: x.mode().iloc[0] if len(x.mode()) else '')
            )
            dmap = {doc: _dot_snap(g, wstart, wend) for doc, g in sub.groupby('医生列表')}
            st['DOT_本期'] = pd.Series(dmap)
            st = st[st['患者数'] >= 1].sort_values('患者数', ascending=False)
            st['rank'] = range(1, len(st) + 1)
            for doc, row in st.iterrows():
                records.append({
                    '品种': b, '药房': ph, '城市': sales[sales['药房名称'] == ph]['城市'].dropna().iloc[0] if '城市' in sales.columns and not sales[sales['药房名称'] == ph]['城市'].dropna().empty else '',
                    'rank': int(row['rank']), '处方医生': doc,
                    '患者数': int(row['患者数']),
                    'DOT_本期': round(row['DOT_本期'], 2) if pd.notna(row['DOT_本期']) else np.nan,
                    '主要医院': row['主要医院']
                })
    doc_df = pd.DataFrame(records)
    if doc_df.empty:
        return doc_df, doc_df, doc_df
    dref = {b: doc_df[(doc_df['品种'] == b) & (doc_df['患者数'] >= 3)]['DOT_本期'].median() for b in BRANDS}
    doc_df['品种参考DOT'] = doc_df['品种'].map(dref)
    top1 = doc_df[doc_df['rank'] == 1].copy().rename(columns={
        '患者数': '药房该品种患者数', '处方医生': '最大患者量医生',
        'DOT_本期': '最大医生DOT'})
    top1 = top1[['品种', '药房', '城市', '药房该品种患者数', '最大患者量医生', '最大医生DOT', '主要医院']].sort_values(
        ['品种', '药房该品种患者数'], ascending=[True, False])
    top5 = doc_df[doc_df['rank'] <= 5].copy()
    top5['DOT相对参考'] = (top5['DOT_本期'] / top5['品种参考DOT']).round(2)
    top5 = top5[['品种', '药房', '城市', 'rank', '处方医生', '患者数', 'DOT_本期',
                 '品种参考DOT', 'DOT相对参考', '主要医院']].sort_values(['品种', '药房', 'rank'])
    docwatch = doc_df[(doc_df['rank'] <= 5) & (doc_df['患者数'] >= 5)].copy()
    docwatch = docwatch[docwatch['DOT_本期'] < docwatch['品种参考DOT']].copy()
    docwatch['DOT相对参考'] = (docwatch['DOT_本期'] / docwatch['品种参考DOT']).round(2)
    def newp_ratio_pharmacy(r):
        sub = sales[(sales['品牌'] == r['品种']) & (sales['药房名称'] == r['药房'])]
        return round(_newp_ratio(sub, sales.groupby('患者ID')['销售时间'].min(), list(pd.period_range(wstart, wend, freq='M'))), 3)
    docwatch['药房该品种新患占比'] = docwatch.apply(newp_ratio_pharmacy, axis=1)
    def dflag(r):
        if r['DOT相对参考'] < 0.5:
            return '明显偏低-重点沟通'
        if r['药房该品种新患占比'] >= 0.5:
            return '新患致低DOT(结论)'
        return '偏低-观察'
    docwatch['风险判定'] = docwatch.apply(dflag, axis=1)
    docwatch['结论与动作'] = np.where(
        docwatch['风险判定'] == '新患致低DOT(结论)',
        '结论：低DOT由新患多导致；动作：稳住新患+增DOT+反馈药企',
        np.where(docwatch['风险判定'] == '明显偏低-重点沟通',
                 '动作：重点医生观念沟通+患者依从性管理', '观察：结合右删失/样本判断'))
    docwatch = docwatch.sort_values(['品种', 'DOT_本期']).rename(columns={'DOT_本期': '该医生DOT'})
    docwatch = docwatch[['品种', '药房', '城市', 'rank', '处方医生', '患者数', '该医生DOT',
                         '品种参考DOT', 'DOT相对参考', '药房该品种新患占比', '风险判定', '结论与动作', '主要医院']]
    return doc_df, top1, top5, docwatch

def pharmacy_dimension(sales):
    """项目药房维度：大店低 DOT 识别。"""
    wstart, wend, _, _ = compute_windows(sales)
    months = list(pd.period_range(wstart, wend, freq='M'))
    fm = sales.groupby('患者ID')['销售时间'].min()
    proj = sales[sales['角色'] == '项目'] if '角色' in sales.columns else sales
    rows = []
    for b in BRANDS:
        sub = proj[proj['品牌'] == b]
        if sub.empty:
            continue
        for ph, g in sub.groupby('药房名称'):
            n = g['患者ID'].nunique()
            if n < 5:
                continue
            d26 = _dot_snap(g, wstart, wend)
            r26 = _rolling_repurchase(g, months, fm)
            rows.append({
                '品种': b, '药房': ph,
                '城市': sales[sales['药房名称'] == ph]['城市'].dropna().iloc[0] if '城市' in sales.columns and not sales[sales['药房名称'] == ph]['城市'].dropna().empty else '',
                '患者数': n,
                'DOT_本期': round(d26, 2) if pd.notna(d26) else np.nan,
                '复购率_本期': round(r26, 3) if pd.notna(r26) else np.nan,
                '脱落率A': round(1 - r26, 3) if pd.notna(r26) else np.nan,
                '新患占比': round(_newp_ratio(g, fm, months), 3)
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    pref = {b: df[df['品种'] == b]['DOT_本期'].median() for b in BRANDS}
    df['品种参考DOT'] = df['品种'].map(pref)
    vol_med = {b: df[df['品种'] == b]['患者数'].median() for b in BRANDS}
    df['体量大'] = df.apply(lambda r: r['患者数'] >= vol_med[r['品种']], axis=1)
    def pflag(r):
        if pd.isna(r['DOT_本期']):
            return '观察'
        if r['体量大'] and r['DOT_本期'] < 0.5 * r['品种参考DOT']:
            return '大店低DOT-重点改善'
        if r['体量大'] and r['DOT_本期'] < r['品种参考DOT']:
            return '大店低DOT-观察'
        if (not r['体量大']) and r['DOT_本期'] < 0.5 * r['品种参考DOT']:
            return '小店低DOT-观察'
        return '正常'
    df['风险等级'] = df.apply(pflag, axis=1)
    return df.sort_values(['品种', '患者数'], ascending=[True, False])

def drill_doctor_for_hospital(sales, hosp_df):
    """钻取：异常医院下拖后腿的医生（按患者量 TOP5）。"""
    wstart, wend, _, _ = compute_windows(sales)
    proj = sales[sales['角色'] == '项目'] if '角色' in sales.columns else sales
    proj2 = proj.copy()
    proj2['医生列表'] = proj2['处方医生'].map(_split_doc)
    proj2 = proj2.explode('医生列表')
    proj2 = proj2[proj2['医生列表'] != '']
    rows = []
    if hosp_df.empty:
        return pd.DataFrame(rows)
    for _, hr in hosp_df[hosp_df['风险等级'].str.contains('重点沟通', na=False)].iterrows():
        b = hr['品种']
        hosp = hr['医疗单位']
        sub = proj2[(proj2['品牌'] == b) & (proj2['医疗单位'] == hosp)]
        if sub.empty:
            continue
        st = sub.groupby('医生列表').agg(
            患者数=('患者ID', 'nunique'),
            主要药房=('药房名称', lambda x: x.mode().iloc[0] if len(x.mode()) else '')
        )
        dmap = {doc: _dot_snap(g, wstart, wend) for doc, g in sub.groupby('医生列表')}
        st['DOT_本期'] = pd.Series(dmap)
        st = st.sort_values('患者数', ascending=False).head(5)
        for doc, row in st.iterrows():
            rows.append({
                '品种': b, '异常医院': hosp, '医院风险等级': hr['风险等级'],
                '患者数': int(row['患者数']), '拖后腿医生': doc,
                '该医生DOT': round(row['DOT_本期'], 2) if pd.notna(row['DOT_本期']) else np.nan,
                '主要药房': row['主要药房'],
                '医院DOT': hr['DOT_本期'], '医院脱落率A': hr['脱落率A']
            })
    return pd.DataFrame(rows).sort_values(['品种', '异常医院', '患者数'], ascending=[True, True, False])

def communication_list(hosp_df, docwatch, pharm_df):
    """半年最该沟通清单：汇总医院/医生/药房层面的重点关注对象。"""
    comm = []
    if not hosp_df.empty:
        for _, r in hosp_df[hosp_df['风险等级'].isin(['高脱落-重点沟通', '高脱落+低DOT-重点沟通', '新患致低DOT(结论)'])].iterrows():
            comm.append({
                '维度': '医院', '品种': r['品种'], '对象': r['医疗单位'], '患者数': r['患者数'],
                '关键指标': f"DOT={r['DOT_本期']},脱落={r['脱落率A']}",
                '风险等级': r['风险等级'], '来源项目药房': r['主要来源项目药房'],
                '结论与动作': r['结论与动作']
            })
    if not docwatch.empty:
        for _, r in docwatch[docwatch['风险判定'].isin(['明显偏低-重点沟通', '新患致低DOT(结论)'])].iterrows():
            comm.append({
                '维度': '医生', '品种': r['品种'], '对象': f"{r['药房']}/{r['处方医生']}",
                '患者数': r['患者数'],
                '关键指标': f"DOT={r['该医生DOT']}(参考{r['品种参考DOT']})",
                '风险等级': r['风险判定'], '来源项目药房': r['药房'],
                '结论与动作': r['结论与动作']
            })
    if not pharm_df.empty:
        for _, r in pharm_df[pharm_df['风险等级'].str.startswith('大店低DOT', na=False)].iterrows():
            comm.append({
                '维度': '药房', '品种': r['品种'], '对象': r['药房'], '患者数': r['患者数'],
                '关键指标': f"DOT={r['DOT_本期']}(参考{r['品种参考DOT']})",
                '风险等级': r['风险等级'], '来源项目药房': r['药房'],
                '结论与动作': '大店/体量大但DOT偏低，优先改善：核查续方管理/外流/老患依从性'
            })
    return pd.DataFrame(comm).sort_values(['品种', '维度', '患者数'], ascending=[True, True, False])

def improvement_actions(res=None):
    """通用改进措施 + 结合本数据实际脱落原因的专项（动态）。res 含 crossref 时追加。"""
    base = [
        {'方向': '稳住新患·提升DOT', '类型': '内部(可控)',
         '动作': '对高新患占比医院持续患者随访管理+患教，缩短新患首购→二购周期',
         '数据依据': '新患窗口 DOT 显著低于老患；成熟队列达二购率通常高于全量',
         '责任方': '患者服务运营+药房药师'},
        {'方向': '新患下降·反馈药企', '类型': '外部(药企)',
         '动作': '将各品种新患同比及"患者池仅靠老患"风险反馈药企区域代表，推动加强新患寻找',
         '数据依据': '新患月度趋势/项目药房新患变化',
         '责任方': '运营+药企代表'},
        {'方向': '高脱落医院·院内外联动', '类型': '内部+外部',
         '动作': '对脱落率>=0.4 的医院，联动对应重点医生做观念沟通+续方提醒',
         '数据依据': '医院维度高脱落+低DOT清单',
         '责任方': '药房+药企代表+医生'},
        {'方向': '老患低DOT·依从性管理', '类型': '内部(可控)',
         '动作': '对老患为主却 DOT 异常低的医生做依从性管理+多盒续方提醒，避免外流',
         '数据依据': '医生维度低 DOT 重点清单',
         '责任方': '药房药师'},
        {'方向': '大店低DOT·运营改善', '类型': '内部(可控)',
         '动作': '对体量大但 DOT 偏低的项目药房，核查续方管理、患者外流、随访执行',
         '数据依据': '项目药房风险等级',
         '责任方': '门店运营'},
    ]
    if res is None:
        return pd.DataFrame(base)
    # 动态专项1：品牌层面可控脱落占比最高的品种
    cb = res.get('crossref_brand')
    if cb is not None and not cb.empty and '可控占比%' in cb.columns:
        top = cb.dropna(subset=['可控占比%']).sort_values('可控占比%', ascending=False).head(3)
        for _, r in top.iterrows():
            ctrl = r.get('可控占比%')
            if pd.isna(ctrl):
                continue
            base.append({
                '方向': f'{r["品牌"]} · 脱落原因干预',
                '类型': '内部(可控)' if ctrl >= 50 else '混合(可控+外部)',
                '动作': f'随访脱落患者中约 {ctrl:.0f}% 为可控原因(经济/副作用/依从性/随访缺失)，建专项：续方提醒+副作用管理+患者教育',
                '数据依据': f'销售脱落率B={r.get("销售脱落率B%")}%；随访脱落可控占比={ctrl:.0f}%',
                '责任方': '患者服务运营+药房药师'})
    # 动态专项2：按药房×品种 可控脱落人数 TOP5
    drp = res.get('dropout_reason_by_pharmacy')
    if drp is not None and not drp.empty:
        ctrl_ph = drp[drp['原因类'] == '可控'].groupby(['药房', '品牌'])['脱落患者数'].sum().reset_index()
        ctrl_ph = ctrl_ph.sort_values('脱落患者数', ascending=False).head(5)
        for _, r in ctrl_ph.iterrows():
            n = int(r['脱落患者数'])
            if n <= 0:
                continue
            base.append({
                '方向': f'{r["药房"]} · {r["品牌"]} · 可控脱落治理',
                '类型': '内部(可控)',
                '动作': f'该药房{r["品牌"]}有 {n} 名患者因可控原因脱落，优先电话随访+续方提醒+到店激励',
                '数据依据': f'近似匹配随访原因=可控，{n}人',
                '责任方': '对应药房药师'})
    return pd.DataFrame(base)

def generate_word_report(res, sales_info=None):
    """生成可下载的 Word 复盘报告（关键结论 + 表格）。"""
    try:
        from docx import Document
        from docx.shared import Inches
    except ImportError:
        return None
    doc = Document()
    doc.add_heading('DTP 患者服务复盘报告', level=0)
    if sales_info:
        doc.add_paragraph(sales_info)
    # 窗口
    wi = res.get('window_info')
    if wi is not None and not wi.empty:
        r = wi.iloc[0]
        doc.add_paragraph(f"分析窗口：本期 {r['本期窗口起点']} ~ {r['本期窗口终点']}，"
                          f"同比 {r['上期窗口起点']} ~ {r['上期窗口终点']}")
    # 留存率
    doc.add_heading('一、留存率 Cohort', level=1)
    doc.add_paragraph('按首购月分群的新患队列，追踪后续每月仍在购药/在治的比例。')
    for title, key in [('口径1｜仅看购药时间', 'retention_overall'),
                       ('口径2｜结合说明书盒数覆盖', 'retention_cov_overall')]:
        df = res.get(key)
        if df is None or df.empty:
            continue
        doc.add_heading(title, level=2)
        t = doc.add_table(rows=1, cols=len(df.columns))
        t.style = 'Light Grid Accent 1'
        hdr = t.rows[0].cells
        for i, c in enumerate(df.columns):
            hdr[i].text = str(c)
        for _, row in df.iterrows():
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = '' if pd.isna(v) else str(v)
    # DOT
    doc.add_heading('二、DOT 分解（老患 vs 新患窗口）', level=1)
    dd = res.get('dot_decomposition')
    if dd is not None and not dd.empty:
        t = doc.add_table(rows=1, cols=len(dd.columns))
        t.style = 'Light Grid Accent 1'
        for i, c in enumerate(dd.columns):
            t.rows[0].cells[i].text = str(c)
        for _, row in dd.iterrows():
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = '' if pd.isna(v) else str(v)
    # 脱落率B
    doc.add_heading('三、脱落率 B（累计沉默）', level=1)
    db = res.get('dropout_B_established_by_brand')
    if db is not None and not db.empty:
        t = doc.add_table(rows=1, cols=len(db.columns))
        t.style = 'Light Grid Accent 1'
        for i, c in enumerate(db.columns):
            t.rows[0].cells[i].text = str(c)
        for _, row in db.iterrows():
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = '' if pd.isna(v) else str(v)
    # 沟通清单
    doc.add_heading('四、重点关注清单', level=1)
    cl = res.get('communication_list')
    if cl is not None and not cl.empty:
        t = doc.add_table(rows=1, cols=len(cl.columns))
        t.style = 'Light Grid Accent 1'
        for i, c in enumerate(cl.columns):
            t.rows[0].cells[i].text = str(c)
        for _, row in cl.iterrows():
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = '' if pd.isna(v) else str(v)
    else:
        doc.add_paragraph('当前数据未触发重点关注阈值。')
    # 改进措施
    doc.add_heading('五、改进措施与责任', level=1)
    ia = res.get('improvement_actions')
    if ia is not None and not ia.empty:
        t = doc.add_table(rows=1, cols=len(ia.columns))
        t.style = 'Light Grid Accent 1'
        for i, c in enumerate(ia.columns):
            t.rows[0].cells[i].text = str(c)
        for _, row in ia.iterrows():
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = '' if pd.isna(v) else str(v)
    # 口径说明
    doc.add_heading('六、口径说明', level=1)
    doc.add_paragraph(
        'DOT = 窗口内购买总盒数 / 去重患者数（盒/人），不换算月数；老患=窗口起始前首购，新患窗口=首购落在窗口内。'
        '留存率口径1仅看当月是否有购药，口径2按说明书每盒天数×购买盒数计算覆盖期。'
        '脱落率B = 末次购药距数据终点 > N×用药间隔。')
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf

# ============================ 编排 ============================
def run_analysis(sales, followup=None, max_k=12, mult=3, with_patient_crossref=True):
    result = {}
    # 窗口信息
    wstart, wend, wstart_prev, wend_prev = compute_windows(sales)
    result['window_info'] = pd.DataFrame([{
        '本期窗口起点': wstart.date(), '本期窗口终点': wend.date(),
        '上期窗口起点': wstart_prev.date(), '上期窗口终点': wend_prev.date()
    }])
    # 留存率 口径1：仅看购药时间（当月是否有购药）
    result['retention_overall'] = compute_retention(sales, max_k)
    result['retention_by_brand'] = compute_retention_by_brand(sales, max_k)
    result['retention_by_brand_pharmacy'] = compute_retention_by_brand_pharmacy(sales, max_k)
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
            cp = crossref_patient(last, followup)
            result['crossref_patient'] = cp
            # 脱落原因 × 药房 × 品种 汇总（仅已匹配到随访原因的患者）
            matched = cp[cp['关联方式'] != '无随访匹配'].dropna(subset=['原因类'])
            if not matched.empty:
                rc = matched.groupby(['药房', '品牌', '原因类']).size().reset_index(name='脱落患者数')
                result['dropout_reason_by_pharmacy'] = rc
    # ===== 新增：DOT / 新患 / 复购 / 维度分析 =====
    result['dot_decomposition'] = dot_decomposition(sales)
    result['new_patient_monthly'] = new_patient_monthly(sales)
    result['new_patient_pharmacy_decline'] = new_patient_pharmacy_decline(sales)
    result['old_patient_multi_box'] = old_patient_multi_box(sales)
    result['repurchase_decomposition'] = repurchase_decomposition(sales)
    result['hospital_dimension'] = hospital_dimension(sales)
    doc_df, top1, top5, docwatch = doctor_dimension(sales)
    result['doctor_all'] = doc_df
    result['doctor_top1'] = top1
    result['doctor_top5'] = top5
    result['doctor_low_dot_watch'] = docwatch
    result['pharmacy_dimension'] = pharmacy_dimension(sales)
    result['drill_hospital_doctor'] = drill_doctor_for_hospital(sales, result['hospital_dimension'])
    result['communication_list'] = communication_list(
        result['hospital_dimension'], result['doctor_low_dot_watch'], result['pharmacy_dimension'])
    result['improvement_actions'] = improvement_actions(result)
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
