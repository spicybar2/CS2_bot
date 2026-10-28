"""验证「老鼠仓」可行性：后来的大涨，之前在盘口上留过建仓痕迹吗？

做法（日级重采样）：
- 拉升事件：某品某天的价格比 7 天前 ≥ +15%
- 建仓痕迹：拉升窗口**之前**那一周里，求购数大幅上升 或 在售数大幅下降
- 对照：全样本里出现同样痕迹的基础概率

如果 P(痕迹|拉升) ≈ P(痕迹)，说明建仓在求购/在售上看不见，老鼠仓抓不到。

    .venv\\Scripts\\python.exe tools\\probe_accum_frontrun.py
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict

PUMP_TH = 15.0          # 拉升定义：7 日涨幅 ≥ 15%
BUY_UP_TH = 30.0        # 建仓痕迹：求购数一周内 +30%
SELL_DOWN_TH = -20.0    # 建仓痕迹：在售数一周内 -20%
MIN_DAYS = 20


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price > 0 order by good_id, ts""").fetchall()
    c.close()

    # 日级重采样：每天取最后一个采样点
    days = defaultdict(dict)
    for r in rows:
        days[r["good_id"]][r["ts"][:10]] = (
            r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"])

    pump_total = pump_with_buy = pump_with_sell = pump_with_both = 0
    all_buy = all_sell = all_both = 0
    total_days = 0
    pump_items = set()

    for gid, daily in days.items():
        dts = sorted(daily)
        if len(dts) < MIN_DAYS:
            continue
        total_days += len(dts)
        for i in range(7, len(dts) - 7):
            p_now = daily[dts[i]][0]
            p_prev = daily[dts[i - 7]][0]
            if not p_now or not p_prev:
                continue
            # 看 [i-14, i-7] 这一周有没有建仓痕迹
            win = dts[max(0, i - 14):i - 6]
            if len(win) < 5:
                continue
            buys = [daily[d][2] for d in win if daily[d][2]]
            sells = [daily[d][1] for d in win if daily[d][1]]
            buy_up = (max(buys) / min(buys) - 1) * 100 if len(buys) > 2 and min(buys) else 0.0
            sell_dn = (min(sells) / max(sells) - 1) * 100 if len(sells) > 2 and max(sells) else 0.0
            has_buy = buy_up >= BUY_UP_TH
            has_sell = sell_dn <= SELL_DOWN_TH
            has_both = has_buy and has_sell

            is_pump = (p_now / p_prev - 1) * 100 >= PUMP_TH
            all_buy += has_buy
            all_sell += has_sell
            all_both += has_both
            if is_pump:
                pump_items.add(gid)
                pump_total += 1
                pump_with_buy += has_buy
                pump_with_sell += has_sell
                pump_with_both += has_both

    print(f"样本：{len(days)} 个品、累计 {total_days} 个品种-日")
    print(f"拉升事件（7 日涨幅 ≥{PUMP_TH:.0f}%）：{pump_total} 次，涉及 {len(pump_items)} 个品\n")

    def pct(n, d):
        return 100.0 * n / d if d else 0.0

    print("=== 拉升前一周，盘口上出现建仓痕迹的比例 ===")
    print(f"{'痕迹':<22}{'拉升组':>18}{'全样本基准':>18}{'倍数':>10}")
    for label, n_pump, n_all in (
        (f"求购 +{BUY_UP_TH:.0f}%", pump_with_buy, all_buy),
        (f"在售 {SELL_DOWN_TH:.0f}%", pump_with_sell, all_sell),
        ("两者同时", pump_with_both, all_both),
    ):
        a = pct(n_pump, pump_total)
        b = pct(n_all, total_days)
        print(f"  {label:<20}{a:>16.1f}%{b:>17.1f}%{(a / b if b else 0):>9.2f}x")

    print("\n结论判据：倍数 ≈ 1.0 表示痕迹与拉升无关（抓不到建仓）；≥2 才有跟庄价值。")


if __name__ == "__main__":
    main()
