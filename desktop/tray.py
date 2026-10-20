"""Windows 托盘入口：打开网页、刷新会话和关闭本工具。"""

import webbrowser

from PIL import Image, ImageDraw
import pystray


ICON_TOP = (99, 102, 241)
ICON_BOTTOM = (67, 56, 202)


def make_icon(size=64):
    # 先按 4 倍尺寸绘制再缩小，PIL 本身不做抗锯齿，小尺寸托盘图标边缘才不发毛。
    big = size * 4
    unit = big / 64

    def box(x0, y0, x1, y1):
        return (x0 * unit, y0 * unit, x1 * unit, y1 * unit)

    gradient = Image.new("RGBA", (big, big))
    gradient_draw = ImageDraw.Draw(gradient)
    for y in range(big):
        t = y / (big - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(ICON_TOP, ICON_BOTTOM))
        gradient_draw.line([(0, y), (big, y)], fill=color)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=14 * unit, fill=255)
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    image.paste(gradient, (0, 0), mask)

    # 白色对话气泡 + 左下尾巴，表示“会话”。
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box(12, 13, 52, 45), radius=10 * unit, fill="white")
    draw.polygon([(18 * unit, 41 * unit), (30 * unit, 44 * unit), (16 * unit, 54 * unit)], fill="white")

    # 气泡内的时钟，表示“历史”。
    stroke = round(3.2 * unit)
    draw.ellipse(box(22, 19, 42, 39), outline=ICON_BOTTOM, width=stroke)
    draw.line([(32 * unit, 23.5 * unit), (32 * unit, 29 * unit), (36.5 * unit, 32 * unit)], fill=ICON_BOTTOM, width=stroke, joint="curve")
    return image.resize((size, size), Image.LANCZOS)


def create_tray(url, refresh, shutdown):
    def quit_app(icon, _item):
        icon.stop()
        shutdown()

    return pystray.Icon("claude-history-browser", make_icon(), "会话记录", menu=pystray.Menu(
        pystray.MenuItem("打开会话记录", lambda: webbrowser.open(url), default=True),
        pystray.MenuItem("刷新记录", lambda: refresh()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出工具", quit_app),
    ))
