# -*- coding: utf-8 -*-
"""V0.3.19 钉"目标服务器 IP"离线自测。

覆盖:config 解析、host 提取、resolver 行为、候选选择(手动/自动/全部失败)、
缓存读写、连续失败换 IP(阈值 + 120s 限频)、状态查询。

用法: .venv\\Scripts\\python.exe -X utf8 test_pin_v0319.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import json
import os
import sys
import tempfile

import yaml

import bot.csqaq.client as C
from bot.config import CSQAQConfig, load_config

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"    [PASS] {name}")
    else:
        print(f"    [FAIL] {name}  {extra}")
        FAILS.append(name)


_CASE = {"n": 0}


def fresh(tmpdir):
    """重置模块级状态 + 把缓存指到**每个用例自己的**临时目录(防用例间串缓存)。"""
    _CASE["n"] += 1
    d = os.path.join(tmpdir, f"case{_CASE['n']}")
    os.makedirs(d, exist_ok=True)
    C._PIN_IP = None
    C._PIN_CANDIDATES = []
    C._PIN_FAIL_STREAK = 0
    C._PIN_SELECTED = False
    C._PIN_LAST_ROTATE = 0.0
    C._PIN_CACHE_PATH = os.path.join(d, "csqaq_pin.json")
    C._dns_candidates = lambda cfg: ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    async def _no_bind(cfg, ip):
        return False
    C._bind_via_ip = _no_bind        # 默认不绑定(离线),个别用例再替换


def fake_probe(ok_ips):
    async def _probe(cfg, ip):
        return ip in ok_ips
    return _probe


def make_cfg(**kw):
    cfg = CSQAQConfig()
    cfg.api_token = "test-token"
    cfg.base_url = "https://api.csqaq.com/api/v1"
    cfg.pin_auto = True
    cfg.pin_server_ip = ""
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


async def main():
    tmpdir = tempfile.mkdtemp(prefix="pin_test_")

    # ---------- [1] 配置解析 ----------
    print("\n  [1] config 解析 pin_auto / pin_server_ip")
    p = os.path.join(tmpdir, "c1.yaml")
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump({"csqaq": {"api_token": "T", "pin_auto": False, "pin_server_ip": "1.2.3.4"}}, f)
    cfg = load_config(p)
    check("pin_auto=False 读进来", cfg.csqaq.pin_auto is False, cfg.csqaq.pin_auto)
    check("pin_server_ip 读进来", cfg.csqaq.pin_server_ip == "1.2.3.4", cfg.csqaq.pin_server_ip)

    p2 = os.path.join(tmpdir, "c2.yaml")
    with open(p2, "w", encoding="utf-8") as f:
        yaml.safe_dump({"csqaq": {"api_token": "T"}}, f)
    cfg2 = load_config(p2)
    check("缺省 pin_auto=True", cfg2.csqaq.pin_auto is True)
    check("缺省 pin_server_ip 为空", cfg2.csqaq.pin_server_ip == "")

    # ---------- [2] host 提取 ----------
    print("\n  [2] _csqaq_host")
    check("正常 base_url", C._csqaq_host(make_cfg()) == "api.csqaq.com")
    check("带尾部斜杠", C._csqaq_host(make_cfg(base_url="https://api.csqaq.com/api/v1/")) == "api.csqaq.com")
    check("空值兜底", C._csqaq_host(make_cfg(base_url="")) == "api.csqaq.com")
    check("None 兜底", C._csqaq_host(None) == "api.csqaq.com")

    # ---------- [3] resolver 行为 ----------
    print("\n  [3] _PinnedResolver")
    fresh(tmpdir)
    C._PIN_IP = "10.0.0.2"

    class FakeFallback:
        def __init__(self):
            self.calls = []

        async def resolve(self, host, port=0, family=0):
            self.calls.append(host)
            return [{"hostname": host, "host": "9.9.9.9", "port": port or 443,
                     "family": 2, "proto": 6, "flags": 0}]

        async def close(self):
            return None

    r = C._PinnedResolver("api.csqaq.com")
    fb = FakeFallback()
    r._fallback = fb
    res = await r.resolve("api.csqaq.com", 443)
    check("钉住域名返回钉的 IP", res[0]["host"] == "10.0.0.2", res)
    check("钉住域名不调系统解析", fb.calls == [], fb.calls)
    res2 = await r.resolve("other.example.com", 443)
    check("其它域名透传", res2[0]["host"] == "9.9.9.9", res2)
    check("其它域名调了系统解析", fb.calls == ["other.example.com"], fb.calls)

    C._PIN_IP = "10.0.0.3"
    res3 = await r.resolve("api.csqaq.com", 443)
    check("换 _PIN_IP 后立即生效(无需重建 session)", res3[0]["host"] == "10.0.0.3", res3)

    C._PIN_IP = None
    res4 = await r.resolve("api.csqaq.com", 443)
    check("不钉时走系统解析", res4[0]["host"] == "9.9.9.9", res4)

    # ---------- [4] 选择逻辑 ----------
    print("\n  [4] select_pin_ip")
    fresh(tmpdir)
    C._probe_pinned_ip = fake_probe({"10.0.0.2"})
    got = await C.select_pin_ip(make_cfg())
    check("自动选中第一个放行的 IP", got == "10.0.0.2", got)
    check("候选顺序:成功者在前", C._PIN_CANDIDATES[0] == "10.0.0.2", C._PIN_CANDIDATES)
    check("候选含其余 A 记录", set(C._PIN_CANDIDATES) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"},
          C._PIN_CANDIDATES)

    # 幂等:再调一次不应重新探测
    calls = {"n": 0}

    async def counting_probe(cfg, ip):
        calls["n"] += 1
        return False
    C._probe_pinned_ip = counting_probe
    again = await C.select_pin_ip(make_cfg())
    check("幂等:已选过直接返回", again == "10.0.0.2", again)
    check("幂等:没有重新探测", calls["n"] == 0, calls["n"])

    # 手动指定优先
    fresh(tmpdir)
    C._probe_pinned_ip = fake_probe({"10.0.0.3", "10.0.0.2"})
    got = await C.select_pin_ip(make_cfg(pin_server_ip="10.0.0.3"))
    check("手动指定优先", got == "10.0.0.3", got)

    # 手动指定不探测(省请求、也避免限频误判)
    probe_calls = {"n": 0}

    async def never_called(cfg, ip):
        probe_calls["n"] += 1
        return False
    fresh(tmpdir)
    C._probe_pinned_ip = never_called
    got = await C.select_pin_ip(make_cfg(pin_server_ip="10.0.0.9"))
    check("手动指定不探测", got == "10.0.0.9" and probe_calls["n"] == 0,
          (got, probe_calls["n"]))

    # 探测结果全不明确(限频) → 先暂钉第一个,别放弃
    async def all_unclear(cfg, ip):
        return None
    fresh(tmpdir)
    C._probe_pinned_ip = all_unclear
    got = await C.select_pin_ip(make_cfg())
    check("全部说不清 → 暂钉第一个候选", got == "10.0.0.1", got)
    check("暂钉也有候选列表", len(C._PIN_CANDIDATES) == 3, C._PIN_CANDIDATES)

    # 全部候选都被拒 → 自己 bind 一次,绑上就用
    fresh(tmpdir)
    state = {"bound": False}

    async def probe_sim(cfg, ip):
        return state["bound"]

    async def bind_sim(cfg, ip):
        state["bound"] = True
        return True
    C._probe_pinned_ip = probe_sim
    C._bind_via_ip = bind_sim
    got = await C.select_pin_ip(make_cfg())
    check("全部被拒 → bind 后钉住第一条入口", got == "10.0.0.1", got)
    check("bind 后候选列表完整", len(C._PIN_CANDIDATES) == 3, C._PIN_CANDIDATES)

    # bind 成功但复核仍被拒 → 不钉(说明 bind 没生效)
    fresh(tmpdir)
    C._probe_pinned_ip = fake_probe(set())

    async def bind_ok2(cfg, ip):
        return True
    C._bind_via_ip = bind_ok2
    got = await C.select_pin_ip(make_cfg())
    check("bind 后复核被拒 → 仍不钉", got is None, got)

    # bind 结果不明确(限频) → 不钉,别乱来
    fresh(tmpdir)
    C._probe_pinned_ip = fake_probe(set())

    async def bind_unclear(cfg, ip):
        return None
    C._bind_via_ip = bind_unclear
    got = await C.select_pin_ip(make_cfg())
    check("bind 不明确 → 不钉", got is None, got)

    # 全部说不清(限频) → 先暂钉,但不去 bind
    fresh(tmpdir)
    bind_called = {"n": 0}

    async def counting_bind(cfg, ip):
        bind_called["n"] += 1
        return True
    C._bind_via_ip = counting_bind

    async def all_unclear2(cfg, ip):
        return None
    C._probe_pinned_ip = all_unclear2
    got = await C.select_pin_ip(make_cfg())
    check("全说不清 → 暂钉第一个且不调 bind", got == "10.0.0.1" and bind_called["n"] == 0,
          (got, bind_called["n"]))

    # 全部失败
    fresh(tmpdir)
    C._probe_pinned_ip = fake_probe(set())
    got = await C.select_pin_ip(make_cfg())
    check("全部失败 → 不钉", got is None, got)
    check("全部失败 → 状态 enabled=False", C.pin_state()["enabled"] is False)

    # pin_auto=False
    fresh(tmpdir)
    C._probe_pinned_ip = fake_probe({"10.0.0.1"})
    got = await C.select_pin_ip(make_cfg(pin_auto=False))
    check("pin_auto=False → 不钉", got is None, got)
    check("pin_auto=False → 不探测",
          C._PIN_IP is None and C._PIN_CANDIDATES == [], C._PIN_CANDIDATES)

    # cfg=None 不炸
    fresh(tmpdir)
    got = await C.select_pin_ip(None)
    check("cfg=None → 不钉且不炸", got is None, got)

    # ---------- [5] 缓存 ----------
    print("\n  [5] 缓存读写")
    fresh(tmpdir)
    C._save_pin_cache(["10.0.0.2", "10.0.0.1"])
    check("缓存文件已写", os.path.exists(C._PIN_CACHE_PATH))
    cached = C._load_pin_cache()
    check("缓存读回一致", cached == ["10.0.0.2", "10.0.0.1"], cached)
    with open(C._PIN_CACHE_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    check("缓存含 saved_at", "saved_at" in raw, raw)
    C._PIN_CACHE_PATH = os.path.join(tmpdir, "missing.json")
    check("缓存不存在→空列表", C._load_pin_cache() == [])

    # 缓存优先作为候选顺序
    fresh(tmpdir)
    C._save_pin_cache(["10.0.0.3"])
    C._probe_pinned_ip = fake_probe({"10.0.0.1", "10.0.0.2", "10.0.0.3"})
    got = await C.select_pin_ip(make_cfg())
    check("上次成功的 IP 优先复用", got == "10.0.0.3", got)

    # ---------- [6] 连续失败换 IP ----------
    print("\n  [6] note_pin_failure 阈值与限频")
    fresh(tmpdir)
    cfg = make_cfg()
    C._PIN_IP = "10.0.0.1"
    C._PIN_CANDIDATES = ["10.0.0.1", "10.0.0.2"]
    C._probe_pinned_ip = fake_probe({"10.0.0.2"})
    await C.note_pin_failure(cfg)
    check("失败 1 次不换", C._PIN_IP == "10.0.0.1", C._PIN_IP)
    check("失败计数=1", C._PIN_FAIL_STREAK == 1, C._PIN_FAIL_STREAK)
    await C.note_pin_failure(cfg)
    check("失败 2 次不换", C._PIN_IP == "10.0.0.1", C._PIN_IP)
    await C.note_pin_failure(cfg)
    check("失败 3 次换到备用 IP", C._PIN_IP == "10.0.0.2", C._PIN_IP)
    check("换 IP 后计数清零", C._PIN_FAIL_STREAK == 0, C._PIN_FAIL_STREAK)

    # 限频:120s 内不重复换
    fresh(tmpdir)
    cfg = make_cfg()
    C._PIN_IP = "10.0.0.1"
    C._PIN_CANDIDATES = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    C._probe_pinned_ip = fake_probe({"10.0.0.2", "10.0.0.3"})
    C._PIN_LAST_ROTATE = C.time.monotonic()          # 假装刚换过
    for _ in range(C._PIN_FAIL_LIMIT + 2):
        await C.note_pin_failure(cfg)
    check("120s 内因限频不换 IP", C._PIN_IP == "10.0.0.1", C._PIN_IP)

    # 所有候选都失败(401)→ V0.3.37 起保留当前 IP,不再退回系统 DNS
    fresh(tmpdir)
    C._PIN_IP = "10.0.0.1"
    C._PIN_CANDIDATES = ["10.0.0.1", "10.0.0.2"]
    C._probe_pinned_ip = fake_probe(set())
    C._PIN_LAST_ROTATE = 0.0
    for _ in range(C._PIN_FAIL_LIMIT):
        await C.note_pin_failure(make_cfg())
    check("候选全失败 → 保留原 pin(不退回 DNS)", C._PIN_IP == "10.0.0.1", C._PIN_IP)

    # 未钉时不该有任何动作
    fresh(tmpdir)
    await C.note_pin_failure(make_cfg())
    check("未钉时 note_pin_failure 无副作用", C._PIN_FAIL_STREAK == 0, C._PIN_FAIL_STREAK)

    # 换钉探测不明确(限频)→ 保留当前 IP,别丢
    async def unclear(cfg, ip):
        return None
    fresh(tmpdir)
    C._PIN_IP = "10.0.0.1"
    C._PIN_CANDIDATES = ["10.0.0.1", "10.0.0.2"]
    C._probe_pinned_ip = unclear
    C._PIN_LAST_ROTATE = 0.0
    for _ in range(C._PIN_FAIL_LIMIT):
        await C.note_pin_failure(make_cfg())
    check("换钉探测限频 → 保留原 IP", C._PIN_IP == "10.0.0.1", C._PIN_IP)
    check("换钉探测限频 → 计数清零", C._PIN_FAIL_STREAK == 0, C._PIN_FAIL_STREAK)

    # ---------- [7] 状态 ----------
    print("\n  [7] pin_state / reset_pin_streak")
    fresh(tmpdir)
    C._PIN_IP = "10.0.0.2"
    C._PIN_CANDIDATES = ["10.0.0.2", "10.0.0.1"]
    C._PIN_FAIL_STREAK = 2
    st = C.pin_state()
    check("state.enabled", st["enabled"] is True, st)
    check("state.ip", st["ip"] == "10.0.0.2", st)
    check("state.candidates", st["candidates"] == ["10.0.0.2", "10.0.0.1"], st)
    check("state.fail_streak", st["fail_streak"] == 2, st)
    C.reset_pin_streak()
    check("reset_pin_streak 清零", C._PIN_FAIL_STREAK == 0, C._PIN_FAIL_STREAK)

    # ---------- [8] 选择锁(防启动并发重复探测) ----------
    print("\n  [8] 选择锁")
    C._PIN_SELECT_LOCK = None
    l1 = C._pin_select_lock_obj()
    l2 = C._pin_select_lock_obj()
    check("锁对象复用(同一个)", l1 is l2 and isinstance(l1, asyncio.Lock), type(l1))

    calls = {"n": 0}

    async def slow_select(cfg, *, force=False):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        C._PIN_SELECTED = True
        return "10.0.0.1"

    orig_select = C.select_pin_ip
    C.select_pin_ip = slow_select

    async def first_call():
        if not C._PIN_SELECTED:
            async with C._pin_select_lock_obj():
                if not C._PIN_SELECTED:
                    await C.select_pin_ip(None)

    C._PIN_SELECTED = False
    await asyncio.gather(*[first_call() for _ in range(5)])
    C.select_pin_ip = orig_select
    check("并发首调只真正选一次", calls["n"] == 1, calls["n"])

    print(f"\n  {len(FAILS)} 个失败" if FAILS else "\n  全部通过 ✓")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
