"""
RAL 寄存器模型浏览器（CustomTkinter GUI）
=========================================

导入一个或多个 SystemVerilog RAL 模型文件（``*_ral_pkg.sv``），以表格形式
可视化 **block / reg / reg_field** 三级结构和各自的特征；字段信息可切换成
位图（bit map）直观查看位域分布。

功能
----
* 多文件导入：``打开文件`` 支持多选，``追加文件`` 在已有结果上继续添加
* 左侧模型树：文件 → block → 寄存器 / 内存，点击即过滤右侧表格
* 六种视图：**Registers** / **Fields** / **Blocks** / **Bitmap** / **All Bitmaps** / **Source**
  （界面上显示的文案一律英文）
* **全部位图**：所有寄存器一次画完（可滚动），纵向对齐便于横向比对位域分布
* 搜索：对当前视图所有列做子串过滤；点击表头排序
* 导出 CSV；选中任意行可在「源码」视图里定位到原始 SV 代码行
* 解析器对自定义继承类通用（``dv_base_reg extends uvm_reg`` 之类无需特殊配置）

运行::

    python ral_viewer.py                       # 自动载入随附的 uart_ral_pkg.sv 示例
    python ral_viewer.py a_ral_pkg.sv b_ral_pkg.sv
自测（渲染 + 断言 + 截图 + 自动退出）::

    python ral_viewer.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from tkinter import Canvas, Text, filedialog, font as tkfont, ttk

import customtkinter as ctk

from ral_parser import (
    LOOSE_BLOCK,
    BlockDef,
    FieldDef,
    ParseResult,
    RegDef,
    collect_macros,
    expand_macros,
    parse_const_expr,
    parse_files,
)

APP_TITLE = "RAL Register Model Browser"

#: 字体族候选（按优先级）。Windows 上很顺的 ``Microsoft YaHei UI`` / ``Consolas``
#: 在 Linux 上**根本不存在**，Tk 只能走 fontconfig 替换：换出来的字形粗细/字面宽
#: 都和请求的不一样（中文常落到点阵或另一次像素渲染的族上），看着就是"发虚"。
#: 所以按平台给候选，运行时用 ``tkfont.families()`` 挑第一个真实存在的。
#: 列表按"Windows 优先"排序即可 —— Linux 上前几个查不到，自然落到 Noto/DejaVu。
UI_FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Segoe UI", "Noto Sans CJK SC", "Source Han Sans SC",
    "WenQuanYi Micro Hei", "Noto Sans", "DejaVu Sans", "Liberation Sans",
    "Ubuntu", "FreeSans",
)
MONO_FONT_CANDIDATES = (
    "Consolas", "Noto Sans Mono CJK SC", "Noto Sans Mono", "DejaVu Sans Mono",
    "Liberation Mono", "Ubuntu Mono", "WenQuanYi Zen Hei Mono", "FreeMono",
)

FONT = UI_FONT_CANDIDATES[0]
MONO = MONO_FONT_CANDIDATES[0]
OUT_DIR = Path(__file__).with_name("_selftest")
EXAMPLE = Path(__file__).with_name("uart_ral_pkg.sv")

#: 视图名 → 分段按钮顺序（同时也是 COLUMNS 的键，界面上一律英文）
VIEWS = ("Registers", "Fields", "Blocks", "Bitmap", "All Bitmaps", "Source")

#: Debug 面板：级别 → (标签, 颜色标签名)
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_LOG_TAG = {"DEBUG": "debug", "INFO": "info", "WARN": "warn", "ERROR": "error"}
_LOG_MAX_LINES = 600          # 面板里最多保留多少行，防止长跑后越来越慢

#: 段错误（segmentation fault）是 C 层崩溃，Python 的 try/except 抓不到，Debug 面板也
#: 来不及刷新。这个临时文件是唯一能留下的现场：进出关键步骤各追加一行并**立刻落盘**，
#: 崩溃后打开它就知道死在哪儿（Linux 上排查"点某个按钮就退出"全靠它）。
TRACE_FILE = Path(tempfile.gettempdir()) / "ral_viewer_trace.log"
_TRACE_MAX_BYTES = 64 * 1024

#: 会让 Tk 文本控件出问题的控制字符：NUL(``\x00``) 首当其冲（Tk 处理内嵌 NUL 的老问题），
#: 其余 C0 控制符（``\t`` / ``\n`` 除外）和 DEL 也一并去掉 —— 它们本来就显示不出来，
#: 却要参与 X11 那边的文本布局计算。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: 源码视图一次最多插多少行，防止误选到一个几千行的 block 把界面拖死
_SOURCE_MAX_LINES = 3000

#: 位图**横向拉长**的档位：倍率是相对「自适应铺满视口」算的，``1.0`` 就是以前的样子。
#: 窄窗口里 32bit 寄存器每格只剩二十来像素，``tx_watermark`` 这种字段名在格子里根本排
#: 不下（``_fit_text`` 会截成 ``tx_wat…``，更窄的直接退化到下方图例），拉长才看得全。
ZOOM_STEPS = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 8.0)

#: 每 bit 格宽的上下限（像素）：太小连位号都糊在一起，太大一屏只剩两格
_CELL_MIN_PX, _CELL_MAX_PX = 6.0, 400.0

#: 读源文件的字节上限：``_class_end`` 要把整个文件读进来扫 ``endclass``，
#: 碰上一个几十 MB 的合并文件会卡住界面
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


def tk_safe(text: str) -> str:
    """
    清掉控制字符，得到可以安全交给 Tk 文本控件的字符串。

    源文件用 ``errors="replace"`` 读进来时：非法字节变成 U+FFFD，但 **NUL 会原样保留**。
    把带 NUL 的串插进 X11 上的 ``Text`` 是 Tk 的经典崩溃点，而它在界面上什么都显示不
    出来，所以直接删除，不做替换。
    """
    return _CTRL_RE.sub("", text)


class _WheelEvent:
    """
    自测用的合成滚轮事件：X11 只给 ``num``，Windows / macOS 只给 ``delta``。

    ``state`` 是按键修饰位（bit0 = Shift），两个平台都带 —— 用来测「Shift+滚轮
    横向滚动」。
    """

    def __init__(self, num: int = 0, delta: int = 0, state: int = 0):
        self.num, self.delta, self.state = num, delta, state


#: 每种访问权限一种颜色（位图配色，兼顾深/浅色背景对比度）
ACCESS_FILL = {
    "RW": "#1e6fd9",
    "RO": "#1f8a4c",
    "WO": "#d97706",
    "W1C": "#8b5cf6",
    "W0C": "#8b5cf6",
    "W1S": "#7c3aed",
    "W0S": "#7c3aed",
    "W1T": "#6d28d9",
    "RC": "#0d9488",
    "RS": "#0d9488",
    "WC": "#0e7490",
    "WS": "#0e7490",
    "WRC": "#0e7490",
    "WRS": "#0e7490",
}
ACCESS_FALLBACK = "#64748b"

#: 表格列定义：视图 → [(列 id, 表头, 宽度, 对齐)]
COLUMNS: dict[str, list[tuple[str, str, int, str]]] = {
    "Registers": [
        ("block", "Block", 128, "w"),
        ("inst", "Instance", 110, "w"),
        ("cls", "Register Class", 185, "w"),
        ("offset", "Offset", 64, "e"),
        ("access", "Access", 60, "center"),
        ("width", "Width", 50, "e"),
        ("reset", "Reset", 88, "e"),
        ("nfield", "Fields", 50, "e"),
        ("uncovered", "Reserved Bits", 92, "e"),
        ("doc", "Description", 200, "w"),
        ("base", "Base Class", 130, "w"),
        ("hdl", "HDL Path", 110, "w"),
        ("loc", "File:Line", 140, "w"),
    ],
    "Fields": [
        ("block", "Block", 128, "w"),
        ("reg", "Register", 160, "w"),
        ("name", "Field", 150, "w"),
        ("bits", "Bit Range", 80, "center"),
        ("width", "Width", 52, "e"),
        ("lsb", "LSB", 46, "e"),
        ("access", "Access", 60, "center"),
        ("reset", "Reset", 80, "e"),
        ("flags", "Attributes", 110, "w"),
        ("doc", "Description", 260, "w"),
        ("loc", "File:Line", 140, "w"),
    ],
    "Blocks": [
        ("cls", "Block", 170, "w"),
        ("base", "Base Class", 160, "w"),
        ("nreg", "Registers", 70, "e"),
        ("nmem", "Memories", 66, "e"),
        ("nfield", "Fields", 56, "e"),
        ("map", "Address Map", 230, "w"),
        ("hdl", "HDL Root", 140, "w"),
        ("locked", "Locked", 60, "center"),
        ("doc", "Description", 220, "w"),
        ("sub", "Sub-blocks / Sub-maps", 200, "w"),
        ("loc", "File:Line", 140, "w"),
    ],
}


def access_fill(access: str) -> str:
    return ACCESS_FILL.get((access or "").upper(), ACCESS_FALLBACK)


def resolve_fonts() -> tuple[str, str, str]:
    """
    挑一组**本机真实存在**的字体族 → ``(界面字体, 等宽字体, 日志说明)``。

    Tk 请求一个不存在的族名时不会报错，而是静默替换 —— 于是"量宽用的字体"和
    "真正画出来的字体"可能不是同一个，Linux 上就会表现为字形发虚、文字互相压叠。
    候选全都不在时退回 Tk 自己的 ``TkDefaultFont`` / ``TkFixedFont``（它们的
    ``actual('family')`` 一定是本机真实族名）。
    """
    try:
        families = {f.lower(): f for f in tkfont.families()}
    except Exception:                      # 没有 Tk / 没有字体列表
        return FONT, MONO, "font list unavailable, using defaults"

    def pick(candidates: tuple[str, ...], named: str) -> str:
        for cand in candidates:
            if cand.lower() in families:
                return families[cand.lower()]
        try:
            return tkfont.nametofont(named).actual("family")
        except Exception:
            return candidates[-1]

    ui = pick(UI_FONT_CANDIDATES, "TkDefaultFont")
    mono = pick(MONO_FONT_CANDIDATES, "TkFixedFont")
    return ui, mono, f"ui={ui!r} mono={mono!r}"


# =============================================================== 主窗口


class RalViewer(ctk.CTk):
    """主窗口：左侧模型树 + 右侧多视图表格/位图/源码。"""

    def __init__(self, paths: list[str] | None = None) -> None:
        super().__init__()
        # 字体族必须在 Tk root 建好之后才能查（tkfont.families() 要连上 X 服务）
        global FONT, MONO
        FONT, MONO, self._font_note = resolve_fonts()
        # 1 磅 = 多少像素。Tk 自己就是按这个值把磅换算成像素的（等价于 tk scaling），
        # 量不到就退回 tk scaling；再离谱就按 1.0 处理 —— 总比把字号算成 0 强
        try:
            self._px_per_pt = float(self.winfo_fpixels("1p"))
        except Exception:
            try:
                self._px_per_pt = float(self.tk.call("tk", "scaling"))
            except Exception:
                self._px_per_pt = 1.0
        if not 0.5 <= self._px_per_pt <= 8.0:
            self._px_per_pt = 1.0
        self.title(APP_TITLE)
        self._fit_geometry()

        self.result = ParseResult()
        self.scope: dict[str, str | None] = {"file": None, "block": None, "reg": None}
        self.query = ""
        self.focus_reg: RegDef | None = None
        self._sort: dict[str, tuple[str, bool]] = {}
        self._rows: list[list[str]] = []
        self._row_meta: list[dict] = []
        self._sel_meta: dict[str, tuple[str, str | None, str | None]] = {}
        self._bitmap_geom: tuple[float, float, float, float, int] | None = None
        self.wall_geom: list[dict] = []          # 全部位图：每条位带的几何信息（悬停/点击用）
        self.wall_active: RegDef | None = None   # 全部位图里当前聚焦的寄存器
        self._font_cache: dict[tuple[str, int], tkfont.Font] = {}
        self._width_cache: dict[tuple[str, int, str], int] = {}   # (family, size, text) → px
        self._bitmap_job: str | None = None      # <Configure> 重绘防抖（位图）
        self._wall_job: str | None = None        # <Configure> 重绘防抖（全部位图）
        self._legend_text: dict[int, str] = {}   # 图例文字缓存，同文本不再 configure
        self.log_open = True                     # 底部 Debug 面板是否展开
        self.cell_zoom = 1.0                     # 位图横向拉长档位（见 ZOOM_STEPS）
        self._overflow_x = False                 # 位图内容是否比视口宽（拉长后要提示横向滚动）
        self._wrap_cache: dict[int, int] = {}    # 标签 → 已设过的 wraplength（避免反复 configure）
        self.log_counts = {"WARN": 0, "ERROR": 0}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(3, weight=0)
        self.grid_rowconfigure(4, weight=0)

        self._init_fonts()
        self._build_toolbar()   # row 0
        self._build_search()    # row 1
        self._build_body()      # row 2
        self._build_status()    # row 3
        self._build_debug()     # row 4

        self._apply_theme()
        self._log("INFO", f"{APP_TITLE} started")
        self._log("INFO", f"Python {sys.version.split()[0]} · CustomTkinter v{ctk.__version__} "
                          f"· DPI scale {self._ui_scale:.2f} · cwd {Path.cwd()}")
        self._log("DEBUG", f"platform {sys.platform} · fonts {self._font_note} "
                           f"· {self._px_per_pt:.3f}px/pt (1pt={self._pen(10)}px at 10pt)")
        # 段错误没法在 Python 里抓，只能靠这份跟踪文件留现场：每次启动清空重来
        try:
            TRACE_FILE.write_text("", encoding="utf-8")
            tk_ver = self.tk.call("info", "patchlevel")
        except Exception:                       # 读不到版本号无所谓，别拖累启动
            tk_ver = "?"
        # 纯 ASCII：这个文件是要在"崩了之后"用 cat / type 看的，别让编码再挡一道
        self._crumb(f"app: started (py{sys.version.split()[0]} {sys.platform} "
                    f"tk {tk_ver} scaling {self._px_per_pt:.3f})")
        self._log("DEBUG", f"crash trace file: {TRACE_FILE}")
        self.after(80, lambda: self._load(paths or [], append=False, quiet=not paths))

    # ------------------------------------------------------------ 构造 UI
    def _fit_geometry(self) -> None:
        """
        按屏幕实际大小定窗口尺寸。

        CustomTkinter 会把 ``geometry()`` 的数值再乘一遍 DPI 缩放系数
        （200% 缩放的 1440×900 屏上，写 1440×860 会变成 2880×1720 溢出屏幕），
        所以这里先按物理像素算出合适尺寸，再除以缩放系数传回去。
        """
        try:
            scale = float(ctk.ScalingTracker.get_window_dpi_scaling(self)) or 1.0
        except Exception:
            scale = 1.0
        self._ui_scale = scale  # ttk 控件不受 CTk 缩放影响，列宽/行高要自己乘
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width = int(min(1440, sw - 40) / scale)
        height = int(min(880, sh - 60) / scale)
        x = max(0, int((sw - width * scale) / 2 / scale))
        y = max(0, int((sh - height * scale) / 3 / scale))
        self.geometry(f"{width}x{height}+{x}+{y}")
        # 最小尺寸不能超过屏幕（Linux 小屏 + 大缩放的组合下，写死的 760 逻辑像素
        # 会变成比屏幕还宽的硬性下限，窗口再也缩不小）
        self.minsize(int(max(420, min(760, sw / scale - 20))),
                     int(max(320, min(500, sh / scale - 40))))

    def _init_fonts(self) -> None:
        self.f_small = ctk.CTkFont(family=FONT, size=11)
        self.f_base = ctk.CTkFont(family=FONT, size=12)
        self.f_bold = ctk.CTkFont(family=FONT, size=12, weight="bold")
        self.f_title = ctk.CTkFont(family=FONT, size=16, weight="bold")
        self.f_mono_s = ctk.CTkFont(family=MONO, size=10)   # 位图 / 全部位图的图例
        # 源码视图是原生 tk.Text，不吃 CTkFont：字号按像素直接给。CTk 内部给字号乘的是
        # DPI 缩放系数，这里照同一口径换算（Tk 的约定：负 size = 像素）
        self.mono_px = max(11, int(round(11 * getattr(self, "_ui_scale", 1.0))))

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(bar, text="RAL Model", font=self.f_title).grid(
            row=0, column=0, padx=(14, 12), pady=10
        )
        ctk.CTkButton(bar, text="Open files…", width=96, command=self.on_open).grid(
            row=0, column=1, padx=4, pady=10
        )
        ctk.CTkButton(bar, text="Add files…", width=88, command=self.on_append).grid(
            row=0, column=2, padx=4, pady=10
        )
        self._ghost(bar, "Load sample", self.on_example, 92).grid(
            row=0, column=3, sticky="w", padx=8, pady=10
        )
        self._ghost(bar, "Clear", self.on_clear, 56).grid(row=0, column=4, padx=4, pady=10)
        self._ghost(bar, "Export CSV", self.on_export, 88).grid(row=0, column=5, padx=4, pady=10)

        self.appearance = ctk.CTkOptionMenu(
            bar, width=80, values=["Dark", "Light"], command=self.on_appearance,
            font=self.f_base, dropdown_font=self.f_base,
        )
        self.appearance.set("Dark")
        self.appearance.grid(row=0, column=6, padx=12, pady=10)
        self._toolbar_wrapped = False
        bar.bind("<Configure>", lambda _e: self._debounce("_toolbar_job", self._fix_toolbar_row))

    def _fix_toolbar_row(self) -> None:
        """
        工具栏放不下时把深浅色切换换到第二行（同 ``_fix_search_row`` 的道理：
        Tk 的 ``grid`` 只会裁剪，不会换行，窄窗口上最右边的控件直接消失）。
        """
        bar = self.appearance.master
        have = bar.winfo_width()
        if have <= 1:                     # 还没布局出来
            return
        need = bar.winfo_reqwidth()
        if not self._toolbar_wrapped and need > have:
            self._toolbar_wrapped = True
            self.appearance.grid_configure(row=1, column=0, columnspan=6, sticky="w",
                                           padx=(14, 12), pady=(0, 10))
        elif self._toolbar_wrapped and need <= have:
            self._toolbar_wrapped = False
            self.appearance.grid_configure(row=0, column=6, columnspan=1, sticky="",
                                           padx=12, pady=10)

    def _ghost(self, parent, text: str, command, width: int) -> ctk.CTkButton:
        """次要按钮：深浅色下都能看清（transparent 在浅色模式下文字会糊掉）。"""
        return ctk.CTkButton(
            parent, text=text, width=width, command=command, font=self.f_base,
            fg_color=("gray80", "gray26"), hover_color=("gray70", "gray36"),
            text_color=("gray10", "gray92"), text_color_disabled=("gray50", "gray50"),
        )

    def _build_search(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, height=44)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 1))
        bar.grid_columnconfigure(0, weight=1)

        self.search = ctk.CTkEntry(
            bar,
            placeholder_text="Filter by register / field / offset / description "
                             "(keywords separated by spaces)",
            font=self.f_base,
        )
        self.search.grid(row=0, column=0, sticky="ew", padx=(14, 0), pady=8)
        self.search.bind("<KeyRelease>", self.on_search)
        self._ghost(bar, "Clear filter", self.on_clear_filter, 92).grid(
            row=0, column=1, padx=8, pady=8
        )
        self.tree_toggle = self._ghost(bar, "Hide tree", self.on_toggle_tree, 88)
        self.tree_toggle.grid(row=0, column=3, padx=(8, 14), pady=8)

        self.view_var = ctk.StringVar(value=VIEWS[0])
        self.segment = ctk.CTkSegmentedButton(
            bar, values=list(VIEWS), variable=self.view_var, command=self.on_view,
            font=self.f_small,
        )
        self.segment.grid(row=0, column=2, padx=(0, 6), pady=8)
        self._search_wrapped = False
        bar.bind("<Configure>", lambda _e: self._debounce("_search_job", self._fix_search_row))

    def _fix_search_row(self) -> None:
        """
        搜索/视图这一行放不下时把视图分段按钮换到第二行。

        Tk 的 ``grid`` 在容器比请求宽度还小时**直接裁剪右边的列**（不会换行、也不会
        自动缩到合适宽度），所以窄窗口上右边的 "All Bitmaps" 那些按钮会整块消失 ——
        Linux 小屏（逻辑宽度 ~680px）就踩到了这个。这里按"除输入框外的固定控件 +
        输入框最小 140px"来判定，宽了就自动换回来。
        """
        bar, seg = self.search.master, self.segment
        have = bar.winfo_width()
        if have <= 1:                     # 还没布局出来
            return
        others = bar.winfo_reqwidth() - self.search.winfo_reqwidth()
        if not self._search_wrapped and others + 140 > have:
            self._search_wrapped = True
            seg.grid_configure(row=1, column=0, columnspan=4, sticky="ew",
                               padx=(14, 14), pady=(0, 8))
        elif self._search_wrapped and others + 140 <= have:
            self._search_wrapped = False
            seg.grid_configure(row=0, column=2, columnspan=1, sticky="",
                               padx=(0, 6), pady=8)

    def _build_body(self) -> None:
        pal = self._palette()
        body = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=10, pady=(6, 0))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.body = body

        # --- 左侧：模型树（窄屏上别占太多横向空间，可用「隐藏结构」整块收起）
        tree_w = 210 if self.winfo_screenwidth() / max(self._ui_scale, 1.0) < 1100 else 320
        left = ctk.CTkFrame(body, width=tree_w)
        self.left = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(left, text="Model Structure", font=self.f_bold, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=10, pady=(8, 4)
        )
        self.tree = ttk.Treeview(left, style="RAL.Treeview", show="tree", selectmode="browse")
        self.tree.column("#0", width=int(round(tree_w * self._ui_scale)), stretch=True,
                         minwidth=int(round(140 * self._ui_scale)))
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        tscroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        tscroll.grid(row=1, column=1, sticky="ns", pady=(0, 6))
        self.tree.configure(yscrollcommand=tscroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # --- 右侧：内容区
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.table = ttk.Treeview(right, style="RAL.Treeview", show="headings", selectmode="browse")
        self.table.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(right, orient="vertical", command=self.table.yview)
        hsb = ttk.Scrollbar(right, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.table.bind("<<TreeviewSelect>>", self.on_table_select)
        self._table_parts = (vsb, hsb)

        self.bitmap_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.bitmap_hint = ctk.CTkLabel(
            self.bitmap_frame,
            text="Select a register in the model tree or the Registers table to see its bit layout",
            font=self.f_base, anchor="w",
        )
        self.bitmap_hint.pack(fill="x", padx=6, pady=(0, 4))
        self.zoom_label = self._build_zoom_bar(self.bitmap_frame)
        bm_holder = ctk.CTkFrame(self.bitmap_frame, fg_color="transparent")
        bm_holder.pack(fill="both", expand=True)
        # 请求高度给小值：画布反正会被 pack 撑满，但请求值一旦偏大，
        # 空间不够时 pack 会先把后面的图例挤没
        self.canvas = Canvas(bm_holder, highlightthickness=0, bd=0, height=60, width=120)
        self.canvas.pack(side="left", fill="both", expand=True)
        bm_vsb = ttk.Scrollbar(bm_holder, orient="vertical", command=self.canvas.yview)
        bm_vsb.pack(side="right", fill="y")
        self.bitmap_hsb = ttk.Scrollbar(self.bitmap_frame, orient="horizontal",
                                        command=self.canvas.xview)
        self.bitmap_hsb.pack(fill="x")
        # y/xscrollincrement 让滚轮的一「格」是固定 24px；没有 scrollregion 时
        # yview_scroll 是空操作，两个都在 render_bitmap() 里按内容设好
        self.canvas.configure(yscrollcommand=bm_vsb.set, xscrollcommand=self.bitmap_hsb.set,
                              yscrollincrement=24, xscrollincrement=24)
        self.canvas.bind("<Configure>", lambda _e: self._debounce("_bitmap_job", self.render_bitmap))
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        for widget in (self.canvas, bm_holder):
            self._bind_wheel(widget, self.on_bitmap_wheel)
        self.bm_parts = (bm_vsb, self.bitmap_hsb)
        self.bitmap_legend = ctk.CTkLabel(self.bitmap_frame, text="", font=self.f_mono_s, anchor="w")
        self.bitmap_legend.pack(fill="x", padx=6, pady=(4, 6))

        # --- 全部位图：所有寄存器一次画完，超出视口可滚动
        self.all_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.wall_hint = ctk.CTkLabel(self.all_frame, text="", font=self.f_base, anchor="w")
        self.wall_hint.pack(fill="x", padx=6, pady=(0, 4))
        self.zoom_label_all = self._build_zoom_bar(self.all_frame)
        holder = ctk.CTkFrame(self.all_frame, fg_color="transparent")
        holder.pack(fill="both", expand=True)
        self.wall_canvas = Canvas(holder, highlightthickness=0, bd=0, height=60, width=120)
        self.wall_canvas.pack(side="left", fill="both", expand=True)
        wsb = ttk.Scrollbar(holder, orient="vertical", command=self.wall_canvas.yview)
        wsb.pack(side="right", fill="y")
        whsb = ttk.Scrollbar(self.all_frame, orient="horizontal", command=self.wall_canvas.xview)
        whsb.pack(fill="x")
        self.wall_canvas.configure(yscrollcommand=wsb.set, xscrollcommand=whsb.set,
                                   yscrollincrement=24, xscrollincrement=24)
        self._wall_parts = (wsb, whsb)
        self.wall_canvas.bind("<Configure>",
                              lambda _e: self._debounce("_wall_job", self.render_all_bitmaps))
        self.wall_canvas.bind("<Motion>", self.on_wall_motion)
        self.wall_canvas.bind("<Button-1>", self.on_wall_click)
        for widget in (self.wall_canvas, holder, self.wall_hint):
            self._bind_wheel(widget, self.on_wall_wheel)
        self.all_legend = ctk.CTkLabel(self.all_frame, text=" ", font=self.f_mono_s, anchor="w")
        self.all_legend.pack(fill="x", padx=6, pady=(4, 6))
        # 图例 / 外框 / 滚动条上也绑一份：鼠标停在这些地方滚轮同样有效
        for widget in (self.all_legend, self.all_frame, wsb, whsb):
            self._bind_wheel(widget, self.on_wall_wheel)

        # 源码视图用**原生 tk.Text**，和 Debug 面板一样，不用 CTkTextbox：
        # * 这是全程序唯一把文件内容整片贴进控件的地方，也是唯一用到 CTkScrollbar
        #   （CTkTextbox 内部自带一个）的地方 —— Linux/X11 上点这个按钮会段错误退出，
        #   而同样贴文本的 Debug 面板（原生 Text + ttk.Scrollbar）一直没出过问题；
        # * 字号口径与 CTkTextbox 时代保持一致，见 _init_fonts() 里的 mono_px。
        self.source_box = Text(right, wrap="none", bd=0, highlightthickness=0,
                               font=(MONO, -self.mono_px), state="disabled", padx=6, pady=4)
        self.source_sb = ttk.Scrollbar(right, orient="vertical", command=self.source_box.yview)
        self.source_box.configure(yscrollcommand=self.source_sb.set)
        self._bind_text_wheel(self.source_box)
        self._show_table()

    def _build_status(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 2))
        bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bar, text="Ready", anchor="w", font=self.f_small, height=24)
        self.status.grid(row=0, column=0, sticky="ew")
        self.log_toggle = self._ghost(bar, "Hide debug log", self.on_toggle_log, 118)
        self.log_toggle.grid(row=0, column=1, padx=(8, 0))

    def _build_debug(self) -> None:
        """
        底部 Debug 面板：解析过程、告警、失败堆栈都往这里写。

        解析抛异常或文件读不出来时自动展开，方便当场定位问题；
        平时可以点状态栏右侧的按钮收起。
        """
        self.log_frame = ctk.CTkFrame(self, corner_radius=0)
        self.log_frame.grid(row=4, column=0, sticky="ew")

        head = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkLabel(head, text="Debug log", font=self.f_bold, anchor="w").pack(side="left")
        self.log_count = ctk.CTkLabel(head, text="", font=self.f_small, anchor="w")
        self.log_count.pack(side="left", padx=(10, 0))
        self._ghost(head, "Copy", self.on_copy_log, 64).pack(side="right")
        self._ghost(head, "Clear", self.on_clear_log, 64).pack(side="right", padx=(0, 6))

        holder = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        holder.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.log_box = Text(
            holder, height=7, wrap="none", bd=0, highlightthickness=0,
            font=self._cf(9, MONO), state="disabled", padx=6, pady=4, spacing1=1,
        )
        self.log_box.pack(side="left", fill="both", expand=True)
        log_sb = ttk.Scrollbar(holder, orient="vertical", command=self.log_box.yview)
        log_sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=log_sb.set)
        self.log_sb = log_sb
        self._bind_text_wheel(self.log_box)

    # ------------------------------------------------------------ 主题
    def _palette(self) -> dict[str, str]:
        if ctk.get_appearance_mode() == "Light":
            return {
                "bg": "#ebebeb", "panel": "#f7f7f8", "field": "#ffffff", "fg": "#1b1b1b",
                "head": "#dcdcdc", "sel": "#3b8ed0", "alt": "#f2f2f3", "grid": "#c9c9c9",
                "canvas": "#ffffff", "muted": "#6b7280",
                "warn": "#8a6100", "error": "#a4262c", "ok": "#1f7a45",
            }
        return {
            "bg": "#1b1b1c", "panel": "#242424", "field": "#2b2b2b", "fg": "#dce4ee",
            "head": "#333333", "sel": "#1f6aa5", "alt": "#2f2f31", "grid": "#3d3d3f",
            "canvas": "#242424", "muted": "#9aa0a6",
            "warn": "#e0a83c", "error": "#ff7b72", "ok": "#4ade80",
        }

    def _apply_theme(self) -> None:
        pal = self._palette()
        scale = getattr(self, "_ui_scale", 1.0)
        # ttk / tk.Text 不受 CTk 缩放影响，字号要自己按 DPI 放大；这里同样落到
        # **整数像素**（负号 = 像素），免得 Linux 上由 Xft 去栅格化小数磅值
        body_px = max(11, int(round(max(9, int(round(10 * scale)))
                                    * getattr(self, "_px_per_pt", 1.0))))
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "RAL.Treeview", background=pal["field"], fieldbackground=pal["field"],
            foreground=pal["fg"], rowheight=max(20, int(round(23 * scale))), borderwidth=0,
            font=(FONT, -body_px),
        )
        style.map("RAL.Treeview", background=[("selected", pal["sel"])],
                  foreground=[("selected", "#ffffff")])
        style.configure(
            "RAL.Treeview.Heading", background=pal["head"], foreground=pal["fg"],
            relief="flat", font=(FONT, -body_px, "bold"), padding=(4, 4),
        )
        style.map("RAL.Treeview.Heading", background=[("active", pal["sel"])])
        style.configure(
            "RAL.Vertical.TScrollbar", background=pal["head"], troughcolor=pal["panel"],
            bordercolor=pal["panel"], arrowcolor=pal["fg"],
        )
        style.configure(
            "RAL.Horizontal.TScrollbar", background=pal["head"], troughcolor=pal["panel"],
            bordercolor=pal["panel"], arrowcolor=pal["fg"],
        )
        # 视图分段按钮：CustomTkinter 里内嵌的按钮不会跟着换外观模式刷新颜色
        # （文字色一直停留在创建时那一套），所以这里每换一次主题就显式刷一遍
        self.segment.configure(
            fg_color=pal["head"], selected_color=pal["sel"],
            selected_hover_color=pal["sel"], unselected_color=pal["head"],
            unselected_hover_color=pal["grid"], text_color=pal["fg"],
            text_color_disabled=pal["muted"],
        )
        self._table_parts[0].configure(style="RAL.Vertical.TScrollbar")
        self._table_parts[1].configure(style="RAL.Horizontal.TScrollbar")
        self._wall_parts[0].configure(style="RAL.Vertical.TScrollbar")
        self._wall_parts[1].configure(style="RAL.Horizontal.TScrollbar")
        # 单寄存器位图的竖 / 横滚动条（横向那条是拉长功能带来的）
        self.bm_parts[0].configure(style="RAL.Vertical.TScrollbar")
        self.bm_parts[1].configure(style="RAL.Horizontal.TScrollbar")
        self.tree.configure(style="RAL.Treeview")
        self.table.configure(style="RAL.Treeview")
        self.canvas.configure(background=pal["canvas"])
        self.wall_canvas.configure(background=pal["canvas"])
        # Debug 面板：tk.Text 不受 CTk 主题影响，颜色得自己刷
        self.log_sb.configure(style="RAL.Vertical.TScrollbar")
        self.log_box.configure(background=pal["field"], foreground=pal["fg"],
                               selectbackground=pal["sel"], insertbackground=pal["fg"])
        self.log_box.tag_configure("ts", foreground=pal["muted"])
        self.log_box.tag_configure("debug", foreground=pal["muted"])
        self.log_box.tag_configure("info", foreground=pal["fg"])
        self.log_box.tag_configure("warn", foreground=pal["warn"])
        self.log_box.tag_configure("error", foreground=pal["error"])
        # 源码视图同样是原生 tk.Text，颜色得自己刷
        self.source_sb.configure(style="RAL.Vertical.TScrollbar")
        self.source_box.configure(background=pal["field"], foreground=pal["fg"],
                                  selectbackground=pal["sel"], insertbackground=pal["fg"])
        self.render_bitmap()
        self.render_all_bitmaps()

    def on_appearance(self, choice: str) -> None:
        ctk.set_appearance_mode("light" if choice == "Light" else "dark")
        self._apply_theme()

    # ------------------------------------------------------------ Debug 面板
    def _crumb(self, step: str) -> None:
        """
        把「当前走到哪一步」追加到临时跟踪文件并立刻落盘。

        段错误发生在 C 层（Tk / X11 / FreeType），Python 的 try/except 拦不住，Debug
        面板里的内容也来不及刷新甚至根本写不进去 —— 这个文件是崩溃后唯一的现场。
        只写一行几十字节，平时开销可以忽略。
        """
        try:
            if TRACE_FILE.exists() and TRACE_FILE.stat().st_size > _TRACE_MAX_BYTES:
                TRACE_FILE.write_text("", encoding="utf-8")
            with open(TRACE_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {step}\n")
        except OSError:
            pass

    def report_callback_exception(self, exc, val, tb) -> None:
        """
        覆盖 Tk 默认的「回调里抛异常只往 stderr 打一行」。

        异常写进 Debug 面板 + 跟踪文件，这样从截图里也能看到出错原因。
        """
        text = "".join(traceback.format_exception(exc, val, tb)).rstrip()
        last = text.splitlines()[-1] if text else "?"
        self._crumb(f"Tk callback raised: {last[:160]}")
        print(text, file=sys.stderr)      # 终端里跑时也要看得见，不能只进面板
        try:
            self._log("ERROR", "Tk callback raised an exception:\n" + text)
        except Exception:                # 面板本身出问题也不能再炸一次
            print(text, file=sys.stderr)

    def _log(self, level: str, message: str) -> None:
        """往底部 Debug 面板写一条带时间戳的记录（WARN / ERROR 同时打印到控制台）。"""
        tag = _LOG_TAG.get(level, "info")
        stamp = time.strftime("%H:%M:%S")
        box = self.log_box
        # 消息里可能夹带源文件内容（比如没解析出来的 offset 原文），同样要过一遍清洗
        body = tk_safe(str(message))
        box.configure(state="normal")
        for i, line in enumerate(body.splitlines() or [""]):
            if i == 0:
                box.insert("end", f"{stamp} ", "ts")
                box.insert("end", f"{level:<5} ", tag)
            else:
                box.insert("end", " " * 15, "ts")   # 续行与首行正文对齐
            box.insert("end", line + "\n", tag)
        if int(box.index("end-1c").split(".")[0]) > _LOG_MAX_LINES:
            box.delete("1.0", f"end-{_LOG_MAX_LINES}lines")
        box.configure(state="disabled")
        box.see("end")
        if level in self.log_counts:
            self.log_counts[level] += 1
            self._update_log_count()
        if level == "ERROR":
            self.set_log_visible(True)              # 出错就把面板顶出来
        if level in ("WARN", "ERROR"):
            print(f"[{level.lower()}] {message}")

    def _update_log_count(self) -> None:
        warn, err = self.log_counts["WARN"], self.log_counts["ERROR"]
        self.log_count.configure(text=f"{warn} warning(s) · {err} error(s)")

    def set_log_visible(self, visible: bool) -> None:
        self.log_open = visible
        if visible:
            self.log_frame.grid(row=4, column=0, sticky="ew")
            self.log_toggle.configure(text="Hide debug log")
        else:
            self.log_frame.grid_remove()
            self.log_toggle.configure(text="Show debug log")

    def on_toggle_log(self) -> None:
        self.set_log_visible(not self.log_open)

    def on_clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.log_counts = {"WARN": 0, "ERROR": 0}
        self._update_log_count()
        self._log("INFO", "Debug log cleared")

    def on_copy_log(self) -> None:
        text = self.log_box.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self._say(f"Debug log copied to clipboard ({len(text.splitlines())} lines)")

    # ------------------------------------------------------------ 数据加载
    def on_open(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select RAL model file(s)",
            filetypes=[("SystemVerilog", "*.sv *.svh *.v"), ("All files", "*.*")],
        )
        if paths:
            self._load(list(paths), append=False)

    def on_append(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add RAL model file(s)",
            filetypes=[("SystemVerilog", "*.sv *.svh *.v"), ("All files", "*.*")],
        )
        if paths:
            self._load(list(paths), append=True)

    def on_example(self) -> None:
        if EXAMPLE.exists():
            self._load([str(EXAMPLE)], append=False)
        else:
            self._log("ERROR", f"Sample file not found: {EXAMPLE}")
            self._say(f"Sample file not found: {EXAMPLE}", "error")

    def on_clear(self) -> None:
        self.result = ParseResult()
        self.scope = {"file": None, "block": None, "reg": None}
        self.focus_reg = None
        self.search.delete(0, "end")
        self.query = ""
        self._rebuild_all()
        self._log("INFO", "Cleared the loaded model")
        self._say("Cleared")

    def _load(self, paths: list[str], append: bool, quiet: bool = False) -> None:
        existing = list(self.result.files) if append else []
        merged = existing + [p for p in paths if p not in existing]
        if not merged:
            if not quiet:
                self._say("No file selected")
            self._rebuild_all()
            return
        self._log("INFO", f"Parsing {len(merged)} file(s) ({'append' if append else 'replace'}): "
                          + ", ".join(Path(p).name for p in merged))
        started = time.perf_counter()
        try:
            result = parse_files(merged)
        except Exception as exc:
            # 解析器自身异常不应让界面挂掉，但必须把完整堆栈留在 Debug 面板里
            detail = traceback.format_exc()
            self._log("ERROR", f"Parse failed: {exc.__class__.__name__}: {exc}")
            self._log("ERROR", detail.rstrip())
            self._say(f"Parse failed: {exc.__class__.__name__}: {exc} (see the Debug log)", "error")
            return
        if result.errors and not result.files and self.result.files:
            # 一个文件都没读进来：保留原来已加载的模型，别把界面清空
            for err in result.errors:
                self._log("ERROR", err)
            self._log("ERROR", "No input file could be read — keeping the previously loaded model")
            self._say(f"Parse failed: {result.errors[0]} (see the Debug log)", "error")
            return
        self.result = result
        elapsed = (time.perf_counter() - started) * 1000.0
        self.scope = {"file": None, "block": None, "reg": None}
        self.focus_reg = self.result.all_regs[0] if self.result.all_regs else None
        self._rebuild_all()

        for warn in self.result.warnings:
            self._log("WARN", warn)
        for err in self.result.errors:
            self._log("ERROR", err)
        self._log("INFO", f"Parsed in {elapsed:.0f} ms · {self.result.summary()}")
        if self.result.macros:
            # 宏会影响数值（比如 map 基地址 `` `UVM_REG_ADDR_WIDTH'h4000 ``），列出来便于排查
            items = [f"{k}={v}" for k, v in sorted(self.result.macros.items())]
            extra = f" (+{len(items) - 12} more)" if len(items) > 12 else ""
            self._log("DEBUG", "macros: " + ", ".join(items[:12]) + extra)

        msg = self.result.summary()
        if self.result.errors:
            msg += f" · {len(self.result.errors)} error(s) — see the Debug log"
            self._say(msg, "error")
        elif self.result.warnings:
            msg += f" · {len(self.result.warnings)} warning(s) — see the Debug log"
            self._say(msg, "warn")
        else:
            self._say(msg)

    # ------------------------------------------------------------ 刷新界面
    def _rebuild_all(self) -> None:
        self._build_tree()
        self.refresh()

    def _build_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._sel_meta.clear()
        res = self.result
        if not res.files:
            return
        for path in res.files:
            name = Path(path).name
            fnode = self.tree.insert("", "end", text=f"📄 {name}", open=True)
            self._sel_meta[fnode] = ("file", path, None)
            for blk in (b for b in res.blocks if b.file == name):
                label = f"▦ {blk.cls}"
                if blk.is_abstract:
                    label += "  (abstract base class)"
                bnode = self.tree.insert(fnode, "end", text=label, open=True)
                self._sel_meta[bnode] = ("block", blk.cls, None)
                for mem in blk.mems:
                    mnode = self.tree.insert(
                        bnode, "end",
                        text=f"▤ {mem.name}  @{mem.offset_text or '—'}  {mem.size_text}",
                    )
                    self._sel_meta[mnode] = ("mem", blk.cls, mem.name)
                for reg in blk.regs:
                    off = self._off_text(reg)
                    rnode = self.tree.insert(
                        bnode, "end",
                        text=f"{off}  {reg.display_name}  ({len(reg.fields)}F {reg.effective_access or '—'})",
                    )
                    self._sel_meta[rnode] = ("reg", blk.cls, reg.inst_name)
            if res.loose_regs:
                lnode = self.tree.insert(fnode, "end", text=f"⚠ {LOOSE_BLOCK}", open=True)
                self._sel_meta[lnode] = ("block", LOOSE_BLOCK, None)
                for reg in res.loose_regs:
                    rnode = self.tree.insert(lnode, "end", text=f"{reg.display_name}  {reg.cls}")
                    self._sel_meta[rnode] = ("reg", LOOSE_BLOCK, reg.inst_name)

    def refresh(self) -> None:
        """按当前视图重建表格。"""
        view = self.view_var.get()
        if view == "Bitmap":
            self._show_bitmap()
            return
        if view == "All Bitmaps":
            self._show_all_bitmaps()
            return
        if view == "Source":
            self._show_source()
            return
        self._show_table()
        self._configure_columns(view)
        rows, metas = self._collect_rows(view)
        if view in self._sort and rows:
            col, desc = self._sort[view]
            idx = [c[0] for c in COLUMNS[view]].index(col)
            pairs = sorted(zip(rows, metas), key=lambda r: _sort_key(r[0][idx]), reverse=desc)
            rows = [p[0] for p in pairs]
            metas = [p[1] for p in pairs]
        self._rows = rows
        self._row_meta = metas
        self.table.delete(*self.table.get_children())
        for i, (row, meta) in enumerate(zip(rows, metas)):
            tag = "odd" if i % 2 else "even"
            if meta.get("kind") == "mem":
                tag += "mem"
            self.table.insert("", "end", values=row, tags=(tag,))
        pal = self._palette()
        self.table.tag_configure("odd", background=pal["alt"])
        self.table.tag_configure("even", background=pal["field"])
        self.table.tag_configure("mem", foreground=pal["muted"])
        covered = f"{len(rows)} row(s)"
        if self.query or self.scope["block"] or self.scope["reg"]:
            covered += " (filtered)"
        self._say(f"{self.result.summary()} · view [{view}] · {covered}")

    def _collect_rows(self, view: str) -> tuple[list[list[str]], list[dict]]:
        """按视图类型 + 当前作用域 + 搜索词生成行数据。"""
        rows: list[list[str]] = []
        metas: list[dict] = []

        def keep(*fields: str) -> bool:
            if not self.query:
                return True
            blob = " ".join(fields).lower()
            return all(k in blob for k in self.query.split())

        if view == "Registers":
            for blk in self.result.blocks:
                for reg in blk.regs:
                    if not self._in_scope(blk.cls, reg.inst_name):
                        continue
                    cells = {
                        "block": blk.cls,
                        "inst": reg.inst_name,
                        "cls": reg.cls,
                        "base": reg.base,
                        "offset": self._off_text(reg),
                        "access": reg.effective_access,
                        "width": str(reg.width),
                        "reset": reg.reset_text or "—",
                        "nfield": str(len(reg.fields)),
                        "uncovered": str(reg.uncovered_bits),
                        "hdl": reg.hdl_path,
                        "doc": reg.doc,
                        "loc": f"{reg.file}:{reg.line}",
                    }
                    if not keep(*cells.values()):
                        continue
                    rows.append([cells[c[0]] for c in COLUMNS[view]])
                    metas.append({"kind": "reg", "reg": reg, "block": blk})
            for reg in self.result.loose_regs:
                if not self._in_scope(LOOSE_BLOCK, reg.inst_name):
                    continue
                cells = {
                    "block": LOOSE_BLOCK, "inst": reg.inst_name, "cls": reg.cls, "base": reg.base,
                    "offset": "—", "access": reg.effective_access, "width": str(reg.width),
                    "reset": reg.reset_text or "—", "nfield": str(len(reg.fields)),
                    "uncovered": str(reg.uncovered_bits), "hdl": reg.hdl_path, "doc": reg.doc,
                    "loc": f"{reg.file}:{reg.line}",
                }
                if not keep(*cells.values()):
                    continue
                rows.append([cells[c[0]] for c in COLUMNS[view]])
                metas.append({"kind": "reg", "reg": reg, "block": None})

        elif view == "Fields":
            for blk in self.result.blocks:
                for reg in blk.regs:
                    for fld in reg.fields:
                        self._field_row(rows, metas, blk.cls, reg, fld, keep, view)
            for reg in self.result.loose_regs:
                for fld in reg.fields:
                    self._field_row(rows, metas, LOOSE_BLOCK, reg, fld, keep, view)

        elif view == "Blocks":
            for blk in self.result.blocks:
                if not self._in_scope(blk.cls, None):
                    continue
                cells = {
                    "cls": blk.cls, "base": blk.base, "nreg": str(len(blk.regs)),
                    "nmem": str(len(blk.mems)), "nfield": str(blk.field_count),
                    "map": blk.map_text, "hdl": blk.hdl_path_root,
                    "locked": "yes" if blk.locked else "no",
                    "sub": " / ".join(blk.subblocks + blk.submaps),
                    "doc": blk.doc, "loc": f"{blk.file}:{blk.line}",
                }
                if not keep(*cells.values()):
                    continue
                rows.append([cells[c[0]] for c in COLUMNS[view]])
                metas.append({"kind": "block", "block": blk})
            if self.result.loose_regs and self._in_scope(LOOSE_BLOCK, None):
                cells = {
                    "cls": LOOSE_BLOCK, "base": "—", "nreg": str(len(self.result.loose_regs)),
                    "nmem": "0", "nfield": str(sum(len(r.fields) for r in self.result.loose_regs)),
                    "map": "—", "hdl": "—", "locked": "—", "sub": "—",
                    "doc": "register classes not instantiated by any block", "loc": "—",
                }
                if keep(*cells.values()):
                    rows.append([cells[c[0]] for c in COLUMNS[view]])
                    metas.append({"kind": "block", "block": None})

        return rows, metas

    def _field_row(self, rows, metas, block_name, reg, fld: FieldDef, keep, view) -> None:
        if not self._in_scope(block_name, reg.inst_name):
            return
        cells = {
            "block": block_name, "reg": reg.display_name, "name": fld.name,
            "bits": fld.bit_range, "width": str(fld.width), "lsb": str(fld.lsb),
            "access": fld.access, "reset": fld.reset_raw or "—",
            "flags": fld.flags or "—", "doc": fld.doc, "loc": f"{fld.file}:{fld.line}",
        }
        if not keep(*cells.values()):
            return
        rows.append([cells[c[0]] for c in COLUMNS[view]])
        metas.append({"kind": "field", "field": fld, "reg": reg})

    def _in_scope(self, block_name: str | None, reg_name: str | None) -> bool:
        sc = self.scope
        if sc["file"]:
            names = {Path(p).name for p in self.result.files}
            if block_name == LOOSE_BLOCK:
                if not any(Path(r.file).name in names for r in self.result.loose_regs):
                    return False
            elif not any(
                b.cls == block_name and b.file in names for b in self.result.blocks
            ):
                return False
        if sc["block"] and sc["block"] != block_name:
            return False
        if sc["reg"]:
            if reg_name != sc["reg"]:
                return False
        return True

    # ------------------------------------------------------------ 视图切换
    def _show_table(self) -> None:
        self._hide_all()
        self.table.grid(row=0, column=0, sticky="nsew")
        self._table_parts[0].grid(row=0, column=1, sticky="ns")
        self._table_parts[1].grid(row=1, column=0, sticky="ew")

    def _show_bitmap(self) -> None:
        self._hide_all()
        self.bitmap_frame.grid(row=0, column=0, sticky="nsew")
        self.render_bitmap()
        reg = self.focus_reg
        self._say(
            f"{self.result.summary()} · view [Bitmap]"
            + (f" · {reg.block_name} › {reg.display_name} ({reg.cls})" if reg else " · no register selected")
        )

    def _show_all_bitmaps(self) -> None:
        """全部位图：把当前作用域内所有寄存器画在同一个可滚动画布上。"""
        self._hide_all()
        self.all_frame.grid(row=0, column=0, sticky="nsew")
        n = len(self._bitmap_regs())
        self.wall_hint.configure(
            text=f"{n} register(s) · LSB on the right (same as the Bitmap view) · "
                 f"hover for field details · click to focus"
        )
        self.render_all_bitmaps()
        filtered = bool(self.query or self.scope["block"] or self.scope["reg"])
        self._say(
            f"{self.result.summary()} · view [All Bitmaps] · {n} register(s)"
            + (" (filtered)" if filtered else "")
        )

    def _show_source(self) -> None:
        self._crumb("source: show begin")
        self._hide_all()
        self.source_box.grid(row=0, column=0, sticky="nsew")
        self.source_sb.grid(row=0, column=1, sticky="ns")
        self._crumb("source: box gridded")
        self.render_source()
        self._say(f"{self.result.summary()} · view [Source]")

    def _hide_all(self) -> None:
        self.table.grid_forget()
        for part in self._table_parts:
            part.grid_forget()
        self.bitmap_frame.grid_forget()
        self.all_frame.grid_forget()
        self.source_box.grid_forget()
        self.source_sb.grid_forget()

    def on_view(self, view: str) -> None:
        self.refresh()

    def goto_view(self, view: str) -> None:
        """程序化切视图（``SegmentedButton.set`` 不会回调 ``command``，故手动触发）。"""
        self.view_var.set(view)
        self.segment.set(view)
        self.refresh()

    def _configure_columns(self, view: str) -> None:
        cols = COLUMNS[view]
        scale = getattr(self, "_ui_scale", 1.0)
        self.table.configure(columns=[c[0] for c in cols])
        for cid, title, width, anchor in cols:
            self.table.heading(cid, text=title, command=lambda c=cid: self.on_sort(c))
            self.table.column(
                cid, width=int(round(width * scale)), minwidth=int(round(38 * scale)),
                anchor=anchor, stretch=(cid in ("doc", "map", "sub", "cls")),
            )

    def on_sort(self, col: str) -> None:
        view = self.view_var.get()
        cur_col, desc = self._sort.get(view, ("", False))
        self._sort[view] = (col, not desc if cur_col == col else False)
        self.refresh()

    # ------------------------------------------------------------ 选择联动
    def on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        kind, block, reg = self._sel_meta.get(sel[0], (None, None, None))
        if kind == "file":
            self.scope = {"file": block, "block": None, "reg": None}
        elif kind == "block":
            self.scope = {"file": None, "block": block, "reg": None}
        elif kind == "reg":
            self.scope = {"file": None, "block": block, "reg": reg}
            target = self._find_reg(block, reg)
            if target:
                self.focus_reg = target
        elif kind == "mem":
            self.scope = {"file": None, "block": block, "reg": None}
        self.refresh()

    def on_table_select(self, _event=None) -> None:
        sel = self.table.selection()
        if not sel:
            return
        idx = self.table.index(sel[0])
        if idx >= len(self._row_meta):
            return
        meta = self._row_meta[idx]
        if meta.get("kind") in ("reg", "field"):
            reg = meta.get("reg")
            if isinstance(reg, RegDef):
                self.focus_reg = reg
                if self.view_var.get() in ("Bitmap", "Source"):
                    self.refresh()
        self._describe(meta)

    @staticmethod
    def _off_text(reg: RegDef, width: int = 2, limit: int = 14) -> str:
        """
        offset 的显示文本。

        解析成功给 ``0x…``；**没解析出来但模型里确实写了**（字面量/宏算不出来）
        就把原文照抄出来，前面加个 ``?`` —— 否则界面只剩一个 "——"，用户没法
        区分"这个寄存器没有 add_reg"和"有 add_reg 但 offset 没解析出来"，
        而这两种情况的排查方向完全不一样。
        """
        if reg.offset is not None:
            return f"0x{reg.offset:0{width}X}"
        raw = (reg.offset_raw or "").strip()
        if raw:
            return "?" + (raw if len(raw) <= limit else raw[:limit - 1] + "…")
        return "——"

    def _describe(self, meta: dict) -> None:
        kind = meta.get("kind")
        if kind == "reg":
            reg: RegDef = meta["reg"]
            offset = reg.offset_raw or ("—" if reg.offset is None else f"0x{reg.offset:X}")
            self._say(
                f"{reg.block_name} · {reg.display_name} ({reg.cls})  offset={offset}"
                f"  access={reg.effective_access}  width={reg.width}  reset={reg.reset_text or '—'}"
                f"  fields={len(reg.fields)}  reserved bits={reg.uncovered_bits}"
                + (f"  doc={reg.doc}" if reg.doc else "")
            )
        elif kind == "field":
            fld: FieldDef = meta["field"]
            reg: RegDef = meta["reg"]
            self._say(
                f"{reg.display_name}.{fld.name}  {fld.bit_range}  {fld.width}bit  {fld.access}"
                f"  reset={fld.reset_raw or '—'}  {fld.flags}"
                + (f"  doc={fld.doc}" if fld.doc else "")
            )
        elif kind == "block":
            blk: BlockDef | None = meta["block"]
            if blk is None:
                self._say(f"{LOOSE_BLOCK}: {len(self.result.loose_regs)} register class(es)")
            else:
                self._say(
                    f"{blk.cls} extends {blk.base or '—'}  regs={len(blk.regs)}  mems={len(blk.mems)}"
                    f"  fields={blk.field_count}  map={blk.map_text or '—'}"
                    f"  HDL root={blk.hdl_path_root or '—'}"
                )

    # ------------------------------------------------------------ 位图
    def _draw_strip(self, cv: Canvas, reg: RegDef, left: float, top: float,
                    cell: float, cell_h: float, pal: dict, *, big: bool) -> dict:
        """
        画一条寄存器位带，返回几何信息（供悬停 / 点击使用）。

        **低位在右**：bit0 永远在最右边（``x = left + (bits-1-bit) * cell``），
        ``big`` 为单寄存器大图（格子更高、写字段权限和位号），否则为「全部位图」的紧凑模式。

        两个性能/观感上的要点：

        * **每段只画一个矩形**。早先按「每个 bit 一个矩形 + 一个位号」画，
          54 个 32bit 寄存器要生成 3888 个图元，重绘 110ms 以上、滚轮一拖就卡；
          现在先铺一层底色（保留位），再按字段段盖上去，同样一张图只要几百个图元。
        * **坐标一律取整**。画布上是物理像素，``cell`` 若是 21.03 这种小数，
          相邻格子会落在半像素上，ClearType 会把文字渲染出灰边，看起来就是"发虚"。
        """
        bits = max(reg.width or 0, 1)
        left, top = float(round(left)), float(round(top))
        cell, cell_h = float(max(1, round(cell))), float(max(1, round(cell_h)))

        owner: dict[int, FieldDef] = {}
        for fld in reg.fields:
            for bit in range(fld.lsb, min(bits, fld.lsb + max(fld.width, 1))):
                owner[bit] = fld

        # 底色 = 没有被字段覆盖的保留位
        cv.create_rectangle(left, top, left + bits * cell, top + cell_h,
                            fill=pal["field"], outline=pal["grid"], tags=("fld",))
        for fld in reg.fields:
            if fld.lsb >= bits:
                continue
            span = min(fld.width, bits - fld.lsb)
            x0 = left + (bits - fld.lsb - span) * cell
            cv.create_rectangle(x0, top, x0 + span * cell - 1, top + cell_h,
                                fill=access_fill(fld.access), outline=pal["canvas"],
                                width=2, tags=("fld",))

        named = 0
        num_size = self._num_font_size(cell) if big else 0
        num_h = self._lh(num_size, MONO) if num_size else 0

        # 位号：只有大图才写。紧凑模式的格子高度塞不下第二行字（200% 缩放下
        # 8~9 磅字的行高就有 21~24px，而格子只有 30px 高），硬画就会和字段名叠成一片。
        if num_size:
            for bit in range(bits):
                cv.create_text(
                    left + (bits - 1 - bit) * cell + cell / 2,
                    top + cell_h - num_h / 2 - 2, text=str(bit),
                    fill="#f2f5ff" if owner.get(bit) else pal["muted"],
                    font=self._cf(num_size, MONO), tags=("fld",),
                )
        if big and cell >= 6:
            # 大图里保留位也要有格子线：底色是一整块，没线就看不出 bit 边界。
            # 只在「相邻两个 bit 都没有字段」的地方补一条竖线，图元数仍然很少。
            for bit in range(1, bits):
                if owner.get(bit) is None and owner.get(bit - 1) is None:
                    x = left + (bits - bit) * cell
                    cv.create_line(x, top, x, top + cell_h,
                                   fill=pal["grid"], tags=("fld",))

        # 宽字段把名字写在色块里；放不下就交给图例，避免文字互相压叠
        for fld in reg.fields:
            if fld.lsb >= bits:
                continue
            span = min(fld.width, bits - fld.lsb)
            span_px = span * cell - 4
            # 字段最低位所在格子的左边缘 …… 最高位所在格子的右边缘
            fx0 = left + (bits - fld.lsb - span) * cell
            fx1 = left + (bits - fld.lsb) * cell
            cx = (fx0 + fx1) / 2          # 越靠左的格子 bit 号越大，故最高位在左
            if big:
                size = 10 if span_px >= 76 else 9
                # 连三个字都排不下就没有认读价值，交给图例
                if span_px < self._tw("MMM", size):
                    continue
                name_h = self._lh(size)
                acc_h = self._lh(8)
                # 位号占了格子最下面一条，字段名和权限在它上面的区域里整体居中
                band = cell_h - num_h - 6
                show_acc = span_px >= 60 and band >= name_h + acc_h
                block_h = name_h + (acc_h if show_acc else 0)
                y0 = top + (band - block_h) / 2
                cv.create_text(cx, round(y0 + name_h / 2),
                               text=self._fit_text(fld.name, span_px, size), fill="#ffffff",
                               font=self._cf(size), tags=("fld",))
                if show_acc:
                    cv.create_text(cx, round(y0 + name_h + acc_h / 2), text=fld.access,
                                   fill="#e6ebff", font=self._cf(8, MONO), tags=("fld",))
            else:
                size = 9 if span_px >= 52 else 8
                if span_px < self._tw("MMM", size):
                    continue
                cv.create_text(cx, top + cell_h / 2,
                               text=self._fit_text(fld.name, span_px, size),
                               fill="#ffffff", font=self._cf(size), tags=("fld",))
            named += 1

        return {"reg": reg, "left": left, "top": top, "cell": cell, "h": cell_h,
                "bits": bits, "owner": owner, "named": named}

    def _draw_bit_axis(self, cv: Canvas, left: float, top: float, cell: float,
                       bits: int, pal: dict, *, font_size: int) -> None:
        """
        位序标注：**左端标 MSB、右端标 LSB，各自锚在自己的端点上**。

        早先版本写成一行左对齐的 ``bit31 ← 高位    低位 → bit0``，后半句靠空格
        顶格排布，会飘到位带中段（也就是高位那一侧）的正下方，看起来像高低位标反了。
        现在两端各用 ``anchor="nw"`` / ``anchor="ne"`` 钉死，文字位置与含义永远一致。
        """
        left, top = float(round(left)), float(round(top))
        right = left + bits * cell
        cv.create_text(left, top, anchor="nw", text=f"MSB  bit{bits - 1}",
                       fill=pal["muted"], font=self._cf(font_size), tags=("axis",))
        # 两端文字都放得下才画右端，免得两段叠在一起（宽度按实际字号量）
        if right - left >= (self._tw(f"MSB  bit{bits - 1}", font_size)
                            + self._tw("LSB  bit0", font_size) + 16):
            cv.create_text(right, top, anchor="ne", text="LSB  bit0",
                           fill=pal["muted"], font=self._cf(font_size), tags=("axis",))

    def _draw_field_legend(self, cv: Canvas, reg: RegDef, left: float, top: float,
                           width_px: float, pal: dict, *, big: bool) -> float:
        """
        字段图例（格子放不下字段名时靠它辨认），超宽自动换行；返回下一行 y。

        顺序**从高位到低位**（``reg.fields`` 是按 lsb 升序的，这里反过来），
        这样图例从左往右读与位带从左往右看（MSB → LSB）方向一致，
        不会出现「图例第一个是最低位、图里最左边却是最高位」的错位感。
        """
        size = 10 if big else 9
        # chip 高度跟着字体的真实行高走：写死 20px 的话，200% 缩放下 9 磅字就有
        # 24px 高，会撑出 chip 去压到下一行图例上
        line_h = self._lh(size)
        chip_h = float(line_h + 8)
        text_dy = chip_h / 2
        left, top = float(round(left)), float(round(top))
        x, y = left, top
        for fld in reversed(reg.fields):
            label = f"{fld.name} {fld.bit_range} {fld.access}"
            chip_w = 24 + self._tw(label, size) + 10
            if x > left and x + chip_w > width_px - 12:
                x, y = left, y + chip_h
            cv.create_rectangle(x, round(y + 4), x + 12, round(y + 4 + line_h),
                                fill=access_fill(fld.access), outline="", tags=("fld",))
            cv.create_text(round(x + 18), round(y + text_dy), anchor="w", text=label,
                           fill=pal["fg"], font=self._cf(size), tags=("fld", "legend"))
            x += chip_w
        return y + chip_h

    def render_bitmap(self) -> None:
        if self.view_var.get() != "Bitmap":
            return
        pal = self._palette()
        cv = self.canvas
        keep_x = cv.xview()[0]           # 缩放 / 换寄存器后停在原处，别弹回最左边
        cv.delete("all")
        cv.configure(background=pal["canvas"])
        reg = self.focus_reg
        if reg is None:
            self.bitmap_hint.configure(text="No register selected")
            self._set_legend(self.bitmap_legend, "")
            self._update_zoom_label(self.zoom_label, 0)
            self._overflow_x = False
            self._bitmap_geom = None
            self._bitmap_named = 0
            return
        width_px = max(cv.winfo_width(), 240)
        self._wrap_label(self.bitmap_hint, width_px)
        self.bitmap_hint.configure(
            text=f"{reg.block_name} › {reg.display_name}  ·  {reg.cls}  ·  "
                 f"offset {'—' if reg.offset is None else hex(reg.offset)}  ·  "
                 f"{reg.width} bit  ·  access {reg.effective_access or '—'}  ·  "
                 f"reset {reg.reset_text or '—'}"
        )
        # 图例固定两行：悬停时也补一行，高度不变就不会因为换行把画布挤得抖动
        self._set_legend(self.bitmap_legend, self._legend_line(
            f"reset value {reg.reset_text or '—'}   │   hover a bit cell for field details",
            "colors " + "  ".join(f"■{a}" for a in ("RW", "RO", "WO", "W1C", "other")),
            width_px=width_px, size=10,
        ))

        bits = max(reg.width or 0, 1)
        # 自适应格宽（窄窗口下会被压到十几像素）再乘缩放档位
        # 内容宽度 = 12 的左边距 + bits*cell + 16 的右边距（与下面的 content_w 一致）
        auto = self._fit_cell(width_px, bits, 12, 16, cap=44, floor=8)
        # 取整：_draw_strip 内部也会取整，这里先取整免得「内容宽度」和实际画出来的
        # 位带差几个像素（高倍率下悬停命中会偏、横向滚动条也会多出无用的空白）
        cell = float(round(self._cell_px(auto)))
        left, top = 12.0, 18.0
        # 格高由「字段名 + 位号 + 权限」三行字的真实行高决定（见 _lh）
        cell_h = float(max(40, self._lh(10) + self._lh(8) * 2 + 14))
        strip = self._draw_strip(cv, reg, left, top, cell, cell_h, pal, big=True)
        self._bitmap_named = strip["named"]     # 拉长后是否有更多字段名挤进格子（自测用）
        axis_y = top + cell_h + 8
        self._draw_bit_axis(cv, left, axis_y, cell, bits, pal, font_size=9)
        legend_y = axis_y + self._lh(9) + 6
        # 拉长后内容比视口宽 —— 把滚动区域放宽，横向滚动条才有东西可滚
        content_w = max(width_px, left + bits * cell + 16)
        legend_end = self._draw_field_legend(cv, reg, left, legend_y, content_w, pal, big=True)
        self._bitmap_geom = (left, top, cell, cell_h, bits)
        cv.configure(scrollregion=(0, 0, content_w, max(legend_end + 8, cv.winfo_height())))
        cv.xview_moveto(keep_x)          # 缩放后停在原来的位置，别弹回最左边
        self._overflow_x = content_w > width_px + 1
        self._update_zoom_label(self.zoom_label, cell)

    def on_canvas_motion(self, event) -> None:
        geom = getattr(self, "_bitmap_geom", None)
        reg = self.focus_reg
        if not geom or reg is None:
            return
        left, top, cell, cell_h, bits = geom
        # 横向能滚动之后 event.x 是「窗口内」坐标，而画布上的图形是按「画布」坐标
        # 摆的，两者差了 xview 的偏移量 —— 必须换算，否则拉长后悬停全错位
        x = self.canvas.canvasx(event.x)
        if not (top <= event.y <= top + cell_h and left <= x):
            return
        bit = bits - 1 - int((x - left) // cell)
        tip = self._fit_text(self._bit_tip(reg, bit), max(self.canvas.winfo_width(), 240) - 12, 10)
        self._set_legend(self.bitmap_legend, tip + "\n")   # 补一行，与静态图例等高

    def _bit_tip(self, reg: RegDef, bit: int) -> str:
        """某个 bit 的悬停提示（属于哪个字段 / 是否为保留位）。"""
        for fld in reg.fields:
            if fld.lsb <= bit <= fld.msb:
                return (f"{fld.name} {fld.bit_range}  {fld.width}bit  {fld.access}  "
                        f"reset={fld.reset_raw or '—'}  {fld.flags}"
                        + (f"  {fld.doc}" if fld.doc else ""))
        return (f"bit{bit} undefined (reserved)  {reg.cls} is {reg.width} bit wide, "
                f"fields cover {reg.covered_bits} bit")

    # ------------------------------------------------------------ 位图横向拉长
    def _build_zoom_bar(self, parent) -> ctk.CTkLabel:
        """
        位图缩放条 ``-  ×1.5 · 51px/bit  +  Fit``（Bitmap / All Bitmaps 各一条，档位共享）。

        返回中间那个标签：两个视图的自适应格宽不一样（单图上限 44px、多图 34px），
        所以实际像素值由各自的渲染函数算好后写进去。
        """
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=6, pady=(0, 4))
        self._ghost(bar, "-", lambda: self.on_zoom(-1), 28).pack(side="left")
        label = ctk.CTkLabel(bar, text="", font=self.f_small, anchor="w")
        label.pack(side="left", padx=6)
        self._ghost(bar, "+", lambda: self.on_zoom(1), 28).pack(side="left")
        self._ghost(bar, "Fit", self.on_zoom_fit, 46).pack(side="left", padx=(6, 0))
        return label

    def on_zoom(self, step: int) -> None:
        """横向拉长 / 收窄一档（见 :data:`ZOOM_STEPS`），拉长后可以左右滚动看全。"""
        idx = self._zoom_index() + step
        idx = max(0, min(len(ZOOM_STEPS) - 1, idx))
        if ZOOM_STEPS[idx] == self.cell_zoom:
            self._say(f"Bitmap zoom is already at the "
                      f"{'maximum' if step > 0 else 'minimum'} ({self.cell_zoom:g}×)")
            return
        self.cell_zoom = ZOOM_STEPS[idx]
        self.render_current_bitmap()
        self._say(f"Bitmap zoom ×{self.cell_zoom:g}"
                  + (" — scroll sideways (Shift+wheel or the bar underneath) to see the rest"
                     if self._overflow_x else ""))

    def on_zoom_fit(self) -> None:
        """回到「自适应铺满视口」的 1×（默认状态）。"""
        if self.cell_zoom == 1.0:
            self._say("Bitmap zoom already fits the window width")
            return
        self.cell_zoom = 1.0
        self.render_current_bitmap()
        self._say("Bitmap zoom back to 1× (fits the window width)")

    def _zoom_index(self) -> int:
        try:
            return ZOOM_STEPS.index(self.cell_zoom)
        except ValueError:                    # 理论上不会发生：档位只从 ZOOM_STEPS 取值
            return 0

    def render_current_bitmap(self) -> None:
        """只重绘当前那个位图视图（缩放时不必走整条 refresh，免得连表格一起刷）。"""
        view = self.view_var.get()
        if view == "Bitmap":
            self.render_bitmap()
        elif view == "All Bitmaps":
            self.render_all_bitmaps()

    def _cell_px(self, auto: float) -> float:
        """实际每 bit 的格宽 = 自适应值 × 缩放档位（夹在上下限之间）。"""
        return float(max(_CELL_MIN_PX, min(_CELL_MAX_PX, auto * self.cell_zoom)))

    @staticmethod
    def _fit_cell(width_px: float, bits: int, left: float, right_pad: float,
                  *, cap: float, floor: float) -> float:
        """
        1× 时的自适应格宽：让 ``left + bits*cell + right_pad`` 正好装进 ``width_px``。

        **向下取整**（不用 ``round``）：内容宽度是拿这个返回值算的，四舍五入多出来
        的那半格会让「自适应」反而比视口宽几个像素 —— 于是 1× 下就冒出一条几乎
        没用、却跟标签上那个 "(fit)" 打架的横向滚动条。

        挤不下时退回 ``floor``（6~8px/bit，再小就只剩几条看不清的竖线）：此时内容
        本来就会超出视口，横向滚动条出现是正确的 —— 总比把格子压成 1px 强。
        窄窗口（Linux 上常见 ~680px 宽的窗口）走的就是这条路。
        """
        auto = int((width_px - left - right_pad) / max(bits, 1))
        return float(max(floor, min(cap, auto)))

    def _update_zoom_label(self, label: ctk.CTkLabel, cell: float) -> None:
        """缩放条上的文字：当前档位 + 实际每 bit 多少像素。"""
        label.configure(text=f"×{self.cell_zoom:g}  ·  {round(cell)}px/bit"
                             + ("" if self.cell_zoom > 1 else "  (fit)"))

    # ------------------------------------------------------------ 全部位图
    def _bitmap_regs(self) -> list[tuple[BlockDef | None, RegDef]]:
        """当前作用域 + 搜索条件下该画位图的寄存器（block 顺序，块内按偏移）。"""
        out: list[tuple[BlockDef | None, RegDef]] = []
        for blk in self.result.blocks:
            for reg in blk.regs:
                if self._in_scope(blk.cls, reg.inst_name):
                    out.append((blk, reg))
        for reg in self.result.loose_regs:
            if self._in_scope(LOOSE_BLOCK, reg.inst_name):
                out.append((None, reg))
        if not self.query:
            return out
        keys = self.query.split()

        def hit(reg: RegDef, blk: BlockDef | None) -> bool:
            blob = " ".join((
                blk.cls if blk else LOOSE_BLOCK, reg.inst_name, reg.cls, reg.base,
                "" if reg.offset is None else f"0x{reg.offset:02X}",
                reg.effective_access, str(reg.width), reg.reset_text, reg.hdl_path,
                reg.doc, " ".join(f.name for f in reg.fields),
            )).lower()
            return all(k in blob for k in keys)

        return [(b, r) for b, r in out if hit(r, b)]

    def render_all_bitmaps(self) -> None:
        """把当前范围内的所有寄存器一次画完（纵向排列，可滚动）。"""
        if self.view_var.get() != "All Bitmaps":
            return
        pal = self._palette()
        cv = self.wall_canvas
        keep_scroll = cv.yview()[0]      # 重绘（缩放 / 点击聚焦）后停在原处，别弹回顶部
        keep_x = cv.xview()[0]
        cv.delete("all")
        cv.configure(background=pal["canvas"])
        width_px = max(cv.winfo_width(), 240)
        self._wrap_label(self.wall_hint, width_px)
        self._set_legend(self.all_legend, " ")   # 重画后悬停文字作废，顺带清掉缓存
        items = self._bitmap_regs()
        self.wall_geom = []
        if not items:
            cv.create_text(12, 14, anchor="nw",
                           text="No matching registers — check the search filter, "
                                "or pick another scope in the model tree on the left",
                           fill=pal["muted"], font=self._cf(11))
            cv.configure(scrollregion=(0, 0, width_px, 48))
            self._update_zoom_label(self.zoom_label_all, 0)
            self._overflow_x = False
            return

        # 所有寄存器共用同一个格宽，纵向就能按位对齐比对
        bits_max = max(max(r.width or 0, 1) for _b, r in items)
        left = 12.0
        gutter = 176.0 if width_px >= 720 else 0.0     # 左侧「偏移 + 名字」栏
        strip_left = left + gutter
        # 内容宽度 = strip_left + bits_max*cell + 14（与下面的 content_w 一致）
        auto = self._fit_cell(width_px, bits_max, strip_left, 14, cap=34, floor=7)
        cell = float(round(self._cell_px(auto)))      # 取整理由同 render_bitmap
        # 图例按「内容宽度」换行：拉长后内容比视口宽，按视口宽换行会白掉一大截
        legend_w = max(width_px, strip_left + bits_max * cell + 14)

        # 行高 / 轴高 / 图例高全部由字体的真实行高推出来，不写死像素 ——
        # 200% 缩放下 8 磅字就有 21px 高，原来写死的 30/18 会让每行文字互相压叠
        axis_font = 8
        cell_h = float(self._lh(9) + 8)                 # 紧凑模式格子里只有一行字段名
        axis_h = float(self._lh(axis_font) + 4)
        gutter_h = float(self._lh(10) + self._lh(8))    # 左栏是上下两行
        row_h = max(cell_h, gutter_h) if gutter else cell_h
        gap = 8.0

        y = 10.0
        for blk, reg in items:
            strip_top = y
            if reg is self.wall_active:      # 聚焦行左侧加一条高亮竖条
                cv.create_rectangle(left - 6, round(strip_top - 4), left - 2,
                                    round(strip_top + row_h + 4), fill=pal["sel"], outline="")
            if gutter:
                off = self._off_text(reg)
                cv.create_text(left, round(strip_top + 2), anchor="nw",
                               text=self._fit_text(f"{off}  {reg.display_name}", gutter - 10, 10),
                               fill=pal["fg"], font=self._cf(10))
                cv.create_text(left, round(strip_top + 2 + self._lh(10)), anchor="nw",
                               text=self._fit_text(
                                   f"{reg.effective_access or '—'} · {reg.width}bit · "
                                   f"{len(reg.fields)} field(s)", gutter - 10, 8),
                               fill=pal["muted"], font=self._cf(8))
            geom = self._draw_strip(cv, reg, strip_left, strip_top, cell, cell_h, pal, big=False)
            geom["block"] = blk
            self.wall_geom.append(geom)
            y = strip_top + row_h + 4
            self._draw_bit_axis(cv, strip_left, y, cell, geom["bits"], pal, font_size=axis_font)
            y += axis_h
            if geom["named"] < len(reg.fields):   # 有字段名没画进格子，补一行图例
                y = self._draw_field_legend(cv, reg, strip_left, y, legend_w, pal, big=False)
            y += gap

        # 拉长后内容比视口宽 —— 把滚动区域放宽，横向滚动条才有东西可滚
        content_w = max(width_px, strip_left + bits_max * cell + 14)
        cv.configure(scrollregion=(0, 0, content_w, max(y + 6, cv.winfo_height())))
        cv.yview_moveto(keep_scroll)
        cv.xview_moveto(keep_x)          # 缩放后停在原来的位置，别弹回最左边
        self._overflow_x = content_w > width_px + 1
        self._update_zoom_label(self.zoom_label_all, cell)

    def on_wall_motion(self, event) -> None:
        # 同 on_canvas_motion：横向滚动后要把窗口坐标换算回画布坐标
        x = self.wall_canvas.canvasx(event.x)
        for geom in self.wall_geom:
            if not (geom["top"] <= event.y <= geom["top"] + geom["h"]):
                continue
            reg = geom["reg"]
            width_px = max(self.wall_canvas.winfo_width(), 240) - 12
            if x < geom["left"]:
                self._set_legend(self.all_legend, f"{reg.display_name} ({reg.cls})")
                return
            bit = geom["bits"] - 1 - int((x - geom["left"]) // geom["cell"])
            if 0 <= bit < geom["bits"]:
                self._set_legend(self.all_legend,
                                 self._fit_text(self._bit_tip(reg, bit), width_px, 10))
            return

    def on_wall_click(self, event) -> None:
        """点某条位带即聚焦该寄存器，「位图」视图随后就显示它的放大图。"""
        x = self.wall_canvas.canvasx(event.x)
        for geom in self.wall_geom:
            if not (geom["top"] <= event.y <= geom["top"] + geom["h"]):
                continue
            if x >= geom["left"]:
                reg = geom["reg"]
                self.focus_reg = self.wall_active = reg
                self._say(f"Focused {reg.block_name} › {reg.display_name} ({reg.cls}) — "
                          f"switch to the Bitmap view for the enlarged layout")
                self.render_all_bitmaps()
                return

    def _bind_wheel(self, widget, handler) -> None:
        """
        绑滚轮事件，三个都绑。

        ``<MouseWheel>`` 是 **Windows / macOS 专有**的虚拟事件；X11 上根本没有它，
        滚轮是按键事件 ``<Button-4>``（上滚）/ ``<Button-5>``（下滚），而且
        ``event.delta`` 这个属性在 X11 上不存在。只绑 ``<MouseWheel>`` 的后果就是：
        Windows 上滚得好好的，Linux 上滚轮彻底没反应（只有拖右侧滚动条才行）。
        """
        widget.bind("<MouseWheel>", handler)     # Windows / macOS
        widget.bind("<Button-4>", handler)       # X11 上滚
        widget.bind("<Button-5>", handler)       # X11 下滚

    @staticmethod
    def _wheel_units(event) -> int:
        """把各平台的滚轮事件归一成「上滚为负、下滚为正」的滚动格数。"""
        num = getattr(event, "num", None)
        if num == 4:                       # X11 上滚
            return -1
        if num == 5:                       # X11 下滚
            return 1
        delta = getattr(event, "delta", 0) or 0
        if not delta:
            return 0
        # Windows 上是 ±120 的整数倍，macOS 上是若干个很小的整数
        return -1 if delta > 0 else 1

    @staticmethod
    def _shift_down(event) -> bool:
        """事件发生的那一刻 Shift 是否按着（X11 的滚轮按键事件也带 state）。"""
        return bool(getattr(event, "state", 0) & 0x0001)

    def _scroll_canvas(self, cv: Canvas, event) -> None:
        """
        画布滚轮：默认纵向，按住 Shift 变横向。

        位图横向拉长后内容比视口宽，光有底下的滚动条不够顺手 —— 滚轮 + Shift
        是各家表格 / 编辑器的通用手势。横向滚动条没东西可滚时忽略，免得手一抖
        视图莫名其妙地跑到别处。
        """
        units = self._wheel_units(event)
        if not units:
            return
        if self._shift_down(event) and self._overflow_x:
            cv.xview_scroll(units, "units")
        else:
            cv.yview_scroll(units, "units")

    def on_wall_wheel(self, event) -> None:
        self._scroll_canvas(self.wall_canvas, event)

    def on_bitmap_wheel(self, event) -> None:
        self._scroll_canvas(self.canvas, event)

    def _bind_text_wheel(self, box: Text) -> None:
        """
        给原生 ``Text`` 补上 X11 滚轮，并 ``return "break"`` 压住类绑定。

        ``Text`` 的类绑定只保证 ``<MouseWheel>``（实测 Tk 8.6 在 Windows 上
        ``bind_class('Text', '<Button-4>')`` 是空的）；X11 那两头是否绑，取决于
        Tk 内部按窗口系统的判断 —— 与其赌，不如自己绑：无论类绑定有没有，
        ``break`` 都保证一次滚动只滚一格，不会滚双份。
        """
        def handler(event, box=box):
            box.yview_scroll(self._wheel_units(event), "units")
            return "break"

        box.bind("<Button-4>", handler)      # X11 上滚
        box.bind("<Button-5>", handler)      # X11 下滚

    # ------------------------------------------------------------ 源码
    def render_source(self) -> None:
        """
        源码视图：在 Python 里拼好整段文本，**一次性**插进文本框。

        这几处细节都有原因：
        * 原来是逐行 ``insert``，一个 block 就是几千次 Tcl 调用 —— 一次插入既快得多，
          也少了几千次踩 Tk/X11 文本控件边界情况的机会；
        * 插进去之前统一过一遍 :func:`tk_safe`：源文件里的 NUL 等控制字符是 Linux 上
          Tk 段错误的经典诱因（读文件用 ``errors="replace"`` 也拦不住 NUL）；
        * 分隔线/行号标记改用纯 ASCII（``-`` ``>`` ``|``）：制表符之外的 ``─`` ``▸``
          属于等宽字体未必带的符号，X11 下要逐字符走 fontconfig 回退找字形，
          而 ASCII 任何字体都有；
        * 整个渲染过程包在 try 里并记面包屑（:meth:`_crumb`）—— 万一还是崩，
          跟踪文件能告诉我们死在读文件、定位行还是插入文本。
        """
        self._crumb("source: render begin")
        try:
            text = self._source_text()
        except Exception as exc:                       # 读文件/定位行都可能失败
            self._crumb(f"source: FAILED {exc.__class__.__name__}: {exc}")
            self._log("WARN", f"Source view: {exc.__class__.__name__}: {exc}")
            text = f"(cannot render the source view)\n{exc.__class__.__name__}: {exc}"
        text = tk_safe(text)                           # 出错分支的文本同样要清洗
        try:
            self.source_box.configure(state="normal")
            self.source_box.delete("1.0", "end")
            self._crumb(f"source: inserting {len(text)} char(s)")
            self.source_box.insert("end", text)
        finally:
            self.source_box.configure(state="disabled")
        self._crumb("source: render done")

    def _source_text(self) -> str:
        """源码视图要显示的整段文本（不含任何控制字符处理，交给 ``tk_safe``）。"""
        target = self._source_target()
        if target is None:
            return ("Select a block / register / field in the model tree "
                    "or the table to show its SystemVerilog source here.")
        path, start, end, title = target
        fpath = self._resolve(path)
        head = f"// {title}\n// {fpath}  line {start}-{end}\n" + "-" * 76 + "\n"
        if not fpath or not fpath.exists():
            return head + f"(source file not found: {path})\n"
        self._crumb(f"source: reading {fpath.name}")
        lines = self._read_lines(fpath)
        first, last = max(1, start - 4), min(len(lines), end + 3)
        body = []
        for i in range(first, last + 1):
            mark = ">" if start <= i <= end else " "
            body.append(f"{mark}{i:>5}| {lines[i - 1]}")
            if len(body) >= _SOURCE_MAX_LINES:
                body.append(f"... (truncated at {_SOURCE_MAX_LINES} lines; "
                            f"pick a smaller block or register)")
                break
        return head + "\n".join(body) + "\n"

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        """按行读源文件（限长、容错）：NUL 等控制字符在读的时候就清掉。"""
        data = path.read_bytes()[:_MAX_SOURCE_BYTES]
        return tk_safe(data.decode("utf-8", errors="replace")).split("\n")

    def _source_target(self) -> tuple[str, int, int, str] | None:
        reg = self.focus_reg
        sel = self.table.selection()
        meta = None
        if sel and self.view_var.get() != "Source":
            idx = self.table.index(sel[0])
            if idx < len(self._row_meta):
                meta = self._row_meta[idx]
        kind = meta.get("kind") if meta else ("reg" if reg else None)
        if kind == "field":
            fld: FieldDef = meta["field"]
            return (fld.file, fld.line, fld.line + 1,
                    f"field {fld.reg.display_name if fld.reg else ''}.{fld.name}")
        if kind == "reg" and (meta or reg):
            r: RegDef = meta["reg"] if meta else reg
            cls_start = self._class_line(r.file, r.cls) or r.line
            return (r.file, cls_start, self._class_end(r.file, r.cls) or r.line + 3,
                    f"register {r.cls}")
        if kind == "block" and meta and meta.get("block"):
            blk: BlockDef = meta["block"]
            start = self._class_line(blk.file, blk.cls) or blk.line
            return (blk.file, start, self._class_end(blk.file, blk.cls) or blk.line + 3,
                    f"Block {blk.cls}")
        return None

    def _resolve(self, name: str) -> Path | None:
        for p in self.result.files:
            if Path(p).name == name:
                return Path(p)
        return None

    def _class_line(self, file: str, cls: str) -> int | None:
        for reg in self.result.all_regs:
            if reg.file == file and reg.cls == cls:
                return reg.line
        for blk in self.result.blocks:
            if blk.file == file and blk.cls == cls:
                return blk.line
        return None

    def _class_end(self, file: str, cls: str) -> int | None:
        """粗略地找到类的 endclass 行：从声明行往下找第一个 endclass。"""
        path = self._resolve(file)
        if not path:
            return None
        start = self._class_line(file, cls)
        if not start:
            return None
        try:
            lines = self._read_lines(path)
        except OSError as exc:
            self._log("WARN", f"Cannot read {path}: {exc}")
            return None
        depth = 0
        for i in range(start - 1, len(lines)):
            depth += len(_class_tokens(lines[i], "class"))
            depth -= lines[i].count("endclass")
            if i > start - 1 and depth <= 0:
                return i + 1
        return None

    # ------------------------------------------------------------ 导出 / 状态
    def on_export(self) -> None:
        view = self.view_var.get()
        if view not in COLUMNS:
            self._say("The Bitmap / Source views have no table to export — "
                      "switch to the Registers / Fields / Blocks view")
            return
        if not self._rows:
            self._say("No rows to export")
            return
        path = filedialog.asksaveasfilename(
            title="Export CSV", defaultextension=".csv",
            initialfile=f"ral_{view}.csv", filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            rows = self.export_rows(view, path)
        except OSError as exc:
            self._say(f"Export failed: {exc}")
            return
        self._say(f"Exported {rows} row(s) → {path}")

    def export_rows(self, view: str, path: str | Path) -> int:
        """把某个视图的表格写成 CSV（UTF-8-BOM，Excel 打开不乱码）。"""
        if view != self.view_var.get():
            self.goto_view(view)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow([c[1] for c in COLUMNS[view]])
            writer.writerows(self._rows)
        return len(self._rows)

    def on_search(self, _event=None) -> None:
        self.query = self.search.get().strip().lower()
        self.refresh()

    def on_toggle_tree(self) -> None:
        """收起 / 展开左侧模型树——小屏幕、高缩放时能把宽度全让给表格。"""
        if self.left.winfo_ismapped():
            self.left.grid_remove()
            self.tree_toggle.configure(text="Show tree")
            self._say("Model tree collapsed (table gets the full width)")
        else:
            self.left.grid()
            self.tree_toggle.configure(text="Hide tree")

    def on_clear_filter(self) -> None:
        self.search.delete(0, "end")
        self.query = ""
        self.scope = {"file": None, "block": None, "reg": None}
        if self.tree.selection():
            self.tree.selection_remove(*self.tree.selection())
        self.refresh()

    def _say(self, text: str, level: str = "info") -> None:
        """状态栏文字；``level`` 用来染色（error 红 / warn 琥珀），便于一眼看出失败。"""
        color = {"error": self._palette()["error"], "warn": self._palette()["warn"],
                 "ok": self._palette()["ok"]}.get(level, ("gray10", "gray90"))
        self.status.configure(text=text, text_color=color)

    def _pen(self, size: int) -> int:
        """
        磅 → **整数像素**（至少 6px）。

        Tk 里字号是"磅"，会乘 ``tk scaling`` 变成像素：96dpi 下 10 磅 = 13.33px。
        **小数像素**交给 Xft / FreeType 栅格化时，一个字模会落在半格上 ——
        Linux 上最常见的"字体发虚"就是这么来的（Windows 的 GDI 会先取整，
        所以同一个界面在 Windows 上看着还行）。取整成像素后每个字模都落在
        像素网格上，且 1 磅的误差肉眼不可见。
        """
        return max(6, int(round(size * self._px_per_pt)))

    def _cf(self, size: int, family: str = "") -> tuple[str, int]:
        """画布文本用的字体元组 ``(族名, -像素)``（Tk 约定：**负 size 是像素**）。"""
        return (family or FONT, -self._pen(size))

    def _tk_font(self, size: int, family: str = FONT) -> tkfont.Font:
        """
        取（并缓存）一个与画布 ``(family, size)`` **逐像素一致**的真实字体，用于量宽。

        必须和 ``_cf()`` 用同一个像素口径，否则量出来的宽度与实际绘制有偏差，
        裁剪（``_fit_text``）和排版都会差一两个字符。
        """
        px = self._pen(size)
        key = (family, px)
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(family=family, size=-px)
        return self._font_cache[key]

    def _debounce(self, slot: str, fn) -> None:
        """
        把画布的 ``<Configure>`` 重绘防抖到 ~90ms。

        ``<Configure>`` 是**每个像素**都会触发的：拖动窗口边缘或拉滚动条时
        会被连续调用几十次，每次都整幅重画（几十毫秒）就会卡成幻灯片。
        这里改成只有停下来 90ms 后才真正重画一次。
        """
        job = getattr(self, slot, None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        try:
            setattr(self, slot, self.after(90, fn))
        except Exception:
            setattr(self, slot, None)

    def _lh(self, size: int, family: str = FONT) -> int:
        """
        该字号在当前 DPI 缩放下的**实际行高（像素）**。

        这是位图排版的关键：``create_text(font=(FONT, 10))`` 里的 10 是**磅**，
        Tk 会按 ``tk scaling`` 换算成像素 —— 本机 200% 缩放下 1 磅 ≈ 2.67px，
        10 磅的字高就是 ~27px 而不是 10px。所以行距、格高、chip 高**不能写死像素**，
        必须从这里量出来，否则两行字会叠在一起（看上去就是"字体发虚/有重影"）。
        """
        return int(self._tk_font(size, family).metrics("linespace"))

    def _num_font_size(self, cell: float) -> int:
        """挑一个能塞进 ``cell`` 宽的位号字号；最小的都放不下就返回 0（不画位号）。"""
        for size in (8, 7, 6):
            if self._tw("00", size, MONO) + 2 <= cell:
                return size
        return 0

    def _set_legend(self, label, text: str) -> None:
        """只在文字真变了的时候写图例，避免鼠标移动时反复触发 CTkLabel 重绘。"""
        if self._legend_text.get(id(label)) != text:
            self._legend_text[id(label)] = text
            label.configure(text=text)

    def _legend_line(self, *parts: str, width_px: float, size: int) -> str:
        """
        把图例的几段文字拼成固定两行（每段按像素宽裁剪，放不下加省略号）。

        固定行数是为了让图例高度不随内容变化 —— 一变就会顶动上面的画布。
        """
        return "\n".join(self._fit_text(p, width_px - 12, size) for p in parts)

    def _wrap_label(self, label, width_px: float) -> None:
        """
        让长文案按可用宽度自动换行。

        英文普遍比中文长，位图/全部位图的标题行在窄窗口里会被直接裁掉，
        所以按画布实际宽度设 ``wraplength``（CTkLabel 用的是逻辑像素，
        这里换算一下）。缓存一份，避免重复 configure 触发重绘抖动。
        """
        logical = max(160, int(width_px / max(self._ui_scale, 1.0)) - 12)
        if self._wrap_cache.get(id(label)) != logical:
            self._wrap_cache[id(label)] = logical
            label.configure(wraplength=logical, justify="left")

    def _tw(self, text: str, size: int, family: str = FONT) -> int:
        """
        量一段文字的像素宽（带缓存）。

        ``Font.measure()`` 每次都走一趟 Tcl，一次 ~0.08ms。「全部位图」里
        几十个寄存器各有字段名、图例标签、位序标注，一轮就是几百次，
        比建图元本身还贵 —— 所以量过的宽度全部记下来复用。
        """
        key = (family, size, text)
        w = self._width_cache.get(key)
        if w is None:
            w = self._tk_font(size, family).measure(text)
            self._width_cache[key] = w
        return w

    def _fit_text(self, text: str, width_px: float, size: int, family: str = FONT) -> str:
        """
        按实际像素宽度裁剪文本，放不下就加省略号。

        二分查找而不是从尾到头逐个试 —— 长名字（真实模型里 ``TOP_CTRL_STATUS``
        这种）一次裁剪就要量二十来次，几十行叠起来就是几十毫秒。
        """
        if self._tw(text, size, family) <= width_px:
            return text
        lo, hi = 0, len(text)          # 答案在 [lo, hi)，lo 恒为可行长度
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self._tw(text[:mid] + "…", size, family) <= width_px:
                lo = mid
            else:
                hi = mid
        return text[:lo] + "…" if lo else ""

    def _find_reg(self, block_name: str, inst: str) -> RegDef | None:
        for reg in self.result.regs_of(block_name):
            if reg.inst_name == inst or reg.display_name == inst:
                return reg
        return None

    # ------------------------------------------------------------ 自测
    def selftest(self) -> int:
        """无人值守自测：载入示例 → 断言 → 逐个视图截图 → 退出。"""
        failures: list[str] = []
        shots: list[Path] = []
        self.attributes("-topmost", True)
        OUT_DIR.mkdir(exist_ok=True)

        def check_parser() -> None:
            res = self.result
            if len(res.blocks) != 1:
                failures.append(f"expected 1 block, got {len(res.blocks)}")
            regs = res.all_regs
            if len(regs) != 7:
                failures.append(f"expected 7 registers, got {len(regs)}")
            if res.field_count != 32:
                failures.append(f"expected 32 fields, got {res.field_count}")
            expect = {"ctrl": 0x00, "status": 0x04, "rdata": 0x08, "wdata": 0x0C,
                      "intr_state": 0x10, "intr_enable": 0x14, "intr_test": 0x18}
            got = {r.inst_name: r.offset for r in regs}
            if got != expect:
                failures.append(f"offsets parsed inconsistently: {got}")
            for cls, want in (("uart_ctrl_reg", "reg"), ("uart_reg_block", "block"),
                              ("uart_status_reg", "reg"), ("uart_intr_test_reg", "reg")):
                if res.kinds.get(cls) != want:
                    failures.append(f"class {cls} should be {want}, got {res.kinds.get(cls)}")
            # 基类（dv_base_reg / dv_base_reg_block / dv_base_reg_field）本文件并未定义，
            # 能解析出字段本身就说明继承链 + 命名规则兜底生效
            ctrl = next((r for r in regs if r.inst_name == "ctrl"), None)
            if ctrl is None:
                failures.append("ctrl register not found")
            else:
                if ctrl.cls != "uart_ctrl_reg" or ctrl.base != "dv_base_reg":
                    failures.append(f"ctrl class/base wrong: {ctrl.cls}/{ctrl.base}")
                if ctrl.width != 32 or len(ctrl.fields) != 8:
                    failures.append(f"ctrl width/field count wrong: {ctrl.width}/{len(ctrl.fields)}")
                rxblvl = next((f for f in ctrl.fields if f.name == "rxblvl"), None)
                if not rxblvl or (rxblvl.width, rxblvl.lsb, rxblvl.access) != (2, 7, "RW"):
                    failures.append(f"rxblvl parsed wrong: {rxblvl}")
                if ctrl.hdl_path != "{ctrl}":
                    failures.append(f"ctrl HDL path wrong: {ctrl.hdl_path!r}")
            blk = res.blocks[0] if res.blocks else None
            if blk and (blk.map_base, blk.map_addr_bytes, blk.hdl_path_root, blk.locked) != (
                0, 4, "tb.dut.uart", True
            ):
                failures.append(
                    f"block map/HDL/lock parsed wrong: {blk.map_base} {blk.map_addr_bytes} "
                    f"{blk.hdl_path_root!r} {blk.locked}"
                )
            if res.warnings:
                failures.append(f"parser produced warnings: {res.warnings}")
            if res.errors:
                failures.append(f"parser produced errors: {res.errors}")

        def check_generic() -> None:
            """通用性回归：非规范类名 + 自定义继承链 + 跨文件引用（samples/ 样本）。"""
            sample_dir = Path(__file__).with_name("samples")
            files = sorted(sample_dir.glob("*.sv")) if sample_dir.exists() else []
            if not files:
                print("[selftest] generic samples   : skipped (no .sv under samples/)")
                return
            other = parse_files([str(p) for p in files])
            for cls, kind in {
                "scratch": "reg",          # scratch extends vendor_base extends uvm_reg
                "vendor_base": "reg",      # 名字里没有 _reg
                "vendor_field": "field",   # 名字里没有 _field
                "vendor_top": "block",     # 基类在另一个文件里
                "side_block": "block",
                "shadow": "reg",
            }.items():
                if other.kinds.get(cls) != kind:
                    failures.append(f"generic sample: {cls} should be {kind}, "
                                    f"got {other.kinds.get(cls)}")
            if len(other.blocks) != 3:
                failures.append(f"generic sample: expected 3 blocks, got {len(other.blocks)}")
            # `` `UVM_REG_ADDR_WIDTH'h4000 `` 里的宏必须展开，否则 map 基地址整块丢失；
            # 样本里没有 `define，所以走的是内置默认值 32bit
            if other.macros.get("UVM_REG_ADDR_WIDTH") != "32":
                failures.append("generic sample: UVM_REG_ADDR_WIDTH should default to 32, "
                                f"got {other.macros.get('UVM_REG_ADDR_WIDTH')!r}")
            top = next((b for b in other.blocks if b.cls == "vendor_top"), None)
            if top is None:
                failures.append("generic sample: vendor_top missing")
            elif top.map_base != 0x4000:
                failures.append(f"generic sample: `UVM_REG_ADDR_WIDTH'h4000 should give map_base "
                                f"0x4000, got {top.map_base if top.map_base is None else hex(top.map_base)}")
            else:
                got_off = {r.inst_name: r.offset for r in top.regs}
                if got_off.get("s0") != 0x4000 or got_off.get("other") != 0x4004:
                    failures.append(f"generic sample: vendor_top offsets wrong: {got_off}")
            side = next((b for b in other.blocks if b.cls == "side_block"), None)
            if side is None:
                failures.append("generic sample: side_block missing")
            else:
                got = {r.inst_name: (r.offset, r.cls, len(r.fields)) for r in side.regs}
                if got.get("sh") != (0x1C, "shadow", 1):
                    failures.append(f"side_block.sh parsed wrong: {got.get('sh')}")
                if got.get("s0", (None,))[0] != 0x00:
                    failures.append(f"side_block.s0 offset wrong: {got.get('s0')}")
                if not side.mems or side.mems[0].depth != 256:
                    failures.append(f"generic sample: uvm_mem parsed wrong: {side.mems}")
                if not side.submaps:
                    failures.append("generic sample: add_submap not parsed")
            print(f"[selftest] generic samples   : {other.summary()}")

        def check_macros() -> None:
            """
            宏回归：`` `UVM_REG_ADDR_WIDTH `` 必须展开（没定义时默认 32bit），
            文件里自己 `` `define `` 的同名宏优先。
            """
            if expand_macros("localparam int W = `UVM_REG_ADDR_WIDTH;",
                             {"UVM_REG_ADDR_WIDTH": "32"}) != "localparam int W = 32;":
                failures.append("expand_macros did not substitute a bare macro")
            lit = 'string s = "`ADDR_W";'
            if expand_macros(lit, {"ADDR_W": "64"}) != lit:
                failures.append("expand_macros must not touch text inside string literals")
            table = collect_macros("`define ADDR_W 64  \n"
                                   "`define WITH_ARG(x) x\n"
                                   "`define EMPTY\n"
                                   "`define WRAPPED 32'h10 \\\n"
                                   "                + 4\n")
            if table.get("ADDR_W") != "64":
                failures.append(f"collect_macros missed a plain define: {table}")
            if "WITH_ARG" in table or "EMPTY" in table:
                failures.append(f"collect_macros kept a macro it should skip: {table}")
            if table.get("WRAPPED") != "32'h10 + 4":
                failures.append(f"collect_macros did not join a \\-continued define: "
                                f"{table.get('WRAPPED')!r}")

            # 端到端：文件里 `define 成 64 时，map 基地址/字节数必须按 64 展开，
            # 而不是内置默认的 32
            probe = OUT_DIR / "_macro_probe_pkg.sv"
            probe.write_text(
                "`define UVM_REG_ADDR_WIDTH 64\n"
                "package macro_probe_pkg;\n"
                "  class macro_probe_block extends uvm_reg_block;\n"
                "    uvm_reg_map m;\n"
                '    function new(string name = "macro_probe_block");\n'
                "      super.new(name);\n"
                "    endfunction\n"
                "    virtual function void build();\n"
                '      m = create_map("m", `UVM_REG_ADDR_WIDTH\'h1000, 8, UVM_LITTLE_ENDIAN);\n'
                "    endfunction\n"
                "  endclass\n"
                "endpackage\n",
                encoding="utf-8",
            )
            try:
                res = parse_files([str(probe)])
            finally:
                probe.unlink(missing_ok=True)
            blk = res.blocks[0] if res.blocks else None
            if blk is None:
                failures.append("macro probe: no block parsed")
            elif (blk.map_base, blk.map_addr_bytes) != (0x1000, 8):
                failures.append(f"macro probe: map parsed wrong: "
                                f"{blk.map_base} {blk.map_addr_bytes}")
            if res.macros.get("UVM_REG_ADDR_WIDTH") != "64":
                failures.append("macro probe: a file-level `define must win over the default, "
                                f"got {res.macros.get('UVM_REG_ADDR_WIDTH')!r}")
            print(f"[selftest] macro resolution  : UVM_REG_ADDR_WIDTH="
                  f"{res.macros.get('UVM_REG_ADDR_WIDTH')} (default 32) · "
                  f"map base={'—' if blk is None else hex(blk.map_base or 0)}")

        def check_offsets() -> None:
            """
            offset 回归：``add_reg()`` 的第一个实参写成 ``this.X`` /
            ``X.get_name()`` / 类名 / create 字面量时都要能认出来；偏移是常量
            表达式（``(32'h30 + 4)``）也要算得出来；实在解析不出来的必须
            **升级成 warning**（以前是静默的，界面上只剩 "——"）。
            """
            for raw, want in (("(32'h30 + 4)", 0x34), ("'h4000", 0x4000),
                              ("32'h20", 0x20), ("8'h10 | 4", 0x14), ("'h0", 0)):
                if parse_const_expr(raw) != want:
                    failures.append(f"parse_const_expr({raw!r}) != {hex(want)}: "
                                    f"{parse_const_expr(raw)}")
            if parse_const_expr("`UNKNOWN_W'h10") is not None:
                failures.append("parse_const_expr must fail on an unresolvable token")

            probe = OUT_DIR / "_offset_probe_pkg.sv"
            probe.write_text(
                "`define WIDTH 32\n"
                "package offset_probe_pkg;\n"
                "  class probe_reg extends uvm_reg;\n"
                "    rand uvm_reg_field f0;\n"
                '    function new(string name = "probe_reg");\n'
                "      super.new(name, 32, UVM_NO_COVERAGE);\n"
                "    endfunction\n"
                "    virtual function void build();\n"
                '      f0 = uvm_reg_field::type_id::create("f0");\n'
                '      f0.configure(this, 8, 0, "RW", 0, 8\'h00, 1, 1, 0);\n'
                "    endfunction\n"
                "  endclass\n"
                "  class probe_reg_b extends probe_reg;\n"
                "  endclass\n"
                "  class probe_block extends uvm_reg_block;\n"
                "    rand probe_reg this_form, call_form, expr_form, macro_form;\n"
                "    rand probe_reg_b cls_form;\n"
                "    rand probe_reg  member_only;\n"
                "    uvm_reg_map m;\n"
                '    function new(string name = "probe_block");\n'
                "      super.new(name);\n"
                "    endfunction\n"
                "    virtual function void build();\n"
                '      this_form  = probe_reg::type_id::create("this_form");\n'
                '      call_form  = probe_reg::type_id::create("call_form");\n'
                '      expr_form  = probe_reg::type_id::create("expr_form");\n'
                '      macro_form = probe_reg::type_id::create("macro_form");\n'
                '      cls_form   = probe_reg_b::type_id::create("cls_form");\n'
                # 句柄名（HANDLE_ONLY）与成员名（member_only）不一致的写法
                '      HANDLE_ONLY = probe_reg::type_id::create("member_only");\n'
                '      m = create_map("m", 0, 4, UVM_LITTLE_ENDIAN);\n'
                '      m.add_reg(this.this_form,           \'h10,  "RW");\n'
                '      m.add_reg(call_form.get_name(),     `WIDTH\'h20, "RW");\n'
                "      m.add_reg(expr_form,  (32'h30 + 4), \"RW\");\n"
                "      m.add_reg(macro_form, `NOT_DEFINED, \"RW\");\n"
                '      m.add_reg(probe_reg_b,              \'h50,  "RW");\n'
                '      m.add_reg(ghost_reg,                \'h60,  "RW");\n'
                "      m.add_reg(probe_reg,                'h70,  \"RW\");\n"
                "      m.add_reg(HANDLE_ONLY,              'h80,  \"RW\");\n"
                "    endfunction\n"
                "  endclass\n"
                # 有 create_map 却完全没有 add_reg 的 block：必须给出提示，
                # 否则界面上就是一片 "——"，用户无从下手
                "  class orphan_block extends uvm_reg_block;\n"
                "    rand probe_reg orphan;\n"
                "    uvm_reg_map m;\n"
                '    function new(string name = "orphan_block");\n'
                "      super.new(name);\n"
                "    endfunction\n"
                "    virtual function void build();\n"
                '      orphan = probe_reg::type_id::create("orphan");\n'
                '      m = create_map("m", 0, 4, UVM_LITTLE_ENDIAN);\n'
                "    endfunction\n"
                "  endclass\n"
                "endpackage\n",
                encoding="utf-8",
            )
            try:
                res = parse_files([str(probe)])
            finally:
                probe.unlink(missing_ok=True)
            blk = next((b for b in res.blocks if b.cls == "probe_block"), None)
            if blk is None:
                failures.append("offset probe: no block parsed")
                print("[selftest] offset resolution : no block")
                return
            got = {r.inst_name: r.offset for r in blk.regs}
            want = {"this_form": 0x10, "call_form": 0x20, "expr_form": 0x34,
                    "macro_form": None, "cls_form": 0x50, "member_only": 0x80}
            got = {k: got.get(k) for k in want}      # 忽略未被识别的 ghost_reg 之类
            if got != want:
                failures.append(f"offset probe: offsets parsed wrongly: {got} != {want}")
            if "HANDLE_ONLY" in {r.inst_name for r in blk.regs}:
                failures.append("offset probe: a handle that only appears in create() "
                                "must not become a second register instance")
            macro = next((r for r in blk.regs if r.inst_name == "macro_form"), None)
            if not macro or macro.offset_raw != "`NOT_DEFINED":
                failures.append("offset probe: unresolvable literal must still be kept "
                                f"as offset_raw, got {getattr(macro, 'offset_raw', None)!r}")
            joined = " | ".join(res.warnings)
            if "ghost_reg" not in joined:
                failures.append(f"offset probe: unmatched add_reg() must warn: {res.warnings}")
            if "macro_form" not in joined:
                failures.append(f"offset probe: unresolved offset must warn: {res.warnings}")
            # 同一个类被实例化多次时按类名反查是**有歧义**的：宁可报"对不上"，
            # 也不能把 'h70 安到某个随机寄存器上
            if 0x70 in got.values():
                failures.append(f"offset probe: ambiguous class name was guessed: {got}")
            if "probe_reg," not in joined and "probe_reg)" not in joined:
                failures.append(f"offset probe: ambiguous add_reg() must warn: {res.warnings}")
            if "no register offset was resolved" not in joined:
                failures.append(f"offset probe: a mapped block with no add_reg() must warn: "
                                f"{res.warnings}")
            print(f"[selftest] offset resolution : {sum(1 for v in got.values() if v is not None)}"
                  f"/{len(got)} resolved · {len(res.warnings)} warning(s)")

        def check_sanitize() -> None:
            """
            控制字符回归：带 NUL / ``\\x01`` 的源文件必须能安全解析，且模型里一个都留不下。

            这类字符不会让解析器出错（``errors="replace"`` 只管非法字节），但会顺着
            offset 原文 / 字段名 / 注释一路流进 Tk 的 Treeview / Text / Canvas ——
            在 X11 上是段错误的经典诱因，界面上还什么都显示不出来。清洗点在读文件处
            （``ral_parser._FILE_CTRL_RE``），这里守住这个约定。
            """
            probe = Path(tempfile.gettempdir()) / "ral_ctrl_probe_pkg.sv"
            probe.write_bytes((
                "package ctrl_probe_pkg;\n"
                "  class ctrl_probe_reg extends uvm_reg;\x00\n"
                "    rand uvm_reg_field f0;\n"
                "    function new(string name = \"ctrl_probe_reg\");\n"
                "      super.new(name, 32, UVM_NO_COVERAGE);\n"
                "    endfunction\n"
                "    virtual function void build();\n"
                "      f0 = uvm_reg_field::type_id::create(\"f\x01o\",, get_full_name());\n"
                "      f0.configure(this, 4, 0, \"RW\", 0, 4'h0, 1, 1, 0);\n"
                "    endfunction\n"
                "  endclass\n"
                "endpackage\n"
            ).encode("utf-8"))
            try:
                res = parse_files([probe])
            finally:
                probe.unlink(missing_ok=True)
            if not res.all_regs:
                failures.append("control-char probe: no register parsed out of the probe file")
                return
            blob = " ".join(
                [res.summary(), *res.warnings, *res.errors]
                + [f"{b.cls} {b.map_name} {n}" for b in res.blocks for n in b.notes]
                + [f"{r.cls} {r.inst_name} {r.reset_text} {r.offset_raw} {r.doc}"
                   for r in res.all_regs]
                + [f"{f.name} {f.reset_raw} {f.doc}" for r in res.all_regs for f in r.fields]
            )
            if any(ord(ch) < 32 and ch not in "\t\n" for ch in blob):
                failures.append(f"control characters leaked out of the parser: {blob!r}")
            names = sorted({f.name for r in res.all_regs for f in r.fields})
            # 声明名是 f0、create() 里写的是 "f\x01o"：解析器取哪个都行，
            # 但取到的必须是清洗过的（"f\x01o" 说明清洗漏了）
            if not [n for n in names if n in ("f0", "fo")]:
                failures.append(f"control-char probe: unexpected field name(s) {names}")
            print(f"[selftest] control chars     : {len(res.all_regs)} register(s) parsed from a "
                  f"NUL/\\x01 file · fields {names} · model text is clean")

        def check_fonts() -> None:
            """字体回归：族名必须是本机真实存在的；画布字号必须是整数像素。"""
            families = {f.lower() for f in tkfont.families()}
            if FONT.lower() not in families:
                failures.append(f"resolved UI font {FONT!r} does not exist on this system")
            if MONO.lower() not in families:
                failures.append(f"resolved mono font {MONO!r} does not exist on this system")
            if self._pen(10) <= 0 or self._pen(10) != int(round(10 * self._px_per_pt)):
                failures.append(f"point→pixel conversion is off: {self._pen(10)}px")
            if self._cf(10)[1] >= 0:
                failures.append("canvas font must use a negative (pixel) size")
            # 量宽用的 Font 必须和画布绘制同口径（同族、同像素），否则裁剪会差字。
            # 注意 actual() 会把像素字号换算回磅，这里要比 cget() 的原始配置
            canvas_fam, canvas_px = self._cf(10)
            measured = self._tk_font(10)
            if measured.actual("family").lower() != canvas_fam.lower() or \
                    int(measured.cget("size")) != canvas_px:
                failures.append(f"measure font {(measured.actual('family'), measured.cget('size'))}"
                                f" != canvas font {(canvas_fam, canvas_px)}")
            # 解析失败的 offset 要把原文显示出来（前面带 ?），不能只剩破折号
            reg = self.focus_reg
            if reg is not None:
                reg.offset, saved = None, reg.offset_raw
                reg.offset_raw = "`NOT_DEFINED"
                if not self._off_text(reg).startswith("?`NOT_DEFINED"):
                    failures.append(f"_off_text lost the raw offset: {self._off_text(reg)!r}")
                reg.offset_raw = ""
                if self._off_text(reg) != "——":
                    failures.append(f"_off_text placeholder wrong: {self._off_text(reg)!r}")
                reg.offset_raw = saved
            print(f"[selftest] fonts             : ui={FONT!r} mono={MONO!r} "
                  f"· {self._px_per_pt:.3f}px/pt (10pt={self._pen(10)}px)")

        def check_wheel() -> None:
            """
            滚轮回归：X11 的滚轮是 Button-4/5、且没有 delta，必须换算得和 Windows 一致。

            这一类 bug 只会在 Linux 上暴露（Windows 有 ``<MouseWheel>``，一直是好的），
            所以断言分两半：事件换算的纯逻辑 + 三个事件确实都绑到了画布上。
            """
            cases = [
                (_WheelEvent(num=4), -1), (_WheelEvent(num=5), 1),           # X11：上/下滚
                (_WheelEvent(delta=120), -1), (_WheelEvent(delta=-120), 1),  # Windows
                (_WheelEvent(delta=1), -1), (_WheelEvent(delta=-1), 1),      # macOS
                (_WheelEvent(), 0),                                          # 都缺就当作没滚
            ]
            for event, want in cases:
                got = self._wheel_units(event)
                if got != want:
                    failures.append(f"wheel: num={event.num} delta={event.delta} → {got}, "
                                    f"expected {want}")
            # 两个位图画布都要绑；单图那条还兼着 Shift+滚轮的横向滚动
            for where, cv, handler in (("All-Bitmaps", self.wall_canvas, "on_wall_wheel"),
                                       ("Bitmap", self.canvas, "on_bitmap_wheel")):
                for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    script = cv.bind(seq)
                    if not script:
                        failures.append(f"{where} canvas has no {seq} binding "
                                        f"(Linux wheel would be dead)")
                    elif handler not in script:
                        failures.append(f"{where} {seq} is bound to the wrong handler: "
                                        f"{script[:60]!r}")
            # 原生 Text（源码视图 / Debug 面板）的 X11 滚轮是我们自己补的
            for name, widget in (("Source text", self.source_box),
                                 ("Debug log text", self.log_box)):
                for seq in ("<Button-4>", "<Button-5>"):
                    if not widget.bind(seq):
                        failures.append(f"{name} box has no {seq} binding "
                                        f"(Linux wheel would be dead)")
            print(f"[selftest] wheel bindings    : canvas bound to MouseWheel/Button-4/Button-5 "
                  f"· {len(cases)} delta cases")

        def check_ui() -> None:
            if not self.tree.get_children():
                failures.append("model tree is empty")
            self.goto_view("Registers")
            self.update_idletasks()
            if len(self._rows) != 7:
                failures.append(f"[Registers] view should have 7 rows, got {len(self._rows)}")
            self.goto_view("Fields")
            if len(self._rows) != 32:
                failures.append(f"[Fields] view should have 32 rows, got {len(self._rows)}")
            self.search.insert(0, "rxblvl")
            self.on_search()
            if len(self._rows) != 1:
                failures.append(f"search 'rxblvl' should match 1 row, got {len(self._rows)}")
            self.on_clear_filter()
            self.on_toggle_tree()          # 收起 / 展开模型树
            self.update_idletasks()
            if self.left.winfo_ismapped():
                failures.append("[Hide tree] did not collapse the model tree")
            self.on_toggle_tree()
            self.update_idletasks()
            if not self.left.winfo_ismapped():
                failures.append("second click did not expand the model tree again")
            if self.tree_toggle.cget("text") != "Hide tree":
                failures.append("tree toggle button label was not restored")
            self.goto_view("Blocks")
            if len(self._rows) != 1:
                failures.append(f"[Blocks] view should have 1 row, got {len(self._rows)}")
            # 树节点联动：选中 block 节点应只留下该 block 的寄存器
            bnode = next((i for i, m in self._sel_meta.items() if m[0] == "block"), None)
            if bnode:
                self.tree.selection_set(bnode)
                self.goto_view("Registers")
                if len(self._rows) != 7:
                    failures.append(f"after selecting a block node: expected 7 rows, "
                                    f"got {len(self._rows)}")
            else:
                failures.append("no block node in the model tree")

        def shoot(name: str) -> Path | None:
            try:
                from PIL import ImageGrab

                self.update_idletasks()
                self.update()
                x, y = self.winfo_rootx(), self.winfo_rooty()
                box = (x, y, x + self.winfo_width(), y + self.winfo_height())
                out = OUT_DIR / name
                ImageGrab.grab(bbox=box).save(out)
                shots.append(out)
                print(f"[selftest] screenshot        : {out.name}")
                return out
            except Exception as exc:  # 截图失败不影响功能判定
                print(f"[selftest] screenshot skipped ({exc.__class__.__name__}: {exc})")
                return None

        def step_views() -> None:
            check_ui()
            ctrl = self._find_reg("uart_reg_block", "ctrl")
            if ctrl:  # 位图视图需要一个焦点寄存器
                self.focus_reg = ctrl
            self.goto_view("Registers")
            self.update_idletasks()
            shoot("ral_registers_dark.png")
            self.goto_view("Fields")
            self.update_idletasks()
            shoot("ral_fields_dark.png")

        def step_bitmap() -> None:
            self.goto_view("Bitmap")
            self.update_idletasks()
            self.render_bitmap()
            shapes = len(self.canvas.find_all())
            if shapes < 8:
                failures.append(f"bitmap drew too few items: {shapes}")
            if self.focus_reg is None or self.focus_reg.inst_name != "ctrl":
                failures.append("bitmap focus register is not ctrl")
            check_bit_axis()
            check_legend_order(self.canvas, "[Bitmap]")
            check_zoom()
            shoot("ral_bitmap_dark.png")

        def check_zoom() -> None:
            """
            横向拉长：格宽变大 → 内容比视口宽 → 横向滚动条真的能滚；Fit 能回到原样。

            断言挑的是「字段能不能显示完整」这件事本身：拉长到最大档时，位带里
            应该能容下 *全部* 字段名（一个都不剩给图例）。
            """
            cv = self.canvas
            self.on_zoom_fit()
            self.update_idletasks()
            if self._bitmap_geom is None:
                failures.append("zoom: the bitmap did not render")
                return
            cell_fit, named_fit = self._bitmap_geom[2], self._bitmap_named
            if self.cell_zoom != 1.0 or cv.xview()[1] < 1.0:
                failures.append(f"zoom: Fit did not restore 1×/no sideways scroll "
                                f"(zoom={self.cell_zoom}, xview={cv.xview()})")
            # 一路拉到最大档
            for _ in range(len(ZOOM_STEPS)):
                self.on_zoom(1)
            self.update_idletasks()
            cell_max, named_max = self._bitmap_geom[2], self._bitmap_named
            fields = len(self.focus_reg.fields)
            if not cell_max > cell_fit:
                failures.append(f"zoom: max zoom did not widen the cells ({cell_fit} → "
                                f"{cell_max} px/bit)")
            if named_max < named_fit:
                failures.append(f"zoom: stretching lost field names ({named_fit} → {named_max})")
            if named_max < fields:
                failures.append(f"zoom: even at {self.cell_zoom:g}× only {named_max} of "
                                f"{fields} field name(s) fit inside the strip")
            if not self._overflow_x or cv.xview()[1] >= 1.0:
                failures.append("zoom: the canvas did not become horizontally scrollable "
                                f"(overflow={self._overflow_x}, xview={cv.xview()})")
            shoot("ral_bitmap_zoom_dark.png")     # 拉长后的样子：字段名全部进格子
            # Shift + 滚轮 = 横向滚动（X11 与 Windows 两种事件都要走通）
            cv.xview_moveto(0.0)
            self.update_idletasks()
            for ev in (_WheelEvent(num=5, state=0x0001), _WheelEvent(delta=-120, state=0x0001)):
                self.on_bitmap_wheel(ev)
            self.update_idletasks()
            if cv.xview()[0] <= 0.0:
                failures.append("zoom: Shift+wheel did not scroll the canvas sideways")
            # 缩放条上的文字要跟着档位走
            if f"{self.cell_zoom:g}" not in str(self.zoom_label.cget("text")):
                failures.append(f"zoom: the label did not follow the zoom step "
                                f"({self.zoom_label.cget('text')!r})")
            # 窄窗口（Linux 上常见 ~680px 宽）下的自适应公式：装得下就必须真的装下
            # （否则 1× 就出现横向滚动条，与 "(fit)" 矛盾）；装不下就退回下限、
            # 老老实实横向溢出，而不是把格子压成 1~2 个像素
            for w, bits in ((680, 32), (560, 32), (680, 64), (360, 64), (240, 64)):
                cell = self._fit_cell(w, bits, 12, 16, cap=44, floor=8)
                content = 12 + bits * cell + 16
                if cell < _CELL_MIN_PX:
                    failures.append(f"zoom: {w}px/bit{bits} produced a {cell}px cell")
                if cell > 8 and content > w:
                    failures.append(f"zoom: the fit formula overflows by "
                                    f"{content - w}px at {w}px wide / {bits} bit")
            self.on_zoom_fit()
            self.update_idletasks()
            if self.cell_zoom != 1.0 or self._bitmap_geom[2] != cell_fit:
                failures.append("zoom: Fit did not bring the bitmap back to 1×")
            print(f"[selftest] bitmap zoom      : 1× {cell_fit:.0f}px/bit ({named_fit}/{fields} "
                  f"field names) → {ZOOM_STEPS[-1]:g}× {cell_max:.0f}px/bit "
                  f"({named_max}/{fields}) · Shift+wheel scrolls sideways")

        def check_bit_axis() -> None:
            """回归：位序标注的位置必须和它写的高低位一致（左边 MSB、右边 LSB）。"""
            cv = self.canvas
            labels = [(cv.coords(i)[0], cv.itemcget(i, "text"))
                      for i in cv.find_withtag("axis")]
            bits = max(self.focus_reg.width or 0, 1)
            hi = [x for x, t in labels if "MSB" in t]
            lo = [x for x, t in labels if "LSB" in t]
            if not hi or not lo:
                failures.append(f"bitmap is missing the MSB/LSB labels: {[t for _x, t in labels]}")
                return
            if min(hi) > max(lo):
                failures.append("MSB/LSB labels are on the wrong ends (MSB belongs on the left)")
            if not any(f"bit{bits - 1}" in t for _x, t in labels if "MSB" in t):
                failures.append("the bit number on the left-end label is not the MSB")
            if not any("bit0" in t for _x, t in labels if "LSB" in t):
                failures.append("the bit number on the right-end label is not bit0")

        def check_legend_order(cv: Canvas, where: str,
                               y_range: tuple[float, float] | None = None) -> None:
            """
            回归：字段图例必须高位在前、低位在后（与位带从左到右同向）。

            ``y_range`` 限定只检查某一段（「全部位图」里一个寄存器一组图例，
            全局串起来当然不是单调的）。
            """
            items: list[tuple[int, float, str]] = []
            for item in cv.find_withtag("legend"):
                text = cv.itemcget(item, "text")
                if not text:
                    continue
                x, y = cv.coords(item)[0], cv.coords(item)[1]
                if y_range and not (y_range[0] <= y <= y_range[1]):
                    continue
                items.append((round(y / 8), x, text))   # 同一行的 y 取整后一起按 x 排
            items.sort()
            seq: list[int] = []
            for _row, _x, text in items:
                hit = re.search(r"\[(\d+)(?::(\d+))?\]", text)
                if hit:
                    seq.append(int(hit.group(1)))
            if len(seq) < 2:
                failures.append(f"{where}: field legend was not drawn ({len(seq)} entries)")
            elif seq != sorted(seq, reverse=True):
                failures.append(f"{where}: field legend is not ordered MSB -> LSB: {seq}")

        def check_strip_geometry() -> None:
            """回归：位带必须低位在右 —— 字段覆盖的位号越大，格子 x 越小。"""
            geom = next((g for g in self.wall_geom if g["reg"].inst_name == "ctrl"), None)
            if geom is None:
                failures.append("[All Bitmaps]: ctrl not found")
                return
            owner, ctrl = geom["owner"], geom["reg"]
            for name, lsb, msb in (("parity_odd", 6, 6), ("rxblvl", 7, 8)):
                fld = next((f for f in ctrl.fields if f.name == name), None)
                if fld is None:
                    failures.append(f"ctrl is missing field {name}")
                    continue
                if owner.get(lsb) is not fld or owner.get(msb) is not fld:
                    failures.append(f"{name} owns the wrong bits: bit{lsb}={owner.get(lsb)} "
                                    f"bit{msb}={owner.get(msb)}")
            x_msb = geom["left"] + (geom["bits"] - 1 - 8) * geom["cell"]   # bit8 的格子
            x_lsb = geom["left"] + (geom["bits"] - 1 - 6) * geom["cell"]   # bit6 的格子
            if not x_msb < x_lsb:
                failures.append("bit strip is mirrored: the MSB should be on the left (smaller x)")

        def step_all_bitmaps() -> None:
            self.goto_view("All Bitmaps")
            self.update_idletasks()
            self.render_all_bitmaps()
            regs = self._bitmap_regs()
            if len(regs) != 7 or len(self.wall_geom) != 7:
                failures.append(
                    f"[All Bitmaps] should draw 7 registers, got {len(self.wall_geom)}/{len(regs)}"
                )
            if len(self.wall_canvas.find_withtag("axis")) < 14:
                failures.append("[All Bitmaps] every row should carry MSB/LSB labels")
            check_strip_geometry()
            idx = next((i for i, g in enumerate(self.wall_geom)
                        if g["reg"].inst_name == "ctrl"), None)
            if idx is not None:  # 只看 ctrl 那一组图例（下面紧挨着 status 的行）
                g = self.wall_geom[idx]
                nxt = (self.wall_geom[idx + 1]["top"] if idx + 1 < len(self.wall_geom)
                       else g["top"] + 200)
                check_legend_order(self.wall_canvas, "[All Bitmaps] ctrl",
                                   (g["top"] + g["h"] + 12, nxt - 2))
            # 搜索过滤后应只剩命中的寄存器（intr_state / intr_enable / intr_test）
            self.search.insert(0, "intr")
            self.on_search()
            if len(self.wall_geom) != 3:
                failures.append(f"[All Bitmaps] filtering by 'intr' should leave 3 registers, "
                                f"got {len(self.wall_geom)}")
            self.on_clear_filter()
            if len(self.wall_geom) != 7:
                failures.append(f"[All Bitmaps] clearing the filter should restore 7 registers, "
                                f"got {len(self.wall_geom)}")
            # 横向拉长在这里也要生效：所有行共用同一个格宽，一起变宽
            named_fit = sum(g["named"] for g in self.wall_geom)
            cell_fit, x_fit = self.wall_geom[0]["cell"], self.wall_canvas.xview()[1]
            for _ in range(len(ZOOM_STEPS)):
                self.on_zoom(1)
            self.update_idletasks()
            named_max = sum(g["named"] for g in self.wall_geom)
            if not (self.wall_geom[0]["cell"] > cell_fit and named_max >= named_fit):
                failures.append(
                    f"[All Bitmaps] zoom did not stretch the rows ({cell_fit} → "
                    f"{self.wall_geom[0]['cell']}px/bit, {named_fit} → {named_max} names)")
            if x_fit >= 1.0 and not (self._overflow_x and self.wall_canvas.xview()[1] < 1.0):
                failures.append("[All Bitmaps] the canvas did not become horizontally "
                                "scrollable after stretching")
            print(f"[selftest] wall zoom        : {cell_fit:.0f} → "
                  f"{self.wall_geom[0]['cell']:.0f}px/bit · field names in strips "
                  f"{named_fit} → {named_max}")
            self.on_zoom_fit()
            self.update_idletasks()
            shoot("ral_all_bitmaps_dark.png")

            # 悬停 / 点击：合成一个落在 wdata 的 data 字段（bit3）上的鼠标事件
            geom = next((g for g in self.wall_geom if g["reg"].inst_name == "wdata"), None)
            if geom is None:
                failures.append("[All Bitmaps]: wdata not found")
            else:
                ev = type("_Evt", (), {})()
                ev.x = geom["left"] + (geom["bits"] - 4) * geom["cell"]
                ev.y = geom["top"] + geom["h"] / 2
                self.on_wall_motion(ev)
                if "data" not in self.all_legend.cget("text"):
                    failures.append(f"[All Bitmaps] hover did not show field details: "
                                    f"{self.all_legend.cget('text')!r}")
                self.on_wall_click(ev)
                if self.focus_reg is not geom["reg"] or self.wall_active is not geom["reg"]:
                    failures.append("[All Bitmaps] click did not focus the matching register")
            # 滚轮真的能滚：内容装不下时才谈得上滚动，装得下就只报一句（不冤枉代码）
            self.wall_canvas.yview_moveto(0.0)
            self.update_idletasks()
            if self.wall_canvas.yview()[1] < 1.0:
                before = self.wall_canvas.yview()
                for ev2 in (_WheelEvent(num=5), _WheelEvent(delta=-120)):  # X11 / Windows 下滚
                    self.on_wall_wheel(ev2)
                self.update_idletasks()
                if self.wall_canvas.yview() == before:
                    failures.append("[All Bitmaps] the wheel handler did not move the canvas")
                print(f"[selftest] wall wheel        : scrolled {before[0]:.3f} → "
                      f"{self.wall_canvas.yview()[0]:.3f}")
                self.wall_canvas.yview_moveto(0.0)
            else:
                print("[selftest] wall wheel        : content fits the viewport, "
                      "wheel path covered by the binding assertions")
            # 复位焦点，免得影响后面的源码视图断言
            self.focus_reg, self.wall_active = self._find_reg("uart_reg_block", "ctrl"), None

        def step_source() -> None:
            self.goto_view("Source")
            self.update_idletasks()
            text = self.source_box.get("1.0", "end")
            if "uart_ctrl_reg" not in text:
                failures.append("Source view does not show the uart_ctrl_reg code")
            # 插进 Tk 文本控件的串里绝不能有控制字符（Linux 上会直接段错误）
            if tk_safe(text) != text:
                failures.append("Source view contains control characters")
            if not any(line.startswith(">") for line in text.splitlines()):
                failures.append("Source view did not mark the selected lines")
            # 行号标记/分隔线走 ASCII：非 ASCII 符号要逐字符做 fontconfig 回退
            # （只断言我们自己生成的那行，源文件正文里有什么符号不归我们管）
            if text.splitlines()[2:3] != ["-" * 76]:
                failures.append("Source view separator is not the ASCII rule")
            # 带 NUL 的源文件必须能被安全读进来（NUL 是 X11 段错误的经典诱因）
            probe = Path(tempfile.gettempdir()) / "ral_nul_probe.sv"
            try:
                probe.write_bytes(b"line1\x00line2\nplain\n")
                got = self._read_lines(probe)
            finally:
                probe.unlink(missing_ok=True)
            if got != ["line1line2", "plain", ""]:
                failures.append(f"a source file with an embedded NUL read back as {got!r}")
            print(f"[selftest] source view       : {len(text.splitlines())} line(s) · "
                  f"{len(text)} char(s) · NUL-safe read ok")
            shoot("ral_source_dark.png")

        def step_export() -> None:
            out = OUT_DIR / "ral_fields.csv"
            try:
                rows = self.export_rows("Fields", out)
            except OSError as exc:
                failures.append(f"CSV export failed: {exc}")
                return
            lines = out.read_text(encoding="utf-8-sig").strip().split("\n")
            if rows != 32 or len(lines) != 33:
                failures.append(f"CSV row count wrong: {rows} rows / {len(lines)} lines in file")
            if "Bit Range" not in lines[0] or "rxblvl" not in out.read_text(encoding="utf-8-sig"):
                failures.append("CSV header or content is missing")
            print(f"[selftest] CSV export        : {out.name} ({rows} rows)")
            self.goto_view("Source")

        def step_light() -> None:
            self.on_appearance("Light")
            self.appearance.set("Light")
            self.goto_view("Registers")
            self.update_idletasks()
            if ctk.get_appearance_mode() != "Light":
                failures.append("switching to light mode failed")
            if len(self._rows) != 7:
                failures.append(f"unexpected row count in light mode: {len(self._rows)}")
            # 源码视图和 Debug 面板是原生 tk.Text，不跟 CTk 主题走 —— 换肤时必须自己刷色，
            # 漏了就是深色字配深色底（或反过来），肉眼一看就是一块空白
            if self.source_box.cget("background") != self._palette()["field"]:
                failures.append("the Source text box did not pick up the light palette")
            shoot("ral_registers_light.png")

        def step_debug() -> None:
            """回归：解析失败时 Debug 面板要自动展开、写进错误、状态栏变红。"""
            self.on_appearance("Dark")
            self.appearance.set("Dark")
            self.on_clear_log()
            self.set_log_visible(False)
            missing = OUT_DIR / "no_such_file.sv"
            before = [r.inst_name for r in self.result.all_regs]
            self._load([str(missing)], append=True)          # 缺失文件读不出来
            self.update_idletasks()
            text = self.log_box.get("1.0", "end")
            if missing.name not in text:
                failures.append("debug log does not mention the unreadable file")
            if "ERROR" not in text:
                failures.append("debug log has no ERROR entry after a failed parse")
            if not self.log_frame.winfo_ismapped():
                failures.append("debug log did not expand automatically on error")
            if self.log_counts["ERROR"] < 1:
                failures.append("error counter was not incremented")
            if [r.inst_name for r in self.result.all_regs] != before:
                failures.append("an unreadable extra file must not drop the loaded model")
            if self.status.cget("text_color") != self._palette()["error"]:
                failures.append("status bar was not switched to the error colour")
            shoot("ral_debug_error_dark.png")

        def finish() -> None:
            print(f"[selftest] summary           : {self.result.summary()}")
            print(f"[selftest] tree root nodes   : {len(self.tree.get_children())}")
            print(f"[selftest] current view      : {self.view_var.get()}")
            print(f"[selftest] table rows        : {len(self._rows)}")
            print(f"[selftest] appearance        : {ctk.get_appearance_mode()}")
            print(f"[selftest] bitmap focus reg  : "
                  f"{self.focus_reg.cls if self.focus_reg else '-'}")
            print(f"[selftest] all-bitmap regs   : {len(self.wall_geom)}")
            print(f"[selftest] debug log lines   : {int(self.log_box.index('end-1c').split('.')[0])}")
            print(f"[selftest] debug counters    : {self.log_counts}")
            print(f"[selftest] screenshots       : {len(shots)}")
            print(f"[selftest] window size       : {self.winfo_width()}x{self.winfo_height()}")
            print(f"[selftest] CustomTkinter     : v{ctk.__version__}")
            if failures:
                for f in failures:
                    print(f"[selftest] FAIL {f}")
                self.destroy()
                return
            print("[selftest] OK - all assertions passed")
            self.destroy()

        def bail() -> None:
            print("[selftest] FAIL - timeout: the UI did not finish rendering within 9 s")
            self.destroy()

        def boot() -> None:
            check_parser()
            check_generic()
            check_macros()
            check_offsets()
            check_sanitize()
            check_fonts()
            check_wheel()
            if not self.result.all_regs:
                failures.append(f"could not parse a register model out of {EXAMPLE}")
                finish()
                return
            self.after(120, step_views)
            self.after(700, step_bitmap)
            self.after(1000, step_all_bitmaps)
            self.after(1350, step_source)
            self.after(1700, step_export)
            self.after(2050, step_light)
            self.after(2600, step_debug)
            self.after(3200, finish)

        self.after(400, boot)
        self.after(9000, bail)
        self.mainloop()
        return 1 if failures else 0


# =============================================================== 小工具


def _sort_key(text: str):
    """表头排序：能当数字看的按数字排，否则按字符串。"""
    s = str(text).strip()
    try:
        return (0, float(s), "")
    except ValueError:
        pass
    if s.startswith("0x"):
        try:
            return (0, float(int(s, 16)), "")
        except ValueError:
            pass
    return (1, 0.0, s.lower())


def _class_tokens(line: str, word: str) -> list[str]:
    return re.findall(rf"\b{word}\b", line)


# =============================================================== 入口


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("files", nargs="*", help="RAL model file(s) to load (any number)")
    parser.add_argument("--selftest", action="store_true",
                        help="self-test: render, assert, screenshot, then exit")
    parser.add_argument("--light", action="store_true", help="start in light appearance mode")
    args = parser.parse_args(argv)

    ctk.set_appearance_mode("light" if (args.light and not args.selftest) else "dark")
    ctk.set_default_color_theme("blue")

    paths = list(args.files)
    if not paths and EXAMPLE.exists():
        paths = [str(EXAMPLE)]
    if args.selftest and not paths:
        print("[selftest] FAIL - sample file uart_ral_pkg.sv not found")
        return 1

    app = RalViewer(paths)
    if args.selftest:
        return app.selftest()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
