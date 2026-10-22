# -*- coding: utf-8 -*-
"""V0.3.12:盘口数量的分平台展示(BUFF / 悠悠有品 各自的变化量与当前值)。

背景(2026-10-10):推送里原来只有「求购(BUFF+悠悠) 52→59(+7/+13.5%)」这种合计口径,
读的人看不出是哪一家在动。改成「求购：BUFF +5 个(52→57)  悠悠有品 +2 个(2→4)」。

本测试锁死:
  [1] normalize_book 的反推保护:只有 total_sell_scope=='buff_yyyp' 才允许
      「合计 − BUFF」还原悠悠在售;缺失/负数一律 None(绝不能显示错数)
  [2] 任一平台缺字段 → 整体 None,调用方回退合计文案(不用 0 冒充)
  [3] 输出格式与需求一致:变化量在前、当前值在后
  [4] detect_phase 的盘口块**优先分平台**、字段不全时**自动回退合计**
  [5] 异动推送卡片不再出现「BUFF+悠悠在售：A + B ≈ C」这种合计读法
  [6] 选品推荐行真的会渲染盘口(note 此前从不被渲染 → 改用 book 字段)
  [7] 判定口径未被改动(合计 sell_num/buy_num 与百分比仍按原样参与判定)

无网络请求。
用法: .venv\\Scripts\\python.exe -X utf8 test_book_split_v0312.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.anomaly.alert import format_row_alert                      # noqa: E402
from bot.anomaly.cycle import detect_phase                          # noqa: E402
from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot  # noqa: E402
from bot.analyzer.recommender import Recommender                    # noqa: E402
from bot.config import AnomalyConfig, RecommendConfig               # noqa: E402
from bot.csqaq.market_counts import (book_split_inline, book_split_line,  # noqa: E402
                                     normalize_book)

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


print("=" * 72)
print("V0.3.12 分平台盘口展示 —— 单元测试")
print("=" * 72)

# ---------- [1][2] normalize_book 的取值与保护 ----------
print("\n[1] normalize_book:直读 / 反推 / 保护")
direct = {"buff_sell_num": 567, "yyyp_sell_num": 512,
          "buff_buy_num": 57, "yyyp_buy_num": 4}
n = normalize_book(direct)
ok("直读四平台", n == {"buff_sell": 567, "yyyp_sell": 512,
                       "buff_buy": 57, "yyyp_buy": 4}, n)

hist = {"buff_sell_num": 567, "total_sell_num": 1079, "total_sell_scope": "buff_yyyp",
        "buff_buy_num": 52, "yyyp_buy_num": 2}
n = normalize_book(hist)
ok("历史行反推悠悠在售 = 合计 − BUFF", n and n["yyyp_sell"] == 512, n)

bad_scope = dict(hist, total_sell_scope=None)
ok("scope 不是 buff_yyyp → 拒绝反推(整行 None)", normalize_book(bad_scope) is None)

neg = dict(hist, total_sell_num=100)
ok("反推为负数 → 拒绝(整行 None)", normalize_book(neg) is None)

no_buy = {"buff_sell_num": 1, "yyyp_sell_num": 2, "buff_buy_num": 3}
ok("缺 yyyp_buy_num → None(不用 0 冒充)", normalize_book(no_buy) is None)

ok("空输入 → None", normalize_book(None) is None and normalize_book({}) is None)

zero_ok = {"buff_sell_num": 0, "yyyp_sell_num": 5, "buff_buy_num": 0, "yyyp_buy_num": 1}
n = normalize_book(zero_ok)
ok("真实的 0 值保留(0 与缺失不同)", n and n["buff_sell"] == 0 and n["buff_buy"] == 0, n)

# 大写/字符串数字(接口偶发字符串)
loose = {"buff_sell_num": "567", "yyyp_sell_num": "512.0",
         "buff_buy_num": "57", "yyyp_buy_num": 4}
ok("字符串数字可解析", normalize_book(loose) is not None)

# ---------- [3] 输出格式 ----------
print("\n[3] 输出格式(变化量在前、当前值在后)")
base = {"buff_sell": 567, "yyyp_sell": 512, "buff_buy": 52, "yyyp_buy": 2}
cur = {"buff_sell": 564, "yyyp_sell": 516, "buff_buy": 57, "yyyp_buy": 4}
s = book_split_line(base, cur, "sell")
ok("在售行格式", s == "在售：BUFF -3 个(567→564)  悠悠有品 +4 个(512→516)", s)
s = book_split_line(base, cur, "buy")
ok("求购行格式", s == "求购：BUFF +5 个(52→57)  悠悠有品 +2 个(2→4)", s)
ok("inline 紧凑版", book_split_inline(base, cur, "buy") == "BUFF +5/悠悠有品 +2",
   book_split_inline(base, cur, "buy"))
ok("缺一侧 → None", book_split_line(None, cur, "sell") is None
   and book_split_line(base, None, "buy") is None)

# ---------- [4] detect_phase:优先分平台 + 回退 ----------
print("\n[4] detect_phase 盘口块:分平台优先 / 字段不全回退合计")
cfg = AnomalyConfig()
now = datetime(2026, 10, 10, 15, 0, 0)


def build_history(with_split=True):
    pts = []
    for i, mins in enumerate((70, 60, 50, 40, 30, 20, 10, 0)):
        buy = 100 + i * 3                      # 单调递增 → 满足"逐段走高"
        d = {"ts": (now - timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S"),
             "price": 100.0, "sell_num": 500, "buy_num": buy, "buy_price": 10.0}
        if with_split:
            d.update({"buff_sell_num": 200, "yyyp_sell_num": 300,
                      "buff_buy_num": buy - 20, "yyyp_buy_num": 20})
        pts.append(HistoryPoint(ts=d["ts"], price=d["price"], sell_num=d["sell_num"],
                                buy_num=d["buy_num"], buy_price=d["buy_price"],
                                buff_sell_num=d.get("buff_sell_num"),
                                yyyp_sell_num=d.get("yyyp_sell_num"),
                                buff_buy_num=d.get("buff_buy_num"),
                                yyyp_buy_num=d.get("yyyp_buy_num")))
    return pts


snap = MetricSnapshot(good_id=1, name="测试品", price=100.0, sell_num=498,
                      buy_num=126, buy_price=10.0, yyyp_sell_num=298,
                      buff_sell_num=200, buff_buy_num=104, yyyp_buy_num=22,
                      buff_yyyp_complete=True)
sig = detect_phase(snap, build_history(True), now, cfg, price_chg_60=0.4)
ok("建仓信号触发", sig is not None and sig.phase == "accum",
   sig.phase if sig else None)
if sig:
    joined = "\n".join(sig.lines)
    ok("盘口块出现分平台在售行", "在售：BUFF" in joined and "悠悠有品" in joined, joined)
    ok("盘口块出现分平台求购行", "求购：BUFF" in joined, joined)
    ok("保留合计百分比", "合计" in joined, joined)
    ok("不再出现旧合计写法", "在售(BUFF+悠悠)" not in joined, joined)
    reason_join = " ".join(sig.reasons)
    ok("reason 内嵌分平台增量", "BUFF +" in reason_join or "BUFF -" in reason_join,
       reason_join)

sig2 = detect_phase(snap, build_history(False), now, cfg, price_chg_60=0.4)
ok("历史缺分平台字段时仍能判定", sig2 is not None and sig2.phase == "accum")
if sig2:
    joined2 = "\n".join(sig2.lines)
    ok("回退到合计口径", "求购(BUFF+悠悠)" in joined2 and "在售(BUFF+悠悠)" in joined2,
       joined2)
    ok("回退时不出现半截分平台行", "悠悠有品" not in joined2, joined2)

# ---------- [5] 推送卡片 ----------
print("\n[5] 推送卡片:不再出现 'BUFF+悠悠在售：A + B ≈ C'")
row = {"name": "测试品", "anomaly_type": "accum", "level": "L2", "price": 100.0,
       "score": 70.0, "position": "中位", "reason": "盘口 2 小时:\n　求购：BUFF +5 个(52→57)  悠悠有品 +2 个(2→4)",
       "detail": "求购/在售比 0.10"}
multi = {"buff_sell_num": 549, "yyyp_sell_num": 512}
card = format_row_alert(row, market=None, mkt=None, multi=multi)
ok("分平台在售行", "在售：BUFF 549  悠悠有品 512  合计 1,061" in card, card)
ok("旧合计写法消失", "≈ 1,061" not in card and "BUFF+悠悠在售：" not in card, card)

# ---------- [6] 选品推荐行真的渲染盘口 ----------
print("\n[6] 选品推荐:book 字段进入 format_line(note 此前从不渲染)")
rec_rows = [{"good_id": 1, "name": "测试品", "main_price": 100.0,
             "sell_price_7": 120.0, "sell_price_rate_7": -18.0,
             "buff_price_chg": 0.5, "buff_sell_num": 200, "yyyp_sell_num": 300,
             "buff_buy_num": 40, "yyyp_buy_num": 20}]
rec = Recommender(RecommendConfig()).recommend(rec_rows, "low_7d")
ok("产出一条推荐", len(rec.items) == 1, len(rec.items))
if rec.items:
    line = rec.items[0].format_line(1)
    ok("format_line 含分平台在售", "在售 BUFF 200/悠悠有品 300" in line, line)
    ok("format_line 含分平台求购", "求购 BUFF 40/悠悠有品 20" in line, line)
rec_no = Recommender(RecommendConfig()).recommend(
    [dict(rec_rows[0], yyyp_sell_num=None, yyyp_buy_num=None)], "low_7d")
ok("字段不全 → 不显示盘口行(不是半截数据)",
   bool(rec_no.items) and "在售" not in rec_no.items[0].format_line(1))

# ---------- [7] 判定口径未变 ----------
print("\n[7] 判定口径未被改动")
eng = AnomalyEngine(cfg)
res = eng.evaluate(snap, build_history(True), now)
ok("评分结果正常产出", res is not None and res.good_id == 1)
ok("合计在售仍参与判定(sell_num 口径不变)", snap.sell_num == 498)
det = {}
eng._score_order(build_history(True), snap, now, {}, det, [], [])
ok("细节里仍记录合计变化", "buy_change" in det and "sell_change" in det, det)
reasons2 = []
# 专门构造"30 分钟窗口买盘强信号"的数据:100 → 180(+80%,绝对 +80 ≥ 高档 15),
# 才能走到评分理由里那条带分平台拆解的文案。
# 注意 _MIN_POINTS={5:3, 30:10, 60:20}:30 分钟窗口**至少要 10 个历史点**,
# 所以按 2 分钟一个点(80 分钟 → 41 点),否则 _past_point 直接返回 None。
strong_pts = []
for mins in range(80, -1, -2):
    buy = 100 if mins >= 30 else 180
    strong_pts.append(HistoryPoint(
        ts=(now - timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S"),
        price=100.0, sell_num=500, buy_num=buy, buy_price=10.0,
        buff_sell_num=200, yyyp_sell_num=300, buff_buy_num=buy - 20, yyyp_buy_num=20))
strong_snap = MetricSnapshot(good_id=1, name="测试品", price=100.0, sell_num=500,
                             buy_num=180, buy_price=10.0, yyyp_sell_num=300,
                             buff_sell_num=200, buff_buy_num=160, yyyp_buy_num=20,
                             buff_yyyp_complete=True)
eng._score_order(strong_pts, strong_snap, now, {}, {}, reasons2, [])
ok("评分理由带分平台(展示层增强)", any("BUFF" in r for r in reasons2), reasons2)
ok("分平台是增量形式", any("BUFF +" in r for r in reasons2), reasons2)

print("\n" + "=" * 72)
print(f"  结果:{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
