# -*- coding: utf-8 -*-
"""校验 run_analysis 返回的 result 是否包含 app.py 引用的所有 key（v23b 加固后）"""
import sys
sys.path.insert(0, r'F:/work buddy内容存储/2026-07-21-20-28-39')
import dtp_engine as E

SRC = r'F:/厂家项目/阿斯利康/2026/半年复盘/英飞凡、利普卓、泰瑞沙、优赫得6.26.xlsx'
F25 = 'E:/下载内容/历史任务 - 2026-07-22T105636.443.xls'
F26 = 'E:/下载内容/历史任务 - 2026-07-22T105440.252.xls'

sales = E.load_sales(SRC)
followup = E.load_followup([F25, F26])
print('APP_VERSION', E.APP_VERSION, flush=True)

# scope 两端都验一遍
for scope in ['项目药房', '全部药房']:
    res = E.run_analysis(sales, followup, max_k=12, mult=3, with_patient_crossref=True,
                         preset='H1_2026', scope=scope)
    need = ['retention_overall', 'retention_by_brand', 'retention_cov_overall',
            'retention_cov_by_brand', 'dropout_A', 'dropout_B_by_brand',
            'dropout_B_established_by_brand', 'repurchase_B_by_brand',
            'repurchase_B_established_by_brand', 'dot_decomposition', 'new_patient_monthly',
            'old_patient_multi_box', 'repurchase_decomposition', 'new_patient_pharmacy_decline',
            'hospital_dimension', 'doctor_low_dot_watch', 'doctor_top1', 'doctor_top5',
            'pharmacy_dimension', 'drill_hospital_doctor', 'crossref_brand',
            'dropout_reason_by_pharmacy', 'communication_list', 'improvement_actions',
            'action_map', 'window_info']
    missing = [k for k in need if k not in res]
    print('scope=%s MISSING KEYS: %s' % (scope, missing), flush=True)
    for k in ['repurchase_B_by_brand', 'repurchase_B_established_by_brand', 'dropout_B_by_brand']:
        v = res.get(k)
        print('  %s -> present, empty=%s' % (k, (v.empty if hasattr(v, 'empty') else 'NA')), flush=True)

print('KEYS_CHECK_DONE', flush=True)
