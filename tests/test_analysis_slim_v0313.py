# -*- coding: utf-8 -*-
"""V0.3.13 回归:/分析 与推送卡片瘦身。

老大 2026-10-10 原话:
  "这个分析有点太冗余了吧" / "扣费后到手…这一段没必要" / "【T+7语境】改为【T+7分析】"
选择:"手续费/双平台压成 1 行" + "顺便清理推送卡片"。

瘦身前:一条 /分析 34 行有效内容 —— "中期结构偏弱"说 3 遍、"买点暂无"说 2 遍、
手续费与双平台用 5 行讲同一件事(两平台在售价/求购价/到手差价/挂价对比)。

本测试锁死瘦身红线,并确认**关键信息一条没丢**:
  [1] 手续费块 ≤2 行(无跨平台价差时),旧四行算式彻底消失
  [2] 价差改说"立即出手折价 x%",不再用会被读反的"价差 +x%"
  [3] 到手行:挂卖/秒出各取到手最高的平台;费率相同只写一个数
  [4] 【T+7语境】→【T+7分析】(全仓库)
  [5] 中期结构:位置与均线并成一行
  [6] 量价块过滤"结论："(与 T+7 段同义),注/短线盘口/大盘保留
  [7] 买点块:暂无分支不再重复结论行
  [8] 周期特征行:不再重复盘口块的窗口名与百分比,保留增量/分平台/门槛
  [9] 推送卡片:盘口块已有"在售："时不重复;回退合计口径时仍补
  [10] 关键信息不丢
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.anomaly.alert import format_row_alert                       # noqa: E402
from bot.anomaly.cycle import detect_phase                           # noqa: E402
from bot.anomaly.engine import HistoryPoint, MetricSnapshot          # noqa: E402
from bot.config import AnomalyConfig, FeeConfig                      # noqa: E402
from bot.qqbot.bot import (_action_extra_lines,                      # noqa: E402
                           _format_mid_term_section, _format_t7_section,
                           _left_right_buy_points, _net_proceeds_line,
                           _proceeds_lines)

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


JOIN = lambda ls: "\n".join(ls)                                       # noqa: E731
FEE = FeeConfig()          # BUFF 2.5% / 悠悠 2%

print("=" * 74)
print("V0.3.13 /分析 + 推送卡片瘦身 —— 回归测试")
print("=" * 74)

# ---------------------------------------------------------------- [1][2] 手续费块
print("\n[1][2] 手续费块压成 1 行 + 价差文案")
base = _proceeds_lines(174.99, 163.0, FEE)
t = JOIN(base)
ok("求购价行保留", "求购价 ¥163.00" in t, base)
ok("改用「立即出手折价 7.4%」", "立即出手折价 7.4%" in t, base)
ok("不再出现「价差 +7.4%」这种会被读反的写法", "价差 +" not in t, base)
ok("无 raw 时只 2 行", len(base) == 2, base)
ok("旧四行算式块消失",
   not any(k in t for k in ("两平台在售价", "两平台求购价",
                            "按各自平台挂售价卖出到手", "扣费后到手")), base)
ok("到手行给出挂卖/秒出两个决策",
   any("挂卖" in x and "秒出" in x for x in base), base)

dual_raw = {"buff_sell_price": 174.99, "yyyp_sell_price": 179.49,
            "buff_buy_price": 163.0, "yyyp_buy_price": 173.0}
dual = _proceeds_lines(174.99, 163.0, FEE, raw=dual_raw)
td = JOIN(dual)
ok("有 raw 且价差 <5% 时不加行(仍 2 行)", len(dual) == 2, dual)
ok("挂卖选到手高的平台(悠悠 179.49×0.98=175.90 > BUFF 170.62)",
   "挂卖 悠悠 ¥175.90" in td, td)
ok("秒出选到手高的平台(悠悠 173.00×0.98=169.54 > BUFF 158.92)",
   "秒出 悠悠 ¥169.54" in td, td)

wide = _proceeds_lines(100.0, 90.0, FEE,
                       raw={"buff_sell_price": 100.0, "yyyp_sell_price": 112.0,
                            "buff_buy_price": 90.0, "yyyp_buy_price": 93.0})
ok("跨平台价差 ≥5% 仍提示(倒货线索)", any("跨平台价差" in x for x in wide), wide)
ok("价差提示带风险口径", any("未计滑点" in x for x in wide), wide)

# ---------------------------------------------------------------- [3] 到手行取值
print("\n[3] 到手行:取高平台 + 费率相同时不啰嗦")
line_low = _net_proceeds_line(100.0, 90.0, 2.5, 2.0,
                              raw={"buff_sell_price": 100.0, "yyyp_sell_price": 101.0})
ok("悠悠仅高 1 元也能翻盘(费率低 0.5%)", "挂卖 悠悠 ¥98.98" in line_low, line_low)
line_eq = _net_proceeds_line(100.0, 90.0, 2.0, 2.0)
ok("费率相同只写一个数", "到手（2%）" in line_eq and "悠悠" not in line_eq, line_eq)
line_none = _net_proceeds_line(0.0, 0.0, 2.5, 2.0)
ok("无价无求购 → 空串(不编造)", line_none == "", line_none)

# ---------------------------------------------------------------- [4] T+7 改名
print("\n[4] 【T+7语境】→【T+7分析】")
view = {"context": "中期偏弱", "position_zone": "low", "position_text": "低位",
        "rate_7": -5.5, "rate_30": -8.0, "ma20": 100.0, "dist20": -7.3, "dist60": -9.8}
t7 = _format_t7_section(view, 0.3, None, False)
ok("段落标题为【T+7分析】", t7[0] == "【T+7分析】", t7)
ok("弱势分支不再重复「中期结构偏弱」", "中期结构偏弱" not in JOIN(t7), t7)
self_name = Path(__file__).name
hits = [p.name for p in list(ROOT.glob("bot/**/*.py")) + list(ROOT.glob("*.py"))
        if "T+7语境" in p.read_text(encoding="utf-8", errors="ignore")
        and p.name != self_name]
ok("全仓库不再有【T+7语境】(本测试文件除外)", not hits, hits)

# ---------------------------------------------------------------- [5] 中期结构合并
print("\n[5] 中期结构:位置与均线并成一行")
mid = _format_mid_term_section(view)
ok("段落 3 行(原 4 行)", len(mid) == 3, mid)
ok("位置与均线同一行", ("位置：" in mid[-1] and "均线：" in mid[-1]), mid)
ok("均线两个周期都在", "20日线" in mid[-1] and "60日线" in mid[-1], mid)
ok("涨跌幅仍在第二行", "7日 -5.5% / 30日 -8.0%" in mid[1], mid)

# ---------------------------------------------------------------- [6] 量价块过滤
print("\n[6] 量价块:过滤结论、保留注与大盘")
act = {"lines": ["T+7核心判断：中期震荡", "阶段：中期震荡", "位置：中位",
                 "注：T+7主结论基于日K趋势", "短线盘口（辅助）：增(+3.2%)",
                 "当前盘口（BUFF+悠悠）：求购 100 / 在售 200",
                 "大盘：逆势走强 / 热度中等", "结论：T+7中期结构偏弱，控制追涨"]}
extra = _action_extra_lines(act)
te = JOIN(extra)
ok("结论行被过滤(避免第 3 次说偏弱)", "结论：" not in te, extra)
ok("注行保留(由调用方收口到末尾)", any(x.startswith("注：") for x in extra), extra)
ok("短线盘口保留", "短线盘口（辅助）" in te, extra)
ok("大盘保留", "大盘：逆势走强" in te, extra)

# ---------------------------------------------------------------- [7] 买点块
print("\n[7] 买点块:暂无分支不重复结论")
closes = [80.0] * 57 + [79.9, 79.6, 79.4]
bp = _left_right_buy_points(closes, closes[-1], pct_7d=-0.6, pct_30d=-0.75)
tb = JOIN(bp)
ok("暂无买点提一次", tb.count("暂无") == 1, bp)
ok("不再有「结论: 暂无买点」重复行", "结论:" not in tb, bp)
ok("观察点保留", "观察点" in tb, bp)
ok("口径行仍在(调用方收口用)", any(str(x).strip().startswith("口径") for x in bp), bp)

# ---------------------------------------------------------------- [8] 周期特征行
print("\n[8] 周期特征行:不再重复盘口块的窗口名与百分比")
cfg = AnomalyConfig()
now = datetime(2026, 10, 10, 15, 0, 0)


def build_history(with_split=True):
    pts = []
    for i, mins in enumerate((70, 60, 50, 40, 30, 20, 10, 0)):
        buy = 100 + i * 3
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
ok("建仓信号仍能触发(判定逻辑没动)", sig is not None and sig.phase == "accum",
   sig.phase if sig else None)
if sig:
    rj = " ".join(sig.reasons)
    block = JOIN(sig.lines)
    ok("特征不再重复窗口名「2 小时」", "2 小时" not in rj, rj)
    ok("特征保留绝对增量(带单数的 +N单)", re.search(r"\+\d+单", rj) is not None, rj)
    ok("特征保留分平台增量", "BUFF" in rj and "悠悠" in rj, rj)
    ok("特征保留门槛说明", "门槛" in rj, rj)
    ok("盘口块仍逐平台给变化量(信息没丢)", "在售：BUFF" in block, block)
    ok("盘口块仍给合计百分比", "合计" in block, block)

# ---------------------------------------------------------------- [9] 推送卡片
print("\n[9] 推送卡片:盘口块已有在售行时不重复")
row_split = {
    "name": "测试品", "anomaly_type": "accum", "level": "L2", "price": 100.0,
    "score": 70.0, "position": "中位",
    "reason": "盘口 2 小时:\n　求购：BUFF +5 个(52→57)  悠悠有品 +2 个(2→4)\n"
              "　在售：BUFF -3 个(567→564)  悠悠有品 +4 个(512→516)　合计 -0.5%",
    "detail": "求购/在售比 0.10"}
card1 = format_row_alert(row_split, market=None, mkt=None,
                         multi={"buff_sell_num": 564, "yyyp_sell_num": 516})
ok("盘口块已有在售 → 不再单列一行「在售：…」",
   card1.count("在售：") == 1, card1)
ok("分平台变化量仍在(信息没丢)", "564" in card1 and "516" in card1, card1)

row_plain = dict(row_split, reason="盘口 2 小时:\n　求购：BUFF +5 个(52→57)")
card2 = format_row_alert(row_plain, market=None, mkt=None,
                         multi={"buff_sell_num": 564, "yyyp_sell_num": 516})
ok("盘口块回退合计口径 → 仍补分平台在售行",
   "在售：BUFF 564  悠悠有品 516  合计 1,080" in card2, card2)

# ---------------------------------------------------------------- [10] 信息不丢
print("\n[10] 关键信息一条不丢")
ok("求购价", "求购价 ¥163.00" in td, td)
ok("到手(挂卖+秒出)", "挂卖 悠悠 ¥175.90" in td and "秒出 悠悠 ¥169.54" in td, td)
ok("两平台费率", "BUFF 2.5% / 悠悠 2%" in td, td)
ok("中期三值(7日/30日/位置/均线)", all(k in JOIN(mid) for k in
                                        ("7日", "30日", "位置：", "20日线", "60日线")), mid)
ok("T+7 判断句", len(t7) == 3 and t7[1] and t7[2], t7)
ok("买点结论与观察点", "观察点" in tb, bp)

print("\n" + "=" * 74)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
