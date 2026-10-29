"""AnomalyEngine 离线测试(PRD V1.0 §18):不依赖真实 API,纯构造数据验证评分与类型判定。

运行: .venv\\Scripts\\python.exe test_anomaly_cases.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta

from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot
from bot.config import load_config

CFG = load_config("config.yaml")
CFG.anomaly.min_anomaly_buy_num = 0
ENGINE = AnomalyEngine(CFG.anomaly)
NOW = datetime(2026, 8, 31, 12, 0, 0)


def ramp(start, end, n=31):
    """线性渐变序列(从旧到新)。"""
    if n <= 1:
        return [end]
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def hist(prices, sells, buys, step_min=2, n=31, tail_points=45, tail_step_min=3):
    """生成历史时序(正序),最后一个点位于 step_min 分钟前。

    近端 n 点保持原始斜率(5m/30m/1h 变化与阈值标定不受影响);
    再向更早补一段「等值尾巴」(值=序列起点),使总跨度 >120 分钟——
    资金周期阶段用 phase_window_minutes=120 的窗口,历史不足则取不到锚点
    (V0.2.24 修正:原 31 点×2min=62 分钟,阶段检测恒为 None)。
    """
    out = []
    base = NOW - timedelta(minutes=step_min * n)
    for i in range(tail_points):
        ts = (base - timedelta(minutes=tail_step_min * (tail_points - i))
              ).strftime("%Y-%m-%d %H:%M:%S")
        out.append(HistoryPoint(ts=ts, price=round(float(prices[0]), 2),
                                sell_num=int(sells[0]), buy_num=int(buys[0])))
    for i in range(n):
        ts = (NOW - timedelta(minutes=step_min * (n - i))).strftime("%Y-%m-%d %H:%M:%S")
        out.append(HistoryPoint(ts=ts, price=round(prices[i], 2),
                                sell_num=int(sells[i]), buy_num=int(buys[i])))
    return out


def snap(**kw):
    base = dict(good_id=1, name="测试饰品", price=1000.0, sell_price=1000.0, buy_price=950.0,
                sell_num=100, buy_num=50, rate_7=0.0, rate_30=0.0,
                price_7=1000.0, price_30=1000.0)
    base.update(kw)
    return MetricSnapshot(**base)


CASES = []


def case(name, snap_obj, history, expect_type=None, expect_dir=None,
         expect_level_min=None, expect_level_max=None, expect_position=None, note=""):
    CASES.append((name, snap_obj, history, expect_type, expect_dir,
                  expect_level_min, expect_level_max, expect_position, note))


# Case 1 价格↑ + 求购↑ + 在售↓ -> 突然拉升 surge
case("1 突然拉升(价涨+求购增+在售减)",
     snap(price=1060, sell_price=1060, buy_price=1010, sell_num=70, buy_num=80,
          rate_7=5.0, rate_30=8.0, price_7=1000, price_30=980),
     hist(ramp(1000, 1058), ramp(100, 72), ramp(50, 78)),
     expect_type="surge", expect_dir="UP", expect_level_min="L2")

# Case 2 价格↑ + 求购↓ + 在售↑(中位) -> 上涨但供需背离 divergence
# V0.3.30:派发改成「求购不增」单边口径后,同样的形态在**高位**会归到 distribute(见 2b),
# 这里把 price_30 放到接近现价(位置≈中位),专门守住 divergence 这一支。
case("2 上涨供需背离(价涨+求购减+在售增,中位)",
     snap(price=1070, sell_price=1070, buy_price=1000, sell_num=135, buy_num=33,
          rate_7=6.0, rate_30=9.0, price_7=1000, price_30=1060),
     hist(ramp(1000, 1068), ramp(100, 132), ramp(50, 35)),
     expect_type="divergence")

# Case 2b 同一形态但在高位 -> 拉高 + 供货涌入 + 撤买单 = 派发(CS 市场标准出货)
case("2b 高位拉高出货(价涨+求购撤+在售增)",
     snap(price=1070, sell_price=1070, buy_price=1000, sell_num=135, buy_num=33,
          rate_7=6.0, rate_30=9.0, price_7=1000, price_30=980),
     hist(ramp(1000, 1068), ramp(100, 132), ramp(50, 35)),
     expect_type="distribute")

# Case 3 价格↓ + 求购↓ + 在售↑ -> 突然跳水 dump
case("3 突然跳水(价跌+求购减+在售增)",
     snap(price=930, sell_price=930, buy_price=880, sell_num=140, buy_num=30,
          rate_7=-7.0, rate_30=-10.0, price_7=1000, price_30=1030),
     hist(ramp(1000, 932), ramp(100, 138), ramp(50, 31)),
     expect_type="dump", expect_dir="DOWN")

# Case 4 价格平 + 求购↑ + 在售↓ -> 建仓 accum(需求增强的持续性加强版)
case("4 需求增强(价平+求购增+在售减)",
     snap(price=1005, sell_price=1005, buy_price=970, sell_num=62, buy_num=95,
          rate_7=0.5, rate_30=1.0, price_7=1000, price_30=995),
     hist(ramp(1000, 1005), ramp(100, 64), ramp(50, 92)),
     expect_type="accum")

# Case 5 价格↓ + 求购↑ + 在售↓ -> 下跌出现承接 stabilize
case("5 下跌后企稳(价跌+求购增+在售减)",
     snap(price=955, sell_price=955, buy_price=930, sell_num=60, buy_num=85,
          rate_7=-4.0, rate_30=-6.0, price_7=1000, price_30=1020),
     hist(ramp(1010, 957), ramp(100, 62), ramp(50, 83)),
     expect_type="stabilize")

# Case 6 低位 + 上涨 + 求购增 -> surge,重点验证"低位"位置标签(区分低位启动与高位追涨)
# 2026-09-04 语义升级:需求须绝对量达标(hist 30m 前 buy≈39 → snap 60 = +21 单,强信号)才算拉升
case("6 低位启动(价格处近期低位且上涨)",
     snap(price=770, sell_price=770, buy_price=740, sell_num=68, buy_num=60,
          rate_7=-3.0, rate_30=-8.0, price_7=1000, price_30=1050),
     hist(ramp(740, 760), ramp(100, 70), ramp(30, 48)),
     expect_type="surge", expect_position="低位")

# Case 7 冷启动:历史严重不足 -> 不得误判为强异动
case("7 冷启动(仅5个快照,指标应 unavailable)",
     snap(price=1060, sell_price=1060, buy_price=1010, sell_num=70, buy_num=80,
          rate_7=5.0, rate_30=8.0, price_7=1000, price_30=980),
     hist(ramp(1000, 1058, 5), ramp(100, 72, 5), ramp(50, 78, 5), n=5, tail_points=0),
     expect_level_max="L2", note="历史不足时按可用权重归一化,不应爆表")

# Case 8 平静商品 -> 低分
case("8 平静无异动(全平)",
     snap(price=1000, sell_price=1000, buy_price=950, sell_num=100, buy_num=50,
          rate_7=0.0, rate_30=0.0, price_7=1000, price_30=1000),
     hist(ramp(1000, 1000), ramp(100, 100), ramp(50, 50)),
     expect_level_max="L1")


def main():
    ok = fail = 0
    print(f"{'用例':<34} {'评分':>6} {'等级':>4} {'方向':>7} {'类型':>12}  结果")
    print("-" * 92)
    for (name, s, h, exp_type, exp_dir, lv_min, lv_max, exp_pos, note) in CASES:
        r = ENGINE.evaluate(s, h, now=NOW)
        errs = []
        if exp_type and r.anomaly_type != exp_type:
            errs.append(f"类型应为 {exp_type}")
        if exp_dir and r.direction != exp_dir:
            errs.append(f"方向应为 {exp_dir}")
        if exp_pos and r.details.get("position") != exp_pos:
            errs.append(f"位置应为 {exp_pos}")
        if lv_min and r.level < lv_min:
            errs.append(f"等级应 ≥{lv_min}")
        if lv_max and r.level > lv_max:
            errs.append(f"等级应 ≤{lv_max}")
        flag = "OK" if not errs else "FAIL " + "; ".join(errs)
        ok, fail = (ok + 1, fail) if not errs else (ok, fail + 1)
        print(f"{name:<34} {r.score:>6.1f} {r.level:>4} {r.direction:>7} "
              f"{r.anomaly_type:>12}  {flag}")
        if r.reasons:
            print(f"    原因: {' / '.join(r.reasons)}")
        print(f"    位置: {r.details.get('position', 'N/A')} | "
              f"5m={fmt(r.details.get('price_change_5m'))} "
              f"30m={fmt(r.details.get('price_change_30m'))} "
              f"1h={fmt(r.details.get('price_change_1h'))} | "
              f"求购={fmt(r.details.get('buy_change'))} 在售={fmt(r.details.get('sell_change'))}")
    print("-" * 92)
    print(f"结果: {ok} 通过 / {fail} 失败")
    return fail


def fmt(v):
    return "N/A" if v is None else f"{v:+.1f}%"


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
