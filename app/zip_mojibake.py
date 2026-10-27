#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zip_mojibake.py —— 修复 ZIP 内中文文件名乱码（GBK/big5 → UTF-8）

【问题成因】
ZIP 规范（APPNOTE）用「通用目的标志位」的第 11 位（0x800）声明文件名编码：
    置位   = 文件名是 UTF-8
    不置位 = 按 CP437 解释（1989 年的古老默认）
Windows 下很多压缩工具（国产压缩软件、老 WinRAR、资源管理器"发送到压缩文件夹"）
用 GBK 写文件名，却**不置这个位**。macOS 于是按 CP437 去解释，
中文名就成了一串 "╦π┴ª╛¡╙¬…"。

【本脚本做什么】
把那些「未置位、但实际是 GBK/big5」的文件名还原出来，改用 UTF-8 写回，
并把 0x800 位立起来。
**只改文件名，内容一个字节都不动**（改完会校验 CRC 与内容 SHA256）。

修复后为什么一定有效：文件名**字节本身**就是 UTF-8，而 macOS 系统编码就是
UTF-8 —— 无论解压工具认不认 0x800 位，只要它按系统编码解释都正确。

【用法】
    python3 fix_zip_filename_encoding.py 包.zip            # 修复（自动备份）
    python3 fix_zip_filename_encoding.py -n 包.zip         # 只看不改（dry-run）
    python3 fix_zip_filename_encoding.py -r ~/Downloads     # 目录内递归查找
    python3 fix_zip_filename_encoding.py --encoding big5 包.zip
    python3 fix_zip_filename_encoding.py --no-backup 包.zip

【退出码】0 = 全部成功或无需处理；1 = 存在失败项

【D-zip】源自 Steven 的 fix_zip_filename_encoding.py（逻辑等价复制，App 自包含）。
GUI 打开 .zip 时调用 auto_fix(path)：inspect → rebuild(+备份) → verify。
"""

from __future__ import annotations   # 兼容 Python 3.9（系统自带的就是 3.9.6）

import argparse
import hashlib
import os
import shutil
import sys
import zipfile
from datetime import datetime

# ---------------------------------------------------------------- 编码判定

# 中文标点与全角符号，参与"像不像中文"的评分
CJK_PUNCT = set("、。，；：？！（）《》〈〉「」『』【】—…·～％＃＆＊＋－／＝＠｜”“‘’")


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF      # 基本汉字
        or 0x3400 <= o <= 0x4DBF   # 扩展 A
        or 0xF900 <= o <= 0xFAFF   # 兼容汉字
        or ch in CJK_PUNCT
    )


def cjk_ratio(s: str) -> float:
    """返回字符串里"中文字符"的占比，用于在多编码候选里挑最优解。"""
    if not s:
        return 0.0
    return sum(1 for c in s if is_cjk(c)) / len(s)


def restore_raw_bytes(name: str) -> bytes:
    """
    zipfile 在 0x800 未置位时按 cp437 解出 filename，
    反向编码即可把磁盘上的原始字节拿回来。
    surrogateescape 兜住极端情况下解不回去的字节。
    """
    return name.encode("cp437", "surrogateescape")


def guess_decode(raw: bytes, forced: str | None = None):
    """
    尝试把原始字节解成有意义的中文名。
    返回 (解码结果, 使用的编码)；判定失败返回 (None, None)。

    判定顺序有讲究：
      1) 先试 UTF-8 —— 有些工具写了 UTF-8 名字却忘了置 0x800 位。
         GBK 字节序列极少能恰好构成合法 UTF-8，所以"UTF-8 解码成功 + 含汉字"
         基本就可以确认。
      2) 再试 gb18030 / big5 —— 老 Windows 工具的主流写法。
         两者都用"汉字占比"打分取高者（gb18030 是 gbk 的超集，一般能赢）。
    """
    if forced:
        try:
            return raw.decode(forced), forced
        except (UnicodeDecodeError, LookupError):
            return None, None

    # ① UTF-8 优先
    try:
        u = raw.decode("utf-8")
        if any(is_cjk(c) for c in u):
            return u, "utf-8"
    except UnicodeDecodeError:
        pass

    # ② GBK / BIG5 打分
    best, best_enc, best_score = None, None, 0.0
    for enc in ("gb18030", "big5"):
        try:
            cand = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        sc = cjk_ratio(cand)
        if sc > best_score:
            best, best_enc, best_score = cand, enc, sc

    # 阈值 0.15：低于这个值说明解出来仍是乱字，不认
    if best is None or best_score < 0.15:
        return None, None
    return best, best_enc


def is_ascii_only(b: bytes) -> bool:
    return all(x < 0x80 for x in b)


# ---------------------------------------------------------------- 单包处理

def inspect(path: str, forced_enc: str | None):
    """
    只读诊断。返回 dict：
        status: ok | fixable | no-change | unknown | error
        items:  [{index, raw, old_display, new_name, enc}, ...] 需要改名的条目
        n_total / n_utf8 / n_ascii / n_unknown
        error: 错误信息
    """
    info = {
        "path": path,
        "status": "ok",
        "items": [],
        "n_total": 0,
        "n_utf8": 0,
        "n_ascii": 0,
        "n_unknown": 0,
        "error": None,
    }
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
    except (zipfile.BadZipFile, OSError) as e:
        info["status"] = "error"
        info["error"] = f"{type(e).__name__}: {e}"
        return info

    info["n_total"] = len(entries)
    if not entries:
        info["status"] = "no-change"
        return info

    for i, z in enumerate(entries):
        zf_flag = bool(z.flag_bits & 0x800)
        if zf_flag:
            info["n_utf8"] += 1
            continue

        raw = restore_raw_bytes(z.filename)
        if is_ascii_only(raw):
            info["n_ascii"] += 1
            continue          # 纯英文名，本来就不会乱

        decoded, enc = guess_decode(raw, forced_enc)
        if decoded is None:
            info["n_unknown"] += 1
            continue          # 还原不出来，别碰

        info["items"].append(
            {
                "index": i,
                "raw": raw,
                "old_display": z.filename,
                "new_name": decoded,
                "enc": enc,
            }
        )

    if info["items"]:
        info["status"] = "fixable"
    else:
        info["status"] = "no-change"
    return info


def rebuild(path: str, info: dict, backup: bool):
    """
    按诊断结果重建 zip：只替换文件名，其余（时间戳/权限/压缩方式/注释）全部保留。
    返回 (ok, detail)
    """
    if backup:
        bak = path + ".orig-bak"
        if os.path.exists(bak):
            bak = path + ".orig-bak." + datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, bak)
        info["backup"] = bak

    tmp = path + ".zipfix.tmp"
    rename_map = {it["index"]: it["new_name"] for it in info["items"]}

    try:
        with zipfile.ZipFile(path) as src, \
             zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for i, z in enumerate(src.infolist()):
                name = rename_map.get(i, z.filename)

                is_dir = z.filename.endswith("/")
                # 目录条目要保持结尾斜杠
                if is_dir and not name.endswith("/"):
                    name += "/"

                ni = zipfile.ZipInfo(name, date_time=z.date_time)
                ni.compress_type = z.compress_type
                ni.external_attr = z.external_attr
                ni.internal_attr = z.internal_attr
                ni.create_system = z.create_system
                ni.comment = z.comment
                ni.extra = z.extra

                if is_dir:
                    out.writestr(ni, b"")
                else:
                    try:
                        out.writestr(ni, src.read(z))
                    except RuntimeError as e:
                        # 加密条目读不了
                        return False, f"条目加密，无法读取: {name} ({e})"

        os.replace(tmp, path)
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, f"{type(e).__name__}: {e}"

    return True, "ok"


def verify(path: str, info: dict):
    """
    修复后校验：条目数、每个条目的 CRC 与内容 SHA256 必须与原包一致。
    返回 (ok, 消息列表)
    """
    msgs = []
    bak = info.get("backup")
    if not bak or not os.path.exists(bak):
        return True, ["（未保留备份，跳过逐字节校验）"]

    with zipfile.ZipFile(bak) as a, zipfile.ZipFile(path) as b:
        la, lb = a.infolist(), b.infolist()
        if len(la) != len(lb):
            return False, [f"条目数不一致: {len(la)} → {len(lb)}"]

        for i, (ia, ib) in enumerate(zip(la, lb)):
            if ia.CRC != ib.CRC:
                return False, [f"第 {i} 项 CRC 不一致: {ia.CRC:08x} → {ib.CRC:08x}"]
            da, db = a.read(ia), b.read(ib)
            if hashlib.sha256(da).hexdigest() != hashlib.sha256(db).hexdigest():
                return False, [f"第 {i} 项内容不一致: {ia.filename}"]
        msgs.append(f"校验通过：{len(la)} 个条目，CRC 与内容 SHA256 全部一致")

        # 确认标志位已立起
        bad = [x.filename for x in lb
               if not (x.flag_bits & 0x800) and not is_ascii_only(restore_raw_bytes(x.filename))]
        if bad:
            return False, [f"仍有 {len(bad)} 个条目未置 UTF-8 标志位: {bad[:3]}"]
        msgs.append("UTF-8 标志位已正确置位")

    return True, msgs


# ---------------------------------------------------------------- GUI 一键入口

def auto_fix(path: str, *, backup: bool = True, encoding: str | None = None) -> dict:
    """
    D-zip 打开 .zip 时调用。返回：
        status: fixed | no-change | skipped | error
        fixed_count / backup / message / path
    失败时原包保持不变（rebuild 失败不替换；verify 失败从备份还原）。
    """
    res = {"path": path, "status": "no-change", "fixed_count": 0,
           "backup": None, "message": ""}

    if not path.lower().endswith(".zip"):
        res["status"] = "skipped"
        res["message"] = "不是 .zip，跳过文件名修复"
        return res

    info = inspect(path, encoding)
    st = info["status"]
    if st == "error":
        res["status"] = "error"
        res["message"] = f"无法按 ZIP 读取，未修复文件名：{info['error']}"
        return res
    if st != "fixable":
        if info["n_unknown"]:
            res["message"] = (f"有 {info['n_unknown']} 个文件名无法判定编码，未做改动")
        else:
            res["message"] = "文件名编码正常，无需修复"
        return res

    # 加密条目 zipfile 读不出明文，rebuild 必然失败；提前拦下，不留备份垃圾
    try:
        with zipfile.ZipFile(path) as zf:
            encrypted = any(z.flag_bits & 0x1 for z in zf.infolist())
    except (zipfile.BadZipFile, OSError) as e:
        res["status"] = "error"
        res["message"] = f"无法按 ZIP 读取：{type(e).__name__}: {e}"
        return res
    if encrypted:
        res["status"] = "error"
        res["message"] = (f"检测到 {len(info['items'])} 个文件名乱码，但这是加密 ZIP，"
                          "无法自动修复文件名（原包未改动）")
        return res

    try:
        ok, detail = rebuild(path, info, backup=backup)
    except OSError as e:   # 备份写不进去（只读目录等）
        ok, detail = False, f"{type(e).__name__}: {e}"
    bak = info.get("backup")
    if not ok:
        # 原包未被替换，刚做的备份没有意义，删掉
        if bak and os.path.exists(bak):
            try:
                os.remove(bak)
            except OSError:
                pass
        res["status"] = "error"
        res["message"] = f"自动修复文件名失败（原包未改动）：{detail}"
        return res

    try:
        vok, msgs = verify(path, info)
    except Exception as e:  # noqa: BLE001
        vok, msgs = False, [f"{type(e).__name__}: {e}"]
    if not vok:
        restored = False
        if bak and os.path.exists(bak):
            try:
                shutil.copy2(bak, path)
                restored = True
            except OSError:
                pass
        res["status"] = "error"
        res["backup"] = bak
        res["message"] = ("修复后校验未通过：" + "；".join(msgs) +
                          ("（已从备份还原原包）" if restored else
                           f"（请勿使用该文件，原包备份：{bak}）"))
        return res

    n = len(info["items"])
    res.update(status="fixed", fixed_count=n, backup=bak)
    msg = f"已自动修复 {n} 个文件名乱码"
    if bak:
        msg += f"；原文件备份为 {os.path.basename(bak)}"
    res["message"] = msg + "\n" + "\n".join(msgs)
    return res


# ---------------------------------------------------------------- 输出

def shorten(s: str, n: int = 46) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def report_one(info: dict, quiet: bool = False) -> bool:
    """打印单个包的处理结果，返回是否成功。"""
    p = info["path"]
    print(f"\n{'─' * 72}")
    print(f"📦 {p}")

    st = info["status"]

    if st == "error":
        print(f"   ✗ 无法打开：{info['error']}")
        return False

    print(f"   条目 {info['n_total']} 个："
          f"{info['n_utf8']} 个已标 UTF-8、{info['n_ascii']} 个纯英文、"
          f"{len(info['items'])} 个疑似乱码、{info['n_unknown']} 个无法判定")

    if st == "no-change":
        if info["n_unknown"]:
            print("   ⚠️  有条目无法判定编码，未做改动。"
                  "若确有乱码，用 --encoding <编码> 强制指定后重试")
        else:
            print("   ✓ 无需修复")
        return True

    if not quiet:
        print("\n   将改名的条目：")
        for it in info["items"]:
            print(f"     [{it['enc']}] {shorten(it['old_display'])}")
            print(f"          → {shorten(it['new_name'])}")

    return True


# ---------------------------------------------------------------- 主流程

def collect(paths, recursive):
    """把输入展开成 zip 文件列表。"""
    out = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            out.append(p)
        elif os.path.isdir(p):
            if recursive:
                for root, _, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(".zip"):
                            out.append(os.path.join(root, f))
            else:
                for f in sorted(os.listdir(p)):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp) and f.lower().endswith(".zip"):
                        out.append(fp)
        else:
            print(f"⚠️  路径不存在，跳过：{p}")
    # 去重并保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def main():
    ap = argparse.ArgumentParser(
        description="修复 ZIP 内中文文件名乱码（GBK/big5 → UTF-8），只改文件名，不动内容",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="+", help="zip 文件或目录")
    ap.add_argument("-n", "--dry-run", action="store_true", help="只诊断，不修改")
    ap.add_argument("-r", "--recursive", action="store_true", help="目录递归查找 .zip")
    ap.add_argument("--encoding", default=None,
                    help="强制指定源编码（gbk / gb18030 / big5 / cp950 等）")
    ap.add_argument("--no-backup", action="store_true",
                    help="不保留 .orig-bak 备份（默认保留）")
    ap.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    args = ap.parse_args()

    targets = collect(args.paths, args.recursive)
    if not targets:
        print("没找到任何 .zip 文件")
        return 0

    n_fix, n_skip, n_fail = 0, 0, 0

    for path in targets:
        info = inspect(path, args.encoding)
        ok = report_one(info, args.quiet)

        if not ok:
            n_fail += 1
            continue

        if info["status"] != "fixable":
            n_skip += 1
            continue

        if args.dry_run:
            print("\n   （dry-run：未做任何修改）")
            n_skip += 1
            continue

        ok, detail = rebuild(path, info, backup=not args.no_backup)
        if not ok:
            print(f"   ✗ 重建失败：{detail}")
            n_fail += 1
            continue

        vok, msgs = verify(path, info)
        for m in msgs:
            print(f"   {'✓' if vok else '✗'} {m}")

        if vok:
            size = os.path.getsize(path)
            print(f"   ✓ 已修复　新体积 {size:,} 字节")
            if info.get("backup"):
                print(f"   ✓ 原文件已备份为 {os.path.basename(info['backup'])}")
            n_fix += 1
        else:
            print("   ✗ 校验未通过，请勿使用该文件")
            n_fail += 1

    print(f"\n{'═' * 72}")
    print(f"汇总：修复 {n_fix} 个 / 无需处理 {n_skip} 个 / 失败 {n_fail} 个")
    if args.dry_run and n_skip:
        print("（dry-run 模式，加 -n 去掉即可实际修复）")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
