# -*- coding: utf-8 -*-
"""V0.3.5:B 池扫描预算分级 + 提前止损 回归测试。

背景(实测 `probe_b_scan_usable_pages.py`):
  * 6 路百分比榜(1/7/30 日涨跌幅)每页 100 条里,能同时过「在售 ≥400」与「价 ≥¥5」
    两道闸的只有 0~1 条 → 深翻 25 页期望收益 ≈ 0~2 品,纯烧配额;
  * 4 路价格榜(售价/求购价升降序)每页稳定 3~8 条 → 深翻仍有价值。
故:百分比榜页数单独分档 + "连续 N 页一个可评分品都没有"就停止翻页。

用法: .venv\\Scripts\\python.exe -X utf8 test_b_scan_budget_v035.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import sys

sys.path.insert(0, ".")

from bot.anomaly.runner import (RANK_SORTS, RANK_SORTS_PCT, _row_scorable,
                                _scan_pool, _should_early_stop, pages_for_sort)

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


class _Cfg:
    def __init__(self, **kw):
        self.b_pages_per_sort = 25
        self.b_pages_pct_sort = 5
        self.b_early_stop_pages = 2
        self.min_scan_sell_num = 400
        self.min_price = 5.0
        for k, v in kw.items():
            setattr(self, k, v)


class _Item:
    """榜单条目替身:`_buff_yyyp_sell_num` 认 raw 里的在售字段。"""

    def __init__(self, gid, main_price, buff_n, yyyp_n):
        self.good_id = gid
        self.name = f"品{gid}"
        self.main_price = main_price
        self.raw = {"buff_sell_num": buff_n, "yyyp_sell_num": yyyp_n,
                    "sell_scope": "buff_yyyp"}


class _Bucket:
    async def acquire(self):
        return None


class _Api:
    """按页返回预设数据,并记录实际请求了哪些页。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def ip_drift_cooling(self):
        return False

    async def get_rank_list(self, page_index=1, page_size=100, sort=""):
        self.calls.append(page_index)
        idx = page_index - 1
        return list(self.pages[idx]) if idx < len(self.pages) else []


class _Store:
    async def async_remove_watch_pool_batch(self, keys, pool=None):
        return 0


def _junk(n, start=1):
    """分币级薄盘:在售不够(<400)→ 扫描阶段即可判定不可评分。"""
    return [_Item(start + i, 0.41, 60, 40) for i in range(n)]


def _thin_pricey(n, start=1):
    """价够但盘薄(实测百分比榜 80% 属于这类)。"""
    return [_Item(start + i, 888.0, 60, 40) for i in range(n)]


def _productive(n, start=1):
    """在售 ≥400 且价 ≥¥5 → 可评分。good_id=0 使其不进 items(避免触发评分链路)。"""
    return [_Item(0, 100.0, 500, 300) for _ in range(n)]


async def _run(pages, cfg, early_stop, tag="B"):
    api = _Api(pages)
    await _scan_pool(cfg, api, _Store(), None, None, _Bucket(), ["价格_售价_降序"],
                     int(getattr(cfg, "b_pages_per_sort", 25)), tag,
                     allow_downgrade=False, early_stop_pages=early_stop)
    return api.calls


def test_pages_for_sort():
    print("\n[1] 页数分档 pages_for_sort")
    cfg = _Cfg()
    check("6 路百分比榜 → b_pages_pct_sort(5)",
          all(pages_for_sort(s, cfg) == 5 for s in RANK_SORTS_PCT))
    check("百分比榜清单就是 6 路",
          len(RANK_SORTS_PCT) == 6 and set(RANK_SORTS_PCT) <= set(RANK_SORTS))
    price_sorts = [s for s in RANK_SORTS if s not in RANK_SORTS_PCT]
    check("4 路价格榜 → b_pages_per_sort(25)",
          len(price_sorts) == 4 and all(pages_for_sort(s, cfg) == 25 for s in price_sorts))
    c2 = _Cfg(b_pages_pct_sort=3, b_pages_per_sort=40)
    check("配置可覆盖", pages_for_sort(RANK_SORTS_PCT[0], c2) == 3
          and pages_for_sort(price_sorts[0], c2) == 40)
    c3 = _Cfg(b_pages_pct_sort=0)
    check("页数 0 → 回落默认 5", pages_for_sort(RANK_SORTS_PCT[0], c3) == 5)
    c4 = _Cfg(b_pages_pct_sort=-3)
    check("页数负数 → 下限保护 1 页", pages_for_sort(RANK_SORTS_PCT[0], c4) == 1)


def test_row_scorable():
    print("\n[2] 可评分判据 _row_scorable(在售闸 + 价格闸的交集)")
    check("盘口齐全+在售达标+价达标 → 可评分",
          _row_scorable(_Item(1, 100.0, 500, 300), 400, 5.0))
    check("在售不达标 → 不可评分(实测百分比榜 80% 是这种)",
          not _row_scorable(_Item(1, 888.0, 60, 40), 400, 5.0))
    check("价 <¥5 → 不可评分", not _row_scorable(_Item(1, 0.41, 5000, 5000), 400, 5.0))
    check("缺悠悠在售字段 → 不可评分(不按 0 算)",
          not _row_scorable(_Item(1, 100.0, 500, None), 400, 5.0))
    check("边界:在售正好等于门槛、价正好等于下限 → 可评分",
          _row_scorable(_Item(1, 5.0, 400, 0), 400, 5.0))


def test_counter_reset():
    print("\n[3] 止损判据 _should_early_stop(整轮累计零收益才算)")
    check("翻到确认页数仍零收益 → 止损", _should_early_stop(3, 0, 3))
    check("还没到确认页数 → 继续翻", not _should_early_stop(2, 0, 3))
    check("本 sort 出过货 → 一路翻到底(中间夹空页也不误杀)",
          not _should_early_stop(25, 7, 3) and not _should_early_stop(4, 1, 3))
    check("确认页数 ≤0 = 关闭", not _should_early_stop(25, 0, 0))


def test_early_stop_fires():
    print("\n[4] 全无可评分品 → 提前止损(省页)")
    cfg = _Cfg()
    pages = [_junk(100, start=p * 100) for p in range(25)]
    calls = asyncio.run(_run(pages, cfg, early_stop=3))
    check(f"只翻了 {len(calls)} 页(确认页数 3)而不是 25 页", calls == [1, 2, 3])
    calls0 = asyncio.run(_run(pages, cfg, early_stop=0))
    check("开关关闭(0)时行为不变:翻满 25 页", len(calls0) == 25)
    calls5 = asyncio.run(_run(pages, cfg, early_stop=5))
    check("确认页数可调(5 → 翻 5 页)", calls5 == [1, 2, 3, 4, 5])


def test_thin_pricey_also_stops():
    print("\n[5] 价够但盘薄(实测占比最高的一类)同样止损")
    cfg = _Cfg()
    pages = [_thin_pricey(100, start=p * 100) for p in range(25)]
    calls = asyncio.run(_run(pages, cfg, early_stop=3))
    check(f"盘薄也算零收益 → 只翻 {len(calls)} 页", calls == [1, 2, 3])


def test_productive_not_stopped():
    print("\n[6] 价格榜(每页有货)不受影响:翻满")
    cfg = _Cfg()
    pages = [_productive(100) for _ in range(25)]
    calls = asyncio.run(_run(pages, cfg, early_stop=3))
    check(f"翻满 {len(calls)} 页(提前止损不误伤)", len(calls) == 25)
    # 第 1 页只有 1 个可评分品也必须一路翻到底
    pages2 = [_junk(99) + _productive(1)] + [_junk(100)] + \
             [_productive(99) + _junk(1)] + [_junk(100) for _ in range(22)]
    calls2 = asyncio.run(_run(pages2, cfg, early_stop=3))
    check(f"首 3 页共 1 个可评分品 → 仍翻满 {len(calls2)} 页", len(calls2) == 25)


def test_reset_prevents_false_stop():
    print("\n[7] 中间有货 → 绝不误停")
    cfg = _Cfg()
    pages = [_junk(100)] + [_productive(100)] + [_junk(100), _junk(100)] + \
            [_productive(100) for _ in range(21)]
    calls = asyncio.run(_run(pages, cfg, early_stop=3))
    check(f"第 1 页空、第 2 页有货 → 继续翻完 {len(calls)} 页", len(calls) == 25)


def test_config_wiring():
    print("\n[8] config.yaml 接线")
    from bot.config import load_config
    cfg = load_config("config.yaml")
    check("b_pages_pct_sort = 5", int(getattr(cfg.anomaly, "b_pages_pct_sort", -1)) == 5)
    check("b_early_stop_pages = 3", int(getattr(cfg.anomaly, "b_early_stop_pages", -1)) == 3)
    check("b_pages_per_sort 仍是 25", int(cfg.anomaly.b_pages_per_sort) == 25)


if __name__ == "__main__":
    print("=" * 68)
    print("V0.3.5 B 池扫描预算分级 + 提前止损测试")
    print("=" * 68)
    test_pages_for_sort()
    test_row_scorable()
    test_counter_reset()
    test_early_stop_fires()
    test_thin_pricey_also_stops()
    test_productive_not_stopped()
    test_reset_prevents_false_stop()
    test_config_wiring()
    print("\n" + "=" * 68)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
