"""引擎封装（7zz）：所有操作通过项目内 bin/7zz 子进程完成。"""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# 项目根目录 = app/ 的上一级（桌面工程 → 项目根；.app 内 → Resources）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENGINE = PROJECT_ROOT / "bin" / "7zz"

LogFn = Callable[[str], None]

# 认得的压缩包扩展名（用于「拖入的是压缩包就当作打开」判断 + 文件对话框过滤）
ARCHIVE_SUFFIXES = (
    ".7z", ".zip", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz",
    ".tar.xz", ".txz", ".gz", ".bz2", ".xz", ".zst", ".cab", ".iso", ".dmg",
    ".wim", ".arj", ".lzh", ".lha", ".cpio", ".rpm", ".deb", ".jar", ".apk",
)
FILETYPE_PATTERN = " ".join("*" + s for s in ARCHIVE_SUFFIXES)


def is_archive_name(path: str) -> bool:
    return str(path).lower().endswith(ARCHIVE_SUFFIXES)



class SevenZipError(RuntimeError):
    """7zz 执行失败。"""

    def __init__(self, message: str, returncode: int = -1, output: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.output = output


@dataclass
class ArchiveEntry:
    path: str          # 归档内相对路径
    size: int          # 字节数（文件夹为 0）
    modified: str      # 修改时间字符串
    is_dir: bool

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class ListResult:
    entries: List[ArchiveEntry] = field(default_factory=list)
    archive_path: str = ""
    archive_type: str = ""
    physical_size: int = 0


def engine_path() -> str:
    return str(ENGINE)


def engine_available() -> bool:
    return ENGINE.is_file() and os.access(ENGINE, os.X_OK)


def engine_version() -> str:
    try:
        out = subprocess.run([str(ENGINE)], capture_output=True, text=True, timeout=10)
        first = (out.stdout or out.stderr).strip().splitlines()
        return first[0] if first else "未知版本"
    except Exception as exc:  # noqa: BLE001
        return f"无法获取版本：{exc}"


# ---------------------------------------------------------------------------
# 通用子进程
# ---------------------------------------------------------------------------

def _base_cmd() -> List[str]:
    if not engine_available():
        raise SevenZipError(f"找不到引擎：{ENGINE}")
    return [str(ENGINE)]


def _password_args(password: Optional[str]) -> List[str]:
    """仅在提供密码时传 -p{pwd}。

    注意：7zz 的空 `-p` 会让部分子命令（如 d/a）进入「Enter password:」交互，
    因此无密码时绝不传 -p；非交互性由 stdin=DEVNULL + start_new_session 保证
    （加密包缺密码会直接报 Wrong password，而不是卡住）。
    """
    if password:
        return ["-p" + password]
    return []


def run_capture(args: Sequence[str], timeout: Optional[int] = None):
    """同步执行，返回 (rc, stdout, stderr)。"""
    cmd = _base_cmd() + list(args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise SevenZipError(f"执行超时（{timeout}s）：{' '.join(cmd)}") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_stream(args: Sequence[str],
               log: Optional[LogFn] = None,
               cancel_event=None,
               cwd: Optional[str] = None) -> int:
    """执行并把 stdout/stderr 流式写入 log 回调；支持 cancel_event 取消。

    返回 returncode（被取消时返回 -15）。
    """
    cmd = _base_cmd() + list(args)
    if log:
        log("$ " + " ".join(_redact(cmd)))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # 独立会话，便于整组取消
        bufsize=1,
        cwd=cwd,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if log:
                log(line.rstrip("\n"))
            if cancel_event is not None and cancel_event.is_set():
                _terminate(proc)
                if log:
                    log("⚠ 已取消操作。")
                return -15
        proc.wait()
        return proc.returncode
    finally:
        if proc.poll() is None:
            _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _redact(cmd: List[str]) -> List[str]:
    """日志里隐藏密码。"""
    out = []
    for part in cmd:
        if part.startswith("-p") and len(part) > 2:
            out.append("-p****")
        else:
            out.append(part)
    return out


def _summarize(stderr: str, stdout: str = "") -> str:
    text = (stderr or stdout or "").strip()
    if not text:
        return "（无错误详情）"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    keep = lines[-8:] if len(lines) > 8 else lines
    return "\n".join(keep)


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------

def _parse_slt(output: str) -> ListResult:
    """解析 7zz l -slt 输出。

    实测 26.03 的 -slt：归档信息块与条目之间以 `----------` 分隔，
    条目与条目之间以空行分隔；每条记录以 `Path =` 开头。
    因此按「遇到新的 Path = 就换记录」来切分最稳。
    """
    result = ListResult()
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in output.splitlines():
        if line.strip() == "----------":
            if current:
                records.append(current)
                current = {}
            continue
        if " = " not in line:
            continue
        key, _, val = line.partition(" = ")
        key = key.strip()
        if key == "Path" and "Path" in current:
            records.append(current)
            current = {}
        current[key] = val.strip()
    if current:
        records.append(current)

    archive_seen = False
    for fields in records:
        path = fields.get("Path", "")
        if not path:
            continue
        is_dir = fields.get("Folder", "-") == "+"
        has_entry_hint = ("Folder" in fields or "Modified" in fields
                          or "Attributes" in fields or "Packed Size" in fields)
        if not archive_seen and ("Type" in fields or "Physical Size" in fields) \
                and not has_entry_hint:
            # 第一个 Path 块是归档自身信息
            result.archive_path = path
            result.archive_type = fields.get("Type", "")
            try:
                result.physical_size = int(fields.get("Physical Size", "0") or 0)
            except ValueError:
                result.physical_size = 0
            archive_seen = True
            continue
        archive_seen = True
        try:
            size = int(fields.get("Size", "0") or 0)
        except ValueError:
            size = 0
        modified = fields.get("Modified", "")
        # 截断到秒，便于表格显示
        if modified and "." in modified:
            modified = modified.split(".")[0]
        result.entries.append(
            ArchiveEntry(path=path, size=size, modified=modified, is_dir=is_dir)
        )
    return result


def list_archive(archive: str, password: Optional[str] = None) -> ListResult:
    """列出归档内容（7zz l -slt）。"""
    args = ["l", "-slt", "-y"] + _password_args(password) + ["--", str(archive)]
    rc, out, err = run_capture(args, timeout=120)
    if rc != 0:
        raise SevenZipError(f"无法打开压缩包：{_summarize(err, out)}", rc, out + err)
    result = _parse_slt(out)
    if not result.entries and not result.archive_type:
        # 某些包解析不出 Type，但仍可能有条目；再兜底一次
        if "Path" not in out:
            raise SevenZipError(f"无法解析压缩包内容：{_summarize(err, out)}", rc, out)
    return result


# ---------------------------------------------------------------------------
# 解压 / 测试 / 删除 / 追加 / 创建
# ---------------------------------------------------------------------------

COMPOUND_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def extract(archive: str,
            dest: str,
            password: Optional[str] = None,
            items: Optional[Sequence[str]] = None,
            log: Optional[LogFn] = None,
            cancel_event=None) -> int:
    """解压（保留路径，7zz x）。

    对 .tar.gz/.tgz 等复合包自动再解一层 tar，避免用户手动二次解压。
    """
    Path(dest).mkdir(parents=True, exist_ok=True)
    args = ["x", "-y", f"-o{dest}"] + _password_args(password) + ["--", str(archive)]
    if items:
        args += list(items)
    rc = run_stream(args, log=log, cancel_event=cancel_event)
    if rc not in (0, 1):  # 1 = 有警告
        raise SevenZipError(f"解压失败（代码 {rc}），详见日志区。", rc)

    name = Path(archive).name.lower()
    if name.endswith(COMPOUND_SUFFIXES) and not items:
        try:
            info = list_archive(archive, password)
        except SevenZipError:
            return rc
        for entry in info.entries:
            if entry.is_dir or not entry.path.lower().endswith(".tar"):
                continue
            tar_file = Path(dest) / entry.path
            if not tar_file.is_file():
                continue
            if log:
                log(f"检测到复合包，自动展开内层 tar：{entry.path}")
            rc2 = run_stream(
                ["x", "-y", f"-o{dest}", "--", str(tar_file)],
                log=log, cancel_event=cancel_event,
            )
            if rc2 in (0, 1):
                try:
                    tar_file.unlink()
                except OSError:
                    pass
            else:
                if log:
                    log(f"⚠ 内层 tar 解压失败（代码 {rc2}），已保留 {entry.path}。")
    return rc


def test(archive: str,
         password: Optional[str] = None,
         log: Optional[LogFn] = None,
         cancel_event=None) -> int:
    """测试完整性（7zz t）。"""
    args = ["t", "-y"] + _password_args(password) + ["--", str(archive)]
    rc = run_stream(args, log=log, cancel_event=cancel_event)
    if rc not in (0, 1):
        raise SevenZipError(f"测试失败（代码 {rc}），详见日志区。", rc)
    return rc


def delete(archive: str,
           items: Sequence[str],
           password: Optional[str] = None,
           log: Optional[LogFn] = None) -> int:
    """删除包内条目（7zz d）。部分格式不支持删除。"""
    if not items:
        raise SevenZipError("未选择要删除的条目。")
    args = ["d", "-y"] + _password_args(password) + ["--", str(archive)] + list(items)
    rc = run_stream(args, log=log)
    if rc not in (0, 1):
        raise SevenZipError(
            f"删除失败（代码 {rc}）。该格式可能不支持删除，或条目路径不匹配，详见日志区。", rc
        )
    return rc


def _relativize(items: Sequence[str]) -> Tuple[str, List[str]]:
    """把待加入的路径换算成「共同父目录 + 相对路径」。

    7zz a/x 会按命令行给的路径写入包内结构；直接传绝对路径会得到
    `Users/xxx/Desktop/a.txt` 这种垃圾层级。这里取所有条目的共同父目录作为 cwd，
    再把条目换成相对名，于是拖入一个文件就是 `a.txt`，拖入一个文件夹就是 `文件夹/…`。
    """
    abs_items = [str(Path(os.path.expanduser(str(p))).absolute()) for p in items]
    parents = [str(Path(p).parent) for p in abs_items]
    base = parents[0]
    if len(parents) > 1:
        try:
            base = os.path.commonpath(parents)
        except ValueError:
            base = "/"
    if not base or not Path(base).is_dir():
        base = "/"
    rel = [os.path.relpath(p, base) for p in abs_items]
    return base, rel


def add_to_archive(archive: str,
                   items: Sequence[str],
                   password: Optional[str] = None,
                   log: Optional[LogFn] = None,
                   cancel_event=None) -> int:
    """向已有归档追加文件/文件夹（7zz a）。"""
    if not items:
        raise SevenZipError("没有可添加的文件。")
    base, rel = _relativize(items)
    archive = str(Path(os.path.expanduser(str(archive))).absolute())
    args = ["a", "-y"] + _password_args(password) + ["--", archive] + rel
    rc = run_stream(args, log=log, cancel_event=cancel_event, cwd=base)
    if rc not in (0, 1):
        raise SevenZipError(
            f"追加失败（代码 {rc}）。该格式可能不支持追加，详见日志区。", rc
        )
    return rc


# 创建相关：格式 / 级别
CREATE_FORMATS = {
    "zip": {"ext": ".zip", "type": "-tzip", "encrypt_names": False},
    "7z": {"ext": ".7z", "type": "-t7z", "encrypt_names": True},
    "tar": {"ext": ".tar", "type": "-ttar", "encrypt_names": False},
    "tar.gz": {"ext": ".tar.gz", "type": None, "encrypt_names": False},  # 两步创建
}

LEVEL_MAP = {
    "仅存储": "0",
    "快速": "1",
    "标准": "5",
    "最大": "9",
}

DEFAULT_LEVEL = "标准"


def create_archive(output: str,
                   items: Sequence[str],
                   fmt: str = "7z",
                   level: str = DEFAULT_LEVEL,
                   password: Optional[str] = None,
                   encrypt_names: bool = True,
                   log: Optional[LogFn] = None,
                   cancel_event=None) -> int:
    """创建压缩包。fmt: zip / 7z / tar / tar.gz。"""
    if not items:
        raise SevenZipError("没有可压缩的文件/文件夹。")
    if fmt not in CREATE_FORMATS:
        raise SevenZipError(f"不支持的格式：{fmt}")
    for it in items:
        if not Path(it).exists():
            raise SevenZipError(f"路径不存在：{it}")
    output = str(Path(os.path.expanduser(str(output))).absolute())

    mx = LEVEL_MAP.get(level, LEVEL_MAP[DEFAULT_LEVEL])
    base, rel = _relativize(items)
    enc_args: List[str] = []
    if password:
        enc_args = _password_args(password)
        if fmt == "7z" and encrypt_names:
            enc_args.append("-mhe=on")
        # zip 加密：7zz 支持 -p；可选 AES256
        if fmt == "zip":
            enc_args.append("-mem=AES256")
        if fmt in ("tar", "tar.gz"):
            raise SevenZipError("tar / tar.gz 格式不支持密码加密，请去掉密码或改用 7z/zip。")

    if fmt == "tar.gz":
        # 两步：先 tar 再 gzip（gzip 只能压单文件流）
        import tempfile
        out_path = Path(output)
        stem = out_path.name
        for suf in (".tar.gz", ".tgz", ".gz"):
            if stem.lower().endswith(suf):
                stem = stem[: -len(suf)]
                break
        stem = stem or "archive"
        with tempfile.TemporaryDirectory(prefix="sevenzip_tg_") as tmp:
            tar_path = str(Path(tmp) / (stem + ".tar"))
            args1 = ["a", "-y", "-ttar", f"-mx={mx}", "--", tar_path] + rel
            rc = run_stream(args1, log=log, cancel_event=cancel_event, cwd=base)
            if rc not in (0, 1):
                raise SevenZipError(f"创建 tar 失败（代码 {rc}），详见日志区。", rc)
            args2 = ["a", "-y", "-tgzip", f"-mx={mx}", "--", str(output), tar_path]
            rc = run_stream(args2, log=log, cancel_event=cancel_event)
            if rc not in (0, 1):
                raise SevenZipError(f"创建 gzip 失败（代码 {rc}），详见日志区。", rc)
        return rc

    type_arg = CREATE_FORMATS[fmt]["type"]
    args = ["a", "-y", type_arg, f"-mx={mx}"] + enc_args + ["--", str(output)] + rel
    rc = run_stream(args, log=log, cancel_event=cancel_event, cwd=base)
    if rc not in (0, 1):
        raise SevenZipError(f"创建压缩包失败（代码 {rc}），详见日志区。", rc)
    return rc


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"
