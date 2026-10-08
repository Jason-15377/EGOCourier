#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EGO Courier 标志（Logo / 图标）生成脚本。

设计说明
--------
新一版标志从「旧版：蓝底相机 + 三色圆点」改为「**蓝底 + 快递箭头 / 波束**」：

* 深蓝圆角方（Windows 磁贴语言）承载品牌主色 #2563eb → #1d4ed8 的竖向渐变；
* 两道白色箭头源自「Courier（信使）」的寄送语义，指向上右方表示「日志送达」；
* 箭头尾部短横线是设备端「日志打包」的抽象，箭头分别代表 app / sdk / firmware 三类日志；
* 右上次级点缀落为青蓝色（#38bdf8），呼应双目相机 / 传感器的高光，不抢主体。

可见内容控制在约 84% 画布内（四周留白 ≈8%），符合 Windows 图标在磁贴与
任务栏中的安全区要求；线条统一 8–9% 画布宽，保证 16px 下仍可辨识。
低分辨率（≤32px）走简化路径：去掉尾部短横线，仅保留双箭头。

产出（写入 assets/）：
    logo_<n>.png     各种尺寸的透明底 PNG（n = 16/24/32/48/64/96/128/256/512）
    logo.png         512px 主 PNG（用于文档 / README）
    ego_courier.ico  多分辨率 Windows 图标（16/20/24/32/40/48/64/96/128/256）

用法:
    python assets/make_logo.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

# ---------------------------------------------------------------- 调色板

BLUE_TOP = (59, 130, 246)     # #3b82f6
BLUE_BOTTOM = (29, 78, 216)   # #1d4ed8
ACCENT = (56, 189, 248)       # #38bdf8 —— 双目高光 / 次要箭头
WHITE = (255, 255, 255)

# Windows 任务栏/开始菜单实际会请求的尺寸（100%–400% 缩放）
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]

# 需要单独落盘的 PNG 尺寸
PNG_SIZES = [16, 24, 32, 48, 64, 96, 128, 256, 512]

# 超级采样倍率：先按 4 倍画再缩回，边缘抗锯齿更干净
SS = 4


def _rounded_square(draw: ImageDraw.ImageDraw, size: int, radius_ratio: float) -> None:
    """竖向渐变圆角方：逐行插值填充，避免引入额外依赖。"""
    r = int(size * radius_ratio)
    # 先画满圆角矩形做裁剪底，再逐行上色
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=255)

    grad = Image.new("RGB", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        # 轻微缓动，让上半部更亮，符合 Windows 图标自上而下的受光习惯
        t = t ** 0.85
        c = tuple(int(BLUE_TOP[i] + (BLUE_BOTTOM[i] - BLUE_TOP[i]) * t) for i in range(3))
        gd.line([(0, y), (size, y)], fill=c)
    draw._image.paste(grad, (0, 0), mask)


def _chevron(draw: ImageDraw.ImageDraw, size: int, cx: float, cy: float,
             w: float, h: float, stroke: int, color: tuple[int, int, int]) -> None:
    """在 (cx, cy) 处画一个指向右上方的箭头（chevron）。

    w/h 为箭头宽高（相对画布比例），stroke 为线宽（相对画布比例）。
    """
    W = size * w
    H = size * h
    x0 = size * cx - W / 2
    y0 = size * cy - H / 2
    lw = max(1, int(size * stroke))

    p_tip = (x0 + W, y0)                       # 右上顶点
    p_left = (x0, y0 + H)                      # 左下
    p_up = (x0, y0)                            # 左上（拐点）
    p_right_bottom = (x0 + W, y0 + H)          # 右下

    # 用 joint="curve" 保证折角圆润，末端圆头
    draw.line([p_left, p_up, p_tip], fill=color, width=lw, joint="curve")
    for p in (p_tip, p_up, p_left):
        rr = lw / 2
        draw.ellipse((p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr), fill=color)
    # 右侧竖直段（让箭头读起来像「↗」而不是「⌐」）
    draw.line([p_tip, p_right_bottom], fill=color, width=lw)


def render(size: int, simple: bool | None = None) -> Image.Image:
    """渲染一个 size×size 的 RGBA 标志。

    simple=True 时使用低分辨率简化版（去掉尾部短横线）。
    默认：size <= 32 自动简化。
    """
    if simple is None:
        simple = size <= 32

    size = int(size)
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 1) 底板
    _rounded_square(d, S, radius_ratio=0.22)

    # 2) 主箭头（白，居中偏左下）
    _chevron(d, S, cx=0.455, cy=0.585, w=0.40, h=0.40, stroke=0.093, color=WHITE)

    # 3) 次级箭头（青色，右上，小幅错位形成「双日志并行」）
    _chevron(d, S, cx=0.635, cy=0.375, w=0.26, h=0.26, stroke=0.078, color=ACCENT)

    # 4) 尾部短横线（仅大尺寸）：三条横杠 = app / sdk / firmware 三类日志
    if not simple:
        lw = max(1, int(S * 0.072))
        base_x = S * 0.20
        for i, yy in enumerate((0.68, 0.775, 0.87)):
            x1 = base_x + i * S * 0.055
            x2 = x1 + S * 0.185
            y = S * yy
            d.line([(x1, y), (x2, y)], fill=WHITE, width=lw)

    img = img.resize((size, size), Image.LANCZOS)
    return img


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))

    # PNG：16–256 逐尺寸渲染；512 用大图渲染
    for n in PNG_SIZES:
        out = os.path.join(here, f"logo_{n}.png")
        render(n).save(out)
        print("wrote", os.path.basename(out))

    # 主 PNG（512，等于 logo_512 的语义化别名）
    render(512).save(os.path.join(here, "logo.png"))

    # 多分辨率 .ico：每帧都用各自尺寸独立渲染，而非缩放同一张图
    frames = [render(n) for n in ICO_SIZES]
    base = max(frames, key=lambda im: im.size[0])
    ico = os.path.join(here, "ego_courier.ico")
    base.save(ico, format="ICO",
              sizes=[(n, n) for n in ICO_SIZES],
              append_images=[f for f in frames if f is not base])
    print("wrote", os.path.basename(ico))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
