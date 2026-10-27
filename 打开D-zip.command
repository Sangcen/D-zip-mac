#!/bin/zsh
# 双击入口（开发/免 .app 直接用）：只使用 ~/.local/bin 下自带 Tk≥8.6 的 Python
set -u

DIR="${0:A:h}"
cd "$DIR" || { echo "无法进入 $DIR"; read -r "?按回车退出…"; exit 1; }

PY_CANDIDATES=(
  "$HOME/.local/bin/python3.12"
  "$HOME/.local/bin/python3"
)

py_tk_ok() {
  local c="$1"
  [[ -x "$c" ]] || return 1
  "$c" -c 'import sys, tkinter; sys.exit(0 if float(tkinter.TkVersion) >= 8.6 else 1)' >/dev/null 2>&1
}

find_python() {
  local c
  for c in "${PY_CANDIDATES[@]}"; do
    if py_tk_ok "$c"; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [[ -z "${PY:-}" ]]; then
  MSG="找不到可用的 Python（需 $HOME/.local/bin/python3.12，自带 Tk 9）。不使用 /usr/bin/python3（Tk 8.5 会导致界面卡死）。"
  echo "错误：$MSG"
  osascript -e "display alert \"D-zip\" message \"$MSG\" as critical" >/dev/null 2>&1 || true
  read -r "?按回车退出…"
  exit 1
fi

export PYTHONUNBUFFERED=1
export TK_SILENCE_DEPRECATION=1
echo "Python：$PY"
echo "Tk：$("$PY" -c 'import tkinter; print(tkinter.Tcl().eval("info patchlevel"))' 2>/dev/null || echo 未知)"
echo "正在启动 D-zip…"
"$PY" "$DIR/app/main.py" "$@"
STATUS=$?
if [[ $STATUS -ne 0 ]]; then
  echo ""
  echo "退出码：$STATUS"
  echo "日志：~/Library/Logs/D-zip.log"
  read -r "?按回车退出…"
fi
exit $STATUS
