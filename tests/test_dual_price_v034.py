# -*- coding: utf-8 -*-
"""V0.3.4 双平台价格口径(BUFF+悠悠)测试。

背景:数量口径 V0.2.20/V0.2.24 早就统一成 BUFF+悠悠,但**价格只有 BUFF**。
实测(2026-09-28,597 品):两平台售价都有 578 品,谁更低基本五五开
(悠悠 302 / BUFF 244 / 相同 32) → 只看 BUFF 会漏掉一半的更优报价。

本测试钉死四件事:
  1. 合成规则(min 售价 / max 求购)与**分档闸 + ¥5 下限**;
  2. 口径标记 scope 的语义(消费者靠它决定能不能当合成口径用);
  3. 缺平台**不能当 0**(0 会被下游当成免费);
  4. 落库往返:新列能写能读,且不传新列的旧路径不报错。
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.csqaq.market_counts import (aggregate_buff_yyyp_prices, cross_spread_pct,
                                     dual_price_columns, price_gate_limit,
                                     rank_dual_price_columns)
from bot.storage.db import SnapshotStore

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class FakeRankItem:
    """榜单对象:有 raw(接口原始行) + 已解析属性。"""

    def __init__(self, raw=None, **attrs):
        self.raw = raw or {}
        for k, v in attrs.items():
            setattr(self, k, v)


def test_gate_bands():
    print("\n[1] 分档闸 + ¥5 下限")
    check("¥4.99 不合成(低于下限)", price_gate_limit(4.99) is None)
    check("¥5.00 → 25% 档", price_gate_limit(5.0) == 25.0)
    check("¥19.99 → 25% 档", price_gate_limit(19.99) == 25.0)
    check("¥20 → 15% 档", price_gate_limit(20.0) == 15.0)
    check("¥100 → 10% 档", price_gate_limit(100.0) == 10.0)
    check("¥1000 → 8% 档", price_gate_limit(1000.0) == 8.0)
    check("¥99999 → 8% 档", price_gate_limit(99999.0) == 8.0)


def test_synthesis():
    print("\n[2] 合成规则(min 售价 / max 求购)")
    raw = {"buff_sell_price": 100.0, "yyyp_sell_price": 104.0,
           "buff_buy_price": 90.0, "yyyp_buy_price": 93.0}
    p = aggregate_buff_yyyp_prices(raw)
    check("售价取两平台较低", p["sell_price"] == 100.0 and p["sell_scope"] == "buff_yyyp")
    check("求购取两平台较高", p["buy_price"] == 93.0 and p["buy_scope"] == "buff_yyyp")
    check("偏差百分比 = 悠悠/BUFF-1", abs(p["sell_dev_pct"] - 4.0) < 1e-9)

    raw2 = {"buff_sell_price": 100.0, "yyyp_sell_price": 96.0,
            "buff_buy_price": 90.0, "yyyp_buy_price": 88.0}
    p2 = aggregate_buff_yyyp_prices(raw2)
    check("悠悠更便宜时取悠悠", p2["sell_price"] == 96.0)
    check("悠悠求购更低时取 BUFF", p2["buy_price"] == 90.0)

    # 高价位用 8% 闸:1000 与 1070 → +7% 通过;1000 与 1090 → +9% 超闸
    p3 = aggregate_buff_yyyp_prices({"buff_sell_price": 1000.0, "yyyp_sell_price": 1070.0})
    check("¥1000 档 +7% 在闸内(8%)", p3["sell_scope"] == "buff_yyyp" and p3["sell_price"] == 1000.0)
    p4 = aggregate_buff_yyyp_prices({"buff_sell_price": 1000.0, "yyyp_sell_price": 1090.0})
    check("¥1000 档 +9% 超闸 → 不合成", p4["sell_price"] is None and p4["sell_scope"] == "buff_conflict")
    check("超闸时偏差仍保留(套利线索)", abs(p4["sell_dev_pct"] - 9.0) < 1e-9)


def test_missing_platform_never_zero():
    print("\n[3] 缺平台不能当 0")
    p = aggregate_buff_yyyp_prices({"buff_sell_price": 50.0, "buff_buy_price": 45.0})
    check("只有 BUFF → scope=buff_only", p["sell_scope"] == "buff_only")
    check("只有 BUFF → 合成价为 None(不是 0)", p["sell_price"] is None)
    check("缺悠悠 → yyyp 价为 None", p["yyyp_sell_price"] is None)

    p2 = aggregate_buff_yyyp_prices({"yyyp_sell_price": 50.0})
    check("只有悠悠 → scope=yyyp_only", p2["sell_scope"] == "yyyp_only")

    p3 = aggregate_buff_yyyp_prices({})
    check("两侧都没有 → scope=None 且价格 None",
          p3["sell_scope"] is None and p3["sell_price"] is None and p3["buy_price"] is None)

    p4 = aggregate_buff_yyyp_prices({"buff_sell_price": 0, "yyyp_sell_price": -3})
    check("0/负数视为缺失(不是免费)", p4["sell_scope"] is None and p4["sell_price"] is None)

    p5 = aggregate_buff_yyyp_prices({"buff_sell_price": 3.0, "yyyp_sell_price": 3.4})
    check("¥3 品两平台都有 → below_floor(不合成)", p5["sell_scope"] == "below_floor"
          and p5["sell_price"] is None)


def test_camel_case_and_columns():
    print("\n[4] camelCase 兼容与落库列")
    p = aggregate_buff_yyyp_prices({"buffSellPrice": 200.0, "yyypSellPrice": 202.0,
                                    "buffBuyPrice": 180.0, "yyypBuyPrice": 184.0})
    check("批量查价接口的 camelCase 也认", p["sell_price"] == 200.0 and p["buy_price"] == 184.0)

    cols = dual_price_columns({"buff_sell_price": 200.0, "yyyp_sell_price": 202.0})
    check("列名齐全", set(cols) == {"yyyp_sell_price", "yyyp_buy_price", "total_sell_price",
                                    "total_buy_price", "total_sell_price_scope",
                                    "total_buy_price_scope"})
    check("合成售价写入列", cols["total_sell_price"] == 200.0)
    check("缺平台时 yyyp 列写 0(与 buff_buy_price 风格一致)",
          dual_price_columns({"buff_sell_price": 200.0})["yyyp_sell_price"] == 0.0
          and dual_price_columns({"buff_sell_price": 200.0})["total_sell_price"] is None)

    spread = cross_spread_pct({"buff_sell_price": 100.0, "yyyp_sell_price": 108.0})
    check("跨平台价差 = +8%", abs(spread - 8.0) < 1e-9)
    check("缺一侧 → 价差 None", cross_spread_pct({"buff_sell_price": 100.0}) is None)


def test_rank_item_columns():
    print("\n[5] 榜单对象(RankItem)取值")
    raw = {"buff_sell_price": 300.0, "yyyp_sell_price": 297.0, "yyyp_buy_price": 280.0}
    it = FakeRankItem(raw=raw, buff_sell_price=300.0, yyyp_sell_price=297.0)
    cols = rank_dual_price_columns(it)
    check("用 raw 拿到悠悠求购价", cols["yyyp_buy_price"] == 280.0)
    check("合成售价=min(300,297)", cols["total_sell_price"] == 297.0)

    it2 = FakeRankItem(raw={}, buff_sell_price=50.0, yyyp_sell_price=51.0, buff_buy_price=45.0)
    cols2 = rank_dual_price_columns(it2)
    check("无 raw 时用属性兜底", cols2["total_sell_price"] == 50.0
          and cols2["total_sell_price_scope"] == "buff_yyyp")
    check("悠悠求购缺失 → 不合成求购", cols2["total_buy_price"] is None)


def test_db_roundtrip():
    print("\n[6] 落库往返(新列能写能读,旧路径不报错)")
    tmp = os.path.join(tempfile.gettempdir(), "test_dual_v034.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    store = SnapshotStore(tmp)
    store.save_listing_history_batch([{
        "good_id": 1, "name": "双平台品", "ts": "2026-09-28 10:00:00",
        "buff_sell_num": 10, "steam_sell_num": 1, "buff_buy_num": 2, "steam_buy_num": 0,
        "buff_sell_price": 100.0, "buff_buy_price": 90.0,
        **dual_price_columns({"buff_sell_price": 100.0, "yyyp_sell_price": 98.0,
                              "buff_buy_price": 90.0, "yyyp_buy_price": 92.0}),
    }, {
        # 旧调用路径:完全不传任何新列(迁移后老代码/老数据场景)
        "good_id": 2, "name": "老路径品", "ts": "2026-09-28 10:00:00",
        "buff_sell_num": 5, "steam_sell_num": 0, "buff_buy_num": 1, "steam_buy_num": 0,
        "buff_sell_price": 60.0,
    }])
    import sqlite3
    conn = sqlite3.connect(tmp)
    row = conn.execute("SELECT yyyp_sell_price, yyyp_buy_price, total_sell_price, "
                       "total_buy_price, total_sell_price_scope FROM item_listing_history "
                       "WHERE good_id=1").fetchone()
    check("双平台列写入正确", row[0] == 98.0 and row[1] == 92.0 and row[2] == 98.0
          and row[3] == 92.0 and row[4] == "buff_yyyp")
    row2 = conn.execute("SELECT total_sell_price, total_sell_price_scope, yyyp_sell_price "
                        "FROM item_listing_history WHERE good_id=2").fetchone()
    check("旧路径不报错且合成列为 NULL", row2[0] is None and row2[1] is None and row2[2] == 0.0)
    conn.close()
    os.remove(tmp)


def test_analysis_lines():
    print("\n[7] /分析 文案(双平台行)")
    from bot.qqbot.bot import _proceeds_lines
    from bot.config import FeeConfig

    fees = FeeConfig()
    base = _proceeds_lines(100.0, 90.0, fees)
    check("不传 raw → 没有悠悠价时不提悠悠",
          any("求购价 ¥90.00" in x for x in base)
          and not any("两平台" in x or "挂卖 悠悠" in x or "秒出 悠悠" in x
                      for x in base))
    dual = _proceeds_lines(100.0, 90.0, fees,
                           raw={"buff_sell_price": 100.0, "yyyp_sell_price": 104.0,
                                "buff_buy_price": 90.0, "yyyp_buy_price": 93.0})
    # V0.3.13:4 行算式压成 1 行结论。悠悠价更高 + 费率更低 → 挂卖/秒出都赢。
    check("给 raw → 一行给到手(挂卖 悠悠 ¥101.92)",
          any("挂卖 悠悠 ¥101.92" in x for x in dual))
    check("给 raw → 一行给到手(秒出 悠悠 ¥91.14)",
          any("秒出 悠悠 ¥91.14" in x for x in dual))
    check("到手行只占一行且带两个平台费率",
          sum(1 for x in dual if x.startswith("到手（")) == 1
          and any("BUFF 2.5% / 悠悠 2%" in x for x in dual))
    check("旧的逐平台罗列行已删",
          not any("两平台在售价" in x or "两平台求购价" in x
                  or "按各自平台挂售价卖出到手" in x for x in dual))
    check("±5% 以内不提示跨平台价差", not any("跨平台价差" in x for x in dual))

    dual2 = _proceeds_lines(100.0, 90.0, fees,
                            raw={"buff_sell_price": 100.0, "yyyp_sell_price": 112.0,
                                 "buff_buy_price": 90.0, "yyyp_buy_price": 93.0})
    check("超 5% 提示跨平台价差", any("跨平台价差" in x for x in dual2))
    only_buff = _proceeds_lines(100.0, 90.0, fees, raw={"buff_sell_price": 100.0})
    check("只有 BUFF 时不多说一句",
          not any("两平台" in x or "挂卖 悠悠" in x or "秒出 悠悠" in x
                  for x in only_buff))


def test_batch_row_builder():
    print("\n[8] 批量查价行构造器(共享,_listing_row_from_batch)")
    from bot.anomaly.runner import _listing_row_from_batch

    # 批量查价回包:camelCase,带悠悠在售数 + 悠悠售价(无 buy 字段,与该接口既有事实一致)
    row = {"buffSellNum": 100, "yyypSellNum": 80, "steamSellNum": 5,
           "buffSellPrice": 120.0, "yyypSellPrice": 118.0}
    built = _listing_row_from_batch(1, "品A", row, "2026-09-28 11:00:00")
    for key in ("yyyp_sell_price", "yyyp_buy_price", "total_sell_price",
                "total_buy_price", "total_sell_price_scope", "total_buy_price_scope"):
        check(f"行内含 {key}", key in built)
    check("合成售价 = min(BUFF 120, 悠悠 118)",
          built["total_sell_price"] == 118.0 and built["total_sell_price_scope"] == "buff_yyyp")
    check("无买盘字段 → 求购不合成(不按 0 处理)",
          built["total_buy_price"] is None and built["total_buy_price_scope"] is None)

    # 没有悠悠价的回包(老接口/字段缺失) → 口径标 buff_only,行为与 V0.3.3 一致
    built2 = _listing_row_from_batch(2, "品B", {"buffSellNum": 10, "yyypSellNum": 3,
                                                "buffSellPrice": 50.0}, "2026-09-28 11:00:00")
    check("缺悠悠售价 → buff_only 且合成价 NULL",
          built2["total_sell_price_scope"] == "buff_only" and built2["total_sell_price"] is None)


if __name__ == "__main__":
    print("=" * 68)
    print("V0.3.4 双平台价格口径测试")
    print("=" * 68)
    test_gate_bands()
    test_synthesis()
    test_missing_platform_never_zero()
    test_camel_case_and_columns()
    test_rank_item_columns()
    test_db_roundtrip()
    test_analysis_lines()
    test_batch_row_builder()
    print("\n" + "=" * 68)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    raise SystemExit(1 if FAIL else 0)
