#!/usr/bin/env python3
"""同步 tg-telegram-imagebed 的 hero.svg 头图版本号。

版本号从仓库根目录 VERSION 文件读取(格式 X.Y.Z,不带 v),
自动替换 assets/readme/hero.svg 中的版本文字(vX.Y.Z,带 v 前缀)。

发版流程调用:python3 scripts/update_hero_version.py
之后 git add assets/readme/hero.svg 一起提交。
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
HERO_SVG = REPO_ROOT / "assets" / "readme" / "hero.svg"

# VERSION 文件:纯数字 X.Y.Z(无 v 前缀)
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
# hero.svg 中的版本文字:vX.Y.Z(带 v 前缀)
SVG_VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+")


def main() -> int:
    if not VERSION_FILE.exists():
        print(f"错误: 找不到 VERSION 文件: {VERSION_FILE}", file=sys.stderr)
        return 1
    if not HERO_SVG.exists():
        print(f"错误: 找不到 hero.svg: {HERO_SVG}", file=sys.stderr)
        return 1

    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.match(version):
        print(f"错误: VERSION 内容不是合法的 X.Y.Z 格式: {version!r}", file=sys.stderr)
        return 1

    svg_version = f"v{version}"
    content = HERO_SVG.read_text(encoding="utf-8")
    matches = SVG_VERSION_PATTERN.findall(content)
    if not matches:
        print(f"错误: hero.svg 中未找到版本号(vX.Y.Z),无法同步", file=sys.stderr)
        return 1

    new_content, count = SVG_VERSION_PATTERN.subn(svg_version, content, count=1)
    if count == 0:
        print(f"hero.svg 版本已是 {svg_version},无需修改")
        return 0

    HERO_SVG.write_text(new_content, encoding="utf-8")
    print(f"✅ hero.svg 版本号已同步: {matches[0]} → {svg_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
