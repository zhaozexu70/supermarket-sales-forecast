# -*- coding: utf-8 -*-
"""统一的作图样式：中文字体、配色、网格"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

# 分类配色（固定顺序使用）
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_MAGENTA, C_GREEN = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
COLORS = [C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_MAGENTA, C_GREEN]
C_GRAY = "#8a8985"
TEXT = "#0b0b0b"
TEXT2 = "#52514e"
GRID = "#e4e3df"


def setup():
    """Windows 一般有 SimHei / Microsoft YaHei，Mac 有 PingFang SC，Linux 用 Noto Sans CJK"""
    candidates = ["SimHei", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
                  "Source Han Sans SC", "WenQuanYi Zen Hei", "Arial Unicode MS"]
    have = {f.name for f in font_manager.fontManager.ttflist}
    fonts = [c for c in candidates if c in have] or ["sans-serif"]
    plt.rcParams.update({
        "font.sans-serif": fonts,
        "font.family": "sans-serif",
        "axes.unicode_minus": False,
        "figure.dpi": 100,
        "savefig.dpi": 200,
        "savefig.bbox": "tight",
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT2,
        "axes.titlecolor": TEXT,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": TEXT2,
        "ytick.color": TEXT2,
        "legend.frameon": False,
        "lines.linewidth": 2,
    })
    return fonts[0]


def save(fig, path):
    fig.savefig(path)
    plt.close(fig)
    print("  图已保存:", path.name)
