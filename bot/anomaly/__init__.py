"""饰品异动雷达(PRD V1.0):三层池 B/A/S + AnomalyScore 综合评分。

模块划分:
- engine.py  AnomalyEngine: 评分 / 方向 / 异动类型判定(纯逻辑,可离线测试)
- pool.py    WatchPoolManager: 三层池升降级与淘汰
"""
