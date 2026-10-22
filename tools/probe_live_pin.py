# -*- coding: utf-8 -*-
"""V0.3.19 真机验证:用**真实 client 代码路径**(含钉 IP)连打 20 次,确认不再漂。

只读:不调用 bind_local_ip,不改服务端绑定。限速 4.5s/次(约 13 req/min,不挤占机器人额度)。
"""
import asyncio
import logging
import sys

from bot.config import load_config
from bot.csqaq import client as C

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")


async def main():
    cfg = load_config("config.yaml")
    c = cfg.csqaq
    c.request_interval = 6.0          # 慢一点,别把账号额度吃满(约 10 req/min)
    print(f"  pin_auto={c.pin_auto}  pin_server_ip='{c.pin_server_ip}'")

    ip = await C.select_pin_ip(c)
    st = C.pin_state()
    print(f"  选中服务器 IP: {ip}")
    print(f"  候选: {st['candidates']}")

    ok = fail = 0
    errs = []
    async with C.CSQAQClient(c) as cli:
        for i in range(20):
            try:
                data = await cli.get_price_by_market_hash_name(
                    ["M4A4 | Griffin (Factory New)"], allow_drift_skip=False)
                ok += 1
                print(f"    #{i+1:<2} ✓ 返回 {len(data) if isinstance(data, dict) else 0} 条  "
                      f"钉住={C.pin_state()['ip']}")
            except Exception as exc:
                fail += 1
                errs.append(f"{type(exc).__name__}: {str(exc)[:80]}")
                print(f"    #{i+1:<2} ✗ {type(exc).__name__}: {str(exc)[:80]}")
        print(f"\n  轻接口结果: {ok} 成功 / {fail} 失败")

        # 重接口再确认一次(大盘 /current_data)
        try:
            c.request_interval = 2.0
            data = await cli.get_current_data("init")
            n = len(data.get("data") or [])
            print(f"  重接口 /current_data: ✓ 返回 {n} 条(钉住={C.pin_state()['ip']})")
        except Exception as exc:
            print(f"  重接口 /current_data: ✗ {type(exc).__name__}: {str(exc)[:100]}")

    print(f"\n  最终状态: {C.pin_state()}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
