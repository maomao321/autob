from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "量化策略流程图.png"
WIDTH, HEIGHT = 2400, 4300

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
PANEL = "#FFFFFF"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size)


REGULAR_PATH = r"C:\Windows\Fonts\msyh.ttc"
BOLD_PATH = r"C:\Windows\Fonts\msyhbd.ttc"
TITLE_FONT = font(BOLD_PATH, 68)
SUBTITLE_FONT = font(REGULAR_PATH, 28)
SECTION_FONT = font(BOLD_PATH, 38)
NODE_FONT = font(REGULAR_PATH, 28)
NODE_BOLD_FONT = font(BOLD_PATH, 28)
SMALL_FONT = font(REGULAR_PATH, 23)
LABEL_FONT = font(BOLD_PATH, 22)
FOOTER_FONT = font(REGULAR_PATH, 25)


image = Image.new("RGB", (WIDTH, HEIGHT), BG)
draw = ImageDraw.Draw(image)


def centered_text(
    bounds: tuple[int, int, int, int],
    text: str,
    text_font: ImageFont.FreeTypeFont = NODE_FONT,
    fill: str = INK,
    spacing: int = 8,
) -> None:
    left, top, right, bottom = bounds
    text_bounds = draw.multiline_textbbox(
        (0, 0), text, font=text_font, spacing=spacing, align="center"
    )
    text_width = text_bounds[2] - text_bounds[0]
    text_height = text_bounds[3] - text_bounds[1]
    x = left + (right - left - text_width) / 2
    y = top + (bottom - top - text_height) / 2 - text_bounds[1]
    draw.multiline_text(
        (x, y),
        text,
        font=text_font,
        fill=fill,
        spacing=spacing,
        align="center",
    )


def panel(y1: int, y2: int, number: str, title: str, color: str) -> None:
    draw.rounded_rectangle(
        (70, y1, WIDTH - 70, y2),
        radius=34,
        fill=PANEL,
        outline="#DCE5EF",
        width=3,
    )
    draw.rounded_rectangle(
        (110, y1 + 28, 178, y1 + 96), radius=18, fill=color
    )
    centered_text(
        (110, y1 + 28, 178, y1 + 96),
        number,
        SECTION_FONT,
        WHITE,
    )
    draw.text(
        (205, y1 + 40), title, font=SECTION_FONT, fill=INK
    )


def box(
    bounds: tuple[int, int, int, int],
    text: str,
    *,
    fill: str = BLUE_BG,
    outline: str = BLUE,
    bold: bool = False,
    text_fill: str = INK,
) -> None:
    draw.rounded_rectangle(
        bounds, radius=24, fill=fill, outline=outline, width=4
    )
    centered_text(
        bounds,
        text,
        NODE_BOLD_FONT if bold else NODE_FONT,
        text_fill,
    )


def diamond(
    center: tuple[int, int],
    radii: tuple[int, int],
    text: str,
    *,
    fill: str = AMBER_BG,
    outline: str = AMBER,
) -> None:
    cx, cy = center
    rx, ry = radii
    points = [(cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy)]
    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=outline, width=4, joint="curve")
    centered_text((cx - rx + 28, cy - ry + 18, cx + rx - 28, cy + ry - 18), text)


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
    size = 18
    wing = 0.58
    p1 = (
        x2 - size * math.cos(angle - wing),
        y2 - size * math.sin(angle - wing),
    )
    p2 = (
        x2 - size * math.cos(angle + wing),
        y2 - size * math.sin(angle + wing),
    )
    draw.polygon([(x2, y2), p1, p2], fill=color)
    if label and label_at:
        bbox = draw.textbbox((0, 0), label, font=LABEL_FONT)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        lx, ly = label_at
        draw.rounded_rectangle(
            (lx - lw / 2 - 10, ly - lh / 2 - 7, lx + lw / 2 + 10, ly + lh / 2 + 7),
            radius=9,
            fill=WHITE,
        )
        draw.text(
            (lx - lw / 2, ly - lh / 2 - bbox[1]),
            label,
            font=LABEL_FONT,
            fill=color,
        )


# Header
draw.text((90, 66), "AutoQuant 量化策略流程图", font=TITLE_FONT, fill=INK)
draw.text(
    (94, 152),
    "日线方向过滤 + 5 分钟 MA 突破 + 订单生命周期与资金安全控制",
    font=SUBTITLE_FONT,
    fill=MUTED,
)
draw.rounded_rectangle((1955, 74, 2305, 142), radius=22, fill=PURPLE_BG)
centered_text((1955, 74, 2305, 142), "当前实现 · v0.3.0", SMALL_FONT, PURPLE)


# 1. Startup
panel(230, 720, "1", "启动与安全恢复", BLUE)
box((125, 385, 385, 515), "启动单只股票\n运行器", bold=True)
box((455, 385, 735, 515), "恢复本地\n订单账本")
box((805, 370, 1190, 530), "中断的 SUBMITTING\n转为 UNKNOWN\n校验股票并同步订单")
diamond((1455, 450), (175, 110), "存在 UNKNOWN\n实盘订单？")
box((1720, 385, 2245, 515), "连接日线 + 5 分钟\nWebSocket 行情", fill=GREEN_BG, outline=GREEN, bold=True)
box((1320, 580, 1590, 680), "安全锁定并停止", fill=RED_BG, outline=RED, bold=True, text_fill=RED)
arrow([(385, 450), (455, 450)])
arrow([(735, 450), (805, 450)])
arrow([(1190, 450), (1280, 450)])
arrow([(1630, 450), (1720, 450)], color=GREEN, label="否", label_at=(1675, 420))
arrow([(1455, 560), (1455, 580)], color=RED, label="是", label_at=(1500, 568))


# 2. Market stream
panel(770, 1470, "2", "行情循环与风险优先级", GREEN)
box((120, 1035, 390, 1165), "接收行情更新", fill=GREEN_BG, outline=GREEN, bold=True)
diamond((590, 1100), (145, 105), "行情周期")

box((800, 870, 1110, 990), "更新当日日线")
box((1210, 850, 1645, 1010), "收 > 开：LONG\n收 < 开：SHORT\n相等：FLAT")
box((1750, 870, 2240, 990), "新交易日清空\n5 分钟预热数据")

box((800, 1170, 1085, 1290), "5 分钟行情更新", fill=GREEN_BG, outline=GREEN)
diamond((1325, 1230), (165, 110), "触发止损\n或止盈？")
box((1585, 1105, 1905, 1225), "生成风险退出\nSELL 信号", fill=PURPLE_BG, outline=PURPLE, bold=True)
diamond((2070, 1300), (150, 105), "K 线已经\n收盘？")

arrow([(390, 1100), (445, 1100)])
arrow([(590, 995), (590, 930), (800, 930)], label="日线", label_at=(690, 900))
arrow([(1110, 930), (1210, 930)])
arrow([(1645, 930), (1750, 930)])
arrow([(590, 1205), (590, 1230), (800, 1230)], label="5 分钟", label_at=(690, 1260))
arrow([(1085, 1230), (1160, 1230)])
arrow([(1490, 1230), (1535, 1230), (1535, 1165), (1585, 1165)], color=PURPLE, label="是", label_at=(1532, 1260))
arrow([(1325, 1340), (1325, 1395), (2070, 1395), (2070, 1405)], label="否", label_at=(1390, 1370))
draw.text((1935, 1414), "否：继续等待", font=SMALL_FONT, fill=MUTED)


# 3. Strategy
panel(1520, 2250, "3", "已收盘 5 分钟 K 线的策略判断", PURPLE)
box((115, 1690, 430, 1830), "过滤重复、乱序\n和跨日 K 线")
diamond((650, 1760), (155, 110), "已有 MA周期 + 1\n根 K 线？")
box((880, 1690, 1195, 1830), "计算上一时点 MA\n和当前 MA")
diamond((1415, 1760), (150, 110), "日线方向")

box((1650, 1600, 2050, 1760), "LONG 条件\n前收盘 ≤ 前MA\n当前收盘 > 当前MA\n且突破前高", fill=GREEN_BG, outline=GREEN)
box((1650, 1860, 2050, 2020), "SHORT 条件\n前收盘 ≥ 前MA\n当前收盘 < 当前MA\n且跌破前低", fill=AMBER_BG, outline=AMBER)
box((2110, 1620, 2290, 1740), "BUY", fill=GREEN_BG, outline=GREEN, bold=True, text_fill=GREEN)
box((2110, 1880, 2290, 2000), "SELL", fill=PURPLE_BG, outline=PURPLE, bold=True, text_fill=PURPLE)
box((830, 2070, 1570, 2175), "任一条件不满足：不产生信号，返回行情循环", fill="#F3F6F9", outline="#A7B6C5")

arrow([(430, 1760), (495, 1760)])
arrow([(805, 1760), (880, 1760)], label="是", label_at=(840, 1725))
arrow([(650, 1870), (650, 2122), (830, 2122)], label="否", label_at=(700, 1905))
arrow([(1195, 1760), (1265, 1760)])
arrow([(1565, 1760), (1605, 1760), (1605, 1680), (1650, 1680)], color=GREEN, label="LONG", label_at=(1607, 1642))
arrow([(1415, 1870), (1415, 1940), (1650, 1940)], color=AMBER, label="SHORT", label_at=(1515, 1905))
arrow([(2050, 1680), (2110, 1680)], color=GREEN)
arrow([(2050, 1940), (2110, 1940)], color=PURPLE)
draw.text((1850, 2080), "FLAT / UNKNOWN → 继续等待", font=SMALL_FONT, fill=MUTED)


# 4. Risk controls
panel(2300, 3120, "4", "下单前资金安全检查", AMBER)
box((110, 2480, 385, 2610), "BUY / SELL\n候选信号", fill=PURPLE_BG, outline=PURPLE, bold=True)
diamond((605, 2545), (160, 115), "停止、未决订单\n或信号过期？")
diamond((950, 2545), (135, 100), "信号方向")
box((1135, 2355, 1515, 2505), "BUY 检查\n已有持仓、入场次数\n单笔及账户日上限", fill=GREEN_BG, outline=GREEN)
box((1135, 2605, 1515, 2755), "SELL 检查\n必须有程序多头\n卖出全部持仓，禁止卖空", fill=PURPLE_BG, outline=PURPLE)
diamond((1725, 2545), (150, 110), "全部检查\n通过？")
box((1945, 2370, 2280, 2500), "SQLite 即时事务\n原子预留资金及订单", fill=AMBER_BG, outline=AMBER)
diamond((2110, 2735), (160, 110), "发送前再次\n收到停止？")
box((1770, 2930, 2060, 3050), "阻止下单\n或本地拒绝", fill=RED_BG, outline=RED, bold=True, text_fill=RED)
box((2140, 2930, 2290, 3050), "提交\nMARKET", fill=GREEN_BG, outline=GREEN, bold=True, text_fill=GREEN)

arrow([(385, 2545), (445, 2545)])
arrow([(765, 2545), (815, 2545)], label="否", label_at=(790, 2510))
arrow([(605, 2660), (605, 2990), (1770, 2990)], color=RED, label="是", label_at=(655, 2700))
arrow([(950, 2445), (950, 2430), (1135, 2430)], color=GREEN, label="BUY", label_at=(1040, 2395))
arrow([(950, 2645), (950, 2680), (1135, 2680)], color=PURPLE, label="SELL", label_at=(1040, 2715))
arrow([(1515, 2430), (1575, 2430), (1575, 2545)], color=GREEN)
arrow([(1515, 2680), (1575, 2680), (1575, 2545)], color=PURPLE)
arrow([(1875, 2545), (1900, 2545), (1900, 2435), (1945, 2435)], label="是", label_at=(1900, 2505))
arrow([(1725, 2655), (1725, 2990), (1770, 2990)], color=RED, label="否", label_at=(1770, 2690))
arrow([(2110, 2500), (2110, 2625)])
arrow([(1950, 2735), (1900, 2735), (1900, 2930)], color=RED, label="是", label_at=(1870, 2780))
arrow([(2110, 2845), (2110, 2990), (2140, 2990)], color=GREEN, label="否", label_at=(2155, 2880))


# 5. Lifecycle
panel(3170, 4130, "5", "订单生命周期与持仓更新", RED)
box((105, 3380, 370, 3510), "订单已提交", fill=GREEN_BG, outline=GREEN, bold=True)
diamond((590, 3445), (145, 105), "交易模式")

box((820, 3270, 1100, 3400), "PAPER\n记录模拟成交", fill=BLUE_BG, outline=BLUE)
box((1220, 3270, 1560, 3400), "直接标记 FILLED")

box((820, 3540, 1100, 3670), "REAL\nBinance 返回", fill=PURPLE_BG, outline=PURPLE)
diamond((1325, 3605), (160, 110), "返回结果")
box((1580, 3440, 1880, 3560), "明确拒绝\n释放预留", fill=RED_BG, outline=RED)
box((1580, 3600, 1880, 3720), "未知结果\nUNKNOWN 锁定", fill=RED_BG, outline=RED, bold=True, text_fill=RED)
box((1580, 3760, 1880, 3880), "已接受\n查询订单详情", fill=GREEN_BG, outline=GREEN)
diamond((2100, 3820), (150, 110), "订单状态")

box((2020, 3260, 2290, 3380), "终态\n更新持仓与均价", fill=GREEN_BG, outline=GREEN, bold=True)
box((1940, 3480, 2290, 3600), "NEW / 部分成交\n阻止后续订单并轮询", fill=AMBER_BG, outline=AMBER)
box((1940, 4000, 2290, 4095), "无法解析 → UNKNOWN 锁定", fill=RED_BG, outline=RED, text_fill=RED)

arrow([(370, 3445), (445, 3445)])
arrow([(590, 3340), (590, 3335), (820, 3335)], color=BLUE, label="PAPER", label_at=(700, 3295))
arrow([(1100, 3335), (1220, 3335)], color=BLUE)
arrow([(1560, 3335), (2020, 3335)], color=GREEN)
arrow([(590, 3550), (590, 3605), (820, 3605)], color=PURPLE, label="REAL", label_at=(700, 3640))
arrow([(1100, 3605), (1165, 3605)])
arrow([(1485, 3605), (1530, 3605), (1530, 3500), (1580, 3500)], color=RED, label="拒绝", label_at=(1515, 3465))
arrow([(1485, 3605), (1580, 3660)], color=RED, label="未知", label_at=(1530, 3665))
arrow([(1325, 3715), (1325, 3820), (1580, 3820)], color=GREEN, label="接受", label_at=(1435, 3780))
arrow([(1880, 3820), (1950, 3820)])
arrow([(2100, 3710), (2100, 3600)], color=AMBER, label="未到终态", label_at=(2180, 3660))
arrow([(2100, 3710), (2100, 3380)], color=GREEN, label="FILLED / 终态", label_at=(2185, 3440))
arrow([(2100, 3930), (2100, 4000)], color=RED, label="异常", label_at=(2160, 3960))


# Footer
draw.rounded_rectangle((90, 4170, 2310, 4260), radius=22, fill="#E9EEF4")
centered_text(
    (110, 4176, 2290, 4254),
    "安全原则：风险退出优先；实盘只做多；未知订单立即锁定；停止策略不会自动平仓。",
    FOOTER_FONT,
    INK,
)


OUTPUT.parent.mkdir(parents=True, exist_ok=True)
image.save(OUTPUT, format="PNG", optimize=True, dpi=(144, 144))
print(OUTPUT)
