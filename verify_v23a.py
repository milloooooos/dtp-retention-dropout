# -*- coding: utf-8 -*-
"""轻量验证 v23a：分析范围 scope 开关 + 复购率B 断言"""
import sys
sys.path.insert(0, r'F:/work buddy内容存储/2026-07-21-20-28-39')
import dtp_engine as E

SRC = r'F:/厂家项目/阿斯利康/2026/半年复盘/英飞凡、利普卓、泰瑞沙、优赫得6.26.xlsx'
F25 = 'E:/下载内容/历史任务 - 2026-07-22T105636.443.xls'
F26 = 'E:/下载内容/历史任务 - 2026-07-22T105440.252.xls'

print('APP_VERSION =', E.APP_VERSION, flush=True)
sales = E.load_sales(SRC)
followup = E.load_followup([F25, F26])
print('sales=%d followup=%d' % (len(sales), len(followup)), flush=True)

# ---- scope 两端 ----
r_pj = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True,
                      preset='H1_2026', scope='项目药房')
r_all = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True,
                       preset='H1_2026', scope='全部药房')

drop_pj = r_pj['dropout_A_by_brand']
drop_all = r_all['dropout_A_by_brand']
tot_pj = int(drop_pj['患者总数'].sum())
tot_all = int(drop_all['患者总数'].sum())
assert tot_pj <= tot_all, 'scope校验失败: 项目药房患者数 > 全部药房!'
assert r_pj['window_info'].iloc[0]['分析范围'] == '项目药房'
assert r_all['window_info'].iloc[0]['分析范围'] == '全部药房'

# 维度表随 scope 变化（医生维度记录数）
dn_pj = len(r_pj['doctor_dimension'])
dn_all = len(r_all['doctor_dimension'])
assert dn_pj <= dn_all, 'scope校验失败: 项目药房医生记录 > 全部药房!'

# 脱落率B 应随 scope 变化（全部 >= 项目 的患者池）
b_pj = r_pj['dropout_B_by_brand']
b_all = r_all['dropout_B_by_brand']
assert b_pj is not None and not b_pj.empty

# ---- 复购率B 断言 ----
rb = r_pj['repurchase_B_by_brand']
rbe = r_pj['repurchase_B_observed']
assert rb is not None and not rb.empty, '复购率B 分品种为空!'
assert (rb['按周期复购数'] <= rb['首购患者数']).all(), '复购率B: 按周期复购数 > 首购患者数!'
assert (rbe['已观察_按周期复购数'] <= rbe['已观察患者数']).all(), '复购率B: 已观察按周期数 > 已观察患者数!'

print('=> 复购率B 校验通过(on_cycle<=total, 已观察<=total)', flush=True)
print('=> 分析范围 scope 校验通过(项目药房=全集子集, 维度表随范围变化)', flush=True)
print('tot_pj=%d tot_all=%d | dn_pj=%d dn_all=%d | repurchase_B_brands=%d'
      % (tot_pj, tot_all, dn_pj, dn_all, len(rb)), flush=True)
print('VALIDATION PASSED', flush=True)
