# -*- coding: utf-8 -*-
"""市场情绪三项改动 —— 回归用例(全部离线,不碰运行中 csqaq.db)。

老大 2026-10-14 选定:做 B(涨跌分布加 7/30 日口径) + C(分类指数全量) + 做 D(情绪按日归档)。
  · V0.3.17 当时的 C 是"只补 3 个盯盘重点";老大看完测试群实发后改口 → V0.3.18 改成**全量**:
    基础 13 个 + 回包里其余全部(接口顺序),不写死名单,新分类自动带上。本文件已同步到新口径。

锁死的行为:
  [1] _rate_line:三窗口格式、缺窗口时降级、无数据返回空串、涨跌比标签阈值
  [2] 口径:三个窗口并列展示(不求和、不互减)——同一批商品在不同时间窗下的家数
  [3] _report_index_names:基础 13 + 回包其余全部 = 23;新分类自动带上;脏 name 不渲染
  [4] 两张卡(市场全景 / 每日播报)都渲染新行与新分类,编号连续,且整卡 < 3000 字符截断线
  [5] sentiment_row:同日重复点取最后一个、缺失字段不炸、无 data 返回 None
  [6] 归档:同一天重复采样只留一行(覆盖更新)、store=None/异常都安全返回 False
  [7] 归档表可与 signal_outcomes 按日期分组(字段齐备)
  [8] 老库 ALTER 迁移补 flat_7/flat_30 且不丢数据
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.anomaly.market_sentiment import archive_sentiment, sentiment_row   # noqa: E402
from bot.qqbot import commands as cmds                                     # noqa: E402
from bot.storage.db import SnapshotStore                                   # noqa: E402

PASS = FAIL = 0
LOOP = asyncio.new_event_loop()

BASE13 = ["饰品指数", "租赁指数", "百元主战", "探员指数", "原皮指数", "红皮指数",
          "千战指数", "武库指数", "贴纸指数", "匕首指数", "手套指数", "挂件指数", "收藏品"]
# 回包里除基础 13 之外的 10 个(2026-10-14 实测的接口顺序)
EXTRA10 = ["一代手套", "二代手套", "三代手套", "音乐盒", "多普勒",
           "伽玛多普勒", "2023巴黎", "2024哥本", "2024上海", "2025奥斯汀"]

# 真实回包形状(2026-10-14 实测值)。up/down 是两次探针实测的真值;
# zero 由 TOTAL 反推,保证「各窗口三态之和恒等」这个口径锚点在用例里严格成立
# (实测 API 三个窗口的和确实都是 26,090,但两次采样之间会有 ±2 的自然抖动)。
TOTAL = 26090
RATE = {"count_positive_1": 3428, "count_negative_1": 9776,
        "count_positive_7": 5135, "count_negative_7": 15829,
        "count_positive_30": 5631, "count_negative_30": 17497}
for _w, _u, _d in ((1, 3428, 9776), (7, 5135, 15829), (30, 5631, 17497)):
    RATE[f"count_zero_{_w}"] = TOTAL - _u - _d


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


def run(coro):
    return LOOP.run_until_complete(coro)


def payload(names=BASE13 + EXTRA10, rate=None, greedy=None):
    subs = [{"name": n, "market_index": 100.0 + i, "chg_rate": 0.5 - i * 0.1,
             "open": 100.0, "close": 101.0, "high": 102.0, "low": 99.0}
            for i, n in enumerate(names)]
    d = {"sub_index_data": subs,
         "greedy_status": {"level": "medium", "label": "中等"},
         "online_number": {"current_number": 510460, "today_peak": 1088211, "month_peak": 1404227},
         "card_price": [{"card_price": 525.0}],
         "greedy": greedy if greedy is not None else [["2026-10-12", 66.1], ["2026-10-13", 68.9],
                                                      ["2026-10-13", 70.5]]}
    if rate is not None:
        d["rate_data"] = rate
    return {"code": 200, "data": d}


print("=" * 80)
print("V0.3.17 市场情绪三项改动 —— 回归")
print("=" * 80)

# ------------------------------------------------------------------ [1][2] B
print("\n[1][2] 涨跌分布:三窗口并列 + 标签 + 降级")
line = cmds._rate_line(RATE)
ok("三窗口格式正确",
   line == "📊 涨跌分布(涨/跌)：今 3428/9776 偏空 ｜ 7日 5135/15829 偏空 ｜ 30日 5631/17497 偏空",
   line)
tot1 = RATE["count_positive_1"] + RATE["count_negative_1"] + RATE["count_zero_1"]
tot7 = RATE["count_positive_7"] + RATE["count_negative_7"] + RATE["count_zero_7"]
tot30 = RATE["count_positive_30"] + RATE["count_negative_30"] + RATE["count_zero_30"]
ok("锚点:三窗口三态之和恒等(同一批商品,窗口可直接比)", tot1 == tot7 == tot30, (tot1, tot7, tot30))
ok("只并列不求和(3 组数字、2 个分隔符;斜杠数=3 对 + 标题里的「涨/跌」= 4)",
   line.count(" ｜ ") == 2 and line.count("/") == 4, (line.count(" ｜ "), line.count("/")))
only1 = cmds._rate_line({"count_positive_1": 100, "count_negative_1": 200, "count_zero_1": 5})
ok("缺 7/30 日字段 → 只渲染今日", only1 == "📊 涨跌分布(涨/跌)：今 100/200 偏空", only1)
ok("完全没有 rate_data → 返回空串(不出半截行)", cmds._rate_line({}) == "")
ok("涨跌比标签 偏多/偏空/均衡(阈值 1.2/0.8 为开区间)",
   (cmds._ratio_tag(130, 100), cmds._ratio_tag(100, 130), cmds._ratio_tag(100, 100),
    cmds._ratio_tag(120, 100), cmds._ratio_tag(0, 0)) == ("偏多", "偏空", "均衡", "均衡", "--"),
   (cmds._ratio_tag(130, 100), cmds._ratio_tag(100, 130)))

# ------------------------------------------------------------------ [3] C
print("\n[3] 分类指数清单:基础 13 + 回包其余全部 = 23,新分类自动带上")
by_all = {n: {} for n in BASE13 + EXTRA10}
names = cmds._report_index_names(by_all)
ok("回包 23 个 → 全部渲染", len(names) == 23, len(names))
ok("基础 13 个仍在最前且顺序不变",
   names[:13] == BASE13 and names[:13] == cmds._REPORT_INDEXES, names[:13])
ok("附加部分与接口顺序一致", names[13:] == EXTRA10, names[13:])
by_base = {n: {} for n in BASE13}
ok("回包只有基础 13 → 就渲染 13 个(不渲染空行)",
   cmds._report_index_names(by_base) == BASE13, cmds._report_index_names(by_base))
newcat = cmds._report_index_names({n: {} for n in BASE13 + EXTRA10 + ["2026科隆"]})
ok("CSQAQ 以后新增分类自动带上(不写死名单)", newcat[-1] == "2026科隆", newcat[-2:])
dirty = cmds._report_index_names({n: {} for n in BASE13 + [None, "", "  "]})
ok("脏 name(None/空串)不渲染成 None 行", len(dirty) == 13, dirty[-3:])
ok("同名重复只算一次", len(cmds._report_index_names({n: {} for n in BASE13 + BASE13})) == 13)

# ------------------------------------------------------------------ [4] 两张卡
print("\n[4] 两张卡都渲染新行与新分类,且不超 3000 字符截断线")
pano = cmds.format_broad_panorama(payload(rate=RATE))
ok("全景段含多窗口涨跌分布行", "📊 涨跌分布(涨/跌)：今 3428/9776 偏空" in pano)
ok("全景段含全部 23 个分类", all(f"{n}：" in pano for n in BASE13 + EXTRA10))
ok("编号连续到 23(14 一代手套 / 23 2025奥斯汀)",
   "14. 一代手套：" in pano and "23. 2025奥斯汀：" in pano,
   [l for l in pano.splitlines() if l.startswith(("14.", "23."))])
ok("原有内容没丢(开盘/在线/卡价/热度)",
   all(k in pano for k in ("⚖️ 开盘", "👥 在线人数", "💳 Steam 美金卡价", "🔥 市场热度：中等")), pano[:200])
daily = cmds.format_daily_report(payload(rate=RATE))
ok("每日播报同样含多窗口行", "📊 涨跌分布(涨/跌)：今 3428/9776 偏空" in daily)
ok("每日播报同样含全部 23 个分类", all(f"{n}:" in daily for n in BASE13 + EXTRA10))
ok("全景段 < 3000 字符", len(pano) < 3000, len(pano))
ok("每日播报整卡 < 3000 字符", len(daily) < 3000, len(daily))
pano_no_rate = cmds.format_broad_panorama(payload(rate=None))
ok("无 rate_data 时不崩、不出半截行、仍出分类指数",
   "涨跌分布" not in pano_no_rate and "饰品分类指数" in pano_no_rate)
ok("空回包返回空串(老行为)", cmds.format_broad_panorama({"data": {}}) == "")
ok("空 sub_index_data 时每日播报走降级文案(老行为)",
   cmds.format_daily_report({"code": 200, "data": {"sub_index_data": []}}) != "")

# ------------------------------------------------------------------ [5] 抽取
print("\n[5] sentiment_row:同日重复点取最后一个,缺字段不炸")
row = sentiment_row(payload(rate=RATE))
ok("day/updated_at 有值", bool(row["day"]) and bool(row["updated_at"]), (row["day"], row["updated_at"]))
ok("热度标签/等级", (row["heat_label"], row["heat_level"]) == ("中等", "medium"), row["heat_label"])
ok("heat_value 取同日最后一个点(70.5 而非 68.9)", row["heat_value"] == 70.5, row["heat_value"])
ok("三窗口涨跌家数齐全",
   (row["up_1"], row["down_1"], row["flat_1"], row["up_7"], row["down_7"], row["flat_7"],
    row["up_30"], row["down_30"], row["flat_30"]) ==
   (3428, 9776, 12886, 5135, 15829, TOTAL - 5135 - 15829, 5631, 17497, TOTAL - 5631 - 17497), row)
ok("落库三窗口各自自洽(分母不用靠外部假设)",
   row["up_1"] + row["down_1"] + row["flat_1"] == TOTAL and
   row["up_7"] + row["down_7"] + row["flat_7"] == TOTAL and
   row["up_30"] + row["down_30"] + row["flat_30"] == TOTAL, row)
ok("index_json 是回包全部 23 个分类的 JSON(归档不受卡片清单限制)",
   len(json.loads(row["index_json"])) == 23 and
   json.loads(row["index_json"])[0]["name"] == "饰品指数", row["index_json"][:80])
ok("无 greedy → heat_value 为 None", sentiment_row(payload(greedy=[]))["heat_value"] is None)
ok("完全没有 data → None", sentiment_row({"code": 500}) is None)
ok("没有情绪字段的空 data → None", sentiment_row({"data": {}}) is None)

# ------------------------------------------------------------------ [6] 归档
print("\n[6] 按日归档:一天一行、覆盖更新、异常安全")
tmp = tempfile.mktemp(suffix=".db")
store = SnapshotStore(tmp)


class _BoomStore:
    async def async_upsert_market_sentiment(self, row):
        raise RuntimeError("模拟写库失败")


ok("store=None → False 且不抛", run(archive_sentiment(None, payload(rate=RATE))) is False)
ok("写库异常 → 吞掉返回 False", run(archive_sentiment(_BoomStore(), payload(rate=RATE))) is False)
ok("正常归档 → True", run(archive_sentiment(store, payload(rate=RATE))) is True)
rows = run(store.async_list_market_sentiment(10))
ok("库里 1 行", len(rows) == 1, len(rows))
ok("落库字段与抽取一致",
   (rows[0]["heat_label"], rows[0]["heat_value"], rows[0]["up_30"]) == ("中等", 70.5, 5631), rows[0])
# 同一天再采一次(值变了)→ 仍只有一行,且被覆盖
p2 = payload(rate=RATE, greedy=[["2026-10-13", 68.9], ["2026-10-14", 72.2]])
run(archive_sentiment(store, p2))
rows2 = run(store.async_list_market_sentiment(10))
ok("同日重复采样仍只 1 行(覆盖更新)", len(rows2) == 1, len(rows2))
ok("值被更新为最新采样", rows2[0]["heat_value"] == 72.2, rows2[0]["heat_value"])
ok("按日取回可用", run(store.async_get_market_sentiment(rows2[0]["day"]))["heat_value"] == 72.2)
# 换一天 → 第二行
from bot.anomaly.market_sentiment import sentiment_row as _sr   # noqa: E402
import datetime as _dt                                          # noqa: E402
other = _sr(payload(rate=RATE), now=_dt.datetime(2026, 10, 15, 15, 0, 0))
run(store.async_upsert_market_sentiment(other))
ok("不同日期各占一行", len(run(store.async_list_market_sentiment(10))) == 2)
# 不绑定"今天"是几号:断言排序是倒序、且两个日期都在(原来写死 == "2026-10-15",
# 一旦系统日期晚于 10-15 就必挂 —— 2026-10-21 实测踩到)
_days = [r["day"] for r in run(store.async_list_market_sentiment(10))]
ok("按 day 倒序返回", _days == sorted(_days, reverse=True) and "2026-10-15" in _days, _days)

# ------------------------------------------------------------------ [7] 可 join
print("\n[7] 归档表能与 signal_outcomes 按日期分组(字段齐备)")
con = sqlite3.connect(tmp)
cols = {r[1] for r in con.execute("PRAGMA table_info(market_sentiment)")}
need = {"day", "heat_label", "heat_value", "up_1", "down_1", "up_7", "down_7",
        "flat_7", "up_30", "down_30", "flat_30"}
ok("market_sentiment 字段齐备(含 flat_7/flat_30 迁移列)", need <= cols, need - cols)
ok("迁移列对老库生效(ALTER 补列,不要求删表)",
   all(c in cols for c in ("flat_7", "flat_30")), cols)
so = {r[1] for r in con.execute("PRAGMA table_info(signal_outcomes)")}
ok("signal_outcomes 有 event_ts/fwd_24h(可 join)",
   {"event_ts", "fwd_24h", "good_id"} <= so, so)
grouped = con.execute(
    "SELECT ms.day, ms.heat_value, COUNT(so.event_id) FROM market_sentiment ms "
    "LEFT JOIN signal_outcomes so ON substr(so.event_ts,1,10)=ms.day "
    "GROUP BY ms.day").fetchall()
ok("按日分组查询可执行(返回 2 天)", len(grouped) == 2, grouped)
con.close()

print("\n[8] 老库迁移:已有 market_sentiment 但缺 flat_7/flat_30 → 自动补列且不丢数据")
tmp2 = tempfile.mktemp(suffix=".db")
con = sqlite3.connect(tmp2)
con.execute("""CREATE TABLE market_sentiment (day TEXT PRIMARY KEY, updated_at TEXT NOT NULL,
    heat_label TEXT, heat_level TEXT, heat_value REAL, index_chg REAL,
    up_1 INTEGER, down_1 INTEGER, flat_1 INTEGER, up_7 INTEGER, down_7 INTEGER,
    up_30 INTEGER, down_30 INTEGER, index_json TEXT)""")
con.execute("INSERT INTO market_sentiment (day, updated_at, heat_label, up_1) "
            "VALUES ('2026-10-01','2026-10-01 15:00:00','低',1)")
con.commit()
con.close()
st2 = SnapshotStore(tmp2)   # 构造即跑 _init_db 迁移
cols2 = {r[1] for r in sqlite3.connect(tmp2).execute("PRAGMA table_info(market_sentiment)")}
ok("老表被 ALTER 补上 flat_7/flat_30", {"flat_7", "flat_30"} <= cols2, cols2)
row_old = sqlite3.connect(tmp2).execute(
    "SELECT heat_label, up_1 FROM market_sentiment WHERE day='2026-10-01'").fetchone()
ok("迁移不丢老数据", row_old == ("低", 1), row_old)
run(st2.async_upsert_market_sentiment(sentiment_row(payload(rate=RATE))))
ok("补列后可正常写入(新行 + 老行共 2 行)", len(run(st2.async_list_market_sentiment(10))) == 2)

print("\n" + "=" * 80)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 80)
sys.exit(1 if FAIL else 0)
