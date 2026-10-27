# -*- coding: utf-8 -*-
"""macOS 原生桥（PyObjC）。

三个能力，全部「可用则用、不可用则安全降级」，不影响 tkinter 主界面：

1. **窗口拖放**：往 Tk 窗口的 contentView 上叠一个透明 NSView，
   注册为 NSDraggingDestination；`hitTest:` 返回 None 让鼠标事件穿透给 Tk，
   但 AppKit 的拖放投递（draggingEntered:/performDragOperation:）不经过 hitTest，
   所以既能收到访达拖入的文件，又不影响点按钮/滚表格。
2. **双击文档打开**：注册 Tcl 命令 `::tk::mac::OpenDocument`（Tk 内建 Apple Event
   处理会调用它），拿到 Finder /「打开方式」传来的路径。
3. **窗口前置 / Dock 图标 / 应用菜单名**：避免启动后藏在别的窗口后面。

PyObjC 由项目自带（`vendor/pylibs`，同步进 `D-zip.app/Contents/Resources/pylibs`），
不依赖系统 Python，也不会拉回 Tk 8.5。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

APP_DIR = Path(__file__).resolve().parent
_PARENT = APP_DIR.parent

# 优先 .app 内的 Resources/pylibs，其次桌面工程的 vendor/pylibs
_VENDOR_DIRS = (
    _PARENT / "pylibs",
    _PARENT / "vendor" / "pylibs",
)

_FILE_URI_TYPE = "public.file-url"
_OLD_FILENAMES_TYPE = "NSFilenamesPboardType"

_state = {
    "view": None,
    "window": None,
    "drop": False,
    "open_doc": False,
    "error": "",
}


# ---------------------------------------------------------------------------
# 依赖装载
# ---------------------------------------------------------------------------

def _ensure_vendor_path() -> Optional[str]:
    for d in _VENDOR_DIRS:
        if d.is_dir():
            p = str(d)
            if p not in sys.path:
                sys.path.insert(0, p)
            return p
    return None


def _load():
    """导入 AppKit；失败返回 None（调用方降级）。"""
    _ensure_vendor_path()
    try:
        import objc  # noqa: F401
        import AppKit  # noqa: F401
        from AppKit import NSApplication, NSApp  # noqa: F401
        return AppKit
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        return None


def available() -> bool:
    return _load() is not None


def status() -> str:
    return (
        f"拖放={'开' if _state['drop'] else '关'} "
        f"双击打开={'开' if _state['open_doc'] else '关'}"
        + (f"（{_state['error']}）" if _state["error"] else "")
    )


# ---------------------------------------------------------------------------
# 窗口 / 激活
# ---------------------------------------------------------------------------

def _tk_ns_window(root):
    """找到 Tk 根窗口对应的 NSWindow（按 contentView 类型 + 标题匹配）。"""
    AppKit = _load()
    if AppKit is None:
        return None
    candidates = []
    for win in AppKit.NSApp.windows():
        cv = win.contentView()
        if cv is None:
            continue
        if "TKContentView" in cv.__class__.__name__:
            candidates.append(win)
    if not candidates:
        return None
    try:
        title = str(root.title())
    except Exception:  # noqa: BLE001
        title = ""
    for win in candidates:
        try:
            if str(win.title()) == title:
                return win
        except Exception:  # noqa: BLE001
            continue
    return candidates[0]


def activate(root=None) -> bool:
    """把本进程提到最前（比 osascript/System Events 更可靠，无需自动化权限）。"""
    AppKit = _load()
    if AppKit is None:
        return False
    try:
        app = AppKit.NSApplication.sharedApplication()
        try:
            app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular
        except Exception:  # noqa: BLE001
            pass
        win = _tk_ns_window(root) if root is not None else None
        if win is not None:
            win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
        return True
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        return False


def set_dock_icon(path: str) -> bool:
    AppKit = _load()
    if AppKit is None or not Path(path).is_file():
        return False
    try:
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))
        if image is None:
            return False
        AppKit.NSApplication.sharedApplication().setApplicationIconImage_(image)
        return True
    except Exception:  # noqa: BLE001
        return False


def set_app_menu_title(title: str) -> bool:
    """把菜单栏第一个菜单（应用菜单）改成传入的 title（本工程传 D-zip），避免显示 “Python”。"""
    AppKit = _load()
    if AppKit is None:
        return False
    try:
        menu = AppKit.NSApp.mainMenu()
        if menu is None or menu.numberOfItems() == 0:
            return False
        item = menu.itemAtIndex_(0)
        if item is None:
            return False
        item.setTitle_(title)
        sub = item.submenu()
        if sub is not None and sub.numberOfItems() > 0:
            hide_item = sub.itemAtIndex_(0)
            try:
                hide_item.setTitle_(f"隐藏 {title}")
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 拖放
# ---------------------------------------------------------------------------

def _pasteboard_paths(pboard) -> List[str]:
    """从拖放剪贴板里取出文件路径（兼容 file-url 与旧式 filenames 类型）。"""
    AppKit = _load()
    paths: List[str] = []
    if AppKit is None or pboard is None:
        return paths
    try:
        urls = pboard.readObjectsForClasses_options_([AppKit.NSURL], None) or []
        for url in urls:
            try:
                p = url.path()
            except Exception:  # noqa: BLE001
                p = None
            if p:
                paths.append(str(p))
    except Exception:  # noqa: BLE001
        pass
    if not paths:
        try:
            plist = pboard.propertyListForType_(_OLD_FILENAMES_TYPE)
            if isinstance(plist, (list, tuple)):
                paths = [str(x) for x in plist]
        except Exception:  # noqa: BLE001
            pass
    # 去重保序
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def install_drop_target(root,
                        on_drop: Callable[[List[str]], None],
                        on_enter: Optional[Callable[[int], None]] = None,
                        on_exit: Optional[Callable[[], None]] = None) -> bool:
    """给 Tk 根窗口挂 NSDraggingDestination。

    on_drop(paths)   放下时回调（主线程，AppKit 事件里；只许排队，不许弹窗）
    on_enter(count)  拖入窗口时回调（count=可接收的文件数）
    on_exit()        拖出窗口/取消时回调
    """
    AppKit = _load()
    if AppKit is None:
        return False
    import objc

    if _state["view"] is not None:
        return True

    root.update_idletasks()
    win = _tk_ns_window(root)
    if win is None:
        _state["error"] = "找不到 Tk 的 NSWindow"
        return False

    content = win.contentView()
    if content is None:
        return False

    def _drop_count(sender) -> int:
        # 拖放会话里不要 Path.exists / stat，iCloud 占位文件会卡住
        return len(_pasteboard_paths(sender.draggingPasteboard()))

    def _set_hover(view, hover: bool) -> None:
        view.hover = hover
        view.setNeedsDisplay_(True)

    class DropView(AppKit.NSView):
        """透明覆盖层：只负责接收拖放，鼠标事件全部穿透。"""

        def initWithFrame_(self, frame):
            self = objc.super(DropView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.hover = False
            self.registerForDraggedTypes_([_FILE_URI_TYPE, _OLD_FILENAMES_TYPE])
            self.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
            return self

        # --- 视觉：不透明=否，未拖拽时不画任何东西 ---
        def isOpaque(self):
            return False

        def hitTest_(self, point):
            # 返回 None → 鼠标事件继续交给下面的 Tk 视图；拖放事件不受影响
            return None

        def drawRect_(self, rect):
            if not self.hover:
                return
            try:
                inset = AppKit.NSInsetRect(rect, 5.0, 5.0)
                path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    inset, 12.0, 12.0
                )
                accent = AppKit.NSColor.controlAccentColor()
                accent.colorWithAlphaComponent_(0.10).set()
                path.fill()
                accent.colorWithAlphaComponent_(0.85).set()
                path.setLineWidth_(3.5)
                path.stroke()
            except Exception:  # noqa: BLE001
                pass

        # --- NSDraggingDestination ---
        def draggingEntered_(self, sender):
            count = _drop_count(sender)
            # 缓存：draggingUpdated: 每次鼠标移动都会调，不要反复读剪贴板 / stat 文件
            self.dropCount = count
            if count == 0:
                return AppKit.NSDragOperationNone
            _set_hover(self, True)
            if on_enter:
                try:
                    on_enter(count)
                except Exception:  # noqa: BLE001
                    pass
            return AppKit.NSDragOperationCopy

        def draggingUpdated_(self, sender):
            if not getattr(self, "dropCount", 0):
                return AppKit.NSDragOperationNone
            return AppKit.NSDragOperationCopy

        def draggingExited_(self, sender):
            _set_hover(self, False)
            if on_exit:
                try:
                    on_exit()
                except Exception:  # noqa: BLE001
                    pass

        def draggingEnded_(self, sender):
            _set_hover(self, False)

        def prepareForDragOperation_(self, sender):
            return _drop_count(sender) > 0

        def performDragOperation_(self, sender):
            _set_hover(self, False)
            # 先把路径拷出来再回调；回调里禁止任何 Tk API，只能入队。
            # 存在性检查留给 Tk 侧；这里只拷贝路径字符串
            paths = list(_pasteboard_paths(sender.draggingPasteboard()))
            if not paths:
                return False
            try:
                on_drop(paths)
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            return True

        def concludeDragOperation_(self, sender):
            pass

    try:
        bounds = content.bounds()
        view = DropView.alloc().initWithFrame_(bounds)
        if view is None:
            return False
        content.addSubview_(view)
        _state["view"] = view
        _state["window"] = win
        _state["drop"] = True
        return True
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        return False


# ---------------------------------------------------------------------------
# 双击文档 / 「打开方式」
# ---------------------------------------------------------------------------

def install_open_document(root, on_paths: Callable[[Sequence[str]], None]) -> bool:
    """注册 Tk 的 Apple Event 钩子：Finder 双击 / 打开方式 / 拖到 Dock 图标。

    只依赖 Tk 内建的事件处理（PyObjC 缺失也能用；只有「激活窗口」会退化为 Tk 自身调用）。
    """
    _load()  # 有 PyObjC 时顺便把 vendor 路径挂上
    ok = False

    def _open(*paths) -> None:
        items = [str(p) for p in paths if p]
        if items:
            on_paths(items)

    try:
        root.createcommand("::tk::mac::OpenDocument", _open)
        ok = True
    except Exception:  # noqa: BLE001
        pass
    try:
        root.createcommand("::tk::mac::OpenDocuments", _open)
        ok = True
    except Exception:  # noqa: BLE001
        pass

    def _reopen(*_args) -> None:
        try:
            activate(root)
            root.deiconify()
            root.lift()
        except Exception:  # noqa: BLE001
            pass

    try:
        root.createcommand("::tk::mac::ReopenApplication", _reopen)
    except Exception:  # noqa: BLE001
        pass

    _state["open_doc"] = ok
    return ok
