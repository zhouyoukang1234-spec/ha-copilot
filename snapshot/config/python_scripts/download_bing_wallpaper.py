#!/usr/bin/env python3
"""Download today's Bing wallpaper into /config/www for use as a dashboard background.

This is a plain CPython script invoked via shell_command (not HA's sandboxed
python_script), so normal imports are allowed. It is intentionally tolerant:
network failures exit 0 so they never spam the HA error log.
"""
import json
import os
import sys
import urllib.request

WWW = "/config/www"
OUT = os.path.join(WWW, "bing_wallpaper.jpg")
API = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN"


def main() -> int:
    try:
        os.makedirs(WWW, exist_ok=True)
        with urllib.request.urlopen(API, timeout=20) as r:
            data = json.loads(r.read())
        rel = data["images"][0]["url"]
        url = "https://www.bing.com" + rel
        with urllib.request.urlopen(url, timeout=30) as r:
            img = r.read()
        with open(OUT, "wb") as f:
            f.write(img)
        print(f"saved {len(img)} bytes -> {OUT}")
    except Exception as exc:  # noqa: BLE001 - never fail the shell_command
        print(f"bing wallpaper skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
