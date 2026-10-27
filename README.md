# D-zip

macOS 上的 7-Zip 图形界面（Tk 9 + 自带 `7zz`）。

## 快速开始

详见 [使用说明.md](./使用说明.md)。

日常安装版一般在 `/Applications/D-zip.app`。本仓库是源码工程：改 `app/` 后执行：

```sh
./sync_to_app.sh
codesign --force --deep --sign - D-zip.app
codesign --verify --deep --strict D-zip.app
```

需要本机用户目录下带 Tk 9 的 Python（如 `~/.local/bin/python3.12`）。

## 目录

| 路径 | 说明 |
| --- | --- |
| `app/` | 界面与逻辑（`main.py` / `macdnd.py` / `sevenz.py` / `zip_mojibake.py`） |
| `bin/7zz` | 7-Zip 引擎 |
| `vendor/pylibs` | 自带 PyObjC（拖放） |
| `sync_to_app.sh` | 同步进 `.app` |
| `打开D-zip.command` | 带终端启动（方便看报错） |

`D-zip.app` 不入库，用上面的脚本生成。
