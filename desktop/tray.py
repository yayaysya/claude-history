"""Windows 托盘入口：打开网页、刷新会话和关闭本工具。"""

import webbrowser

from PIL import Image, ImageDraw
import pystray


def make_icon(size=64):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 64
    def points(values):
        return [(int(x * scale), int(y * scale)) for x, y in values]
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(13 * scale), fill="#34664c")
    draw.line(points([(16, 15), (49, 15), (49, 43), (28, 43), (16, 53), (16, 15)]), fill="white", width=max(2, int(3 * scale)), joint="curve")
    draw.line(points([(24, 25), (41, 25)]), fill="white", width=max(2, int(3 * scale)))
    draw.line(points([(24, 34), (36, 34)]), fill="white", width=max(2, int(3 * scale)))
    return image


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
