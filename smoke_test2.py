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

for preset, cs, ce in [('H1_2026', None, None), ('roll1y', None, None), ('full', None, None),
                        ('custom', '2026-01-01', '2026-07-31')]:
    print('\n################## PRESET = %s ##################' % preset)
    res = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True,
                         preset=preset, custom_start=cs, custom_end=ce)
    print('window_info:')
    print(res.get('window_info').to_string(index=False))
    print('\n===== 脱落率B 分品种 (终点=窗口终点) =====')
    print(res.get('dropout_B_by_brand').to_string(index=False) if res.get('dropout_B_by_brand') is not None and not res.get('dropout_B_by_brand').empty else 'EMPTY')
    print('\n===== 脱落原因细分类分布 (窗内) =====')
    d = res.get('dropout_reason_detail')
    print(d.to_string(index=False) if d is not None and not d.empty else 'EMPTY')
    print('\n===== 覆盖率 meta =====')
    print(res.get('dropout_reason_meta'))
    print('\n===== 脱落原因 按品种拆分 (头部) =====')
    drb = res.get('dropout_reason_by_brand')
    print(drb.head(20).to_string(index=False) if drb is not None and not drb.empty else 'EMPTY')

print('\n===== H1_2026 跨表患者级/双视角 抽样 =====')
res = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True, preset='H1_2026')
cov = res.get('crossref_coverage')
print(cov.to_string(index=False) if cov is not None and not cov.empty else 'EMPTY')
cp = res.get('crossref_patient')
if cp is not None and not cp.empty:
    print('match_rate=%.1f%% low_match=%s' % (cp.attrs.get('match_rate',0)*100, cp.attrs.get('low_match')))
pld = res.get('patient_level_detail')
print(pld.head(5).to_string(index=False) if pld is not None and not pld.empty else 'EMPTY')
print('\n===== DOT 口径验证：方案A(固定1年窗)后,H1 与 custom 是否可比 =====')
r_h1 = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True, preset='H1_2026')
r_cu = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True,
                      preset='custom', custom_start='2026-01-01', custom_end='2026-07-31')
dd_h1 = r_h1.get('dot_decomposition')
dd_cu = r_cu.get('dot_decomposition')
if dd_h1 is not None and dd_cu is not None and not dd_h1.empty:
    print('H1_2026  DOT窗口:', r_h1['window_info'].iloc[0]['DOT口径(盒/人,不按月归一)'])
    print('custom   DOT窗口:', r_cu['window_info'].iloc[0]['DOT口径(盒/人,不按月归一)'])
    m = dd_h1[['品种', 'DOT_本期_全']].merge(dd_cu[['品种', 'DOT_本期_全']], on='品种', suffixes=('_H1', '_custom'))
    m['差异'] = (m['DOT_本期_全_custom'] - m['DOT_本期_全_H1']).round(2)
    print(m.to_string(index=False))
    print('=> 两者皆为「窗口终点前推1年」固定窗,量级应接近(差异<=1-2盒属正常区间错位);不再是短窗机械压低')

print('\n===== 复购率B (结合用药周期 / 首购→二购 on-cycle) =====')
rb = r_h1.get('repurchase_B_by_brand')
rbe = r_h1.get('repurchase_B_established_by_brand')
print('复购率B 分品种(全量首购患者):')
print(rb.to_string(index=False) if rb is not None and not rb.empty else 'EMPTY')
print('复购率B 已观察(剔除右删失):')
print(rbe.to_string(index=False) if rbe is not None and not rbe.empty else 'EMPTY')
assert rb is not None and not rb.empty, '复购率B 分品种为空!'
assert (rb['按周期复购数'] <= rb['首购患者数']).all(), '复购率B: 按周期复购数 > 首购患者数!'
assert (rbe['已观察_按周期复购数'] <= rbe['已观察患者数']).all(), '复购率B: 已观察按周期数 > 已观察患者数!'
print('=> 复购率B 校验通过(on_cycle<=total, 已观察<=total);注意复购率B≠(1-脱落率B)')

print('\nALL KEYS:', sorted(res.keys()))
print('\nSMOKE TEST OK')
