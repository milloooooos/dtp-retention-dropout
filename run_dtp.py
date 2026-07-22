# -*- coding: utf-8 -*-
"""本地批量运行 dtp_engine，产出 Excel 分析报告（留存率/脱落率A/脱落率B/跨表原因）。"""
import pandas as pd, os
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
import dtp_engine as E

OUT = 'output'
os.makedirs(OUT, exist_ok=True)
SRC = r'F:/厂家项目/阿斯利康/2026/半年复盘/英飞凡、利普卓、泰瑞沙、优赫得6.26.xlsx'
F25 = 'E:/下载内容/历史任务 - 2026-07-22T105636.443.xls'
F26 = 'E:/下载内容/历史任务 - 2026-07-22T105440.252.xls'

print('加载数据...')
sales = E.load_sales(SRC)
followup = E.load_followup([F25, F26])
print(f'  销售: {len(sales)} 行, 患者 {sales["患者ID"].nunique()}, 时间 {sales["销售时间"].min().date()}~{sales["销售时间"].max().date()}')
print(f'  随访: {len(followup)} 行, 状态类分布 {followup["状态类"].value_counts().to_dict()}')

res = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True)

xlsx = os.path.join(OUT, 'DTP_留存率与脱落分析_v1.xlsx')
with pd.ExcelWriter(xlsx, engine='openpyxl') as xw:
    # 说明
    notes = pd.DataFrame({'说明': [
        'DTP 留存率 / 脱落率(A滚动+B累计沉默) / 跨表脱落原因 自动分析',
        '数据源：销售底表 + 随访历史任务表（2025H1+2026H1）',
        '',
        '【留存率】按首购月分群（=新患队列），Mk留存% = 该群在首购后第k月"有购药"的患者占比。',
        '  注意：① 为"当月有购药"口径，因多盒购买可非单调递减；② 近期cohort窗口不足显示为空(右删失)。',
        '',
        '【脱落率A·滚动】基准月M-2有购药、观察窗M-1∪M无购药→脱落。月度口径，H1取月均。',
        '',
        '【脱落率B·累计沉默】末次购药距数据终点 > 3×用药间隔→真停药。新近患者(首购在终点前3×间隔内)',
        '  免除右删失，故另给"已观察%"列（仅统计有充分观察期的患者）。',
        '',
        '【跨表关联】销售算出的脱落患者 → 随访表的脱落原因(可控/不可控映射)。',
        '  两表无共同患者主键(会员号格式不同)，故：① 品牌层面关联为主(可靠)；② 近似患者级(姓名+品种+首购月)',
        '  仅作补充，同名歧义大、匹配率有限，置信度低。',
        '  随访表不含优赫得(0行)，故优赫得跨表原因为空(覆盖缺口，非错误)。',
        '',
        '【脱落映射】原因→可控/不可控分类规则见 action_map 及 dtp_engine.classify_reason。',
    ]})
    notes.to_excel(xw, sheet_name='说明', index=False)
    res['retention_overall'].to_excel(xw, sheet_name='1_留存率_整体', index=False)
    res['retention_by_brand'].to_excel(xw, sheet_name='2_留存率_分品种', index=False)
    res['dropout_A']['整体_月度'].to_excel(xw, sheet_name='3_脱落率A_整体月度', index=False)
    if '分品种_月度' in res['dropout_A']:
        res['dropout_A']['分品种_月度'].to_excel(xw, sheet_name='4_脱落率A_分品种', index=False)
    if '分药房_月度' in res['dropout_A']:
        res['dropout_A']['分药房_月度'].to_excel(xw, sheet_name='5_脱落率A_分药房', index=False)
    res['dropout_B_by_brand'].to_excel(xw, sheet_name='6_脱落率B_分品种', index=False)
    res['dropout_B_established_by_brand'].to_excel(xw, sheet_name='6b_脱落率B_已观察', index=False)
    if 'crossref_brand' in res:
        res['crossref_brand'].to_excel(xw, sheet_name='7_跨表关联_品牌层面', index=False)
    if 'crossref_patient' in res:
        res['crossref_patient'].to_excel(xw, sheet_name='8_跨表关联_患者级近似', index=False)
    res['action_map'].to_excel(xw, sheet_name='9_可控脱落行动建议', index=False)

# 简单样式
from openpyxl import load_workbook
wb = load_workbook(xlsx)
hdr_fill = PatternFill('solid', fgColor='4472C4')
for ws in wb.worksheets:
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for col in ws.columns:
        w = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 10), 40)
    ws.freeze_panes = 'A2'
wb.save(xlsx)

print('\n已生成:', xlsx)
print('\n=== 留存率 整体(首尾) ===')
print(res['retention_overall'].head(3).to_string(index=False))
print(res['retention_overall'].tail(3).to_string(index=False))
print('\n=== 脱落率B 分品种(全部) ===')
print(res['dropout_B_by_brand'].to_string(index=False))
print('\n=== 脱落率B 分品种(已观察) ===')
print(res['dropout_B_established_by_brand'].to_string(index=False))
if 'crossref_brand' in res:
    print('\n=== 跨表关联 品牌层面 ===')
    print(res['crossref_brand'].to_string(index=False))
