# -*- coding: utf-8 -*-
"""冒烟测试：验证修正后的脱落原因分类 / 覆盖率 / 双视角 是否正常。"""
import pandas as pd
import dtp_engine as E

SRC = r'F:/厂家项目/阿斯利康/2026/半年复盘/英飞凡、利普卓、泰瑞沙、优赫得6.26.xlsx'
F25 = 'E:/下载内容/历史任务 - 2026-07-22T105636.443.xls'
F26 = 'E:/下载内容/历史任务 - 2026-07-22T105440.252.xls'

print('APP_VERSION =', E.APP_VERSION)
sales = E.load_sales(SRC)
print('销售行数:', len(sales), '患者:', sales['患者ID'].nunique())
print('销售底表列名样本:', [c for c in sales.columns][:15])
followup = E.load_followup([F25, F26])
print('\n随访行数:', len(followup))
print('随访列名样本:', [c for c in followup.columns])
print('\n随访 一级分类 分布:')
print(followup['一级分类'].value_counts(dropna=False).to_string())
print('\n随访 脱落原因分类代码 TOP:')
print(followup['脱落原因分类代码'].value_counts(dropna=False).head(12).to_string())

res = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True)

print('\n===== 脱落原因细分类分布 =====')
d = res.get('dropout_reason_detail')
print(d.to_string(index=False) if d is not None and not d.empty else 'EMPTY')
print('\n===== 一级分类汇总 =====')
print(res.get('dropout_reason_lvl1').to_string(index=False) if res.get('dropout_reason_lvl1') is not None else 'EMPTY')
print('\n===== 覆盖率 meta =====')
print(res.get('dropout_reason_meta'))
print('\n===== 跨表覆盖率概览 =====')
cov = res.get('crossref_coverage')
print(cov.to_string(index=False) if cov is not None and not cov.empty else 'EMPTY')
cp = res.get('crossref_patient')
if cp is not None and not cp.empty:
    print('\n跨表患者级 match_rate=%.1f%% low_match=%s' % (cp.attrs.get('match_rate',0)*100, cp.attrs.get('low_match')))
    print('患者级样例(5行):')
    cols = [c for c in ['患者姓名','品牌','药房','关联方式','最新一次原因(随访)','最新一次分类','脱落原因(随访)','脱落原因分类','一级分类'] if c in cp.columns]
    print(cp[cols].head(5).to_string(index=False))
print('\n===== 脱落原因×药房×品种(样本) =====')
drp = res.get('dropout_reason_by_pharmacy')
print(drp.head(10).to_string(index=False) if drp is not None and not drp.empty else 'EMPTY')
print('\n===== 患者级双视角明细(随访侧, 5行) =====')
pld = res.get('patient_level_detail')
print(pld.head(5).to_string(index=False) if pld is not None and not pld.empty else 'EMPTY')
print('\n===== crossref_brand =====')
print(res.get('crossref_brand').to_string(index=False) if res.get('crossref_brand') is not None else 'EMPTY')
print('\nALL KEYS:', sorted(res.keys()))
print('\nSMOKE TEST OK')
