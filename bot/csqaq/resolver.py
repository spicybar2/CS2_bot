# -*- coding: utf-8 -*-
"""饰品价格解析器:按中文关键词解析出英文名并实时查价。

链路(逐级兜底):
1. 中英词典生成候选(zh_to_en_candidates) -> getPriceByMarketHashName
2. 全量索引 goods_index 按中文名模糊匹配 -> 详情接口拿 market_hash_name -> 查价
这样 CSQAQ 收录的任何饰品都能查到,不再依赖词典覆盖。
"""
import logging
import re
from typing import Any, Dict, List, Optional

from bot.csqaq.skins import zh_to_en_candidates

logger = logging.getLogger("resolver")


async def resolve_prices(api, store, keyword: str) -> Dict[str, Dict[str, Any]]:
    """返回 {market_hash_name: 行情行}。查不到返回空 dict。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return {}

    # 1) 词典候选
    cands = zh_to_en_candidates(keyword)
    if cands:
        try:
            result = await api.get_price_by_market_hash_name(cands[:50])
            if result:
                return result
        except Exception as exc:
            logger.warning("[resolver] 词典候选查价失败 %s: %s", keyword, exc)

    # 2) 全量索引兜底:按中文名匹配,逐个拿英文名查价。
    #    兼容用户带磨损后缀/StatTrak/全角括号的写法(如「M4A4 | 狮鹫（崭新出厂）」):
    #    整体 LIKE 会因管道符/括号差异落空,改为分词 AND 匹配 goods_index。
    toks = [t for t in re.split(r"[\s|（）()【】\[\],，。、]+", keyword) if len(t) > 1]
    if not toks:
        toks = [keyword]
    try:
        rows = await store.async_query_goods_by_keyword(toks[0], limit=500)
        if len(toks) > 1:
            rows = [r for r in rows if all(t in r["name"] for t in toks[1:])]
        rows = rows[:10]
    except Exception as exc:
        logger.warning("[resolver] 全量索引查询失败 %s: %s", keyword, exc)
        return {}
    for r in rows:
        try:
            gi = await api.get_good_info(r["good_id"])
        except Exception as exc:
            logger.warning("[resolver] 详情失败 id=%s: %s", r["good_id"], exc)
            continue
        mhn = gi.get("market_hash_name")
        if not mhn:
            continue
        try:
            result = await api.get_price_by_market_hash_name([mhn])
        except Exception as exc:
            logger.warning("[resolver] 索引查价失败 %s: %s", mhn, exc)
            continue
        if result:
            logger.info("[resolver] 全量索引兜底命中: %s -> %s", keyword, mhn)
            return result
    return {}
