from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "大模型开仓决策流程图.png"
WIDTH, HEIGHT = 2200, 3600

BG = "#F4F7FB"
INK = "#17324D"
MUTED = "#5E7184"
LINE = "#52708D"
BLUE = "#2563EB"
BLUE_BG = "#EAF2FF"
GREEN = "#078A68"
GREEN_BG = "#E8F7F1"
AMBER = "#C56A08"
AMBER_BG = "#FFF4DF"
RED = "#C9363E"
RED_BG = "#FDECEE"
PURPLE = "#7557C7"
PURPLE_BG = "#F1ECFF"
WHITE = "#FFFFFF"


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size)


REGULAR_PATH = r"C:\Windows\Fonts\msyh.ttc"
BOLD_PATH = r"C:\Windows\Fonts\msyhbd.ttc"
TITLE_FONT = load_font(BOLD_PATH, 62)
SUBTITLE_FONT = load_font(REGULAR_PATH, 27)
SECTION_FONT = load_font(BOLD_PATH, 34)
NODE_FONT = load_font(REGULAR_PATH, 25)
NODE_BOLD_FONT = load_font(BOLD_PATH, 25)
LABEL_FONT = load_font(BOLD_PATH, 20)
FOOTER_FONT = load_font(REGULAR_PATH, 23)

image = Image.new("RGB", (WIDTH, HEIGHT), BG)
draw = ImageDraw.Draw(image)


def centered_text(
    bounds: tuple[int, int, int, int],
    value: str,
    text_font: ImageFont.FreeTypeFont = NODE_FONT,
    fill: str = INK,
    spacing: int = 7,
) -> None:
    left, top, right, bottom = bounds
    box = draw.multiline_textbbox(
        (0, 0), value, font=text_font, spacing=spacing, align="center"
    )
    width = box[2] - box[0]
    height = box[3] - box[1]
    x = left + (right - left - width) / 2
    y = top + (bottom - top - height) / 2 - box[1]
    draw.multiline_text(
        (x, y),
        value,
        font=text_font,
        fill=fill,
        spacing=spacing,
        align="center",
    )


def panel(y1: int, y2: int, number: str, title: str, color: str) -> None:
    draw.rounded_rectangle(
        (55, y1, WIDTH - 55, y2),
        radius=30,
        fill=WHITE,
        outline="#DCE5EF",
        width=3,
    )
    draw.rounded_rectangle((88, y1 + 24, 150, y1 + 86), radius=16, fill=color)
    centered_text((88, y1 + 24, 150, y1 + 86), number, SECTION_FONT, WHITE)
    draw.text((175, y1 + 32), title, font=SECTION_FONT, fill=INK)


def box(
    bounds: tuple[int, int, int, int],
    value: str,
    *,
    fill: str = BLUE_BG,
    outline: str = BLUE,
    bold: bool = False,
    text_fill: str = INK,
) -> None:
    draw.rounded_rectangle(bounds, radius=22, fill=fill, outline=outline, width=4)
    centered_text(
        bounds,
        value,
        NODE_BOLD_FONT if bold else NODE_FONT,
        text_fill,
    )


def diamond(
    center: tuple[int, int],
    radii: tuple[int, int],
    value: str,
    *,
    fill: str = AMBER_BG,
    outline: str = AMBER,
) -> None:
    cx, cy = center
    rx, ry = radii
    points = [(cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy)]
    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=outline, width=4, joint="curve")
    centered_text((cx - rx + 25, cy - ry + 16, cx + rx - 25, cy + ry - 16), value)


def arrow(
    points: list[tuple[int, int]],
    *,
    color: str = LINE,
    label: str | None = None,
    label_at: tuple[int, int] | None = None,
    width: int = 5,
) -> None:
    draw.line(points, fill=color, width=width, joint="curve")
    (x1, y1), (x2, y2) = points[-2], points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 17
    wing = 0.58
    p1 = (x2 - size * math.cos(angle - wing), y2 - size * math.sin(angle - wing))
    p2 = (x2 - size * math.cos(angle + wing), y2 - size * math.sin(angle + wing))
    draw.polygon([(x2, y2), p1, p2], fill=color)
    if label and label_at:
        bbox = draw.textbbox((0, 0), label, font=LABEL_FONT)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        lx, ly = label_at
        draw.rounded_rectangle(
            (lx - lw / 2 - 9, ly - lh / 2 - 6, lx + lw / 2 + 9, ly + lh / 2 + 6),
            radius=8,
            fill=WHITE,
        )
        draw.text(
            (lx - lw / 2, ly - lh / 2 - bbox[1]),
            label,
            font=LABEL_FONT,
            fill=color,
        )


# Header
draw.text((75, 55), "AutoQuant 大模型开仓决策流程图", font=TITLE_FONT, fill=INK)
draw.text(
    (79, 135),
    "ChatGPT / DeepSeek / DUAL · 今日方向决策 + 五分钟候选入场时机审核",
    font=SUBTITLE_FONT,
    fill=MUTED,
)


# 1. Configuration
panel(210, 680, "1", "启用开关、模式与凭据校验", BLUE)
box((95, 360, 350, 475), "启动运行器", bold=True)
diamond((555, 418), (145, 100), "启用大模型？")
box((805, 275, 1125, 390), "关闭：使用手动方向", fill="#F3F6F9", outline="#8CA0B3")
diamond((980, 520), (150, 105), "模型模式")
box((1265, 285, 1635, 405), "CHATGPT\n校验 OpenAI Key")
box((1265, 455, 1635, 575), "DEEPSEEK\n校验 DeepSeek Key")
box((1750, 360, 2085, 490), "DUAL\n校验两个 Key", fill=PURPLE_BG, outline=PURPLE)
box((1695, 555, 2100, 640), "缺少凭据 → 拒绝启动", fill=RED_BG, outline=RED, text_fill=RED)
arrow([(350, 418), (410, 418)])
arrow([(700, 418), (805, 335)], label="否", label_at=(750, 355))
arrow([(555, 518), (555, 520), (830, 520)], label="是", label_at=(720, 490))
arrow([(1130, 520), (1200, 520), (1200, 345), (1265, 345)], label="GPT", label_at=(1198, 395))
arrow([(1130, 520), (1265, 515)], label="DeepSeek", label_at=(1198, 555))
arrow([(980, 625), (980, 650), (1915, 650), (1915, 490)], color=PURPLE, label="DUAL", label_at=(1510, 620))


# 2. Daily direction
panel(730, 1480, "2", "每个交易日的大模型方向决策", GREEN)
box((90, 905, 340, 1025), "收到当日日线", fill=GREEN_BG, outline=GREEN, bold=True)
box((430, 870, 830, 1060), "采集市场上下文\n近期新闻、SPY/QQQ\n标的走势、当日日线")
diamond((1045, 965), (145, 105), "数据完整且\n有近期新闻？")
diamond((1385, 965), (145, 105), "单模型 / DUAL")
box((1640, 800, 2075, 930), "单模型：结构化输出\n方向、置信度、依据、风险")
box((1640, 1040, 2075, 1170), "DUAL：并行请求\nChatGPT + DeepSeek", fill=PURPLE_BG, outline=PURPLE)
diamond((1385, 1275), (165, 115), "响应有效、达到阈值\n且 DUAL 方向一致？")
box((805, 1210, 1135, 1340), "否：FLAT\n当日禁止新开仓", fill=RED_BG, outline=RED, bold=True, text_fill=RED)
box((1640, 1210, 2075, 1340), "是：写入 LONG / SHORT\n或模型主动给出的 FLAT", fill=GREEN_BG, outline=GREEN, bold=True)
arrow([(340, 965), (430, 965)])
arrow([(830, 965), (900, 965)])
arrow([(1190, 965), (1240, 965)], label="是", label_at=(1215, 930))
arrow([(1045, 1070), (1045, 1210)], color=RED, label="否", label_at=(1090, 1130))
arrow([(1530, 965), (1580, 965), (1580, 865), (1640, 865)], label="单模型", label_at=(1585, 925))
arrow([(1385, 1070), (1385, 1105), (1640, 1105)], color=PURPLE, label="DUAL", label_at=(1510, 1075))
arrow([(1855, 930), (1855, 1210), (1550, 1275)])
arrow([(1640, 1105), (1550, 1235)])
arrow([(1220, 1275), (1135, 1275)], color=RED, label="否", label_at=(1175, 1240))
arrow([(1550, 1275), (1640, 1275)], color=GREEN, label="是", label_at=(1595, 1240))


# 3. Candidate signal
panel(1530, 2110, "3", "五分钟策略与风险退出优先级", PURPLE)
box((90, 1715, 390, 1845), "MA 预热完成\n接收 5 分钟行情", fill=PURPLE_BG, outline=PURPLE)
diamond((625, 1780), (155, 105), "触发止损\n或止盈？")
box((875, 1600, 1190, 1725), "风险退出信号\n绕过大模型审核", fill=PURPLE_BG, outline=PURPLE, bold=True)
diamond((1015, 1905), (165, 110), "收盘 K 线满足\nMA 突破条件？")
box((1315, 1835, 1650, 1975), "生成 BUY / SELL\n候选开仓信号", fill=GREEN_BG, outline=GREEN, bold=True)
box((1770, 1835, 2075, 1975), "等待下一根\n5 分钟 K 线", fill="#F3F6F9", outline="#8CA0B3")
arrow([(390, 1780), (470, 1780)])
arrow([(780, 1780), (830, 1780), (830, 1662), (875, 1662)], color=PURPLE, label="是", label_at=(830, 1740))
arrow([(625, 1885), (625, 1905), (850, 1905)], label="否", label_at=(750, 1870))
arrow([(1180, 1905), (1315, 1905)], color=GREEN, label="是", label_at=(1245, 1870))
arrow([(1015, 2015), (1015, 2050), (1920, 2050), (1920, 1975)], label="否", label_at=(1135, 2020))


# 4. Entry timing gate
panel(2160, 2995, "4", "候选开仓时机审核", AMBER)
diamond((290, 2390), (185, 125), "基础检查通过？\n未决订单、持仓、次数\n方向、状态、时效")
diamond((700, 2390), (150, 105), "大模型启用？")
box((980, 2240, 1405, 2390), "组装时机上下文\n候选原因、当前 K 线\n最近 12 根 K 线 + 日级上下文")
diamond((1675, 2315), (150, 105), "单模型 / DUAL")
box((1900, 2180, 2100, 2310), "请求\nENTER / WAIT")
box((1900, 2380, 2100, 2510), "并行请求\n两个模型", fill=PURPLE_BG, outline=PURPLE)
diamond((1660, 2675), (185, 125), "单模型 ENTER 且达阈值；\n或 DUAL 两者均 ENTER\n且均达阈值？")
box((1080, 2610, 1395, 2740), "WAIT / 异常\n放弃本次信号", fill=RED_BG, outline=RED, bold=True, text_fill=RED)
diamond((1975, 2720), (145, 105), "审核后信号\n仍有效？")
box((1645, 2860, 2075, 2950), "进入最终风控与订单执行", fill=GREEN_BG, outline=GREEN, bold=True)
arrow([(475, 2390), (550, 2390)], label="是", label_at=(510, 2355))
arrow([(290, 2515), (290, 2710), (1080, 2710)], color=RED, label="否", label_at=(340, 2560))
arrow([(850, 2390), (980, 2315)], label="是", label_at=(905, 2315))
arrow([(700, 2495), (700, 2905), (1645, 2905)], color=GREEN, label="否", label_at=(745, 2550))
arrow([(1405, 2315), (1525, 2315)])
arrow([(1825, 2315), (1900, 2245)], label="单模型", label_at=(1850, 2220))
arrow([(1675, 2420), (1675, 2445), (1900, 2445)], color=PURPLE, label="DUAL", label_at=(1800, 2410))
arrow([(2000, 2310), (2000, 2570), (1815, 2635)])
arrow([(1900, 2445), (1830, 2605)])
arrow([(1475, 2675), (1395, 2675)], color=RED, label="否", label_at=(1435, 2640))
arrow([(1845, 2675), (1880, 2675)], color=GREEN, label="是", label_at=(1860, 2640))
arrow([(1975, 2825), (1975, 2860)], color=GREEN, label="是", label_at=(2020, 2845))
arrow([(1830, 2720), (1395, 2720)], color=RED, label="否", label_at=(1580, 2685))


# 5. Risk and order
panel(3045, 3485, "5", "安全落单与持续运行", RED)
box((90, 3200, 390, 3330), "风险退出 /\n获准开仓信号", fill=PURPLE_BG, outline=PURPLE, bold=True)
box((500, 3180, 855, 3350), "金额与数量风控\nSQLite 原子预留\n发送前停止复查")
diamond((1110, 3265), (150, 105), "全部检查\n通过？")
box((1360, 3150, 1680, 3280), "提交 MARKET 订单", fill=GREEN_BG, outline=GREEN, bold=True)
box((1775, 3150, 2100, 3280), "更新订单、持仓、均价\n次数与日志", fill=GREEN_BG, outline=GREEN)
box((1360, 3340, 1680, 3445), "阻止下单并记录原因", fill=RED_BG, outline=RED, text_fill=RED)
arrow([(390, 3265), (500, 3265)])
arrow([(855, 3265), (960, 3265)])
arrow([(1260, 3265), (1360, 3215)], color=GREEN, label="是", label_at=(1310, 3190))
arrow([(1110, 3370), (1110, 3392), (1360, 3392)], color=RED, label="否", label_at=(1220, 3360))
arrow([(1680, 3215), (1775, 3215)], color=GREEN)


# Cross-section connectors and footer
arrow([(965, 680), (965, 730)], color=GREEN)
arrow([(1855, 1340), (1855, 1480), (240, 1480), (240, 1715)], color=GREEN)
arrow([(1478, 1975), (1478, 2110), (290, 2110), (290, 2265)], color=GREEN)
arrow([(1032, 1725), (1032, 3045), (240, 3045), (240, 3200)], color=PURPLE)
arrow([(1860, 2950), (1860, 3045), (240, 3045), (240, 3200)], color=GREEN)

draw.rounded_rectangle((75, 3515, WIDTH - 75, 3575), radius=18, fill="#E9EEF4")
centered_text(
    (95, 3518, WIDTH - 95, 3572),
    "失败关闭原则：数据缺失、调用异常、低置信度或双模型分歧 → FLAT / WAIT；风险退出不受模型阻塞。",
    FOOTER_FONT,
    INK,
)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
image.save(OUTPUT, format="PNG", optimize=True, dpi=(144, 144))
print(OUTPUT)
