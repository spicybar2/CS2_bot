# -*- coding: utf-8 -*-
"""成交量 / 换手率特征的实际可用性。

只读 data/csqaq.db。

    .venv\\Scripts\\python.exe tools\\probe_volume_coverage.py

背景：引擎里有一整套「量价解算」打分逻辑，前提是 CSQAQ 单品详情能给出
新鲜的 Steam 成交量。本脚本回答：这个前提成立吗？
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"


def main() -> None:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=" * 78)
    print("成交量特征可用性")
    print("=" * 78)

    total = c.execute("select count(*) from anomaly_events").fetchone()[0]
    has = c.execute("select count(*) from anomaly_events "
                    "where transaction_volume is not null").fetchone()[0]
    print(f"\n[1] 事件表成交量覆盖")
    print(f"   事件总数 {total}，带成交量 {has} 条（{100.0 * has / max(total, 1):.1f}%）")

    print(f"\n[2] item_turnover_history 的周期分布")
    rows = c.execute("""select substr(period_at,1,7) ym, count(*) n
                        from item_turnover_history group by 1 order by 1""").fetchall()
    for r in rows:
        print(f"   {r['ym']}  {r['n']} 条")
    span = c.execute("select min(period_at), max(period_at), count(*) from item_turnover_history"
                     ).fetchone()
    print(f"   区间 {span[0]} → {span[1]}，共 {span[2]} 条")

    last = c.execute("select max(observed_at) from item_turnover_history").fetchone()[0]
    print(f"   最近入库时间 {last}")

    print(f"\n[3] 判定")
    if has == 0:
        print("   § 成交量特征从未生效：period_at 全部是旧周期，被 48h 新鲜度闸拦掉。")
        print("   § 引擎的「量价解算」维度实际为空转 —— 需要换数据源才能做需求侧选品。")
    else:
        print("   § 成交量特征有部分覆盖，可继续使用。")

    c.close()


if __name__ == "__main__":
    main()
