# -*- coding: utf-8 -*-
"""
DTP 药房脱落原因清洗与用药周期状态分类核心逻辑
====================================================
所有分类规则集中在此模块，Streamlit 应用与命令行脚本均复用。

字段依赖（随访底表）：
  - 未按计划持续用药原因  : 脱落原因原始文本
  - 用药周期状态          : 周期状态原始文本
  - 患者 / 门店           : 患者定位
  - 执行时间 / 创建日期    : 判断"最新一次"随访
"""
import pandas as pd

# ============================================================
# 一、脱落原因分类框架（用户定义）
# ============================================================
# 一级：医生相关 / 患者相关 / 其他
# 二级：10 个细类
CATEGORY_DISPLAY = {
    "1_医嘱改变用药时间间隔": "遵医嘱改变用药时间间隔",
    "2_医嘱停药": "医嘱停药",
    "3_医嘱换药": "医嘱换药",
    "4_不良反应_剂量调整或停药": "患者因不良反应带来的剂量调整或停药",
    "5_认知不足而停药": "患者对疾病和药物认知不足而停药",
    "6_经济负担": "经济负担",
    "7_去世": "去世",
    "8_换渠道_区域购药": "换渠道/区域购药",
    "9_联系失败_随访困难": "联系失败/随访困难",
    "10_其他原因": "其他原因",
}

DOCTOR_CODES = ["1_医嘱改变用药时间间隔", "2_医嘱停药", "3_医嘱换药"]
PATIENT_CODES = ["4_不良反应_剂量调整或停药", "5_认知不足而停药", "6_经济负担",
                 "7_去世", "8_换渠道_区域购药", "9_联系失败_随访困难"]
OTHER_CODES = ["10_其他原因"]

LEVEL1_OF = {}
for _c in DOCTOR_CODES:
    LEVEL1_OF[_c] = "一、医生相关原因"
for _c in PATIENT_CODES:
    LEVEL1_OF[_c] = "二、患者相关原因"
for _c in OTHER_CODES:
    LEVEL1_OF[_c] = "三、其他原因"


def classify_reason(reason, status=None):
    """把一条『未按计划持续用药原因』文本归到框架分类代码。
    返回分类代码字符串；若 reason 为空则返回 None（表示无脱落原因/正常）。
    """
    if reason is None or (isinstance(reason, float) and pd.isna(reason)):
        return None
    s = str(reason).strip()
    if s == "" or s.lower() == "nan":
        return None

    # ===== 一、医生相关 =====
    # 1. 医嘱改变用药时间间隔 / 剂量
    if s in ["遵医嘱：改变用药时间间隔", "目前已经减药，延长用药间隔或减少单次用药剂量"] \
            or "改变用药时间间隔" in s or s == "遵医嘱：改变用药剂量":
        return "1_医嘱改变用药时间间隔"

    # 2. 医嘱停药
    if s in ["遵医嘱停药", "遵医嘱，疗程结束停药", "遵医嘱暂停用药",
             "疾病进展更换治疗方案，遵医嘱停药", "遵医嘱，病情变化停药/换药",
             "遵医嘱（手术/放化疗/剂量调整）", "遵医嘱，不良反应停药", "遵医嘱"] \
            or ("停药" in s and ("医嘱" in s or "遵" in s)):
        return "2_医嘱停药"

    # 3. 医嘱换药 / 改方案
    if s in ["遵医嘱换方", "目前已经换药", "疾病进展或者耐药换药", "更换方案", "遵医嘱，减量用药"] \
            or "换方" in s or ("换药" in s and ("医嘱" in s or "遵" in s)):
        return "3_医嘱换药"

    # ===== 二、患者相关 =====
    # 4. 不良反应
    if s in ["不良反应，遵医嘱停药/自主停药", "患者因为不良反应带来的剂量调整或停药", "自行减少用药剂量"] \
            or "不良反应" in s:
        return "4_不良反应_剂量调整或停药"

    # 5. 认知不足 / 自行停药 / 依从性差
    if s in ["患者自行停药", "患者其它原因自行延迟（观念不强）", "依从性差，不规律用药",
             "患者自行暂停", "自主停药，达到治疗目标停药", "疗效稳定，自主停药",
             "依从性差-患者自行延长用药间隔"] \
            or ("自行" in s and "停" in s) or "依从性差" in s or "观念不强" in s:
        return "5_认知不足而停药"

    # 6. 经济负担
    if "经济负担" in s:
        return "6_经济负担"

    # 7. 去世
    if s == "去世" or "去世" in s:
        return "7_去世"

    # 8. 换渠道 / 区域购药
    channel_kw = ["转竞争药房", "更换渠道", "转本地医院", "转院内购药", "转其他药房",
                  "回医院购药", "去异地购药", "本市换药店", "其他渠道购药",
                  "仍在用药，改为在其他药店购药", "换城市", "回医保归属地", "本店不再服务",
                  "购药不方便", "配送不方便", "转竞品", "当期转渠道", "转院内", "转医院"]
    for kw in channel_kw:
        if kw in s:
            return "8_换渠道_区域购药"
    if "渠道" in s or "转" in s:
        return "8_换渠道_区域购药"

    # 9. 联系失败 / 随访困难
    if s in ["连续两个周期无法联系/患者联系不上/拒绝随访", "未拨通/挂断",
             "此问题回访中未采集到明确答案", "未探寻到原因"] \
            or "无法联系" in s or "联系不上" in s or "拒绝随访" in s or "未拨通" in s \
            or "挂断" in s:
        return "9_联系失败_随访困难"

    # 10. 兜底
    return "10_其他原因"


# ============================================================
# 二、用药周期状态分组
# ============================================================
STATUS_GROUPS = {
    "按计划持续用药": ["按计划持续用药"],
    "改变用药间隔时间": ["未规范用药（拉长用药间隔）", "改变用药间隔时间", "推迟用药", "延迟用药", "改变用药剂量"],
    "停药——脱落": ["停药----脱落", "停药后复购"],
    "随访失败": ["随访失败", "未按计划持续用药-不依从", "其它"],
    "当期转渠道": ["当期转渠道", "转院内购药", "换药", "持续用药中--流失", "推迟购药"],
}
_STATUS_TO_GROUP = {}
for _g, _lst in STATUS_GROUPS.items():
    for _s in _lst:
        _STATUS_TO_GROUP[_s] = _g

# 有效随访判定关键词（脱落原因/状态里出现即视为无效）
REJECT_KW = ["拒绝", "拒访", "不配合", "拒绝随访"]
UNREACH_KW = ["未拨通", "挂断", "无法联系", "联系不上", "打不通"]


def status_to_group(status):
    """把『用药周期状态』原始值归到 5 个大组之一，未识别归 '其他'。"""
    if status is None or (isinstance(status, float) and pd.isna(status)):
        return None
    s = str(status).strip()
    if s == "" or s.lower() == "nan":
        return None
    return _STATUS_TO_GROUP.get(s, "其他")


# ============================================================
# 三、时间列解析（判断"最新一次"随访）
# ============================================================
DATE_COL_CANDIDATES = ["执行时间", "创建日期", "随访时间", "计划时间", "更新时间"]


def parse_row_datetime(row, date_cols):
    for c in date_cols:
        if c in row and pd.notna(row[c]):
            try:
                return pd.to_datetime(row[c])
            except Exception:
                pass
    return pd.NaT


# ============================================================
# 四、主流程：清洗随访底表
# ============================================================
def _pick_col(df, candidates):
    """按候选名列表返回 df 中第一个存在的列名。"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def clean_followup(df):
    """输入原始随访底表 DataFrame，返回：
      cleaned : 每条随访任务加了『脱落原因分类代码/名称/一级分类』『周期状态组』的明细
      meta    : 识别到的关键列名 dict
    """
    df = df.copy()
    reason_col = _pick_col(df, ["未按计划持续用药原因", "脱落/流失原因", "脱落原因"])
    status_col = _pick_col(df, ["用药周期状态", "周期状态"])
    patient_col = _pick_col(df, ["患者", "患者姓名", "会员姓名", "姓名"])
    store_col = _pick_col(df, ["门店", "药房", "药房名称"])
    date_cols = [c for c in DATE_COL_CANDIDATES if c in df.columns]

    # 分类
    if reason_col:
        df["脱落原因分类代码"] = df.apply(
            lambda r: classify_reason(r.get(reason_col), r.get(status_col) if status_col else None), axis=1)
    else:
        df["脱落原因分类代码"] = None
    df["脱落原因分类"] = df["脱落原因分类代码"].map(lambda c: CATEGORY_DISPLAY.get(c) if c else None)
    df["一级分类"] = df["脱落原因分类代码"].map(lambda c: LEVEL1_OF.get(c) if c else None)

    # 周期状态组
    if status_col:
        df["周期状态组"] = df[status_col].map(status_to_group)
    else:
        df["周期状态组"] = None

    # 时间
    if date_cols:
        df["_dt"] = df.apply(lambda r: parse_row_datetime(r, date_cols), axis=1)
    else:
        df["_dt"] = pd.NaT

    meta = {
        "reason_col": reason_col, "status_col": status_col,
        "patient_col": patient_col, "store_col": store_col,
        "date_cols": date_cols,
    }
    return df, meta


def reason_distribution(cleaned):
    """脱落原因分布（基于每条随访记录，仅统计有脱落原因的记录）。"""
    sub = cleaned[cleaned["脱落原因分类代码"].notna()]
    total = len(sub)
    rows = []
    for code, name in CATEGORY_DISPLAY.items():
        cnt = int((sub["脱落原因分类代码"] == code).sum())
        if cnt == 0:
            continue
        rows.append({
            "一级分类": LEVEL1_OF[code], "脱落原因": name,
            "记录数": cnt, "占比": round(cnt / total * 100, 2) if total else 0,
        })
    dist = pd.DataFrame(rows).sort_values("记录数", ascending=False).reset_index(drop=True)
    # 一级汇总
    lvl1 = (dist.groupby("一级分类")["记录数"].sum()
            .reset_index().sort_values("记录数", ascending=False))
    lvl1["占比"] = (lvl1["记录数"] / total * 100).round(2) if total else 0
    return dist, lvl1, total


def status_distribution(cleaned):
    """用药周期状态占比（基于每条随访记录）。"""
    sub = cleaned[cleaned["周期状态组"].notna()]
    total = len(sub)
    vc = sub["周期状态组"].value_counts()
    rows = [{"用药周期状态": k, "记录数": int(v),
             "占比": round(v / total * 100, 2) if total else 0} for k, v in vc.items()]
    return pd.DataFrame(rows), total


def extract_target_patients(df, patient_candidates=None):
    """从『患者名单 / 销售明细 / 患者明细』类文件中抽取患者姓名集合。
    返回 (names:set, matched_col:str|None)。matched_col 为识别到的患者列名，未识别则为 None。

    自动处理以下情况：
    - 常规姓名列：患者、患者姓名、会员姓名 等
    - 复合标识列：患者唯一标识（格式 "姓名|xxx" 自动拆分）
    - 多种销售底表变体列名
    """
    if patient_candidates is None:
        patient_candidates = [
            "患者", "患者姓名", "会员姓名", "会员", "姓名",
            "客户姓名", "顾客", "购药人", "病人姓名", "买家", "购药人姓名",
            # 复合标识列（需拆分）
            "患者唯一标识", "患者ID", "会员编号",
            # 更多常见别名
            "姓名 ", "患者名称", "客户", "病人", "用户名",
        ]
    col = _pick_col(df, patient_candidates)
    if not col:
        return set(), None
    raw_series = df[col].dropna().astype(str).str.strip()

    # 判断是否为"姓名|xxx"复合格式（如：刘碧容|内江第一药房）
    sample = raw_series.iloc[0] if len(raw_series) else ""
    is_composite = bool(sample) and "|" in str(sample) and len(str(sample).split("|")) >= 2

    if is_composite:
        names = raw_series.apply(lambda v: str(v).split("|")[0].strip() if "|" in str(v) else str(v).strip())
    else:
        names = raw_series

    names = names[~names.str.lower().isin(["nan", "none", "", "无", "-"])]
    return set(names.unique()), col


def patient_level_detail(cleaned, meta):
    """患者级明细：每位患者取『最新一次记录』与『最近一次有原因记录』两个视角，
    列出原始原因 ↔ 映射分类 对照。
    """
    patient_col = meta["patient_col"]
    store_col = meta["store_col"]
    reason_col = meta["reason_col"]
    status_col = meta["status_col"]
    if not patient_col:
        return pd.DataFrame()

    key_cols = [patient_col] + ([store_col] if store_col else [])
    rows = []
    for key, g in cleaned.groupby(key_cols):
        g = g.sort_values("_dt", ascending=False)
        # 最新一次
        last = g.iloc[0]
        last_reason = last.get(reason_col) if reason_col else None
        last_reason = "" if (last_reason is None or pd.isna(last_reason)) else str(last_reason).strip()
        last_map = CATEGORY_DISPLAY.get(classify_reason(last_reason), "随访未体现脱落") if last_reason else "随访未体现脱落"
        # 最近一次有原因
        gr = g[g["脱落原因分类代码"].notna()]
        if len(gr) > 0:
            lr = gr.iloc[0]
            lr_reason = str(lr.get(reason_col)).strip()
            lr_map = CATEGORY_DISPLAY.get(lr["脱落原因分类代码"], lr["脱落原因分类代码"])
        else:
            lr_reason = ""
            lr_map = "随访未体现脱落"

        if isinstance(key, tuple):
            name = key[0]
            store = key[1] if len(key) > 1 else ""
        else:
            name = key
            store = ""
        rows.append({
            "患者": name, "门店": store,
            "随访记录数": len(g),
            "最新周期状态": (str(last.get(status_col)).strip() if status_col and pd.notna(last.get(status_col)) else ""),
            "本身写(最新一次)": last_reason,
            "映射(最新一次)": last_map,
            "本身写(最近一次有原因)": lr_reason,
            "映射(最近一次有原因)": lr_map,
        })
    return pd.DataFrame(rows).sort_values(["门店", "患者"]).reset_index(drop=True)
