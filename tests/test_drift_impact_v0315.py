# -*- coding: utf-8 -*-
"""V0.3.15 回归:IP 漂移对盯盘的影响 —— 两项修复。

老大 2026-10-13 问"IP 漂移是否对盯盘有直观影响",查完后选定修两项:
  ① **超时重绑绕过冷却**(当天 408 次 `超时后重绑未成(/info/get_rank_list): cooldown`)
  ② **停采期只停榜单、不停盯盘**(漂移冷却期把盯盘的批量查价/单品详情/市场环境一起停了)

实测背景(见 D:\\CS2BOT\\.workbuddy\\memory\\2026-10-13.md):
  · 10-13 抓取轮次达成率 76%(45/59),4 轮残缺(最低 100/1020 条),最长空档 125 分钟
  · 自选盯盘 18 件**全部**同步断档 121.4 分钟(正常 15.3 分钟一个点)
  · 12:30:56 → 14:00:55 **90 分钟零日志**:停在「漂移冷却失败」、恢复于「bind 接口 429」
  · 515 个超时**全部**是 /info/get_rank_list;盯盘的批量查价只有 33 次受限

本用例锁死修复后的行为:
  [1] 超时触发(bypass_cooldown=True)时,冷却期内也能真重绑
  [2] 401 漂移路径**不变**,照旧尊重冷却(退避对"确认是漂移"仍然有效)
  [3] 超时重绑有 30s 起步、随连续失败逐级加长的下限,不会变成重绑风暴
  [4] 漂移冷却期内:榜单(get_rank_list)照旧停;盯盘三件套照常放行
  [5] 重绑失败退避阶梯未回归(120→240→480→600 封顶)
  [6] 传 allow_drift_skip=True 时,老行为(直接跳过)仍可用
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.config import CSQAQConfig                                  # noqa: E402
from bot.csqaq import client as cq                                  # noqa: E402
from bot.csqaq.client import CSQAQClient                            # noqa: E402

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


def reset_globals():
    cq._IP_REBIND_UNTIL = 0.0
    cq._IP_DRIFT_UNTIL = 0.0
    cq._IP_REBIND_FAILS = 0
    cq._IP_LAST_DRIFT_IP = None
    cq._TIMEOUT_REBIND_LAST = 0.0


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """假的 CSQAQ HTTP 会话:任何请求都返回 code=200 的空数据。"""

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return _Resp({"code": 200, "data": {"success": {}}})

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return _Resp({"code": 200, "data": {"sub_index_data": []}})


def make_client(interval=0.0):
    cfg = CSQAQConfig(api_token="t", request_interval=interval,
                      request_timeout=5.0, retry_times=1)
    api = CSQAQClient(cfg)
    sess = _Session()

    async def fake_ensure():
        return sess

    api._ensure_session = fake_ensure          # type: ignore[assignment]
    return api, sess


LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


print("=" * 78)
print("V0.3.15 IP 漂移 × 盯盘 —— 修复回归")
print("=" * 78)

# ------------------------------------------------------------------ [1] 超时越过冷却
print("\n[1] 超时重绑:冷却期内也能真重绑(bypass_cooldown=True)")


class _BindSpy:
    def __init__(self, result=True):
        self.calls = 0
        self.result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def bind_local_ip(self):
        self.calls += 1
        if not self.result:
            return False, "绑定失败:接口返回 429"
        return True, "绑定IP更新成功，当前绑定IP为：1.2.3.4"

    async def probe_ip_status(self, **kw):
        """重绑成功后,真实现会立刻探测新出口是否可用。"""
        return True, "ok"


def patch_bind(spy):
    def factory(cfg):
        return spy
    cq.CSQAQClient = factory                    # type: ignore[assignment]


reset_globals()
spy = _BindSpy()
patch_bind(spy)
cq._IP_REBIND_UNTIL = time.monotonic() + 600           # 冷却 600 秒中
bon, info = run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="/info/get_rank_list",
                                       cooldown_seconds=600, bypass_cooldown=True))
ok("超时路径:冷却期内仍然发起了重绑", spy.calls == 1, f"calls={spy.calls} info={info}")
ok("超时路径:返回成功", bon is True, info)

# ------------------------------------------------------------------ [2] 401 仍尊重冷却
print("\n[2] 401 漂移路径不变:照旧尊重冷却")
reset_globals()
spy2 = _BindSpy()
patch_bind(spy2)
cq._IP_REBIND_UNTIL = time.monotonic() + 600
bon2, info2 = run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="/info/get_rank_list",
                                         cooldown_seconds=600))
ok("非超时路径:冷却期内不重绑", spy2.calls == 0, f"calls={spy2.calls}")
ok("非超时路径:返回 cooldown", info2 == "cooldown", info2)

# ------------------------------------------------------------------ [3] 超时重绑下限
# V0.3.19:35s → 300s 起步(钉住服务器 IP 后出口恒定,超时不再是漂移信号),阶梯 300→600→1200→1800
print("\n[3] 超时重绑有下限:300s 起步(钉 IP 后超时不再等于漂移),随连续失败逐级加长")
reset_globals()
spy3 = _BindSpy(result=False)
patch_bind(spy3)
cq._IP_REBIND_UNTIL = time.monotonic() + 600
run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="x", bypass_cooldown=True))
first_last = cq._TIMEOUT_REBIND_LAST
cq._IP_REBIND_UNTIL = time.monotonic() + 600           # 再上锁
bon3, info3 = run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="x",
                                         bypass_cooldown=True))
ok("300 秒内的第二次超时重绑被拦(不会形成风暴)",
   info3.startswith("timeout-gap"), info3)
ok("拦截时没有再打 bind", spy3.calls == 1, f"calls={spy3.calls}")
ok("时间戳被记录(供下次比较)", first_last > 0, first_last)

# 连续失败 2 次后,下限翻倍到 60 秒
reset_globals()
spy4 = _BindSpy(result=False)
patch_bind(spy4)
cq._IP_REBIND_UNTIL = time.monotonic() + 600
run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="x", bypass_cooldown=True))
ok("第一次失败后 fails=1", cq._IP_REBIND_FAILS == 1, cq._IP_REBIND_FAILS)
# fails=1 → 下限 300s,所以等满 300 秒后放行(阶梯 300→600→1200→1800)
cq._TIMEOUT_REBIND_LAST = time.monotonic() - 301
cq._IP_REBIND_UNTIL = time.monotonic() + 600
bon4a, info4a = run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="x",
                                           bypass_cooldown=True))
ok("fails=1 时下限 300 秒:等满后放行", not info4a.startswith("timeout-gap"), info4a)
ok("  放行时确实又打了 bind", spy4.calls == 2, f"calls={spy4.calls}")
ok("第二次失败后 fails=2", cq._IP_REBIND_FAILS == 2, cq._IP_REBIND_FAILS)
# fails=2 → 下限升到 600s,300 秒不再够
cq._TIMEOUT_REBIND_LAST = time.monotonic() - 301
cq._IP_REBIND_UNTIL = time.monotonic() + 600
bon4, info4 = run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), source="x",
                                         bypass_cooldown=True))
ok("fails=2 时下限升到 600 秒(300 秒被拦)", info4.startswith("timeout-gap"), info4)
ok("  拦截时没有再打 bind", spy4.calls == 2, f"calls={spy4.calls}")

# ------------------------------------------------------------------ [4] 漂移期只停榜单
print("\n[4] 漂移冷却期内:榜单照旧停,盯盘三件套放行")
cq.CSQAQClient = CSQAQClient                    # 还原真类(前面被 patch_bind 换过)


def drift_on():
    reset_globals()
    cq._IP_DRIFT_UNTIL = time.monotonic() + 120


drift_on()
api, sess = make_client()
try:
    run(api.get_rank_list(page_index=1, page_size=100))
    ok("榜单:漂移期被跳过(打了也是白打)", False, "没有抛错")
except RuntimeError as exc:
    ok("榜单:漂移期被跳过(打了也是白打)", "漂移冷却中" in str(exc), str(exc)[:70])

drift_on()
api2, sess2 = make_client()
try:
    out = run(api2.get_price_by_market_hash_name(["AK-47 | Redline (Field-Tested)"]))
    ok("盯盘批量查价:漂移期照常放行(不再直接跳过)", True)
    ok("  └ 确实发出了请求", any("getPriceByMarketHashName" in u for _, u in sess2.calls),
       sess2.calls)
except RuntimeError as exc:
    ok("盯盘批量查价:漂移期照常放行(不再直接跳过)", False, str(exc)[:70])

drift_on()
api3, sess3 = make_client()
try:
    run(api3.get_good_info(1))
    ok("单品详情 /info/good:漂移期照常放行", True)
except RuntimeError as exc:
    ok("单品详情 /info/good:漂移期照常放行", False, str(exc)[:70])

drift_on()
api4, sess4 = make_client()
try:
    run(api4.get_current_data())
    ok("市场环境 /current_data:漂移期照常放行(推送卡片要用)", True)
except RuntimeError as exc:
    ok("市场环境 /current_data:漂移期照常放行(推送卡片要用)", False, str(exc)[:70])

# ------------------------------------------------------------------ [5] 退避阶梯不回归
print("\n[5] 重绑失败退避阶梯未回归(120→240→480→600 封顶)")
reset_globals()
ladder = []
for n in range(1, 6):
    cq._IP_REBIND_FAILS = n
    ladder.append(round(cq._rebind_fail_backoff(600)))
ok("阶梯 120/240/480/600/600", ladder == [120, 240, 480, 600, 600], str(ladder))

# ------------------------------------------------------------------ [6] 老行为可选
print("\n[6] 显式传 allow_drift_skip=True 时,老行为仍可用")
drift_on()
api5, _ = make_client()
try:
    run(api5.get_price_by_market_hash_name(["x"], allow_drift_skip=True))
    ok("allow_drift_skip=True 时照旧直接跳过", False, "没有抛错")
except RuntimeError as exc:
    ok("allow_drift_skip=True 时照旧直接跳过", "漂移冷却中" in str(exc), str(exc)[:70])

# ------------------------------------------------------------------ [7] bind 提示判定
print("\n[7] bind 接口提示判定(上线核验抓到的真 bug)")
api6, _ = make_client()


def bind_with(msg):
    async def fake_post(path, payload, **kw):
        return {"code": 200, "data": msg}
    api6._post = fake_post                       # type: ignore[assignment]
    return run(api6.bind_local_ip())


b_ok, m_ok = bind_with("绑定IP更新成功，当前绑定IP为：14.145.53.65")
ok("成功提示 → True", b_ok is True, f"{b_ok} {m_ok}")
b_rl, m_rl = bind_with("请求频率过快，绑定IP频率限制为30s")
ok("「请求频率过快」→ False(旧版当成功,白重试一次)", b_rl is False, f"{b_rl} {m_rl}")
b_fl, _ = bind_with("绑定失败")
ok("「绑定失败」→ False", b_fl is False, b_fl)

# ------------------------------------------------------------------ [8] runner 门控结构锁
print("\n[8] runner.py 结构锁:榜单该停、盯盘五处不该再停")
src = (ROOT.parent / "bot" / "anomaly" / "runner.py").read_text(encoding="utf-8")
for text in ("英文名补全跳过", "求购补查跳过", "A 池批量跟踪跳过",
             "S 池批量跟踪跳过", "BUFF+悠悠同步跳过"):
    ok(f"盯盘门控已放开: {text}", text not in src)
ok("榜单扫描仍然保留门控", "榜单扫描跳过" in src)
n_try = src.count("漂移冷却中仍尝试(V0.3.15)")
ok("五处盯盘改成仍尝试", n_try == 5, n_try)

print("\n" + "=" * 78)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
