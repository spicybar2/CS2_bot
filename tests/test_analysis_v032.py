# -*- coding: utf-8 -*-
"""V0.3.2 回归:/分析 成交口径 + 大盘对照 + ★ 品日K 编码修复。

覆盖:
  1. `_proceeds_lines` —— 求购价/价差/扣费后到手(含费率可配置、求购价缺失降级)
  2. `_action_extra_lines` —— 只取正文未展示的行(过滤与【中期结构】【流动性】重复的行)
  3. `config.fees` —— 默认值 + YAML 覆盖
  4. SteamDT 请求体编码 —— 含 `★` 的 marketHashName 必须转义成 \\u2605 再发(HTTP 400 → 200)

纯离线(第 4 项拦截 subprocess,不发真实请求)。用法: .venv\\Scripts\\python.exe test_analysis_v032.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, "D:/cs2-qq-bot")

from bot.config import FeeConfig, load_config
from bot.qqbot.bot import _action_extra_lines, _proceeds_lines
from bot.steamdt.client import SteamDTClient

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


def joined(lines) -> str:
    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------- 1. 成交口径
def test_proceeds() -> None:
    fees = FeeConfig()          # BUFF 2.5 / 悠悠 2.0 / Steam 15
    lines = _proceeds_lines(13599.5, 12800.0, fees)
    t = joined(lines)
    check("到手:显示求购价", "求购价 ¥12,800.00" in t)
    check("到手:价差改用「立即出手折价」(V0.3.13)", "立即出手折价 6.2%" in t)
    check("到手:不再用带 + 号的「价差 +6.2%」(会被读成求购价更高)", "价差 +" not in t)
    check("到手:一行给挂卖到手(V0.3.13)", "挂卖 BUFF ¥13,259.51" in t)
    check("到手:一行给秒出到手(V0.3.13)", "秒出 BUFF ¥12,480.00" in t)
    check("到手:费率写成一行(两平台不同则都写)",
          "到手（BUFF 2.5% / 悠悠 2%）" in t)
    check("到手:旧的 4 行算式块已删",
          "两平台在售价" not in t and "两平台求购价" not in t
          and "按各自平台挂售价卖出到手" not in t and "扣费后到手" not in t)
    check("到手:无 raw 时最多 2 行", len(lines) <= 2)
    check("到手:价差小则提示易出手", "价差小,出手容易" in joined(_proceeds_lines(100.0, 98.0, fees)))
    check("到手:价差大则提示难成交", "价差大" in joined(_proceeds_lines(100.0, 70.0, fees)))

    # 求购价缺失 → 明确降级,不编造
    t2 = joined(_proceeds_lines(69.88, 0, fees))
    check("到手:求购价缺失时明说", "求购价不可用" in t2)
    check("到手:求购价缺失仍给到手价", "挂卖 BUFF ¥68.13" in t2)

    # 费率可配置
    t3 = joined(_proceeds_lines(100.0, 0, FeeConfig(buff_sell_pct=5.0, yyyp_sell_pct=2.0)))
    check("到手:费率可配置(5% → 95.00)", "¥95.00" in t3)
    check("到手:费率为 0 时不再重复提示", joined(_proceeds_lines(100.0, 0, FeeConfig(buff_sell_pct=2.0, yyyp_sell_pct=2.0)))
          .count("悠悠有品") == 0)


# ------------------------------------------------------- 2. 正文未展示的行
def test_action_extra() -> None:
    act = {"lines": [
        "T+7核心判断：中期震荡",
        "阶段：中期震荡",
        "位置：中位",
        "注：T+7主结论基于日K趋势",
        "短线盘口（辅助）：增(+3.2%)",
        "当前盘口（BUFF+悠悠）：求购 100 / 在售 200",
        "大盘：逆势走强 / 热度中等",
        "结论：T+7中期方向暂不明确",
    ]}
    out = _action_extra_lines(act)
    t = joined(out)
    check("量价块:保留短线盘口辅助", "短线盘口（辅助）" in t)
    check("量价块:保留大盘对照", "大盘：逆势走强" in t)
    check("量价块:过滤结论(V0.3.13,与【T+7分析】同义)", "结论：" not in t)
    check("量价块:保留口径注(由调用方收口到末尾)", "注：T+7主结论基于日K趋势" in t)
    check("量价块:过滤 T+7核心判断(与中期结构重复)", "T+7核心判断" not in t)
    check("量价块:过滤 阶段/位置", "阶段：" not in t and "位置：" not in t)
    check("量价块:过滤 当前盘口(流动性段已有)", "当前盘口" not in t)
    check("量价块:空/None 安全", _action_extra_lines(None) == [] and _action_extra_lines({}) == [])


# ------------------------------------------------------------ 3. 费率配置
def test_fee_config() -> None:
    cfg = load_config("config.yaml")
    check("配置:生产 fees 已生效", cfg.fees.buff_sell_pct == 2.5 and cfg.fees.yyyp_sell_pct == 2.0)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("fees:\n  buff_sell_pct: 3.5\n  steam_sell_pct: 13.0\n")
        c2 = load_config(p)
        check("配置:YAML 覆盖生效", c2.fees.buff_sell_pct == 3.5 and c2.fees.steam_sell_pct == 13.0)
        check("配置:未覆盖项保持默认", c2.fees.yyyp_sell_pct == 2.0)


# ---------------------------------------------------- 4. ★ 品请求体编码
def test_star_encoding() -> None:
    cfg = load_config("config.yaml")
    cli = SteamDTClient(cfg.steamdt)
    captured = {}

    class _P:
        stdout = b'{"code":200,"data":{"points":[]}}\n200'

    real_run = subprocess.run

    def fake_run(cmd, capture_output=True, timeout=25):  # noqa: ARG001
        captured["cmd"] = list(cmd)
        return _P()

    subprocess.run = fake_run
    try:
        code, body = asyncio.run(cli._curl_request(
            "POST", "/open/cs2/item/v1/kline",
            {"marketHashName": "★ Butterfly Knife | Doppler (Factory New)", "type": 2}))
    finally:
        subprocess.run = real_run

    cmd = captured.get("cmd") or []
    payload = cmd[cmd.index("-d") + 1] if "-d" in cmd else ""
    check("★品:请求体为纯 ASCII", payload.isascii(), f"payload={payload[:60]!r}")
    check("★品:★ 已转义为 \\u2605", "\\u2605" in payload, f"payload={payload[:80]!r}")
    check("★品:仍是合法 JSON 且名字未丢", json.loads(payload)["marketHashName"].startswith("★"))
    check("★品:返回状态解析正常", code == 200)


def main() -> None:
    print("=" * 62)
    print("V0.3.2 回归:成交口径 / 大盘对照 / ★ 品日K编码")
    print("=" * 62)
    test_proceeds()
    test_action_extra()
    test_fee_config()
    test_star_encoding()
    print()
    print(f"结果: 通过 {_PASS} / 失败 {_FAIL}")


if __name__ == "__main__":
    main()
