# -*- coding: utf-8 -*-
"""结算异动事件的前瞻收益(T+1d/3d/7d)→ signal_outcomes 表。

用途
----
把「异动信号到底有没有前瞻性」变成可查的数据:每条事件落库时的要价作为基准,
用事件之后的真实落库快照价算 24h/72h/168h 收益,支持反复重算(按 event_id 幂等)。

用法
----
    python scripts/track_signal_outcomes.py              # 结算 + 打印汇总
    python scripts/track_signal_outcomes.py --days 30    # 只看近 30 天待结算事件
    python scripts/track_signal_outcomes.py --report     # 只读汇总,不写库

说明
----
- 只读 item_listing_history、只写 signal_outcomes 新表,不触碰三层池/推送/冷却;
- 可在机器人运行中执行(WAL + 短事务);
- 收益是要价口径的百分比变动,未扣平台手续费与滑点,不等于可实现收益;
- 前瞻价取不到就留空(NULL),不插值、不换平台 —— 数据边界铁律。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from bot.analyzer.outcome import format_summary, settle_event, summarize  # noqa: E402
from bot.storage.db import SnapshotStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="结算异动事件前瞻收益")
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "data", "csqaq.db"))
    ap.add_argument("--days", type=int, default=60, help="只看最近 N 天的事件(默认 60)")
    ap.add_argument("--limit", type=int, default=2000, help="单次最多结算多少条")
    ap.add_argument("--report", action="store_true", help="只读汇总,不写库")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[outcome] 数据库不存在: {args.db}")
        return 1

    store = SnapshotStore(args.db)
    now = datetime.now()
    if args.report:
        rows = [r for r in store.list_signal_outcomes() if (r.get("event_ts") or "") >=
                (now - timedelta(days=args.days)).strftime("%Y-%m-%d")]
        print(f"[outcome] 已结算 {len(rows)} 条(近 {args.days} 天)\n")
        print(format_summary(summarize(rows)))
        return 0

    # 待结算:至少满 7 天(否则 168h 窗口必然取不到价)
    events = store.list_events_for_settlement(limit=args.limit, min_age_hours=168)
    cutoff = (now - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    events = [e for e in events if (e.get("ts") or "") >= cutoff]
    print(f"[outcome] 待结算 {len(events)} 条(评分>0 且满 7 天)")

    out, skipped = [], 0
    for e in events:
        ts = (e.get("ts") or "").strip()
        try:
            t_event = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        # 前瞻窗口必须从**事件时刻**算起(而不是当天 0 点),否则 T+7d 目标点会落在窗口之外
        ts_from = t_event.strftime("%Y-%m-%d %H:%M:%S")
        ts_to = (t_event + timedelta(hours=168 + 6)).strftime("%Y-%m-%d %H:%M:%S")
        series = store.get_listing_price_series(int(e["good_id"]), ts_from, ts_to)
        if not series:
            # 事件之后该品再无落库快照(出池/低流动性) → 记一行空结果,避免每轮重复扫描
            skipped += 1
        out.append(settle_event(e, series))

    if out:
        store.upsert_signal_outcomes(out)
    print(f"[outcome] 写入 {len(out)} 条(其中 {skipped} 条无后续快照,收益留空)")

    all_rows = store.list_signal_outcomes()
    recent = [r for r in all_rows if (r.get("event_ts") or "") >=
              (now - timedelta(days=args.days)).strftime("%Y-%m-%d")]
    print()
    print(format_summary(summarize(recent)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
