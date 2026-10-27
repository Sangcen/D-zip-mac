#!/bin/zsh
# 把桌面工程同步进 D-zip.app（.app 必须自包含：Resources 里跑自己的代码与引擎）
set -euo pipefail
cd "${0:A:h}"

APP="D-zip.app"
RES="$APP/Contents/Resources"

echo "→ 同步 app/ → $RES/app"
mkdir -p "$RES/app" "$RES/bin" "$RES/pylibs"
rsync -a --delete --exclude '__pycache__' app/ "$RES/app/"

echo "→ 同步 bin/7zz"
rsync -a bin/7zz "$RES/bin/7zz"
chmod +x "$RES/bin/7zz"

echo "→ 同步 vendor/pylibs（PyObjC：拖放 / Apple Event）"
rsync -a vendor/pylibs/ "$RES/pylibs/"

echo "→ 同步图标与说明"
[[ -f AppIcon.icns ]] && cp -f AppIcon.icns "$RES/AppIcon.icns"
[[ -f 使用说明.md ]] && cp -f 使用说明.md "$RES/使用说明.md"

echo "→ 去隔离属性并 ad-hoc 签名"
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
codesign -dv "$APP" 2>&1 | head -3

echo "✓ 完成：$PWD/$APP"
