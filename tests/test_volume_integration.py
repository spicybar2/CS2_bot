# -*- coding: utf-8 -*-
"""单品 Steam 成交量接入回归。"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta
import os
import tempfile

from bot.analyzer.price_action import analyze_price_action
from bot.anomaly.alert import format_row_alert
from bot.anomaly.runner import (
    _latest_known_buy, _listing_row_from_batch, _snapshot_from_batch,
)
from bot.csqaq.volume import normalize_turnover, turnover_change
from bot.csqaq.market_counts import aggregate_market_counts
from bot.storage.db import SnapshotStore


def main() -> None:
    now = datetime.now()
    fresh = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    stale = "2024-01-01T00:00:00"

    m = normalize_turnover({"turnover_number": 14, "turnover_avg_price": 60.9,
                            "period_at": fresh}, now=now)
    assert m["turnover_fresh"] is True
    assert abs(turnover_change(120, 100) - 20.0) < 1e-9
    counts = aggregate_market_counts({
        "buffBuyNum": 115, "buffSellNum": 1019,
        "steamBuyNum": 11858, "steamSellNum": 471,
        "yyypBuyNum": 121, "yyypSellNum": 3447,
        "c5BuyNum": 85, "c5SellNum": 738,
        "igxeBuyNum": 26, "igxeSellNum": 116,
        "ecoBuyNum": 37, "ecoSellNum": 821,
    })
    # V0.2.11: steamBuyNum(Steam 无求购机制,脏数据)被剔除 → 求购合计不含 11858
    assert counts["buy_num"] == 115 + 121 + 85 + 26 + 37      # = 384
    assert counts["sell_num"] == 6612
    assert counts["steam_buy_num"] is None          # 剔除后显式 None
    assert counts["buy_complete"] is True           # steam-buy 缺失不影响完整性判定

    action = analyze_price_action(
        price=100, price_change_30m=2, transaction_volume=120,
        previous_transaction_volume=100, transaction_period_at=fresh,
        buy_num=12242, sell_num=6612, book_scope="全平台",
    )
    assert action["volume_fresh"] is True
    assert abs(action["volume_change"] - 20.0) < 1e-9
    # V0.2.9: 该用例未提供盘口数据(buy/sell_num 与变化率均缺) → 旧版伪装"量平价涨",
    # 现应显式为"盘口未知·价涨"
    assert action["relation"] == "盘口未知·价涨"
    assert action["phase"] == "盘口数据不足"
    assert "盘口量价解算" in action["lines"][0]
    assert "当前盘口（全平台）：求购 12,242 / 在售 6,612" in "\n".join(action["lines"])

    # A/S 批量查价快照转换：验证 camelCase 字段落库且不混入 Steam 求购。
    batch_row = {
        "buffSellPrice": 100.0, "buffBuyPrice": 95.0,
        "buffSellNum": 56, "buffBuyNum": 8, "yyypSellNum": 740,
        "steamSellNum": 11, "steamBuyNum": 99999,
        "sell_price_rate_7": 3.0, "sell_price_rate_30": 12.0,
    }
    batch_snap = _snapshot_from_batch(13493, "AK-47 | 抽象派 1337", batch_row)
    batch_hist = _listing_row_from_batch(
        13493, "AK-47 | 抽象派 1337", batch_row, "2026-09-09 12:00:00")
    assert batch_snap.sell_num == 796 and batch_snap.buy_num == 0
    assert batch_hist["total_sell_num"] == 796
    assert batch_hist["total_sell_scope"] == "buff_yyyp"
    # V0.2.24: 求购侧统一 BUFF+悠悠——行内有 buy 字段时写合计(BUFF 8 + 悠悠 0)
    assert batch_hist["total_buy_num"] is None
    assert batch_hist["total_buy_scope"] is None
    assert batch_hist["yyyp_buy_num"] == 0
    # 行内有悠悠求购时，只有一个 buy 字段仍视为缺失（批量接口不提供求购）。
    yyyp_buy_row = dict(batch_row, yyypBuyNum=5)
    yyyp_hist = _listing_row_from_batch(
        13493, "AK-47 | 抽象派 1337", yyyp_buy_row, "2026-09-09 12:00:00")
    assert yyyp_hist["total_buy_num"] == 13 and yyyp_hist["yyyp_buy_num"] == 5
    # 批量查价真实回包没有任何 buy 字段 → 依赖 carry-forward 保住口径连续
    missing_buy_row = dict(batch_row)
    missing_buy_row.pop("buffBuyNum")
    missing_hist = _listing_row_from_batch(
        13493, "AK-47 | 抽象派 1337", missing_buy_row, "2026-09-09 12:00:00")
    assert missing_hist["buff_buy_num"] is None and missing_hist["total_buy_num"] is None
    carry_hist = _listing_row_from_batch(
        13493, "AK-47 | 抽象派 1337", missing_buy_row, "2026-09-09 12:00:00",
        buy_carry=(13, 8, 5))
    assert carry_hist["total_buy_num"] == 13 and carry_hist["total_buy_scope"] == "buff_yyyp"
    # _latest_known_buy:只认新口径(total_buy_scope=buff_yyyp),旧 BUFF 单平台点不作基线
    assert _latest_known_buy([{"buff_buy_num": None}, {"buff_buy_num": 8}]) is None
    assert _latest_known_buy([
        {"total_buy_scope": "buff_yyyp", "total_buy_num": 7, "buff_buy_num": 5, "yyyp_buy_num": 2},
        {"total_buy_scope": "buff_yyyp", "total_buy_num": 13, "buff_buy_num": 8, "yyyp_buy_num": 5},
    ]) == (13, 8, 5)

    # T+7 视角不应因缺少 30m 快照而把主阶段标为“盘口数据不足”。
    t7_action = analyze_price_action(
        price=100, price_change_1d=-0.8, rate_7=-3.8, rate_30=15.8,
        buy_num=8, sell_num=56, t7_mode=True, book_scope="BUFF",
    )
    assert t7_action["t7_mode"] is True
    assert t7_action["t7_context"] == "中期上行中的回调"
    assert t7_action["phase"] == "中期上行中的回调"
    t7_text = "\n".join(t7_action["lines"])
    assert "T+7核心判断：中期上行中的回调" in t7_text
    assert "短线盘口（辅助）：数据不足（不影响 T+7 结论）" in t7_text
    assert "T+7以中期上行结构为主" in t7_text

    stale_action = analyze_price_action(
        price=100, price_change_30m=2, transaction_volume=120,
        previous_transaction_volume=100, transaction_period_at=stale,
    )
    assert stale_action["volume_fresh"] is False
    assert "盘口量价解算" in stale_action["lines"][0]
    assert "Steam成交参考" not in "\n".join(stale_action["lines"])

    row = {
        "anomaly_type": "surge", "level": "L2", "name": "M4A4 | 狮鹫",
        "price": 100, "score": 60, "price_change_5m": 0,
        "price_change_30m": 2, "price_change_1d": 1,
        "buy_change": 0, "sell_change": 0, "position": "中位",
        "transaction_volume": 120, "transaction_volume_fresh": 1,
        "transaction_volume_change": 20, "transaction_period_at": fresh,
    }
    text = format_row_alert(row)
    assert "Steam成交" not in text and "盘口量价解算" in text

    path = os.path.join(tempfile.mkdtemp(), "volume.db")
    store = SnapshotStore(path)
    store.add_watch("g", "u", "X", "X", 100, alert_type="supply", last_sell_num=10)
    assert store.list_all_watches()[0]["last_sell_num_scope"] == "all"
    now_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    store.save_listing_history_batch([{
        "good_id": 1328, "name": "M4A4 | 狮鹫", "ts": now_ts,
        "buff_sell_num": 1, "steam_sell_num": 2, "buff_buy_num": 3,
        "steam_buy_num": 4, "buff_sell_price": 1, "buff_buy_price": 1,
        "total_sell_num": 5, "total_buy_num": 7,
        "total_sell_scope": "all",
    }])
    series = store.get_listing_series(1328, "M4A4 | 狮鹫", hours=2)
    assert series and series[0]["total_buy_num"] == 7
    store.upsert_turnover_snapshot([
        {"good_id": 1328, "name": "M4A4 | 狮鹫", "turnover_number": 100,
         "turnover_avg_price": 60.0,
         "period_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")},
        {"good_id": 1328, "name": "M4A4 | 狮鹫", "turnover_number": 120,
         "turnover_avg_price": 61.0, "period_at": fresh},
    ])
    history = store.get_turnover_history_batch([1328], limit=4)[1328]
    assert [x["turnover_number"] for x in history] == [120, 100]
    store.insert_anomaly_events([{
        "good_id": 1328, "name": "M4A4 | 狮鹫", "anomaly_type": "surge",
        "drop_pct": 0, "abs_drop": 0, "window_min": 0, "sell_prev": 0,
        "sell_cur": 0, "level": "L2", "detail": "", "score": 60,
        "transaction_volume": 120, "transaction_avg_price": 61,
        "transaction_period_at": fresh, "transaction_volume_change": 20,
        "transaction_volume_fresh": 1,
    }])
    saved = store.list_active_anomalies(1)[0]
    assert saved["transaction_volume"] == 120 and saved["transaction_volume_fresh"] == 1
    print("volume integration: PASS")


if __name__ == "__main__":
    main()
