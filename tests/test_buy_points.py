# -*- coding: utf-8 -*-
"""/分析 左侧/右侧买点双体系回归测试(纯函数,不依赖网络)。

覆盖: 纯左侧候选 / 纯右侧候选 / 两者都无 / 两者同时存在 + 数据不足降级。
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import sys
sys.path.insert(0, "D:/cs2-qq-bot")

from bot.qqbot.bot import _left_right_buy_points

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  OK  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}  {detail}")


def lin(start: float, end: float, n: int) -> list:
    if n <= 1:
        return [end]
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def joined(lines):
    return "\n".join(lines) if lines else ""


def test_left_only() -> None:
    """纯左侧:深跌后从低点小幅回升(确认项 3/3),但现价仍在 20 日线下方 → 不触发右侧。"""
    closes = lin(200.0, 60.0, 30) + [58, 57, 56, 55, 54, 53, 52, 51, 50.5, 50,
                                     50.5, 51, 51.5, 52, 52.5, 53]
    lines = _left_right_buy_points(closes, closes[-1], pct_7d=2.0, pct_30d=-45.0)
    t = joined(lines)
    check("纯左侧:出现左侧买点块", lines is not None and "左侧买点" in t)
    check("纯左侧:不出现右侧买点块", "右侧买点" not in t)
    check("纯左侧:结论偏左侧", lines is not None and "偏左侧" in t)
    check("纯左侧:候选含低位/深跌", "候选背景" in t and "深跌" in t)
    check("纯左侧:确认项达标(≥2/3)", "确认项 3/3" in t or "确认项 2/3" in t)
    check("纯左侧:带数据口径提示", "确认项≥2 项才是关键" in t)


def test_left_background_only() -> None:
    """左侧背景满足但确认项不足 → 明确说"不构成买点"(V0.3.2:候选条件单独无优势)。"""
    closes = lin(200.0, 75.0, 39) + [60, 59, 58.5, 58, 57.8, 57.6, 57.5, 57.4, 57.3,
                                     57.2, 57.1, 57.05, 57, 57, 57, 57, 57.1, 57.05, 57, 57.0]
    lines = _left_right_buy_points(closes, closes[-1], pct_7d=0.0, pct_30d=-47.0)
    t = joined(lines)
    check("确认不足:仍展示左侧背景块", lines is not None and "左侧买点" in t)
    check("确认不足:标注确认不足", "确认不足,仅观察" in t)
    check("确认不足:结论不构成买点", "暂不构成买点" in t and "确认项不足" in t)


def test_right_only() -> None:
    """纯右侧:上行中站上20日线,非低位深跌。"""
    closes = [55.0] * 40 + [60, 62, 64, 66, 68, 70, 72, 74, 76, 78, 80, 82, 84,
                            86, 88, 90, 92, 94, 95, 96]
    lines = _left_right_buy_points(closes, closes[-1], pct_7d=7.9, pct_30d=75.0)
    t = joined(lines)
    check("纯右侧:出现右侧买点块", lines is not None and "右侧买点" in t)
    check("纯右侧:不出现左侧买点块", "左侧买点" not in t)
    check("纯右侧:结论偏右侧", lines is not None and "偏右侧" in t)
    check("纯右侧:确认含7日转正✓", "7日转正✓" in t)


def test_none() -> None:
    """两者都无:低位但无深跌,收盘也未站上20日线。"""
    closes = [80.0] * 57 + [79.9, 79.6, 79.4]
    lines = _left_right_buy_points(closes, closes[-1], pct_7d=-0.6, pct_30d=-0.75)
    t = joined(lines)
    check("都无:出现暂无买点", lines is not None and "暂无" in t)
    check("都无:无左侧块", "左侧买点" not in t)
    check("都无:无右侧块", "右侧买点" not in t)
    # V0.3.13:「结论: 暂无买点…」与上一行同义 → 删掉;口径行保留但由调用方收口到末尾
    check("都无:不再重复一行结论", "结论:" not in t)
    check("都无:口径行仍在(供末尾收口)", "口径:" in t)


def test_both() -> None:
    """两者并存:深跌崩谷后小幅反弹,收盘刚站上仍低位的20日线。"""
    seg = [70, 69, 68, 67, 66, 65, 60, 57, 55, 54, 53.5, 53, 52.8, 52.6, 52.5,
           52.6, 52.8, 54, 57, 60.2]
    closes = [130.0] * 40 + seg
    lines = _left_right_buy_points(closes, closes[-1], pct_7d=14.0, pct_30d=-53.0)
    t = joined(lines)
    check("并存:左侧块出现", lines is not None and "左侧买点" in t)
    check("并存:右侧块出现", lines is not None and "右侧买点" in t)
    check("并存:结论并存", lines is not None and "并存" in t)
    check("并存:右侧候选含站上20日线", "站上20日线" in t)


def test_insufficient() -> None:
    """数据不足(<20根日K)→ None,不渲染任何买点段。"""
    check("数据不足返回None", _left_right_buy_points([100.0] * 10, 100.0, 0.0, 0.0) is None)
    check("空序列返回None", _left_right_buy_points([], 0.0, None, None) is None)


def test_no_book_words() -> None:
    """红线:输出严禁出现 成交量/放量 字样。"""
    closes = lin(200.0, 75.0, 39) + [60, 59, 58.5, 58, 57.8, 57.6, 57.5, 57.4, 57.3,
                                     57.2, 57.1, 57.05, 57, 57, 57, 57, 57.1, 57.05, 57, 57.0]
    for kw in ["成交量", "放量", "缩量"]:
        lines = _left_right_buy_points(closes, closes[-1], pct_7d=0.0, pct_30d=-47.0)
        check(f"无成交量词[{kw}]", joined(lines).find(kw) == -1)


def test_t7_ignores_short_book() -> None:
    """T+7 买点不把 30m 盘口改善作为必要确认项。"""
    closes = [55.0] * 40 + [60, 62, 64, 66, 68, 70, 72, 74, 76, 78,
                             80, 82, 84, 86, 88, 90, 92, 94, 95, 96]
    lines = _left_right_buy_points(
        closes, closes[-1], pct_7d=7.9, pct_30d=75.0,
        book_state="增", book_pressure=20.0, use_short_book=False,
    )
    t = joined(lines)
    check("T+7不依赖短线盘口", "短线盘口同步(可选)-" in t)
    check("T+7右侧结论不要求盘口改善", "+7日转正再评估" in t and "盘口改善 全满足" not in t)
    check("T+7候选不追加盘口承接", "盘口承接增强" not in t and "盘口同步改善" not in t)


def main() -> None:
    print("=" * 60)
    print("左侧/右侧买点双体系回归")
    print("=" * 60)
    test_left_only()
    test_left_background_only()
    test_right_only()
    test_none()
    test_both()
    test_insufficient()
    test_no_book_words()
    test_t7_ignores_short_book()
    print()
    print(f"结果: 通过 {_PASS} / 失败 {_FAIL}")


if __name__ == "__main__":
    main()
