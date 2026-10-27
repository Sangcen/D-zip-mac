#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-zip（内置 7zz 引擎）— 7z Mac 图形壳（tkinter + Tk 9）。

用法：
    python3 app/main.py [压缩包路径 ...]

要求：Python 3.12（$HOME/.local/bin/python3.12，自带 Tk 9）。系统 /usr/bin/python3 的
Tk 8.5 会让中文界面 100% CPU，启动脚本已禁止回退到它。
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))

import macdnd  # noqa: E402
import sevenz  # noqa: E402
import zip_mojibake  # noqa: E402

APP_NAME = "D-zip"
APP_TITLE = "D-zip — 7z Mac"

# 日志：行数上限 + 批量刷新间隔（毫秒）+ 折叠时内存缓冲上限
LOG_MAX = 2000
LOG_TRIM_SLACK = 400
LOG_BATCH_MS = 120
LOG_BACKLOG_MAX = 4000

MUTED = "#6e6e73"
ROW_ALT = "#f4f5f5"          # 访达列表的交替行
BACKGROUND = "#f5f5f7"
SURFACE = "#ffffff"
TEXT = "#1d1d1f"
BORDER = "#d9d9de"
ACCENT = "#007aff"
DROP_BACKGROUND = "#f0f6ff"
SYMBOL = "#aeaeb2"
DIR_TEXT = "#3a4a5e"
EMPTY_TITLE = "没有打开的压缩包"
EMPTY_DETAIL = "拖入压缩包即可浏览，拖入普通文件可新建压缩包。"
EMPTY_HINT = "打开…  ⌘O        新建…  ⌘N"
PROGRESS_RE = re.compile(r"(\d{1,3})\s?%")


class ToolButton(tk.Canvas):
    """工具栏按钮：Canvas 自绘圆角矩形 + 文字。

    aqua ttk.Button / tk.Label 的 padding 在 macOS Tk9 上不可靠；这里按字体行高
    直接算出画布尺寸，上下各留 PAD_Y、左右各留 PAD_X。
    漏白根因：工具栏是 ttk.Frame，aqua 主题无视 TFrame 的 background，实际画的是
    系统窗口色（浅色模式约 #ececec），而画布若设 #f5f5f7 就会在按钮外露出一圈浅色
    直角矩形。所以画布底色必须用同一个系统色 CHROME_BG，四角才真正与工具栏同色。
    形状只用整数坐标的 oval + rectangle 实心填充（Quartz 原生抗锯齿），
    不用 polygon（macOS 上顶点被取整，圆角发毛），更禁止 smooth polygon。
    configure(state=...) 兼容 _set_busy；禁用时点击/悬停全部失效。
    """
    PAD_X = 18
    PAD_Y = 14
    # macOS push button 圆角约 6pt
    RADIUS = 6
    # aqua ttk.Frame 实际绘制的底色（随浅色/深色模式变化）
    CHROME_BG = "systemWindowBackgroundColor"

    def __init__(self, parent, text: str, cmd: Callable[[], None],
                 font=None, primary: bool = False,
                 pad_x: Optional[int] = None, pad_y: Optional[int] = None):
        self._font = font or tkfont.nametofont("TkDefaultFont")
        # 状态栏/日志头的小按钮传较小留白；默认沿用工具栏尺寸
        self._pad_x = self.PAD_X if pad_x is None else pad_x
        self._pad_y = self.PAD_Y if pad_y is None else pad_y
        self._text = text
        self._cmd = cmd
        self._primary = primary
        self._enabled = True
        self._hovering = False
        self._pressed = False
        super().__init__(
            parent,
            background=self.CHROME_BG,
            highlightthickness=0,
            highlightbackground=self.CHROME_BG,
            bd=0,
            cursor="pointinghand",
        )
        self._layout()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # 颜色：(填充, 边框, 文字)
    def _colors(self):
        if not self._enabled:
            return ("#f0f0f2", "#dcdce0", "#a1a1a6")
        if self._primary:
            fill = "#0062cc" if self._pressed else ("#0070eb" if self._hovering else ACCENT)
            return (fill, fill, "#ffffff")
        fill = "#e4e4e8" if self._pressed else ("#f7f7f8" if self._hovering else SURFACE)
        return (fill, "#c4c4c9", TEXT)

    def _layout(self) -> None:
        tw = self._font.measure(self._text)
        th = self._font.metrics("linespace")
        self._bw = tw + 2 * self._pad_x
        self._bh = th + 2 * self._pad_y
        super().configure(width=self._bw, height=self._bh)
        self._draw()

    def _round_rect(self, x1: int, y1: int, x2: int, y2: int, r: int, color: str) -> None:
        """实心圆角矩形：四个 oval 做角 + 两个矩形做十字，全部整数坐标、无描边。

        Tk 在 macOS 上会把多边形顶点取整到 pt（Retina 上 = 2 像素），采样圆弧
        必然发毛；而 oval 交给 Quartz 原生抗锯齿填充，弧线干净，矩形边严格笔直。
        """
        d = 2 * r
        kw = {"fill": color, "outline": "", "width": 0}
        for ox, oy in ((x1, y1), (x2 - d, y1), (x1, y2 - d), (x2 - d, y2 - d)):
            self.create_oval(ox, oy, ox + d, oy + d, **kw)
        self.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        self.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)

    def _draw(self) -> None:
        self.delete("all")
        fill, border, fg = self._colors()
        r = self.RADIUS
        # 外层整块画边框色，内层缩 1pt 画填充色：边框是「两层实心形状的差」，
        # 不靠描边对齐；填充色永远被边框包住，圆角外只剩画布底色（=工具栏底色）
        x1, y1, x2, y2 = 1, 1, self._bw - 1, self._bh - 1
        self._round_rect(x1, y1, x2, y2, r, border)
        self._round_rect(x1 + 1, y1 + 1, x2 - 1, y2 - 1, r - 1, fill)
        self.create_text(self._bw // 2, self._bh // 2, text=self._text,
                         font=self._font, fill=fg, anchor="center")

    def _inside(self, e) -> bool:
        return 0 <= e.x < self._bw and 0 <= e.y < self._bh

    def _on_enter(self, _e=None) -> None:
        self._hovering = True
        if self._enabled:
            self._draw()

    def _on_leave(self, _e=None) -> None:
        self._hovering = False
        self._pressed = False
        if self._enabled:
            self._draw()

    def _on_press(self, _e=None) -> None:
        if self._enabled:
            self._pressed = True
            self._draw()

    def _on_release(self, e) -> None:
        fire = self._enabled and self._pressed and self._inside(e)
        self._pressed = False
        if self._enabled:
            self._draw()
        if fire:
            self._cmd()

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if "state" in kw:
            st = kw.pop("state")
            self._enabled = str(st) != str(tk.DISABLED)
            self._pressed = False
            super().configure(cursor="pointinghand" if self._enabled else "arrow")
            self._draw()
        if "text" in kw:
            self._text = kw.pop("text")
            self._layout()
        if cnf or kw:
            return super().configure(cnf, **kw)
        return None

    config = configure


class App(tk.Tk):
    def __init__(self, initial_paths: Optional[Sequence[str]] = None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x720")
        self.minsize(880, 560)

        self.archive_path: Optional[str] = None
        self.archive_type: str = ""
        self.entries: List[sevenz.ArchiveEntry] = []
        self.busy = False
        self.cancel_event = threading.Event()
        self.msg_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()

        # 日志：后台线程只往 deque 里追加，UI 线程定时批量写入
        self._log_buf: "deque[str]" = deque(maxlen=LOG_BACKLOG_MAX)
        self._log_last = ""
        self._log_pending = 0
        self._log_visible = False

        self._sort_col = "name"
        self._sort_desc = False
        self._fill_token = 0
        self._hover_hint = False
        # 拖放总线：AppKit 回调里只 put 纯 Python 对象，绝不能调 Tk（after/configure/
        # messagebox 都会在 macOS 拖放会话里死锁）。真正处理只在 _poll_queue。
        self._dnd_q: "queue.Queue[tuple]" = queue.Queue()
        self._drag_want: Optional[int] = None
        self._drop_pending: List[str] = []
        self._drop_job: Optional[str] = None
        self._drop_seq = 0

        self.configure(background=BACKGROUND)
        self._tune_fonts()
        self._build_styles()
        self._build_menu()
        self._build_toolbar()
        self._build_infobar()
        self._build_statusbar()
        self._build_body()
        self._bind_keys()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()
        self.after(LOG_BATCH_MS, self._flush_log)

        self._log(f"引擎：{sevenz.engine_path()}")
        self._log(f"版本：{sevenz.engine_version()}")
        self._log(f"Python {sys.version.split()[0]} · Tk {self.tk.call('info', 'patchlevel')}")

        self.after(80, self._setup_native)
        self.after(120, self._bring_to_front)

        if initial_paths:
            first = next((p for p in initial_paths if Path(p).exists()), None)
            if first:
                self.after(220, lambda: self.open_archive(first))

    # ------------------------------------------------------------ 外观基础设施
    def _tune_fonts(self) -> None:
        # 保留 Aqua 解析出的系统字体族；不要把 TkDefaultFont 当作字体族名。
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        for name, size in (
            ("TkDefaultFont", 13), ("TkTextFont", 13), ("TkMenuFont", 13),
            ("TkHeadingFont", 12), ("TkTooltipFont", 11),
        ):
            tkfont.nametofont(name).configure(family=family, size=size)
        self._ui_fonts = {}
        for name, size, weight in (
            ("AppTitleFont", 17, "bold"), ("AppSectionFont", 13, "bold"),
            ("AppSmallFont", 11, "normal"), ("AppStatusFont", 11, "normal"),
            ("AppEmptyFont", 17, "bold"), ("AppSymbolFont", 44, "normal"),
            ("AppDialogTitleFont", 15, "bold"),
        ):
            self._ui_fonts[name] = tkfont.Font(
                self, name=name, family=family, size=size, weight=weight,
            )

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BACKGROUND)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT,
                        font="TkDefaultFont")
        style.configure("TCheckbutton", background=BACKGROUND, font="TkDefaultFont")
        style.configure("TButton", padding=(18, 10))
        style.configure("Toolbar.TButton", padding=(18, 10), width=0)
        style.configure("Primary.TButton", padding=(18, 10), width=0)
        style.configure("Cancel.TButton", padding=(12, 6), width=0, font="AppSmallFont")
        style.configure("Form.TLabel", background=BACKGROUND, foreground=TEXT)
        style.configure("Note.TLabel", background=BACKGROUND, foreground=MUTED,
                        font="AppSmallFont")
        style.configure("DialogTitle.TLabel", font="AppDialogTitleFont")
        # 使用 Aqua 自带的工具按钮元素，不自己绘制按钮或覆盖系统焦点环。
        if "RecessedButton.button" in style.element_names():
            style.layout("Quiet.TButton", [("RecessedButton.button", {
                "sticky": "nswe", "children": [("Button.padding", {
                    "sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})],
                })],
            })])
        else:
            style.layout("Quiet.TButton", style.layout("Toolbutton"))
        style.configure("Quiet.TButton", padding=(14, 8), width=0, font="TkDefaultFont")
        style.configure("Mini.TButton", padding=(10, 5), width=0, font="AppSmallFont")
        style.configure("Treeview", rowheight=26, font="TkTextFont",
                        background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                        borderwidth=0)
        style.configure("Treeview.Heading", font="AppSmallFont", padding=(8, 4))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font="AppTitleFont")
        style.configure("Section.TLabel", foreground=MUTED, font="AppSmallFont")
        style.configure("Group.TLabel", font="AppSectionFont")
        style.configure("Status.TLabel", foreground=MUTED, font="AppStatusFont")
        style.configure("DropHint.TLabel", foreground=ACCENT, font="AppSmallFont")
        style.configure("Status.Horizontal.TProgressbar", thickness=4)

    # ------------------------------------------------------------ 菜单 / 快捷键
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="打开压缩包…", accelerator="⌘O", command=self.choose_open)
        m_file.add_command(label="关闭当前压缩包", accelerator="⌘W", command=self.close_archive)
        m_file.add_separator()
        m_file.add_command(label="新建压缩包…", accelerator="⌘N", command=self.open_create_dialog)
        m_file.add_command(label="添加文件到当前包…", accelerator="⌘⇧A",
                           command=self.add_to_current)
        m_file.add_separator()
        m_file.add_command(label="退出", accelerator="⌘Q", command=self._on_close)
        menubar.add_cascade(label="文件", menu=m_file)

        m_op = tk.Menu(menubar, tearoff=0)
        m_op.add_command(label="解压到…", accelerator="⌘E", command=lambda: self.do_extract(pick=True))
        m_op.add_command(label="解压到当前文件夹", accelerator="⌘D",
                         command=lambda: self.do_extract(pick=False))
        m_op.add_command(label="解压选中项到…", command=lambda: self.do_extract(pick=True, selected=True))
        m_op.add_separator()
        m_op.add_command(label="测试完整性", accelerator="⌘T", command=self.do_test)
        m_op.add_command(label="删除选中条目", command=self.do_delete)
        m_op.add_separator()
        m_op.add_command(label="修复 ZIP 文件名乱码…", command=self.fix_mojibake_manual)
        m_op.add_separator()
        m_op.add_command(label="刷新列表", accelerator="⌘R", command=self.refresh)
        m_op.add_command(label="取消当前任务", accelerator="⌘.", command=self._cancel)
        menubar.add_cascade(label="操作", menu=m_op)

        m_view = tk.Menu(menubar, tearoff=0)
        m_view.add_command(label="显示/隐藏日志", accelerator="⌘L", command=self.toggle_log)
        m_view.add_command(label="清空日志", command=self.clear_log)
        menubar.add_cascade(label="视图", menu=m_view)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="使用说明", command=self.show_readme)
        m_help.add_command(label="关于 D-zip", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=m_help)

        self.config(menu=menubar)

    def _bind_keys(self) -> None:
        self.bind("<Command-o>", lambda e: self.choose_open())
        self.bind("<Command-n>", lambda e: self.open_create_dialog())
        self.bind("<Command-e>", lambda e: self.do_extract(pick=True))
        self.bind("<Command-d>", lambda e: self.do_extract(pick=False))
        self.bind("<Command-t>", lambda e: self.do_test())
        self.bind("<Command-r>", lambda e: self.refresh())
        self.bind("<Command-l>", lambda e: self.toggle_log())
        self.bind("<Command-w>", lambda e: self.close_archive())
        self.bind("<Command-period>", lambda e: self._cancel())
        self.tree.bind("<Command-a>", lambda e: (self.tree.selection_set(
            self.tree.get_children()), "break")[1])
        self.tree.bind("<Delete>", lambda e: self.do_delete())
        self.tree.bind("<BackSpace>", lambda e: self.do_delete())

    # ------------------------------------------------------------ 工具栏
    def _build_toolbar(self) -> None:
        # 一行：左侧主操作，右侧次要操作。
        # 不用 aqua ttk.Button：系统原生按钮几乎不吃 padding，文字会一直贴边。
        bar = ttk.Frame(self, padding=(20, 14, 20, 12))
        bar.pack(side=tk.TOP, fill=tk.X)
        try:
            btn_font = tkfont.nametofont("TkDefaultFont")
        except tk.TclError:
            btn_font = None

        def mk(parent, text: str, cmd: Callable[[], None],
               primary: bool = False, side: str = tk.LEFT) -> "ToolButton":
            b = ToolButton(parent, text, cmd, btn_font, primary=primary)
            b.pack(side=side, padx=(0, 8) if side == tk.LEFT else (8, 0))
            return b

        def gap(parent) -> None:
            ttk.Frame(parent, width=14).pack(side=tk.LEFT)

        self.btn_open = mk(bar, "打开…", self.choose_open, primary=True)
        self.btn_new = mk(bar, "新建…", self.open_create_dialog)
        gap(bar)
        self.btn_ext = mk(bar, "解压到…", lambda: self.do_extract(pick=True), primary=True)
        self.btn_ext_cur = mk(bar, "解压到当前", lambda: self.do_extract(pick=False))

        self.btn_refresh = mk(bar, "刷新", self.refresh, side=tk.RIGHT)
        self.btn_test = mk(bar, "测试", self.do_test, side=tk.RIGHT)
        self.btn_del = mk(bar, "删除", self.do_delete, side=tk.RIGHT)
        self.btn_add_cur = mk(bar, "添加文件…", self.add_to_current, side=tk.RIGHT)

    # ------------------------------------------------------------ 归档信息条
    def _build_infobar(self) -> None:
        wrap = ttk.Frame(self, padding=(20, 0, 20, 12))
        wrap.pack(side=tk.TOP, fill=tk.X)
        ttk.Separator(wrap, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 12))
        row = ttk.Frame(wrap)
        row.pack(fill=tk.X)
        row.columnconfigure(0, weight=1)
        self.info_title = ttk.Label(row, text="压缩包", style="Title.TLabel",
                                     width=1, anchor=tk.W)
        self.info_title.grid(row=0, column=0, sticky="ew", padx=(0, 20))
        self.info_meta = ttk.Label(row, text="尚未打开压缩包",
                                    style="Muted.TLabel", width=1, anchor=tk.W)
        self.info_meta.grid(row=1, column=0, sticky="ew", pady=(2, 0), padx=(0, 20))

        # 密码属于「当前压缩包」，放在信息条右侧，不再占工具栏。
        password = ttk.Frame(row)
        password.grid(row=0, column=1, rowspan=2, sticky="e")
        self.pw_show = tk.BooleanVar(value=False)
        self.pw_enabled = tk.BooleanVar(value=False)
        self.pw_var = tk.StringVar()
        self.pw_chk = ttk.Checkbutton(password, text="使用密码", variable=self.pw_enabled,
                                      command=self._toggle_pw)
        self.pw_chk.pack(side=tk.LEFT)
        self.pw_entry = ttk.Entry(password, textvariable=self.pw_var, show="•", width=14,
                                  state=tk.DISABLED)
        self.pw_entry.pack(side=tk.LEFT, padx=(8, 8))
        self.pw_show_chk = ttk.Checkbutton(password, text="显示", variable=self.pw_show,
                                           command=self._toggle_pw_show)
        self.pw_show_chk.pack(side=tk.LEFT)

    # ------------------------------------------------------------ 主体
    def _build_body(self) -> None:
        self.paned = ttk.Panedwindow(self, orient=tk.VERTICAL)
        self.paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=20, pady=(0, 12))

        # --- 表格 ---
        self.table_frame = tk.Frame(self.paned, background=SURFACE,
                                    highlightbackground=BORDER, highlightthickness=1,
                                    borderwidth=0)
        cols = ("name", "size", "modified", "path")
        self.tree = ttk.Treeview(self.table_frame, columns=cols, show="headings",
                                 selectmode="extended")
        headings = {
            "name": ("名称", tk.W, 300),
            "size": ("大小", tk.E, 100),
            "modified": ("修改时间", tk.W, 172),
            "path": ("包内路径", tk.W, 420),
        }
        for key, (text, anchor, width) in headings.items():
            self.tree.heading(key, text=text, anchor=anchor,
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("name", "path")))
        self.tree.tag_configure("odd", background=ROW_ALT)
        self.tree.tag_configure("dir", foreground=DIR_TEXT)

        vsb = ttk.Scrollbar(self.table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(self.table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.table_frame.rowconfigure(0, weight=1)
        self.table_frame.columnconfigure(0, weight=1)

        # 普通 Frame/Label 空状态，不绘制 Canvas，也不插入伪文件行。
        # Aqua 的 themed Frame/Label 会采用系统底色；普通 Tk 容器保证空状态白底。
        self.empty_frame = tk.Frame(self.tree, background=SURFACE, padx=24, pady=12)
        self.empty_symbol = tk.Label(self.empty_frame, text="⇩", background=SURFACE,
                                     foreground=SYMBOL, font="AppSymbolFont")
        self.empty_symbol.pack(pady=(0, 6))
        self.empty_title = tk.Label(self.empty_frame, text=EMPTY_TITLE,
                                    background=SURFACE, foreground=TEXT, font="AppEmptyFont")
        self.empty_title.pack()
        self.empty_detail = tk.Label(
            self.empty_frame, text=EMPTY_DETAIL, background=SURFACE,
            foreground=MUTED, font="TkDefaultFont", wraplength=480, justify=tk.CENTER,
        )
        self.empty_detail.pack(pady=(8, 0))
        self.empty_hint = tk.Label(
            self.empty_frame, text=EMPTY_HINT,
            background=SURFACE, foreground=SYMBOL, font="AppSmallFont",
        )
        self.empty_hint.pack(pady=(14, 0))
        self._empty_widgets = (self.empty_symbol, self.empty_title,
                               self.empty_detail, self.empty_hint)
        self.empty_frame.place(relx=0.5, rely=0.46, anchor=tk.CENTER)
        # 注意：不要在 Treeview <Configure> 里 pack/unpack，会触发重入重绘导致界面闪烁。

        self.tree.bind("<Button-2>", self._context_menu)
        self.tree.bind("<Control-Button-1>", self._context_menu)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_selection_label())
        self.paned.add(self.table_frame, weight=5)

        # --- 日志 ---
        self.log_frame = ttk.Frame(self.paned)
        head = ttk.Frame(self.log_frame)
        head.pack(fill=tk.X, pady=(8, 6))
        ttk.Label(head, text="日志", style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Label(head, text="7zz 输出", style="Section.TLabel").pack(side=tk.LEFT, padx=8)
        self._mini_btn(head, "收起", self.toggle_log).pack(side=tk.RIGHT)
        self._mini_btn(head, "清空", self.clear_log).pack(side=tk.RIGHT, padx=(0, 6))

        log_surface = tk.Frame(self.log_frame, background=SURFACE,
                               highlightbackground=BORDER, highlightthickness=1,
                               borderwidth=0)
        log_surface.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(
            log_surface, height=6, wrap=tk.NONE, state=tk.DISABLED,
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
            background=SURFACE, foreground=TEXT, insertbackground=TEXT,
            font=("Menlo", 11), padx=12, pady=10, spacing1=0, spacing3=1,
        )
        lsb = ttk.Scrollbar(log_surface, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        # 日志默认收起（⌘L / 状态栏「日志」展开）：主窗口只留列表，更接近访达。
        self._log_visible = False
        self._sash_done = False
        self.paned.bind("<Configure>", self._on_paned_configure)

    def _on_paned_configure(self, event) -> None:
        if self._sash_done or event.height < 200:
            return
        self._sash_done = True
        try:
            self.paned.sashpos(0, int(event.height * 0.68))
        except tk.TclError:
            pass

    def _apply_sash(self) -> None:
        try:
            height = self.paned.winfo_height()
            if height > 200:
                self.paned.sashpos(0, int(height * 0.68))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 状态栏
    # 小号 ToolButton：状态栏与日志头共用，取代不吃 padding 的 aqua Mini/Cancel.TButton
    MINI_PAD_X = 13
    MINI_PAD_Y = 8

    def _mini_btn(self, parent, text: str, cmd: Callable[[], None]) -> "ToolButton":
        return ToolButton(parent, text, cmd, tkfont.nametofont("AppSmallFont"),
                          pad_x=self.MINI_PAD_X, pad_y=self.MINI_PAD_Y)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(20, 5, 20, 6))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status, style="Status.TLabel",
                  anchor=tk.W, width=1).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.log_btn = self._mini_btn(bar, "显示日志", self.toggle_log)
        self.log_btn.pack(side=tk.RIGHT, padx=(10, 0))
        # 进度条与「取消」仅在任务进行时出现（_set_busy 负责 pack / pack_forget）
        self.btn_cancel = self._mini_btn(bar, "取消", self._cancel)
        self.prog = ttk.Progressbar(bar, mode="indeterminate", length=140,
                                    style="Status.Horizontal.TProgressbar")
        self.sel_label = ttk.Label(bar, text="", style="Status.TLabel")
        self.sel_label.pack(side=tk.RIGHT)

    # ------------------------------------------------------------ 原生能力
    def _setup_native(self) -> None:
        ok = macdnd.install_drop_target(
            self,
            on_drop=self._queue_drop,
            # 悬停高亮也会从 AppKit 进 Tk，先关掉，优先保证不卡死
            on_enter=None,
            on_exit=None,
        )
        self._log("拖放：可用（访达拖文件进窗口即添加到当前包）" if ok
                  else "拖放：不可用（用「添加文件…」按钮）")
        if macdnd.install_open_document(
            self, lambda paths: self._dnd_q.put(("open", list(paths)))
        ):
            self._log("双击打开：已注册（Finder / 打开方式打开压缩包）")
        macdnd.set_app_menu_title(APP_NAME)
        icon = PROJECT_ROOT / "AppIcon.icns"
        if icon.is_file():
            macdnd.set_dock_icon(str(icon))

    def _bring_to_front(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass
        if macdnd.activate(self):
            return
        try:
            import subprocess
            subprocess.Popen(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost of first process '
                 f'whose unix id is {os.getpid()} to true'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 日志（节流）
    def _log(self, msg: str) -> None:
        """线程安全：只入队，UI 线程每 120ms 批量写入一次。"""
        for line in str(msg).splitlines() or [""]:
            self._log_buf.append(line)

    def _flush_log(self) -> None:
        # 收起时不碰 Text，日志留在有上限的 deque 里，展开时一次补写
        if self._log_visible:
            text = self._take_log_text()
            if text:
                self._insert_log(text)
        self.after(LOG_BATCH_MS, self._flush_log)

    def _take_log_text(self) -> str:
        if not self._log_buf:
            return ""
        lines: List[str] = []
        while self._log_buf:
            try:
                line = self._log_buf.popleft()
            except IndexError:
                break
            # 7zz 进度用 \r 刷新，保留最后一段，去掉重复行
            if "\r" in line:
                line = line.split("\r")[-1]
            if line == self._log_last and line.strip():
                continue
            self._log_last = line
            lines.append(line)
        return "\n".join(lines)

    def _insert_log(self, text: str) -> None:
        if not self._log_visible:
            # 折叠时不写 Text，仅保留在内存缓冲（expand 时统一补写）
            self._log_buf.appendleft(text)
            return
        try:
            at_bottom = self.log_text.yview()[1] > 0.98
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, text + "\n")
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > LOG_MAX + LOG_TRIM_SLACK:
                self.log_text.delete("1.0", f"{total - LOG_MAX}.0")
            if at_bottom:
                self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
            # 进度条：从 7zz 百分比推进
            if self.busy:
                found = PROGRESS_RE.findall(text)
                if found:
                    pct = max(0, min(100, int(found[-1])))
                    if str(self.prog.cget("mode")) != "determinate":
                        self.prog.stop()
                        self.prog.configure(mode="determinate", maximum=100)
                    self.prog["value"] = pct
        except tk.TclError:
            pass

    def clear_log(self) -> None:
        self._log_buf.clear()
        self._log_last = ""
        try:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def toggle_log(self) -> None:
        if self._log_visible:
            self._log_visible = False
            try:
                self.paned.forget(self.log_frame)
            except tk.TclError:
                pass
            self.log_btn.configure(text="显示日志")
            self.status.set("日志已收起（⌘L 展开）")
        else:
            self._log_visible = True
            try:
                self.paned.add(self.log_frame, weight=2)
            except tk.TclError:
                pass
            self.log_btn.configure(text="隐藏日志")
            self.after(50, self._apply_sash)
            self.after(80, self._drain_log_to_text)

    def _drain_log_to_text(self) -> None:
        """折叠期间缓存的日志一次性补写回 Text。"""
        cache = list(self._log_buf)
        self._log_buf.clear()
        if cache:
            self._log_last = cache[-1]
            self._insert_log("\n".join(cache))

    # ------------------------------------------------------------ 队列 / 忙碌态
    def _ui(self, fn: Callable[[], None]) -> None:
        self.msg_queue.put(fn)

    def _poll_queue(self) -> None:
        try:
            while True:
                fn = self.msg_queue.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        self._drain_dnd()
        self.after(60, self._poll_queue)

    def _drain_dnd(self) -> None:
        """把 AppKit 投进来的拖放事件搬到 Tk 定时器上处理。"""
        got_drop = False
        while True:
            try:
                kind, payload = self._dnd_q.get_nowait()
            except queue.Empty:
                break
            if kind == "drop":
                self._drop_pending.extend(str(p) for p in payload)
                got_drop = True
            elif kind == "open":
                paths = [str(p) for p in payload]
                self.after(120, lambda p=paths: self._handle_open_paths(p))
        if got_drop and self._drop_job is None:
            # 等拖放会话彻底结束再动手；50ms 仍可能撞上 AppKit 收尾
            self._drop_job = self.after(180, self._process_drop)

    def _set_status(self, text: str) -> None:
        self._ui(lambda: self.status.set(text))

    def _set_busy(self, busy: bool, label: str = "") -> None:
        """UI 线程调用时同步生效（self.busy 立刻可见，杜绝拖放/快捷键重入）；
        后台线程调用时经队列回到 UI 线程。"""
        def _apply() -> None:
            self.busy = busy
            state = tk.DISABLED if busy else tk.NORMAL
            for b in (self.btn_open, self.btn_ext, self.btn_ext_cur, self.btn_test,
                      self.btn_del, self.btn_refresh, self.btn_new, self.btn_add_cur):
                try:
                    b.configure(state=state)
                except tk.TclError:
                    pass
            try:
                if busy:
                    self.btn_cancel.pack(side=tk.RIGHT, padx=(8, 0), after=self.log_btn)
                    self.prog.pack(side=tk.RIGHT, padx=(12, 0), after=self.btn_cancel)
                    self.prog.stop()
                    self.prog.configure(mode="indeterminate")
                    self.prog.start(14)
                else:
                    self.prog.stop()
                    self.prog.pack_forget()
                    self.btn_cancel.pack_forget()
                    self.prog.configure(mode="determinate", maximum=100)
                    self.prog["value"] = 0
            except tk.TclError:
                pass
            self.status.set(label or ("正在处理…" if busy else "就绪"))
        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self._ui(_apply)

    def _run_task(self, title: str,
                  work: Callable[[Callable[[str], None]], None],
                  on_done: Optional[Callable[[], None]] = None) -> None:
        """在后台线程执行 work(log_fn)，结束后恢复 UI（on_done 在 UI 线程、解除忙碌后执行）。"""
        if self.busy:
            self.status.set("已有任务正在进行，请先等待完成或取消。")
            self._log("忽略新任务：已有任务在运行。")
            return
        self.cancel_event.clear()
        self._set_busy(True, title)   # UI 线程：同步置 busy

        def runner() -> None:
            try:
                work(self._log)
            except sevenz.SevenZipError as exc:
                self._log(f"✗ {exc}")
                self._ui(lambda: messagebox.showerror(APP_TITLE, str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._log("✗ 未预期错误：\n" + traceback.format_exc())
                self._ui(lambda: messagebox.showerror(APP_TITLE, f"未预期错误：{exc}"))
            finally:
                self._set_busy(False)
                if on_done is not None:
                    self._ui(on_done)

        try:
            threading.Thread(target=runner, daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            self._set_busy(False)
            self._log(f"✗ 无法启动后台任务：{exc}")

    # ------------------------------------------------------------ 小工具
    def _password(self) -> Optional[str]:
        return self.pw_var.get() if self.pw_enabled.get() else None

    def _toggle_pw(self) -> None:
        self.pw_entry.configure(state=tk.NORMAL if self.pw_enabled.get() else tk.DISABLED)
        if self.pw_enabled.get():
            self.pw_entry.focus_set()

    def _toggle_pw_show(self) -> None:
        self.pw_entry.configure(show="" if self.pw_show.get() else "•")

    def _require_archive(self) -> bool:
        if not self.archive_path:
            messagebox.showinfo(APP_TITLE, "请先打开一个压缩包。")
            return False
        return True

    def _selected_paths(self) -> List[str]:
        out = []
        for iid in self.tree.selection():
            vals = self.tree.item(iid, "values")
            if vals and len(vals) >= 4:
                out.append(str(vals[3]))
        return out

    def _cancel(self) -> None:
        if not self.busy:
            return
        self.cancel_event.set()
        self._log("⚠ 正在尝试取消…")
        self.status.set("正在取消…")

    def _update_selection_label(self) -> None:
        sel = self.tree.selection()
        if not sel:
            self.sel_label.configure(text="")
            return
        total = 0
        for iid in sel:
            vals = self.tree.item(iid, "values")
            try:
                total += int(str(vals[1]).split()[0]) if vals[1] else 0
            except Exception:  # noqa: BLE001
                pass
        text = f"已选 {len(sel)} 项"
        if total:
            text += f" · {sevenz.format_size(total)}"
        self.sel_label.configure(text=text)

    def _set_info(self, name: str, meta: str) -> None:
        self.info_title.configure(text=name)
        self.info_meta.configure(text=meta or "尚未打开压缩包")
        if self.archive_path:
            self.empty_frame.place_forget()
        else:
            self.info_title.configure(text="压缩包")
            self.empty_frame.place(relx=0.5, rely=0.46, anchor=tk.CENTER)

    # ------------------------------------------------------------ 打开 / 列表
    def choose_open(self) -> None:
        path = filedialog.askopenfilename(
            title="选择压缩包",
            filetypes=[("压缩包", sevenz.FILETYPE_PATTERN), ("所有文件", "*.*")],
        )
        if path:
            self.open_archive(path)

    def close_archive(self) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "请等待当前任务结束。")
            return
        self.archive_path = None
        self.archive_type = ""
        self.entries = []
        self.tree.delete(*self.tree.get_children())
        self.title(APP_TITLE)
        self._set_info("尚未打开压缩包", "")
        self.status.set("就绪")
        self.sel_label.configure(text="")
        self._log("已关闭当前压缩包。")

    def open_archive(self, path: str) -> None:
        path = str(Path(path).expanduser().resolve())
        if not Path(path).is_file():
            messagebox.showerror(APP_TITLE, f"文件不存在：{path}")
            return
        self.archive_path = path
        self.title(f"{Path(path).name} — {APP_NAME}")
        print(f"[sevenzip] open {path}", flush=True)
        self._log(f"打开：{path}")
        self._set_info(Path(path).name, sevenz.format_size(Path(path).stat().st_size))
        self.refresh()

    def refresh(self) -> None:
        if self.busy:
            self._log("有任务正在运行，结束后会自动刷新。")
            return
        if not self._require_archive():
            return
        path = self.archive_path
        pw = self._password()

        def work(log_fn: Callable[[str], None]) -> None:
            # 乱码 ZIP：必须在 7zz 列表之前把文件名改成 UTF-8
            if Path(path).suffix.lower() == ".zip":
                r = self._mojibake_fix(path, quiet_ok=True)
                if r["status"] == "fixed":
                    fix_note.append(r["message"].splitlines()[0])
            try:
                result = sevenz.list_archive(path, pw)
            except sevenz.SevenZipError as exc:
                self._log(f"✗ 列表失败：{exc}")
                self._ui(lambda: messagebox.showerror(APP_TITLE, f"打开/列表失败：\n{exc}"))
                return
            self.entries = result.entries
            self.archive_type = result.archive_type or ""

            def apply() -> None:
                self._set_info(
                    Path(path).name,
                    f"{result.archive_type or '未知格式'} · {len(result.entries)} 项 · "
                    f"{sevenz.format_size(result.physical_size or Path(path).stat().st_size)}",
                )
                self._fill_tree()
                self._log(f"已列出 {len(result.entries)} 项"
                          f"（类型：{result.archive_type or '?'}）。")
            self._ui(apply)

        fix_note: List[str] = []

        def done() -> None:
            # _set_busy(False) 会把状态栏重置为「就绪」，修复提示放在其后
            if fix_note:
                self.status.set(fix_note[0])

        self._run_task("正在读取压缩包…", work, on_done=done)

    def _mojibake_fix(self, path: str, quiet_ok: bool = False) -> dict:
        """后台线程调用：修复 ZIP 文件名乱码。失败只警告，不阻止继续打开。"""
        try:
            r = zip_mojibake.auto_fix(path)
        except Exception as exc:  # noqa: BLE001
            r = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        st, msg = r["status"], r["message"]
        if st == "fixed":
            self._log("✓ " + msg)
            first = msg.splitlines()[0]
            self._ui(lambda: self.status.set(first))
        elif st == "error":
            self._log("⚠ 文件名乱码修复：" + msg)
            self._ui(lambda: messagebox.showwarning(
                APP_TITLE, f"{msg}\n\n将按原样继续打开（中文名可能仍是乱码）。"))
        elif not quiet_ok or "无法判定" in msg:
            self._log(msg)
        else:
            self._log("文件名编码正常，无需修复。")
        return r

    def fix_mojibake_manual(self) -> None:
        """菜单：对当前 .zip（或另选一个）再跑一次文件名修复。"""
        path = self.archive_path
        if not (path and Path(path).suffix.lower() == ".zip"):
            path = filedialog.askopenfilename(
                title="选择要修复文件名的 ZIP", filetypes=[("ZIP", "*.zip *.ZIP")])
            if not path:
                return
        is_current = path == self.archive_path

        def work(log_fn: Callable[[str], None]) -> None:
            r = self._mojibake_fix(path)
            if r["status"] != "error":
                self._ui(lambda: messagebox.showinfo(APP_TITLE, r["message"].splitlines()[0]))

        self._run_task("正在修复 ZIP 文件名…", work,
                       on_done=self.refresh if is_current else None)

    def _sorted_entries(self) -> List[sevenz.ArchiveEntry]:
        key = self._sort_col
        if key == "size":
            keyfn = lambda e: (not e.is_dir, e.size)
        elif key == "modified":
            keyfn = lambda e: (not e.is_dir, e.modified)
        elif key == "path":
            keyfn = lambda e: (not e.is_dir, e.path.lower())
        else:
            keyfn = lambda e: (not e.is_dir, e.name.lower())
        return sorted(self.entries, key=keyfn, reverse=self._sort_desc)

    def _sort_by(self, col: str) -> None:
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        arrows = {"name": "名称", "size": "大小", "modified": "修改时间", "path": "包内路径"}
        arrow = " ▼" if self._sort_desc else " ▲"
        for key, text in arrows.items():
            self.tree.heading(key, text=text + (arrow if key == col else ""))
        self._fill_tree()

    def _fill_tree(self) -> None:
        """分批插入，几十万条目也不会卡死 UI。"""
        self._fill_token += 1
        token = self._fill_token
        self.tree.delete(*self.tree.get_children())
        rows = self._sorted_entries()
        chunk = 400

        def step(start: int) -> None:
            if token != self._fill_token:
                return
            end = min(start + chunk, len(rows))
            for idx in range(start, end):
                e = rows[idx]
                tags = []
                if e.is_dir:
                    tags.append("dir")
                if idx % 2:
                    tags.append("odd")
                self.tree.insert("", tk.END, values=(
                    ("📁 " if e.is_dir else "") + e.name,
                    "" if e.is_dir else sevenz.format_size(e.size),
                    e.modified,
                    e.path,
                ), tags=tuple(tags))
            if end < len(rows):
                self.after(10, lambda: step(end))

        step(0)
        self._update_selection_label()

    # ------------------------------------------------------------ 解压
    def do_extract(self, pick: bool = True, selected: bool = False) -> None:
        if not self._require_archive():
            return
        items: Optional[List[str]] = None
        if selected:
            items = self._selected_paths()
            if not items:
                messagebox.showinfo(APP_TITLE, "请先在列表中选中要解压的条目。")
                return
        if pick:
            dest = filedialog.askdirectory(title="选择解压目标目录")
            if not dest:
                return
        else:
            dest = str(Path(self.archive_path).parent)
        src = self.archive_path
        pw = self._password()
        self._log(f"解压到：{dest}" + (f"（仅 {len(items)} 项）" if items else ""))

        def work(log_fn: Callable[[str], None]) -> None:
            sevenz.extract(src, dest, password=pw, items=items, log=log_fn,
                           cancel_event=self.cancel_event)
            self._log(f"✓ 解压完成 → {dest}")

        self._run_task("正在解压…", work)

    def _on_double_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        self.do_extract(pick=False, selected=True)

    # ------------------------------------------------------------ 测试 / 删除
    def do_test(self) -> None:
        if not self._require_archive():
            return
        src = self.archive_path
        pw = self._password()

        def work(log_fn: Callable[[str], None]) -> None:
            self._log(f"开始测试完整性：{src}")
            sevenz.test(src, password=pw, log=log_fn, cancel_event=self.cancel_event)
            self._log("✓ 测试通过。")

        self._run_task("正在测试完整性…", work)

    def do_delete(self) -> None:
        if not self._require_archive():
            return
        items = self._selected_paths()
        if not items:
            messagebox.showinfo(APP_TITLE, "请先在列表中选中要删除的条目。")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"确定从包中删除选中的 {len(items)} 项？\n（不可撤销；tar/gz 等格式不支持删除）",
        ):
            return
        src = self.archive_path
        pw = self._password()

        def work(log_fn: Callable[[str], None]) -> None:
            self._log(f"删除 {len(items)} 项…")
            sevenz.delete(src, items, password=pw, log=log_fn)
            self._log("✓ 删除完成。")

        self._run_task("正在删除…", work, on_done=self.refresh)

    # ------------------------------------------------------------ 追加
    def add_to_current(self, paths: Optional[Sequence[str]] = None) -> None:
        if not self._require_archive():
            return
        if paths is None:
            picked = filedialog.askopenfilenames(title="选择要添加的文件/文件夹")
            if not picked:
                return
            paths = list(picked)
        items = [str(p) for p in paths]
        if not items:
            return
        src = self.archive_path
        pw = self._password()

        def work(log_fn: Callable[[str], None]) -> None:
            self._log(f"追加 {len(items)} 项到 {Path(src).name} …")
            sevenz.add_to_archive(src, items, password=pw, log=log_fn,
                                  cancel_event=self.cancel_event)
            self._log("✓ 追加完成。")

        self._run_task("正在添加到当前包…", work, on_done=self.refresh)

    # ------------------------------------------------------------ 拖放
    # 铁律：AppKit draggingEntered:/performDragOperation: 里禁止任何 Tk API
    # （含 after / after_idle / configure / messagebox / grab）。只往 _dnd_q put。
    def _queue_drop(self, paths: Sequence[str]) -> None:
        """AppKit performDragOperation:：只入队，立刻返回。"""
        try:
            self._dnd_q.put(("drop", [str(p) for p in paths]))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _process_drop(self) -> None:
        self._drop_job = None
        paths, self._drop_pending = self._drop_pending, []
        try:
            self._handle_drop(paths)
        except Exception:  # noqa: BLE001
            self._log("✗ 处理拖入失败：\n" + traceback.format_exc())
            self.status.set("拖入处理失败，详见日志")

    def _handle_drop(self, paths: Sequence[str]) -> None:
        """访达拖入（已在 Tk 定时器里）：有打开的包 → 后台追加；没有 → 打开 / 新建。"""
        seen = set()
        paths = [p for p in (str(x) for x in paths)
                 if Path(p).exists() and not (p in seen or seen.add(p))]
        if not paths:
            return
        print(f"[sevenzip] drop {paths}", flush=True)
        self._log(f"收到拖入 {len(paths)} 项")
        if self.busy:
            self.status.set("已有任务在进行，本次拖入已忽略")
            self._log(f"拖入 {len(paths)} 项被忽略：当前有任务在运行。")
            return
        if self.archive_path:
            if self.archive_path in paths:
                self.status.set("不能把压缩包自身添加进去")
                return
            # 已打开压缩包：追加。不再弹任何对话框。
            self.add_to_current(paths)
            return

        archives = [p for p in paths if sevenz.is_archive_name(p)]
        if archives:
            self.open_archive(archives[0])
            if len(paths) > 1:
                self._log(f"其余 {len(paths) - 1} 项已忽略（可打开包后再拖入以添加）。")
            return

        # 无打开包且非压缩包：直接进新建对话框，避开 askyesno 模态死锁
        self.status.set(f"收到 {len(paths)} 项 · 打开新建对话框")
        self.after(120, lambda: self.open_create_dialog(initial_items=paths))

    def _handle_open_paths(self, paths: Sequence[str]) -> None:
        """Finder 双击 / 「打开方式」/ Dock 图标拖入（Apple Event）。"""
        paths = [str(p) for p in paths if Path(p).exists()]
        if not paths:
            return
        print(f"[sevenzip] odoc {paths}", flush=True)
        first = next((p for p in paths if sevenz.is_archive_name(p)), paths[0])
        if self.busy:
            self._log(f"收到打开请求：{first}（当前有任务在跑，稍后重试）")
            return
        self.open_archive(first)

    # ------------------------------------------------------------ 新建压缩包
    def open_create_dialog(self, initial_items: Optional[Sequence[str]] = None) -> None:
        if self.busy:
            self.status.set("请等待当前任务结束后再新建")
            return
        dlg = tk.Toplevel(self)
        dlg.title("新建压缩包")
        dlg.geometry("740x760")
        dlg.minsize(660, 680)
        dlg.configure(background=BACKGROUND)
        dlg.transient(self)

        def _grab() -> None:
            # grab 延后到窗口映射之后，避免和拖放 / 上一个模态框同一拍抢输入
            try:
                if dlg.winfo_exists():
                    dlg.grab_set()
                    dlg.focus_set()
            except tk.TclError:
                pass
        dlg.after(60, _grab)

        queue_items: List[str] = [str(p) for p in (initial_items or [])]

        frame = ttk.Frame(dlg, padding=(28, 24, 28, 24))
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, text="新建压缩包", style="DialogTitle.TLabel").grid(
            row=0, column=0, sticky=tk.W,
        )
        ttk.Label(frame, text="选择要打包的内容，设置格式后保存。",
                  style="Note.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(4, 18))

        # --- 内容 ---
        # 对话框专用 Treeview 样式：行高加大，避免中文被裁切；不影响主窗口列表
        dstyle = ttk.Style(dlg)
        dstyle.configure("Create.Treeview", rowheight=32)
        dstyle.configure("Create.Treeview.Heading", padding=(10, 6))

        content = ttk.Labelframe(frame, text="内容", padding=(16, 12, 16, 16))
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        qframe = tk.Frame(content, background=SURFACE, highlightbackground=BORDER,
                          highlightthickness=1, borderwidth=0)
        qframe.grid(row=0, column=0, sticky="nsew")
        qlist = ttk.Treeview(qframe, columns=("name", "where"), show="headings",
                             selectmode="extended", height=10,
                             style="Create.Treeview")
        qlist.heading("name", text="名称", anchor=tk.W)
        qlist.heading("where", text="位置", anchor=tk.W)
        qlist.column("name", width=260, minwidth=140, anchor=tk.W, stretch=True)
        qlist.column("where", width=340, minwidth=160, anchor=tk.W, stretch=True)
        qsb = ttk.Scrollbar(qframe, orient=tk.VERTICAL, command=qlist.yview)
        qlist.configure(yscrollcommand=qsb.set)
        qlist.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        qsb.pack(side=tk.RIGHT, fill=tk.Y)

        def _label(path: str):
            p = Path(path)
            name = p.name + ("/" if p.is_dir() else "")
            return name, str(p.parent)

        def refresh_list() -> None:
            qlist.delete(*qlist.get_children())
            for it in queue_items:
                name, where = _label(it)
                qlist.insert("", tk.END, values=(name, where), tags=(it,))
            count_label.configure(text=f"{len(queue_items)} 项")

        def add_files() -> None:
            for p in filedialog.askopenfilenames(title="添加文件", parent=dlg):
                if p not in queue_items:
                    queue_items.append(p)
            refresh_list()

        def add_folder() -> None:
            p = filedialog.askdirectory(title="添加文件夹", parent=dlg)
            if p and p not in queue_items:
                queue_items.append(p)
            refresh_list()

        def remove_sel() -> None:
            for iid in qlist.selection():
                tags = qlist.item(iid, "tags")
                if tags and tags[0] in queue_items:
                    queue_items.remove(tags[0])
            refresh_list()

        btns = ttk.Frame(content)
        btns.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="添加文件…", style="Toolbar.TButton",
                   command=add_files).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btns, text="添加文件夹…", style="Toolbar.TButton",
                   command=add_folder).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btns, text="移除", style="Toolbar.TButton",
                   command=remove_sel).pack(side=tk.LEFT)
        count_label = ttk.Label(btns, text="0 项", style="Note.TLabel")
        count_label.pack(side=tk.RIGHT)

        # --- 选项：标签右对齐、控件左对齐的系统表单 ---
        opt = ttk.Labelframe(frame, text="选项", padding=(16, 12, 16, 16))
        opt.grid(row=3, column=0, sticky="ew", pady=(20, 0))
        opt.columnconfigure(1, weight=1)

        def form_label(text: str, row: int) -> None:
            ttk.Label(opt, text=text, style="Form.TLabel", anchor=tk.E, width=8).grid(
                row=row, column=0, sticky=tk.E, padx=(0, 12), pady=7,
            )

        form_label("格式：", 0)
        fmt_var = tk.StringVar(value="7z")
        fmt_box = ttk.Combobox(opt, textvariable=fmt_var, state="readonly", width=14,
                               values=("7z", "zip", "tar", "tar.gz"))
        fmt_box.grid(row=0, column=1, sticky=tk.W, pady=7)

        form_label("压缩级别：", 1)
        lv_var = tk.StringVar(value=sevenz.DEFAULT_LEVEL)
        ttk.Combobox(opt, textvariable=lv_var, state="readonly", width=14,
                     values=tuple(sevenz.LEVEL_MAP.keys())).grid(
                         row=1, column=1, sticky=tk.W, pady=7,
                     )

        form_label("密码：", 2)
        pw_row = ttk.Frame(opt)
        pw_row.grid(row=2, column=1, sticky="ew", pady=7)
        pw_var = tk.StringVar()
        pw_entry = ttk.Entry(pw_row, textvariable=pw_var, show="•", width=22)
        pw_entry.pack(side=tk.LEFT)
        enc_names = tk.BooleanVar(value=True)
        enc_chk = ttk.Checkbutton(pw_row, text="加密文件名", variable=enc_names)
        enc_chk.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(opt, text="密码可选；加密文件名仅 7z 支持，tar / tar.gz 不支持密码。",
                  style="Note.TLabel").grid(row=3, column=1, sticky=tk.W, pady=(4, 0))

        # --- 保存位置 ---
        outframe = ttk.Labelframe(frame, text="存储为", padding=(16, 12, 16, 16))
        outframe.grid(row=4, column=0, sticky="ew", pady=(20, 0))
        outframe.columnconfigure(0, weight=1)
        out_var = tk.StringVar()
        ttk.Entry(outframe, textvariable=out_var).grid(row=0, column=0, sticky="ew",
                                                       padx=(0, 10), ipady=2)

        def pick_out() -> None:
            ext = sevenz.CREATE_FORMATS.get(fmt_var.get(), {}).get("ext", ".7z")
            types = [("所有文件", "*.*")]
            if fmt_var.get() == "tar.gz":
                types.insert(0, ("tar.gz", "*.tar.gz"))
            p = filedialog.asksaveasfilename(title="保存压缩包为", parent=dlg,
                                             initialfile="archive" + ext,
                                             defaultextension=ext, filetypes=types)
            if p:
                out_var.set(p)

        ttk.Button(outframe, text="选取…", command=pick_out).grid(row=0, column=1, sticky="ns")

        def on_fmt_change(*_args) -> None:
            fmt = fmt_var.get()
            if fmt in ("tar", "tar.gz"):
                pw_entry.configure(state=tk.DISABLED)
                enc_chk.state(["disabled"])
            else:
                pw_entry.configure(state=tk.NORMAL)
                enc_chk.state(["!disabled"] if fmt == "7z" else ["disabled"])

        fmt_box.bind("<<ComboboxSelected>>", on_fmt_change)
        on_fmt_change()

        def do_create() -> None:
            if not queue_items:
                messagebox.showinfo("新建压缩包", "请先把文件/文件夹加入队列。", parent=dlg)
                return
            out = out_var.get().strip()
            if not out:
                messagebox.showinfo("新建压缩包", "请选择保存位置。", parent=dlg)
                return
            fmt = fmt_var.get()
            pw = pw_var.get() or None
            if pw and fmt in ("tar", "tar.gz"):
                messagebox.showerror("新建压缩包", "tar / tar.gz 不支持密码。", parent=dlg)
                return
            dlg.destroy()
            self._log(f"创建 {fmt} → {out}（{len(queue_items)} 项，级别 {lv_var.get()}）")

            def work(log_fn: Callable[[str], None]) -> None:
                sevenz.create_archive(
                    out, list(queue_items), fmt=fmt, level=lv_var.get(),
                    password=pw, encrypt_names=enc_names.get(),
                    log=log_fn, cancel_event=self.cancel_event,
                )
                self._log(f"✓ 创建完成：{out}")

            self._run_task("正在创建压缩包…", work,
                           on_done=lambda: self.open_archive(out))

        # --- 按钮区：右下角「取消 / 创建」，回车 = 创建 ---
        bottom = ttk.Frame(frame)
        bottom.grid(row=5, column=0, sticky="ew", pady=(24, 4))
        ttk.Button(bottom, text="创建", command=do_create,
                   default="active", width=8).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="取消", command=dlg.destroy,
                   width=8).pack(side=tk.RIGHT, padx=(0, 10))

        refresh_list()
        if queue_items:
            out_ext = sevenz.CREATE_FORMATS["7z"]["ext"]
            out_var.set(str(Path(queue_items[0]).parent / ("archive" + out_ext)))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Return>", lambda e: do_create())

    # ------------------------------------------------------------ 右键菜单
    def _context_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        has_sel = bool(self.tree.selection())
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="解压选中项到…", state=tk.NORMAL if has_sel else tk.DISABLED,
                         command=lambda: self.do_extract(pick=True, selected=True))
        menu.add_command(label="解压到当前文件夹", command=lambda: self.do_extract(pick=False))
        menu.add_separator()
        menu.add_command(label="测试完整性", command=self.do_test)
        menu.add_command(label="删除选中条目", state=tk.NORMAL if has_sel else tk.DISABLED,
                         command=self.do_delete)
        menu.add_command(label="复制包内路径", state=tk.NORMAL if has_sel else tk.DISABLED,
                         command=self._copy_paths)
        menu.add_separator()
        menu.add_command(label="刷新列表", command=self.refresh)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_paths(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(paths))
        self._log(f"已复制 {len(paths)} 条包内路径。")

    # ------------------------------------------------------------ 帮助
    def _readme_path(self) -> Optional[Path]:
        for p in (APP_DIR / "使用说明.md", PROJECT_ROOT / "使用说明.md",
                  PROJECT_ROOT.parent / "使用说明.md"):
            if p.is_file():
                return p
        return None

    def show_readme(self) -> None:
        top = tk.Toplevel(self)
        top.title("使用说明 — D-zip")
        top.geometry("780x660")
        top.minsize(560, 420)
        top.configure(background=BACKGROUND)
        frame = ttk.Frame(top, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="使用说明", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(frame, text="D-zip · 浏览、解压与创建压缩包", style="Muted.TLabel").pack(
            anchor=tk.W, pady=(6, 16),
        )
        content = tk.Frame(frame, background=SURFACE, highlightbackground=BORDER,
                            highlightthickness=1, borderwidth=0)
        content.pack(fill=tk.BOTH, expand=True)
        text = tk.Text(content, wrap=tk.WORD, font="TkTextFont", padx=16, pady=16,
                       spacing1=2, spacing3=5, relief=tk.FLAT, borderwidth=0,
                       background=SURFACE, foreground=TEXT, highlightthickness=0)
        sb = ttk.Scrollbar(content, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        readme = self._readme_path()
        try:
            text.insert(tk.END, readme.read_text(encoding="utf-8") if readme else "（找不到 使用说明.md）")
        except OSError as exc:
            text.insert(tk.END, f"读取失败：{exc}")
        text.configure(state=tk.DISABLED)

    def show_about(self) -> None:
        # 保留系统原生提示框，以 message/detail 区分标题与说明。
        messagebox.showinfo(
            "关于 D-zip", APP_NAME, parent=self,
            detail=(
                "内置 7zz 引擎的压缩工具 · tkinter + Tk 9\n\n"
                "浏览、解压、追加、删除与完整性测试。\n"
                "创建 7z、zip、tar 与 tar.gz；RAR 仅支持解压，不能新建或追加。\n"
                "从访达拖入文件，可添加到当前压缩包。\n\n"
                f"引擎：{sevenz.engine_path()}\n{sevenz.engine_version()}"
            ),
        )

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(APP_TITLE, "仍有任务在运行，确定退出？"):
                return
            self.cancel_event.set()
        self.destroy()


def main() -> int:
    if not sevenz.engine_available():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            f"找不到引擎文件：{sevenz.engine_path()}\n请确认 bin/7zz 存在且可执行。",
        )
        return 1

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    app = App(initial_paths=args)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
