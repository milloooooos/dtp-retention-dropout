"""验证：新患口径改为「按药房首购」后，跨药房转入应被正确计为该药房新患。"""
import os, sys, pandas as pd, numpy as np
sys.path.insert(0, os.getcwd())
import dtp_engine as E

# 构造合成数据
rows = [
    # P1: 2025-03 在非项目药房 NP 首购泰瑞沙；2026-03 在项目药房 PP 再购泰瑞沙（跨药房转入）
    dict(商品名称='泰瑞沙', 销售时间='2025-03-10', 销售数量=1, 患者ID='P1', 药房名称='NP', 处方医生='D1', 医疗单位='H1'),
    dict(商品名称='泰瑞沙', 销售时间='2026-03-10', 销售数量=1, 患者ID='P1', 药房名称='PP', 处方医生='D1', 医疗单位='H1'),
    # P2: 2026-01 在 PP 首购泰瑞沙
    dict(商品名称='泰瑞沙', 销售时间='2026-01-10', 销售数量=1, 患者ID='P2', 药房名称='PP', 处方医生='D2', 医疗单位='H1'),
    # P3: 2026-02 在 PP 首购利普卓
    dict(商品名称='利普卓', 销售时间='2026-02-10', 销售数量=1, 患者ID='P3', 药房名称='PP', 处方医生='D2', 医疗单位='H2'),
]
df = pd.DataFrame(rows)
sales = E.load_sales(df)  # 归一化：品牌/ym 等
# 手动打角色：PP=项目, NP=非项目
sales['角色'] = sales['药房名称'].map({'PP': '项目', 'NP': '非项目'})

wstart, wend = pd.Timestamp(2026, 1, 1), pd.Timestamp(2026, 6, 30)

print('=== 按药房首购：泰瑞沙 × PP 的新患_本期 应为 2 (P1 跨转 + P2) ===')
decl = E.new_patient_pharmacy_decline(sales, '项目', wstart, wend)
print(decl[decl['药房'] == 'PP'][['品种', '药房', '新患_本期', '新患_上期']].to_string(index=False))

print('\n=== 月度趋势（按药房首购，首购品种归因） ===')
mon = E.new_patient_monthly(sales)
print(mon.to_string(index=False))

print('\n=== 药房维度 新患占比（PP，合成数据患者数<5 会被阈值过滤，符合预期） ===')
pd_dim = E.pharmacy_dimension(sales, '项目', wstart, wend)
if pd_dim is None or pd_dim.empty:
    print('(空表：合成数据 PP 患者数不足 5，被 n<5 阈值过滤——真实数据不受影响)')
else:
    print(pd_dim[pd_dim['药房'] == 'PP'][['品种', '药房', '患者数', '新患占比']].to_string(index=False))

print('\n=== run_analysis 端到端冒烟（scope=项目） ===')
res = E.run_analysis(sales, followup=None, preset='custom', custom_start='2026-01-01', custom_end='2026-06-30', scope='项目')
print('run_analysis OK, keys =', len(res))
decl2 = res.get('new_patient_pharmacy_decline')
print('药房新患同比(项目) 行数 =', 0 if decl2 is None or decl2.empty else len(decl2))

# 断言
tar = decl[(decl['品种'] == '泰瑞沙') & (decl['药房'] == 'PP')]
assert tar['新患_本期'].iloc[0] == 2, f"FAIL: 期望泰瑞沙×PP 新患_本期=2, 实际={tar['新患_本期'].iloc[0]}"
np_row = decl[(decl['品种'] == '利普卓') & (decl['药房'] == 'PP')]
assert np_row['新患_本期'].iloc[0] == 1, f"FAIL: 利普卓×PP 应=1"
print('\nPASS：跨药房转入(P1)被正确计为项目药房 PP 的新患(2026-03)；全局首购2025-03不再排除它。')
print('APP_VERSION =', E.APP_VERSION)
