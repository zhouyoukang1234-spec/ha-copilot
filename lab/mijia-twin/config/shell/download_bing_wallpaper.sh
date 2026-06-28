#!/usr/bin/env bash
# 下载必应每日壁纸到 /config/www/bing_wallpaper.jpg, 供前端做 UI 背景。
# 自包含: 仅依赖 curl + python3 (HA 容器自带), 无第三方集成、无外部模型。
# 由自动化 `download_bing_wallpaper_daily` 触发 (shell_command.download_bing_wallpaper)。
set -euo pipefail
WWW_DIR="${HA_CONFIG_DIR:-/config}/www"
OUT="${WWW_DIR}/bing_wallpaper.jpg"
API="https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=en-US"
mkdir -p "${WWW_DIR}"
rel="$(curl -fsSL -m 20 "${API}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["images"][0]["url"])')"
curl -fsSL -m 60 "https://www.bing.com${rel}" -o "${OUT}"
echo "bing wallpaper saved to ${OUT}"
