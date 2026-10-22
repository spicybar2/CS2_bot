# -*- coding: utf-8 -*-
"""价格走势 PNG 生成(纯本地绘制,无外部图表库依赖,使用 Pillow)。

设计要点:
- 用 Pillow 直接绘制折线图:坐标轴/网格/数据点/标题,比文本 sparkline 直观
- 标题为饰品名(走 CJK 字体,优先 Windows 微软雅黑);无 CJK 字体时回退默认字体
- 折线颜色按涨跌着色:末值>=首值用红(涨),否则用绿(跌),契合 A 股习惯
- 返回生成的 PNG 文件路径;任何异常都返回 None(调用方回退文本 sparkline)
"""
import logging
import os
import tempfile
import time
from typing import List, Optional, Sequence, Tuple, Union

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("chart")

# 候选 CJK 字体(Windows 平台),用于中文标题/标签
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\SourceHanSansSC-Regular.otf",
]


def _cjk_font(size: int) -> ImageFont.ImageFont:
    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=0)
            except Exception:
                continue
    return ImageFont.load_default()


def make_trend_png(
    prices: List[float],
    name: str,
    current: float,
    labels: Optional[Sequence[Union[str, Tuple[int, str]]]] = None,
    synthesized: bool = False,
    out_path: Optional[str] = None,
) -> Optional[str]:
    """把价格序列绘制成走势 PNG,返回文件路径;数据不足或异常返回 None。

    Args:
        prices: 价格序列(升序,>=2 点)
        name: 饰品名(作为标题)
        current: 当前价(用于末点标注)
        labels: x 轴标签(如 ["90天前","30天前","7天前","现在"]);为空时按记录序号标注
        synthesized: 是否为涨跌幅反推的估算形态(标题加"(估算)")
        out_path: 指定输出路径,否则写入系统临时目录
    """
    if len(prices) < 2:
        return None
    try:
        W, H = 920, 480
        pad_l, pad_r, pad_t, pad_b = 78, 34, 74, 52
        img = Image.new("RGB", (W, H), (255, 255, 255))
        draw = ImageDraw.Draw(img)

        font_title = _cjk_font(22)
        font_axis = _cjk_font(16)
        font_small = _cjk_font(14)

        x0, y0 = pad_l, pad_t
        x1, y1 = W - pad_r, H - pad_b
        pw, ph = x1 - x0, y1 - y0

        lo, hi = min(prices), max(prices)
        if hi == lo:
            hi = lo + 1.0
            lo = max(0.0, lo - 1.0)
        span = hi - lo
        lo_m = max(0.0, lo - span * 0.12)
        hi_m = hi + span * 0.12
        span_m = hi_m - lo_m

        n = len(prices)

        def px(i: int) -> float:
            return x0 + (pw * i / (n - 1)) if n > 1 else x0 + pw / 2

        def py(v: float) -> float:
            return y0 + ph * (1 - (v - lo_m) / span_m)

        # 涨/跌配色(红涨绿跌)
        up = prices[-1] >= prices[0]
        line_color = (214, 48, 49) if up else (46, 139, 87)
        grid_color = (226, 226, 226)
        axis_color = (120, 120, 120)
        text_color = (60, 60, 60)

        # 横向网格 + y 轴价签(¥)
        for t in range(6):
            v = lo_m + span_m * t / 5
            yy = py(v)
            draw.line([(x0, yy), (x1, yy)], fill=grid_color, width=1)
            draw.text((x0 - 10, yy), f"¥{v:,.0f}",
                      font=font_axis, fill=axis_color, anchor="rm")

        # x 轴标签
        if labels:
            for i, lab in enumerate(labels):
                if isinstance(lab, tuple) and len(lab) == 2:
                    idx, txt = int(lab[0]), str(lab[1])
                else:
                    idx, txt = i, str(lab)
                idx = max(0, min(n - 1, idx))
                draw.text((px(idx), y1 + 12), txt,
                          font=font_small, fill=axis_color, anchor="mt")
        else:
            idxs = [0, n // 2, n - 1] if n > 2 else list(range(n))
            for i in idxs:
                draw.text((px(i), y1 + 12), str(i + 1),
                          font=font_small, fill=axis_color, anchor="mt")

        # 折线 + 数据点
        pts = [(px(i), py(v)) for i, v in enumerate(prices)]
        draw.line(pts, fill=line_color, width=3)
        for xx, yy in pts:
            draw.ellipse([xx - 4, yy - 4, xx + 4, yy + 4], fill=line_color)

        # 末点(当前价)标注
        last_x, last_y = pts[-1]
        tag = f"¥{current:,.2f}"
        draw.text((last_x, last_y - 16), tag,
                  font=font_small, fill=line_color, anchor="mm")

        # 标题
        title = (name or "饰品走势") + ("（估算）" if synthesized else "")
        draw.text((W / 2, 28), title, font=font_title, fill=text_color, anchor="mm")

        # 副标题:记录数 / 涨跌幅
        if n > 1:
            chg = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else 0
            sub = f"共 {n} 个数据点 · 区间{chg:+.1f}%"
            draw.text((W / 2, 52), sub, font=font_small, fill=axis_color, anchor="mm")

        if not out_path:
            out_path = os.path.join(
                tempfile.gettempdir(),
                f"csqaq_trend_{int(time.time() * 1000)}.png")
        img.save(out_path, "PNG")
        return out_path
    except Exception as exc:
        logger.warning("[chart] 生成走势图失败: %s", exc)
        return None
