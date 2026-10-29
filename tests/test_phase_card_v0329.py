# -*- coding: utf-8 -*-
"""V0.3.29 周期阶段卡片:结论必须随状态变化(位置决定含义)。

核心回归点
------------------------------------------------------------------
同一句「求购 +14%」在低位是吸筹迹象、在高位是诱多预警。
本用例守的是:标题/判读/结论/尾注四处都不能写死,必须由 (阶段, 区间位置) 决定。

另守两条口径:
1. 卡片上不出现实现细节([L2] 等级、异动评分、门槛单数、n=34 这类自曝);
2. 统计段必须带「统计截至」日期,且现价不在统计档位时要明说。
"""
import pathlib as _pl
import sys as _sys

_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta

from bot.anomaly import phase_rules
from bot.anomaly.alert import _format_phase_alert
from bot.anomaly.cycle import detect_phase
from bot.anomaly.engine import HistoryPoint, MetricSnapshot
from bot.config import AnomalyConfig

CFG = AnomalyConfig()
BASE = datetime(2026, 10, 29, 12, 0, 0)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def make_history(buy_seq, sell_seq, price_seq, buy_price_seq=None, step_min=5):
    n = len(buy_seq)
    start = BASE - timedelta(minutes=step_min * (n - 1))
    pts = []
    for i in range(n):
        ts = (start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%d %H:%M:%S")
        pts.append(HistoryPoint(ts=ts, price=float(price_seq[i]),
                                sell_num=int(sell_seq[i]), buy_num=int(buy_seq[i]),
                                buy_price=float((buy_price_seq or price_seq)[i])))
    return pts


def flat(n, v):
    return [v] * n


def snap(buy=1140, sell=3000, price=100.0, buy_price=95.0):
    return MetricSnapshot(good_id=1, name="测试品", price=price, sell_price=price,
                          buy_price=buy_price, sell_num=sell, buy_num=buy,
                          rate_7=0.0, rate_30=0.0, price_7=price * 0.98,
                          price_30=price * 0.95)


BUY_SEQ = [1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140]
HIST = make_history(BUY_SEQ, flat(13, 3000), flat(13, 100.0), flat(13, 95.0))


def card_of(sig) -> str:
    row = {"anomaly_type": sig.phase, "level": "L2", "name": "测试品",
           "price": 100.0, "score": 73.0, "position": sig.position,
           "reason": "\n".join(sig.lines), "detail": "\n".join(sig.reading)}
    return _format_phase_alert(row)


# ---------------------------------------------------------------- [1] 状态计算
def test_buckets():
    print("\n[1] 区间分位与分档(边界口径)")
    check("分位计算", abs(phase_rules.range_pct((60.0, 200.0), 100.0) - 28.571) < 0.01)
    check("区间数据缺失 → None", phase_rules.range_pct(None, 100.0) is None)
    check("区间退化(最高=最低) → None", phase_rules.range_pct((100.0, 100.0), 100.0) is None)
    check("价格缺失 → None", phase_rules.range_pct((60.0, 200.0), 0) is None)
    check("30.0 分位算低位", phase_rules.pos_of(30.0) == "低位")
    check("30.1 分位算中位", phase_rules.pos_of(30.1) == "中位")
    check("69.9 分位算中位", phase_rules.pos_of(69.9) == "中位")
    check("70.0 分位算高位", phase_rules.pos_of(70.0) == "高位")
    check("分位缺失回退中位(最保守)", phase_rules.pos_of(None) == "中位")


# ---------------------------------------------------------------- [2] 同一信号·三种位置
def test_same_signal_three_positions():
    print("\n[2] 同一句「求购 +14%」在三种位置下结论必须不同")
    cases = [
        ("低位", (60.0, 200.0), "👁", "低位吸筹迹象"),
        ("中位", (60.0, 140.0), "👁", "建仓迹象"),
        ("高位", (60.0, 110.0), "🚨", "高位诱多预警"),
    ]
    readings = {}
    for label, rng, icon, title in cases:
        sig = detect_phase(snap(), HIST, BASE, CFG, price_chg_60=0.0, range_30d=rng)
        check(f"{label} 触发建仓", sig is not None and sig.phase == "accum")
        if sig is None:
            continue
        check(f"{label} → 位置={label}", sig.position == label, sig.position)
        check(f"{label} → 标题「{title}」", sig.title == title, sig.title)
        check(f"{label} → 图标 {icon}", sig.icon == icon, sig.icon)
        readings[label] = "\n".join(sig.reading)
        card = card_of(sig)
        check(f"{label} 卡片首行含图标与标题", card.splitlines()[0] == f"{icon} {title}")

    low, mid, high = readings.get("低位", ""), readings.get("中位", ""), readings.get("高位", "")
    check("三种位置的判读互不相同", len({low, mid, high}) == 3)
    # 判读句必须与「历史参照」用同一个数字:求购价没抬时增量 +1.1pp,不能说成「值得跟踪」
    check("低位判读按数据说话(未抬价→不说明有资金进场)",
          "不说明有资金进场" in low and "诱多" not in low, low)
    check("高位结论说「诱多」", "诱多" in high and "不说明有资金进场" not in high, high)
    check("中位结论说「难以区分」", "难以区分" in mid)
    check("低位卡不再堆尾注(结论段已说完)", "不构成买点" not in card_of(
        detect_phase(snap(), HIST, BASE, CFG, price_chg_60=0.0, range_30d=(60.0, 200.0))))
    check("低位结论仍给出否定判断", "仍非正期望" in card_of(
        detect_phase(snap(), HIST, BASE, CFG, price_chg_60=0.0, range_30d=(60.0, 200.0))))
    check("高位尾注:不建议介入", "不建议介入" in card_of(
        detect_phase(snap(), HIST, BASE, CFG, price_chg_60=0.0, range_30d=(60.0, 110.0))))


# ---------------------------------------------------------------- [3] 卡片不出现实现细节
def test_card_hygiene():
    print("\n[3] 卡片不出现实现细节与写死结论")
    sig = detect_phase(snap(), HIST, BASE, CFG, price_chg_60=0.0, range_30d=(60.0, 140.0))
    card = card_of(sig)
    check("无 [L2]/[L3] 等级标签", "[L2]" not in card and "[L3]" not in card)
    check("无「异动评分」", "异动评分" not in card)
    check("无门槛实现细节", "门槛" not in card)
    check("无 n=34 这类小样本自曝", "n=" not in card)
    check("无 pp 缩写堆砌", card.count("pp") <= 2)
    check("统计段带「统计截至」", "统计截至" in card)
    check("统计段给出样本量与涨跌", "个品" in card and "典型涨跌" in card)
    check("统计段带同位置基准对照", "基准" in card)
    check("仍无成交量类词", not any(w in card for w in ("成交量", "放量", "缩量", "天量")))
    check("保留免责句", "不构成买卖建议" in card)


# ---------------------------------------------------------------- [4] 统计适用性
def test_stat_scope():
    print("\n[4] 统计档位不适用时必须明说")
    cheap = snap(buy=1140, sell=3000, price=8.0, buy_price=7.0)
    sig = detect_phase(cheap, HIST, BASE, CFG, price_chg_60=0.0, range_30d=(6.0, 9.0))
    check("低价品仍能判建仓", sig is not None and sig.phase == "accum")
    if sig:
        txt = "\n".join(sig.reading)
        check("提示不在统计范围内", "不在统计范围内" in txt, txt)
        check("说明统计档位", "≥¥100" in txt, txt)
    lines = phase_rules.stat_lines("accum", "低位", 5000.0)
    check("高价品给出统计", any("次（" in x or "例" in x for x in lines)
          and not any("不在统计" in x for x in lines))
    lines_unknown = phase_rules.stat_lines("accum", "低位", None)
    check("价格未知时不误报档位", not any("不在统计" in x for x in lines_unknown))


# ---------------------------------------------------------------- [5] 其他阶段同款排版
def test_other_phases():
    print("\n[5] 启动/派发沿用同款排版与统计口径")
    for phase in ("launch", "distribute"):
        icon, title = phase_rules.headline(phase, "高位")
        check(f"{phase} 标题非空", bool(title) and bool(icon))
        lines = phase_rules.stat_lines(phase, "高位", 500.0)
        check(f"{phase} 统计段有内容",
              bool(lines) and any("次（" in x or "例" in x for x in lines), str(lines))
        foot = phase_rules.footer(phase, "高位")
        check(f"{phase} 尾注非空", bool(foot.strip()))


# ---------------------------------------------------------------- [6] 采样稀疏提示
def test_thin_sampling():
    print("\n[6] 采样点不足时如实提示")
    lines = phase_rules.reading("accum", "低位", 10.0, 14.0, 0.0, 0.0, 2.6, samples=2)
    check("1~2 个采样点时提示可靠性", any("采样点" in x and "有限" in x for x in lines),
          str(lines))
    lines_ok = phase_rules.reading("accum", "低位", 10.0, 14.0, 0.0, 0.0, 2.6, samples=8)
    check("采样充足时不提示", not any("采样点" in x for x in lines_ok))


# ---------------------------------------------------------------- [7] 派发:求购撤单也算
def test_distribute_buy_withdraw():
    print("\n[7] 派发「求购不增」单边口径(CS 市场:庄家先撤买单再出货)")
    def dist_hist(buy_seq, sell_seq):
        return make_history(buy_seq, sell_seq, [100.0] * len(buy_seq), [95.0] * len(buy_seq))

    # 在售 +6.7%、价格贴区间最高、求购 -20%(撤单) —— 旧 abs() 口径会判「不是停滞」而漏掉
    h_wd = dist_hist([1000 - i * 16 for i in range(13)], [3000 + i * 16 for i in range(13)])
    sig_wd = detect_phase(snap(buy=808, sell=3192), h_wd, BASE, CFG,
                          price_chg_60=0.0, range_30d=(60.0, 100.0))
    check("求购 -20% 仍判派发", sig_wd is not None and sig_wd.phase == "distribute",
          sig_wd.phase if sig_wd else None)
    if sig_wd:
        check("原因文案写明「撤单」", any("撤单" in r for r in sig_wd.reasons), str(sig_wd.reasons))

    # 求购 +9%(上升)—— 实测该组明显更好,必须继续排除
    h_up = dist_hist([1000 + i * 7 for i in range(13)], [3000 + i * 16 for i in range(13)])
    sig_up = detect_phase(snap(buy=1084, sell=3192), h_up, BASE, CFG,
                          price_chg_60=0.0, range_30d=(60.0, 100.0))
    check("求购 +8% 不算派发", not (sig_up is not None and sig_up.phase == "distribute"),
          sig_up.phase if sig_up else None)


# ---------------------------------------------------------------- [8] 统计口径不伪造粒度
def test_stat_scope_label():
    print("\n[8] 启动/派发统计不按位置伪造粒度")
    lines = phase_rules.stat_lines("distribute", "高位", 500.0)
    joined = "\n".join(lines)
    check("派发给合计口径", "不分位置合计" in joined, joined)
    check("派发不再假装有分位置样本", "高位形态" not in joined, joined)
    lines_a = phase_rules.stat_lines("accum", "高位", 500.0)
    check("建仓仍按位置给", "高位形态" in "\n".join(lines_a), "\n".join(lines_a))


# ---------------------------------------------------------------- [9] 抬价收货子形态
def test_bid_lift():
    print("\n[9] 「抬价收货」:求购价上移 vs 原地堆量,结论必须分开")
    # 求购量 +14%、卖价横盘、在售不动;唯一区别是最高求购价 95 → 98(+3.2%)
    bp_seq = [95.0 + i * 0.25 for i in range(13)]
    h_lift = make_history(BUY_SEQ, flat(13, 3000), flat(13, 100.0), bp_seq)
    sig = detect_phase(snap(buy=1140, buy_price=98.0), h_lift, BASE, CFG,
                       price_chg_60=0.0, range_30d=(60.0, 200.0))
    check("抬价版仍判建仓", sig is not None and sig.phase == "accum",
          sig.phase if sig else None)
    if sig:
        txt = "\n".join(sig.reading)
        check("判读点明「抬价收货」", "抬价收货" in txt, txt)
        check("判读写明求购价上移幅度", "上移" in txt, txt)
        check("统计用抬价组扣费后胜率 20.1%", "20.1" in txt, txt)
        check("统计带扣去手续费后胜率", "扣去手续费后胜率" in txt and "2.56" in txt, txt)
        check("不印 T+24h（建仓按 7 天持有看）", "T+24h" not in txt, txt)
        # 只留两行：胜率(概率) 与 典型涨跌(幅度)，都自带标签
        check("典型涨跌单独一行带标签", "典型涨跌　-1.22%" in txt, txt)
        check("不再印上涨率（与胜率重复）", "上涨率" not in txt, txt)
        check("结论依据改用胜率", "胜率较基准" in txt, txt)
        check("不用「中位」这种统计黑话", "中位" not in txt, txt)
        check("低位不再堆重复尾注", phase_rules.footer("accum+lift", "低位") == "")
        check("盘中行显示求购价上移", any("上移" in ln for ln in sig.lines),
              str(sig.lines))

    sig_flat = detect_phase(snap(buy=1140, buy_price=95.0), HIST, BASE, CFG,
                            price_chg_60=0.0, range_30d=(60.0, 200.0))
    if sig_flat:
        txt2 = "\n".join(sig_flat.reading)
        check("未抬价版不讲抬价", "抬价收货" not in txt2, txt2)
        check("未抬价版统计用堆量组胜率 15.6%",
              "扣去手续费后胜率　T+7 15.6%" in txt2, txt2)
        check("两种版本结论不同", txt2 != "\n".join(sig.reading) if sig else False)


def main():
    test_buckets()
    test_same_signal_three_positions()
    test_card_hygiene()
    test_stat_scope()
    test_other_phases()
    test_thin_sampling()
    test_distribute_buy_withdraw()
    test_stat_scope_label()
    test_bid_lift()
    print(f"\n通过 {PASS} 项, 失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    _sys.exit(main())
