# -*- coding: utf-8 -*-
"""配置加载模块。"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

import yaml

from bot.anomaly.cost import CostGateConfig


@dataclass
class CSQAQConfig:
    api_token: str = ""
    base_url: str = "https://api.csqaq.com/api/v1"
    request_interval: float = 1.0
    # V0.3.9:单次请求超时。原为硬编码 30s —— 实测批量查价分片只有 50 件、榜单页 ~1s,
    # 30s 意味着落到未绑定 WAN 的请求要"白等 30 秒"才失败(而失败又会杀掉连接→重连再赌 WAN)。
    # 15s 足够(留 10 倍余量),同时把漂移期的无效等待砍半。
    request_timeout: float = 15.0
    # V0.3.9:检测到漂移后"暂停普通请求"的秒数(原=重绑退避上限 600s,一旦重绑失败就整机空转
    # 10 分钟)。现与重绑失败退避解耦:暂停只 120s,失败退避仍封顶 rebind_cooldown_seconds。
    drift_pause_seconds: int = 120
    retry_times: int = 3
    # IP 漂移止血(2026-09-04):检测到漂移后进入冷却期,期间普通请求快速失败不烧额度;
    # 同一漂移期只允许一次 bind_local_ip(单飞锁)。600s(V0.0.5):重绑失败也进冷却,
    # 且 bind 接口官方限频 30s/次,冷却太短会在出口抖动时把 bind 打出风控(429→挂起连接)。
    rebind_cooldown_seconds: int = 600
    # V0.3.19:钉"目标服务器 IP"。实测本机出口 IP 由目标服务器 IP 决定,而 api.csqaq.com 有 5 个
    # A 记录、只有 2 个走的是已绑定出口 → 不钉时 8 次里只成功 2 次。钉住后 8/8 全通。
    # pin_auto=true 启动时自动探测选一个放行的 IP(结果缓存到 data/csqaq_pin.json);
    # 想手动指定就填 pin_server_ip。
    pin_auto: bool = True
    pin_server_ip: str = ""


@dataclass
class SteamDTConfig:
    """SteamDT 开放平台(大盘指数专用源)。

    仅用 Bearer API_KEY 认证,无 IP 白名单绑定(已实测当前漂移 IP 直接 200)。
    健壮性与 CSQAQ 的 IP 漂移自恢复对齐:调用失败自动重试 + 健康循环探测 +
    大盘回退 CSQAQ 兜底,保证大盘数据不断、不误触。
    """
    api_key: str = ""
    base_url: str = "https://open.steamdt.com"
    request_interval: float = 1.0
    retry_times: int = 3
    # 缓存 TTL(秒):V0.0.8 300→3600——大盘指数本身是小时级数据(historyMarketIndexList 每小时一点),
    # 1 小时缓存零信息损失,把 broad index 真实网络请求从 ~48 次/天砍到 ~24 次/天,规避免费配额耗尽。
    cache_ttl_seconds: int = 3600   # 大盘指数内存缓存(秒):限频极宽,仅作保险
    # ---- V0.3.14:broad 接口实测限频是「约 30 秒一次」,不是"配额总量耗尽" ----
    # 2026-10-12 实测:连打(间隔 1.5s)必回 code=4005「当前接口请求已达到上限」;
    # 等 15s 仍被拒;等 30s 成功;等 60s 成功。原始单发 GET /open/cs2/broad/v1/index
    # 一直是 200 + success:true + 完整数据 —— 接口从来没坏,是客户端 1.5 秒连试 3 次
    # 自己撞出来的 4005,再把整个接口族锁 1800 秒,于是 5 周来一直在"自锁"。
    broad_min_gap_seconds: float = 30.0     # 两次 broad 网络请求的最小间隔(实测下界)
    broad_retry_delay_seconds: float = 35.0  # 失败后**后台**重试的等待(不阻塞调用方)
    broad_cooldown_seconds: float = 120.0   # 首次失败的基础退避(原 1800s 过长,会盖住 30s 就能恢复的事实)


@dataclass
class QQConfig:
    appid: str = ""
    secret: str = ""
    keywords: Dict[str, List[str]] = field(default_factory=lambda: {
        "recommend": ["推荐", "选品", "有什么好买的", "买什么"],
        "price": ["价格", "行情", "多少钱"],
        "rank": ["排行", "涨跌榜", "排行榜"],
        "help": ["帮助", "help", "指令"],
    })


@dataclass
class SchedulerConfig:
    fetch_interval_minutes: int = 15
    rank_page_size: int = 100
    rank_max_pages: int = 3


@dataclass
class PushConfig:
    enable: bool = False
    group_openids: List[str] = field(default_factory=list)
    times: List[str] = field(default_factory=lambda: ["15:00"])  # 每日播报定时点列表(HH:MM)，支持多次推送


@dataclass
class RecommendConfig:
    min_price: float = 50.0
    max_price: float = 20000.0   # 推荐价格上限：放宽到 20000，让流动性好的高价枪/刀也能进超跌榜
    min_liquidity: int = 50      # 超跌流动性门槛：BUFF/悠悠在售+求购 + Steam 在售合计≥该值才算"好出手"
                                 # (V0.2.29:Steam 求购字段是脏数据,已从合计中剔除)
    top_n: int = 8


@dataclass
class LLMConfig:
    """AI 智能体(LLM)配置。OpenAI 兼容 chat/completions 接口,各家可切换:

    - 智谱 GLM-4-Flash(免费): base_url=https://open.bigmodel.cn/api/paas/v4
    - 腾讯混元: base_url=https://api.hunyuan.cloud.tencent.com/v1
    - DeepSeek:  base_url=https://api.deepseek.com/v1
    """
    enable: bool = False
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str = ""
    model: str = "glm-4-flash"
    timeout: float = 30.0
    max_tokens: int = 600
    temperature: float = 0.7
    history_len: int = 8       # 每个会话保留的历史消息条数
    history_ttl: int = 1800    # 会话上下文有效期(秒)
    route_all_nl: bool = True  # True=所有自然语言都走 AI;False=仅本地未识别意图走 AI


@dataclass
class VipConfig:
    """会员群白名单:以下会员功能仅这些群可用。"""
    group_openids: List[str] = field(default_factory=list)
    watch_enable: bool = True   # 盯盘功能是否限会员群


@dataclass
class FreeConfig:
    """免费群:开启盯盘但限每用户数量,作为会员转化钩子。大盘指数盯盘仍限会员群。"""
    group_openids: List[str] = field(default_factory=list)
    watch_enable: bool = False          # 免费群是否开盯盘
    max_watches_per_user: int = 1       # 每用户最多盯盘项数(0 表示不准添加)


@dataclass
class AnomalyConfig:
    """饰品异动雷达(PRD V1.0): 三层池 B/A/S + AnomalyScore 综合评分。

    数据源边界(硬约束,不得违反):
    - CSQAQ 单品详情可提供 Steam turnover_number,但过期/缺失时一律不伪造成交量
    - 可用真实指标: 在售数/求购数/买卖价差/今日涨跌 buff_price_chg/7日/30日涨跌/历史价格位置
    - 替代口径: 用「求购增 + 在售减」表述需求增强,不得写成「成交量放大」

    三层池数据获取方式(与直觉不同,务必注意):
    - B 全量池走「多维度榜单深翻」而非 goods_index:goods_index 仅存中文名,
      而批量查价接口 getPriceByMarketHashName 只认 market_hash_name(英文名),
      逐个转换需 2.5 万次 /info/good 请求,不可行。榜单自带完整行情,无需英文名。
    - A 活跃池由榜单发现 + 3 分钟批量跟踪组成，短线时序独立于榜单刷新。
    - S 超级池走批量查价(50 件/批),需要英文名 → 建 goods_mhn_map 增量映射表缓存。
    """
    enable: bool = True

    # ---- B 全量发现池(榜单深翻) ----
    b_enable: bool = True
    b_size: int = 3000                 # B 池最大存量；超过后淘汰最低分，避免长尾无限膨胀
    b_pages_per_sort: int = 25          # 每个 sort 深翻页数(×100 件/页)
    b_pages_pct_sort: int = 5           # V0.3.5:涨跌幅(百分比)榜单独页数(该榜被分币品霸榜,深翻无收益)
    b_early_stop_pages: int = 3         # V0.3.5:翻到第 N 页仍"整轮零可评分品"就止损本 sort(0=关)
    b_interval_min: int = 180           # 完成一轮全量扫描的周期(分钟)

    # ---- A 活跃监控池 ----
    a_size: int = 800                   # A 池目标规模(保守起步,避免撞免费 Key 隐性配额)
    a_interval_min: int = 10            # 兼容旧配置:榜单发现频率(分钟)
    a_pages: int = 1                    # 每个 sort 取前 N 页(10 sort×1页=10请求/轮,去重≈600-900件)。
    a_discover_interval_min: int = 10   # A 榜单发现频率;释放额度给 A 池批量跟踪
    a_track_enable: bool = True         # 是否独立批量跟踪 A 池成员
    a_track_interval_sec: int = 180     # A 池批量跟踪周期(默认 3 分钟)
    a_track_batch_size: int = 800       # 每轮最多跟踪的 A 池成员数(单次 ≤50)
    a_track_hydrate_per_round: int = 5  # 每轮补全 market_hash_name 的详情请求数

    # ---- S 超级异动池 ----
    s_size: int = 50
    s_interval_sec: int = 30
    platform_sync_batch_size: int = 20  # 每 10 分钟详情补全数,给 A/S 短线跟踪留额度

    # ---- 全量周期总结快照 ----
    # 直接复用榜单批量行情，不逐品调用 /info/good；仅统计 BUFF+悠悠在售>阈值的品。
    summary_enable: bool = True
    summary_interval_min: int = 120
    summary_pages_per_sort: int = 25
    summary_min_sell_num: int = 200
    phase_window_minutes: int = 60  # 生产配置改为120;默认60兼容旧单元测试/旧调用

    # ---- 评分权重(合计 100) ----
    w_price_5m: float = 15.0
    w_price_30m: float = 10.0
    w_price_1h: float = 10.0
    w_buy_change: float = 10.0          # 求购数量增加(需求侧)
    w_sell_change: float = 10.0         # 在售数量减少(供应侧)
    w_order_ratio: float = 5.0          # 买卖盘数量比异常
    w_spread: float = 15.0              # 买卖价差变化
    w_trend: float = 15.0               # 短中期趋势一致性
    w_position: float = 10.0            # 历史价格位置(区分低位/高位)

    # ---- 质量门槛(治本,而非调阈值) ----
    # 低价款/死盘/薄盘在百分比口径下波动虚高(0.04->0.03 即 -25%),会把评分顶到 L2 刷屏;
    # 而高价正经品反而很少触发。故在评分入口直接拦截无真实交易价值的噪声。
    min_price: float = 5.0          # 主参考价低于此值(元)不评分不推送(¥5 以下多为无流动性噪声)
    min_listing: int = 10           # 在售+求购总数低于此值视为无流动性噪声,跳过
    min_buy_num: int = 1            # 求购数低于此值视为无真实需求(死盘),跳过不评分
    min_anomaly_buy_num: int = 50  # 异动有效求购下限(BUFF+悠悠合计)
    anomaly_buy_grace_minutes: int = 60  # 真实求购持续低于异动门槛多久后才移出监控池
    min_sell_num: int = 3           # 在售挂单下限:低于此值视为无真实交易市场(薄盘噪声,如 1 个挂单的 ¥500 品)
    min_depth: float = 600.0        # 在售侧市场深度 = sell_num*price 低于此值(元)视为薄盘噪声,跳过(¥200 太松;¥1000 会误伤价差正常低价品如 TRY 金色印花)
    max_sell_buy_ratio: float = 3.0  # 售价/求购价超过此倍数=求购全是捡漏单(死盘),跳过。
    # 实测:死盘品(格洛克蓝色裂纹 6.6x、MAC-10 孔雀石 27.8x、UMP 辐射警告 21.5x)求购价只有售价的 1/6~1/27,
    # 而正经流动品(美杜莎/黄金藤蔓/手套)价差恒在 1.04~1.4x,此闸门一拦一个准,绝不误伤。
    thin_noise_price_pct: float = 10.0  # 「价动量不动」闸门:30m 价格变化% 超此值,且盘口几乎未同步变化时,判参考价塌方/薄盘噪声。
    # ---- 价格绝对变动下限(V0.2.29) ----
    # 盘口数量早就是「百分比 + 绝对增量 + 基线分档」三重判定,价格此前只有百分比 ——
    # 不对称:¥9 品价差从 1% 变 4.4%(3 毛钱)也按百分比拿满分。实测推送事件价格中位 ¥40、
    # 52% 在 ¥50 以下,与「价的噪声没防住」直接相关。
    # 语义:某窗口 |现价 - 基准价| < 此值(元) → 该窗口不计分,但权重仍占位(避免归一化虚高)。
    price_abs_min: float = 2.0
    # ---- 买卖价差变化口径(V0.2.29) ----
    # 旧实现取绝对值 → 价差「扩大」(对买方是恶化)与「收窄」同分;且兜底路径算的是
    # 「价差的百分比变化」(百分比之百分比),低价品会出现 340% 这种尺度假信号。
    spread_pct_full: float = 30.0   # 榜单源(sell_buy_diff_chg_buff,%):达此值满分;仅收窄计分
    spread_pp_full: float = 3.0     # 批量源自算:改用「价差百分点变化」,达此值满分;仅收窄计分
    # ---- 中期基线合理性闸(V0.2.29) ----
    # 实测 ¥20 品出现 7日 +665% / 30日 +3000% 的陈旧基线,却一路被 TrendScore 与方向票放大,
    # 把噪声顶进 L3。与 recommender._pass_common 的 |rate_7|>=100 排除口径保持一致。
    trend_baseline_max_7d: float = 100.0
    trend_baseline_max_30d: float = 200.0
    # ---- 高价品流动性闸门(治本:高价收藏品挂单多但成交极稀,2~5% 波动多为参考价微调) ----
    # 实测:M4A4 咆哮 FN(¥44300,在售94/求购15)30m -3.3% 但求购 0%、在售仅 +2.2%——
    # 价格动了盘口死水,属参考价微调非真异动,却反复触发 L2。而内格夫雷神之锤(求购+20%)等
    # 盘口真变化的品应保留。故对高价品单设更严格的盘口配合判定。
    high_price_tier: float = 5000.0   # 单价 ≥ 此值(元)适用高价品流动性闸门
    high_price_noise_pct: float = 2.0 # 高价品 30m 价格变化% ≥ 此值即视为"价格在动"(低价品阈值 10%)
    high_price_num_sig: float = 5.0   # 高价品盘口(求购/在售 30m 变化%)低于此值视为"盘口未配合"
    high_price_min_buy: int = 3       # 高价品求购数下限:求购 < 此值=买盘稀薄(如双栖 ¥16800 仅 2 个求购),转手难,拦
    # ---- BUFF+悠悠流动性闸门 ----
    # 异动雷达统一使用 BUFF 与悠悠有品在售；两字段缺一时不参与流动性判定。
    min_platform_sell: int = 30       # S 池引擎闸门:BUFF+悠悠在售低于此值=真死盘(engine 用,yyyp>0 才判)
    # 扫描前置过滤:接口明确同时提供 BUFF+悠悠在售时,低于此值不进入历史/评分/监控池。
    # 两平台字段不完整时不作判断;推送前复核仍使用同一口径。
    min_scan_sell_num: int = 400
    # 推送前复核阈值(按价格带,BUFF+悠悠口径):
    # 实测「新星|灼木」¥19/两平台在售较少仍被推——低价品靠量,两平台<200 的 ¥300 以下品
    # 市场总价值 <¥6 万,基本无倒手空间,必须拦。
    platform_sell_low: int = 200      # 价格 < platform_sell_tier 的品:全平台在售下限
    platform_sell_high: int = 200     # 价格 ≥ platform_sell_tier 的品:全平台在售下限
    platform_sell_tier: float = 300.0 # 价格分带阈值(元)
    # ---- 离谱涨幅门(数据故障拦截,而非调阈值) ----
    # CSQAQ 陈旧/错误基线会让薄盘品 1h 显示 +3231%(实际是 5m 不动、参考价错)。
    # 这种在 CS2 里几乎不可能真实发生,直接判为数据故障拦截,不评分不推送。
    implausible_30m: float = 200.0  # |30m 价格变化%| 超过此值视为数据故障(实测真实泵升极少超 200%/30m)
    implausible_1h: float = 300.0   # |1h 价格变化%| 超过此值视为数据故障(20 倍以上波动几乎必是错基线)
    quality_reject_confirm_rounds: int = 3  # 质量闸门连续失败次数后才移除池成员

    # ---- 异动等级(L1 观察 / L2 预警 / L3 强异动 / L4 极端) ----
    level_watch: float = 40.0
    level_warning: float = 55.0
    level_strong: float = 70.0
    level_extreme: float = 85.0

    # ---- 三层池升降级 ----
    # 2026-09-04 调整(老大):A 池作高频观察层——升级线放宽到 52 让活跃中价品更快进 A;
    # 降级线 38/12 轮 + stale 回落清理「跌出榜单的僵尸」;升级需 b_confirm_rounds 轮确认。
    b_to_a: float = 52.0                # B→A 评分阈值(原 55;偏保守可试 50)
    a_to_s: float = 70.0                # A→S 评分阈值
    s_to_a: float = 55.0                # S→A:连续 s_to_a_rounds 轮低于此值
    a_to_b: float = 38.0                # A→B:连续 a_to_b_rounds 轮低于此值(原 40)
    s_to_a_rounds: int = 3
    a_to_b_rounds: int = 12             # A→B 确认轮数(原 10)
    s_max_minutes: int = 120            # S 池 TTL,超时强制降级(防止永久占满)
    b_confirm_rounds: int = 2           # 升级 A/S 的连续信号确认轮数(降抖,真信号才升级)
    a_stale_minutes: int = 180          # A 池品「跌出榜单」判僵尸时限:超此时长未被任何扫描
                                        # 更新(=已不在活跃榜)且 last_score<b_to_a → 回落 B 让位

    # ---- 盘口信号三重判定(需求/供给增强;2026-09-04,老大) ----
    # 「求购增加/在售减少」从只看百分比升级为 百分比 + 绝对增量 + 基线分档:
    #   500→550(+50,+10%)=强信号;5→6(+1,+20%)=绝对量不足,噪声不计分。
    # 强信号 = |pct| ≥ book_pct_min 且 绝对变化 ≥ 对应基线档的 abs 阈值。
    book_base_low: int = 10             # 基线(求购/在售数量)<10 = 低基线
    book_base_mid: int = 50             # 10~50 中基线;≥50 高基线
    book_abs_low: int = 3               # 低基线:绝对变化需 ≥3(5→6 +1 不足,拦)
    book_abs_mid: int = 5               # 中基线:≥5
    book_abs_high: int = 15             # 高基线:≥15(500→550 +50 ✓)
    book_pct_min: float = 8.0           # 百分比下限(%):低于此值即使量大也不算强信号(温和波动)
    book_confirm_rounds: int = 2        # 「连续两轮都满足才算有效信号」轮数(engine 单轮标 strong,
                                        # pool 用 hit_count 累计,升级/计分用)

    # ---- 告警去重(分级冷却,分钟):同一物品两次推送的最小间隔 ----
    # 冷却存于常驻表 anomaly_push_cooldown(独立于三层池,出池不丢),避免物品在池间
    # 反复进出被当成"新异动"短间隔重复推送。L3 此前 5 分钟偏短已拉长。
    cooldown_l2_min: int = 30
    cooldown_l3_min: int = 20
    cooldown_l4_min: int = 12
    cooldown_upgrade_delta: float = 10.0  # 评分较上次变化超过此值可突破冷却再推
    # V0.3.16 每品每日推送上限(老大 2026-10-14 拍板)。
    # 起因:6822 蝴蝶刀|渐变之色 累计推 25 次、7474 运动手套|潘多拉之盒 累计 23 次,
    # 最近 3 天这两件占了全部推送的 29%。根因是"同一波反复报"——高价活跃品的求购单
    # 基数 30~60,±8 单的正常波动就能反复命中建仓指纹(12% + 绝对地板),分数在 73↔96
    # 之间振荡,于是三道放行口(Δ≥10 突破冷却 / 冷却到期后 Δ≥5 / 级别变化不算同波)
    # 几乎每轮都能命中一条;而冷却方向还是反的(分越高越短:L4 只 12 分钟)。
    # 上限是最后一道兜底:超过后**仍然落库**(push_status='capped',供 /分析 与
    # signal_outcomes 用)但不再推送。0 = 不限。
    max_push_per_item_per_day: int = 3

    # ---- 资金周期四阶段检测(V0.2.0,2026-09-06 老大拍板;V0.3.0 阈值重构) ----
    # 建仓(吸筹)/启动(抬价)/派发(出货)/消化(盘整),检测器并行独立(不串联),
    # 指纹窗口由 phase_window_minutes 控制(生产为2小时总结快照);短线评分仍单独使用5m/30m/1h。
    #
    # V0.3.0 重构依据(2026-09-24 实测,17.2 天/4 万+ 快照):
    #  · 建仓名义 6% 从来不是真门槛:旧「基线分档×2」绝对量闸把中位有效门槛抬到 34.1%,
    #    能过 min_price 闸的品里 90.6% 面对 ≥20%、只有 2.1% 真按 6% 走;且 49→50 单跨越
    #    档位时绝对增量跳 3 倍(10→30 单)。故改为连续式 max(3 单, 基线×百分比),取消分档。
    #  · 启动是唯一「T+24h 起超额就不为正、逐窗单调恶化」的阶段(T+7d 中位 -13.28%、
    #    同期池子 -3.68% → 超额 -9.60pp),而它此前只要两点对比够数就触发(无持续性、
    #    不看供应侧、不管是否已涨过)。故补四道锁:逐段走高 / 在售不增 / 不追高 / 金额下限。
    #  · 派发「近30日高位」旧实现是 price/sell_price_30-1(30 日**涨幅**,非区间位置),
    #    与真实区间位置一致率仅 81.3%;17.2 天只报 5 条(0.3 条/天,vs 建仓 7.0、启动 4.8)。
    #    故改为真实区间位置(现价 ≥ 近 N 日最高价 × 比例),并在售暴增门槛 10%→6%。
    #  · 保底分把评分架空:209 条阶段事件里 208 条恰好等于保底分(建仓 120/121、启动 83/83、
    #    派发 5/5),故加「基础分闸门」——基础分太低就不给保底,按真实评分走。
    phase_enable: bool = True
    accum_buy_pct_min: float = 12.0    # 建仓:窗口求购净增 ≥12%(V0.3.0:6%→12%,绝对量闸降为地板后由它当主闸)
    accum_abs_min_units: int = 3       # 建仓:绝对增量地板(单);实际要求 = max(地板, 基线×百分比)
    accum_abs_mult: float = 2.0        # 【已废弃·V0.3.0】旧分档绝对量倍数,周期判定不再读取(保留字段兼容旧配置)
    accum_price_quiet: float = 3.0     # 建仓:价格 |chg| <3%(横盘期;涨了就是启动/派发)
    accum_price_quiet_abs: float = 0.5 # 建仓:横盘金额豁免(元)——¥6 品 3% 仅 ¥0.18,一个报价台阶就会破坏横盘
    accum_sell_max: float = 5.0        # 建仓:在售增幅上限(供给在被消化而非堆积)
    launch_buy_price_pct: float = 3.0  # 启动:求购价上移 ≥3%(V0.3.0:2%→3%)
    launch_price_min: float = 1.0      # 启动:价格跟涨 ≥1%
    launch_price_abs_min: float = 2.0  # 启动:价格跟涨的绝对金额下限(元);¥6 品 +1% = ¥0.06 不算启动
    launch_sell_max: float = 5.0       # 启动:在售增幅上限(价格涨同时货暴增 = 派发,不是启动)
    launch_rate7_max: float = 15.0     # 启动:7 日涨幅上限(已涨过一大截的不再喊启动,避免追高)
    distrib_sell_pct: float = 6.0      # 派发:在售暴增 ≥6%(V0.3.0:10%→6%,绝对量改连续式后由它当主闸)
    distrib_range_pct: float = 90.0    # 派发:现价 ≥ 近 distrib_range_days 日最高价 × 此比例 = 真·高位
    distrib_range_days: int = 30       # 派发:区间回看天数
    distrib_pos_high: float = 8.0      # 派发:区间数据缺失时的旧口径回退(pos_30 ≥8% = 30 日涨幅)
    distrib_buy_stall: float = 2.0     # 派发:求购 |chg| <2% = 停滞
    fake_wall_price_dn: float = -2.0   # 假买墙:求购增但价格跌超此值
    fake_wall_sell_up: float = 10.0    # 假买墙:且在售暴增超此值 → 挂单诱多,不触发
    digest_sell_pct: float = 3.0       # 消化:在售净减 ≥3%(/分析 标签)
    digest_buy_pct: float = 2.0        # 消化:求购净增 ≥2%(/分析 标签)
    phase_score_accum: float = 55.0    # 【已废弃·V0.3.0】旧保底分,改加分制后不再读取(保留字段兼容旧配置)
    phase_score_launch: float = 55.0   # 【已废弃·V0.3.0】
    phase_score_distrib: float = 70.0  # 【已废弃·V0.3.0】
    # V0.3.0 阶段加分(取代保底分):score = min(100, 基础分 + 加分)
    # 实测阶段信号的真实基础分只有 6~20 分(建仓 11.3 / 启动 10.7 / 派发 6.4)—— 评分体系
    # 对「价格没动」的指纹天然不给分(PriceScore 0),所以「基础分<35 就不保底」的写法会把
    # 阶段信号全部杀光。改成加分制后:基础分≈0 的噪声自然到不了 L2(55)推送线,基础分高的
    # 品还能升到 L3,评分重新参与取舍且没有悬崖。
    phase_bonus_accum: float = 45.0    # 建仓:11.3+45≈56(刚过 L2);5+45=50(不推)
    phase_bonus_launch: float = 45.0   # 启动:同建仓(实测表现最差,不额外加码)
    phase_bonus_distrib: float = 65.0  # 派发:6.4+65≈71(过 L3=70);0+65=65(不推)
    # 建仓推送尾注(空字符串 = 关闭):实测该指纹 T+24h 相对大盘略优、T+7d 转负,避免误导成"低位埋伏"
    phase_accum_note: str = ("短线提示:历史实测(17 天/n=34)本指纹 T+1 日相对大盘 +2.2pp、"
                             "T+7 日 -5.9pp,按短线看待,别当低位长线埋伏")

    # 异动推送目标群白名单(V0.2.0:暂时只对 1105775575 开放);空 = 沿用 vip_only 规则
    push_group_openids: List[str] = field(default_factory=list)

    # ---- 推送 ----
    vip_only: bool = True
    push_top_n: int = 5

    # ---- 限流(全局目标 ≤13 req/min) ----
    max_req_per_min: int = 13

    # ---- 成本门槛观察(V0.3.27)----
    # 与费用口径联动(卖出费取 fees 段),故解析放在 fees 之后。
    # 职责只有一个:给每条异动候选算出「折后边际 = 折扣 - 往返成本」并落库,
    # 供日后用真实结果验证「边际高的候选是否真的更赚钱」。mode=observe 不改推送。
    cost_gate: "CostGateConfig" = field(default_factory=lambda: CostGateConfig())


@dataclass
class FeeConfig:
    """卖出费率(V0.3.2):仅用于 `/分析` 展示「扣费后到手」,不参与任何评分与阈值。"""

    buff_sell_pct: float = 2.5      # BUFF 卖出手续费%
    yyyp_sell_pct: float = 2.0      # 悠悠有品卖出手续费%
    steam_sell_pct: float = 15.0    # Steam 社区市场总费率%(买家侧,卖家到手≈85%,且余额不可直接提现)


@dataclass
class AppConfig:
    csqaq: CSQAQConfig = field(default_factory=CSQAQConfig)
    steamdt: SteamDTConfig = field(default_factory=SteamDTConfig)
    qq: QQConfig = field(default_factory=QQConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    push: PushConfig = field(default_factory=PushConfig)
    recommend: RecommendConfig = field(default_factory=RecommendConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    vip: VipConfig = field(default_factory=VipConfig)
    free: FreeConfig = field(default_factory=FreeConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    notify_group_openid: str = ""   # 通知群:IP漂移提醒等主动推送只发这个群(留空则发所有已知群)
    admin_user_openids: List[str] = field(default_factory=list)      # 私聊发公告的管理员白名单
    announce_groups: Dict[str, str] = field(default_factory=dict)    # 公告目标群: 群号/别名 -> group_openid


def load_config(path: str = "config.yaml") -> AppConfig:
    """从 YAML 文件加载配置，缺省字段使用默认值。"""
    cfg = AppConfig()
    if not os.path.exists(path):
        return cfg

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if "csqaq" in raw:
        c = raw["csqaq"]
        cfg.csqaq.api_token = c.get("api_token", cfg.csqaq.api_token)
        cfg.csqaq.base_url = c.get("base_url", cfg.csqaq.base_url).rstrip("/")
        cfg.csqaq.request_interval = float(c.get("request_interval", cfg.csqaq.request_interval))
        cfg.csqaq.retry_times = int(c.get("retry_times", cfg.csqaq.retry_times))
        cfg.csqaq.rebind_cooldown_seconds = int(
            c.get("rebind_cooldown_seconds", cfg.csqaq.rebind_cooldown_seconds))
        # V0.3.19:钉目标服务器 IP(治 IP 漂移的根)
        cfg.csqaq.pin_auto = bool(c.get("pin_auto", cfg.csqaq.pin_auto))
        cfg.csqaq.pin_server_ip = str(c.get("pin_server_ip", cfg.csqaq.pin_server_ip) or "").strip()

    if "steamdt" in raw:
        c = raw["steamdt"]
        cfg.steamdt.api_key = c.get("api_key", cfg.steamdt.api_key)
        cfg.steamdt.base_url = str(c.get("base_url", cfg.steamdt.base_url)).rstrip("/")
        cfg.steamdt.request_interval = float(c.get("request_interval", cfg.steamdt.request_interval))
        cfg.steamdt.retry_times = int(c.get("retry_times", cfg.steamdt.retry_times))
        cfg.steamdt.cache_ttl_seconds = int(c.get("cache_ttl_seconds", cfg.steamdt.cache_ttl_seconds))

    if "qq" in raw:
        c = raw["qq"]
        cfg.qq.appid = str(c.get("appid", cfg.qq.appid))
        cfg.qq.secret = str(c.get("secret", cfg.qq.secret))
        if "keywords" in c:
            kw = c["keywords"]
            for k in ("recommend", "price", "rank", "help"):
                if k in kw:
                    cfg.qq.keywords[k] = list(kw[k])

    if "scheduler" in raw:
        c = raw["scheduler"]
        cfg.scheduler.fetch_interval_minutes = int(c.get("fetch_interval_minutes", cfg.scheduler.fetch_interval_minutes))
        cfg.scheduler.rank_page_size = int(c.get("rank_page_size", cfg.scheduler.rank_page_size))
        cfg.scheduler.rank_max_pages = int(c.get("rank_max_pages", cfg.scheduler.rank_max_pages))

    if "push" in raw:
        c = raw["push"]
        cfg.push.enable = bool(c.get("enable", cfg.push.enable))
        cfg.push.group_openids = list(c.get("group_openids", cfg.push.group_openids))
        # 每日播报定时点：优先读 times 列表，兼容旧 time 单时间点写法
        _times = c.get("times")
        if _times:
            cfg.push.times = [str(t) for t in _times]
        elif "time" in c:
            cfg.push.times = [str(c["time"])]

    if "recommend" in raw:
        c = raw["recommend"]
        cfg.recommend.min_price = float(c.get("min_price", cfg.recommend.min_price))
        cfg.recommend.max_price = float(c.get("max_price", cfg.recommend.max_price))
        cfg.recommend.min_liquidity = int(c.get("min_liquidity", cfg.recommend.min_liquidity))
        cfg.recommend.top_n = int(c.get("top_n", cfg.recommend.top_n))

    if "llm" in raw:
        c = raw["llm"]
        cfg.llm.enable = bool(c.get("enable", cfg.llm.enable))
        cfg.llm.base_url = str(c.get("base_url", cfg.llm.base_url)).rstrip("/")
        cfg.llm.api_key = str(c.get("api_key", cfg.llm.api_key))
        cfg.llm.model = str(c.get("model", cfg.llm.model))
        cfg.llm.timeout = float(c.get("timeout", cfg.llm.timeout))
        cfg.llm.max_tokens = int(c.get("max_tokens", cfg.llm.max_tokens))
        cfg.llm.temperature = float(c.get("temperature", cfg.llm.temperature))
        cfg.llm.history_len = int(c.get("history_len", cfg.llm.history_len))
        cfg.llm.history_ttl = int(c.get("history_ttl", cfg.llm.history_ttl))
        cfg.llm.route_all_nl = bool(c.get("route_all_nl", cfg.llm.route_all_nl))

    if "vip" in raw:
        c = raw["vip"]
        cfg.vip.group_openids = list(c.get("group_openids", cfg.vip.group_openids))
        cfg.vip.watch_enable = bool(c.get("watch_enable", cfg.vip.watch_enable))

    if "free" in raw:
        c = raw["free"]
        cfg.free.group_openids = list(c.get("group_openids", cfg.free.group_openids))
        cfg.free.watch_enable = bool(c.get("watch_enable", cfg.free.watch_enable))
        cfg.free.max_watches_per_user = int(c.get("max_watches_per_user", cfg.free.max_watches_per_user))

    if "anomaly" in raw:
        c = raw["anomaly"]
        cfg.anomaly.enable = bool(c.get("enable", cfg.anomaly.enable))
        cfg.anomaly.b_enable = bool(c.get("b_enable", cfg.anomaly.b_enable))
        cfg.anomaly.b_size = int(c.get("b_size", cfg.anomaly.b_size))
        cfg.anomaly.b_pages_per_sort = int(c.get("b_pages_per_sort", cfg.anomaly.b_pages_per_sort))
        cfg.anomaly.b_pages_pct_sort = int(c.get("b_pages_pct_sort", cfg.anomaly.b_pages_pct_sort))
        cfg.anomaly.b_early_stop_pages = int(
            c.get("b_early_stop_pages", cfg.anomaly.b_early_stop_pages))
        cfg.anomaly.b_interval_min = int(c.get("b_interval_min", cfg.anomaly.b_interval_min))
        cfg.anomaly.a_size = int(c.get("a_size", cfg.anomaly.a_size))
        cfg.anomaly.a_interval_min = int(c.get("a_interval_min", cfg.anomaly.a_interval_min))
        cfg.anomaly.a_pages = int(c.get("a_pages", cfg.anomaly.a_pages))
        cfg.anomaly.a_discover_interval_min = int(
            c.get("a_discover_interval_min", cfg.anomaly.a_discover_interval_min))
        cfg.anomaly.a_track_enable = bool(c.get("a_track_enable", cfg.anomaly.a_track_enable))
        cfg.anomaly.a_track_interval_sec = int(
            c.get("a_track_interval_sec", cfg.anomaly.a_track_interval_sec))
        cfg.anomaly.a_track_batch_size = int(
            c.get("a_track_batch_size", cfg.anomaly.a_track_batch_size))
        cfg.anomaly.a_track_hydrate_per_round = int(
            c.get("a_track_hydrate_per_round", cfg.anomaly.a_track_hydrate_per_round))
        cfg.anomaly.s_size = int(c.get("s_size", cfg.anomaly.s_size))
        cfg.anomaly.s_interval_sec = int(c.get("s_interval_sec", cfg.anomaly.s_interval_sec))
        cfg.anomaly.platform_sync_batch_size = int(
            c.get("platform_sync_batch_size", cfg.anomaly.platform_sync_batch_size))
        cfg.anomaly.summary_enable = bool(c.get("summary_enable", cfg.anomaly.summary_enable))
        cfg.anomaly.summary_interval_min = int(
            c.get("summary_interval_min", cfg.anomaly.summary_interval_min))
        cfg.anomaly.summary_pages_per_sort = int(
            c.get("summary_pages_per_sort", cfg.anomaly.summary_pages_per_sort))
        cfg.anomaly.summary_min_sell_num = int(
            c.get("summary_min_sell_num", cfg.anomaly.summary_min_sell_num))
        cfg.anomaly.phase_window_minutes = int(
            c.get("phase_window_minutes", cfg.anomaly.phase_window_minutes))
        cfg.anomaly.min_price = float(c.get("min_price", cfg.anomaly.min_price))
        cfg.anomaly.min_listing = int(c.get("min_listing", cfg.anomaly.min_listing))
        cfg.anomaly.min_buy_num = int(c.get("min_buy_num", cfg.anomaly.min_buy_num))
        cfg.anomaly.min_anomaly_buy_num = int(
            c.get("min_anomaly_buy_num", cfg.anomaly.min_anomaly_buy_num))
        cfg.anomaly.anomaly_buy_grace_minutes = int(
            c.get("anomaly_buy_grace_minutes", cfg.anomaly.anomaly_buy_grace_minutes))
        cfg.anomaly.min_depth = float(c.get("min_depth", cfg.anomaly.min_depth))
        cfg.anomaly.min_sell_num = int(c.get("min_sell_num", cfg.anomaly.min_sell_num))
        cfg.anomaly.max_sell_buy_ratio = float(c.get("max_sell_buy_ratio", cfg.anomaly.max_sell_buy_ratio))
        cfg.anomaly.thin_noise_price_pct = float(c.get("thin_noise_price_pct", cfg.anomaly.thin_noise_price_pct))
        cfg.anomaly.price_abs_min = float(c.get("price_abs_min", cfg.anomaly.price_abs_min))
        cfg.anomaly.spread_pct_full = float(c.get("spread_pct_full", cfg.anomaly.spread_pct_full))
        cfg.anomaly.spread_pp_full = float(c.get("spread_pp_full", cfg.anomaly.spread_pp_full))
        cfg.anomaly.trend_baseline_max_7d = float(
            c.get("trend_baseline_max_7d", cfg.anomaly.trend_baseline_max_7d))
        cfg.anomaly.trend_baseline_max_30d = float(
            c.get("trend_baseline_max_30d", cfg.anomaly.trend_baseline_max_30d))
        cfg.anomaly.high_price_tier = float(c.get("high_price_tier", cfg.anomaly.high_price_tier))
        cfg.anomaly.high_price_noise_pct = float(c.get("high_price_noise_pct", cfg.anomaly.high_price_noise_pct))
        cfg.anomaly.high_price_num_sig = float(c.get("high_price_num_sig", cfg.anomaly.high_price_num_sig))
        cfg.anomaly.high_price_min_buy = int(c.get("high_price_min_buy", cfg.anomaly.high_price_min_buy))
        cfg.anomaly.min_platform_sell = int(c.get("min_platform_sell", cfg.anomaly.min_platform_sell))
        cfg.anomaly.min_scan_sell_num = int(c.get("min_scan_sell_num", cfg.anomaly.min_scan_sell_num))
        cfg.anomaly.platform_sell_low = int(c.get("platform_sell_low", cfg.anomaly.platform_sell_low))
        cfg.anomaly.platform_sell_high = int(c.get("platform_sell_high", cfg.anomaly.platform_sell_high))
        cfg.anomaly.platform_sell_tier = float(c.get("platform_sell_tier", cfg.anomaly.platform_sell_tier))
        cfg.anomaly.implausible_30m = float(c.get("implausible_30m", cfg.anomaly.implausible_30m))
        cfg.anomaly.implausible_1h = float(c.get("implausible_1h", cfg.anomaly.implausible_1h))
        cfg.anomaly.quality_reject_confirm_rounds = int(
            c.get("quality_reject_confirm_rounds", cfg.anomaly.quality_reject_confirm_rounds))
        for k in ("w_price_5m", "w_price_30m", "w_price_1h", "w_buy_change", "w_sell_change",
                  "w_order_ratio", "w_spread", "w_trend", "w_position",
                  "level_watch", "level_warning", "level_strong", "level_extreme",
                  "b_to_a", "a_to_s", "s_to_a", "a_to_b", "cooldown_upgrade_delta"):
            if k in c:
                setattr(cfg.anomaly, k, float(c[k]))
        for k in ("s_to_a_rounds", "a_to_b_rounds", "s_max_minutes",
                  "b_confirm_rounds", "a_stale_minutes",
                  "book_base_low", "book_base_mid", "book_abs_low", "book_abs_mid",
                  "book_abs_high", "book_confirm_rounds",
                  "cooldown_l2_min", "cooldown_l3_min", "cooldown_l4_min",
                  "max_push_per_item_per_day",
                  "push_top_n", "max_req_per_min"):
            if k in c:
                setattr(cfg.anomaly, k, int(c[k]))
        if "book_pct_min" in c:
            cfg.anomaly.book_pct_min = float(c["book_pct_min"])
        cfg.anomaly.vip_only = bool(c.get("vip_only", cfg.anomaly.vip_only))
        for k in ("accum_buy_pct_min", "accum_abs_mult", "accum_price_quiet", "accum_sell_max",
                  "accum_price_quiet_abs", "launch_buy_price_pct", "launch_price_min",
                  "launch_price_abs_min", "launch_sell_max", "launch_rate7_max",
                  "distrib_sell_pct", "distrib_range_pct",
                  "distrib_pos_high", "distrib_buy_stall", "fake_wall_price_dn",
                  "fake_wall_sell_up", "digest_sell_pct", "digest_buy_pct",
                  "phase_score_accum", "phase_score_launch", "phase_score_distrib",
                  "phase_bonus_accum", "phase_bonus_launch", "phase_bonus_distrib"):
            if k in c:
                setattr(cfg.anomaly, k, float(c[k]))
        for k in ("accum_abs_min_units", "distrib_range_days"):
            if k in c:
                setattr(cfg.anomaly, k, int(c[k]))
        if "phase_accum_note" in c:
            cfg.anomaly.phase_accum_note = str(c.get("phase_accum_note") or "")
        if "phase_enable" in c:
            cfg.anomaly.phase_enable = bool(c["phase_enable"])
        if "push_group_openids" in c:
            cfg.anomaly.push_group_openids = [str(g) for g in (c.get("push_group_openids") or [])]

    if "fees" in raw:
        c = raw["fees"] or {}
        for k in ("buff_sell_pct", "yyyp_sell_pct", "steam_sell_pct"):
            if k in c:
                setattr(cfg.fees, k, float(c[k]))

    # V0.3.27 成本门槛观察:卖出费口径继承 fees 段(买入无手续费,只算卖出费 + 提现)。
    # 必须放在 fees 之后解析,否则拿到的是默认费率。
    if "cost_gate" in raw:
        cfg.anomaly.cost_gate = CostGateConfig.from_mapping(
            raw.get("cost_gate") or {},
            buff_fee=cfg.fees.buff_sell_pct,
            yyyp_fee=cfg.fees.yyyp_sell_pct)

    if "admin" in raw:
        c = raw["admin"]
        cfg.admin_user_openids = list(c.get("user_openids", cfg.admin_user_openids))

    if "announce" in raw:
        c = raw["announce"]
        groups = c.get("groups") or {}
        cfg.announce_groups = {str(k): str(v) for k, v in groups.items()}

    cfg.notify_group_openid = str(raw.get("notify_group_openid", cfg.notify_group_openid))

    return cfg


def validate_config(cfg: AppConfig) -> List[str]:
    """校验配置完整性，返回缺失项提示列表。"""
    errors = []
    if not cfg.csqaq.api_token or cfg.csqaq.api_token.startswith("在这里填入"):
        errors.append("config.yaml 缺少 CSQAQ api_token（到 https://csqaq.com/home 注册获取）")
    if not cfg.qq.appid or cfg.qq.appid.startswith("在这里填入"):
        errors.append("config.yaml 缺少 QQ 机器人 appid（到 https://q.qq.com 创建机器人）")
    if not cfg.qq.secret or cfg.qq.secret.startswith("在这里填入"):
        errors.append("config.yaml 缺少 QQ 机器人 secret")
    return errors
