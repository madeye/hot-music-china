#!/usr/bin/env python3
"""Generate the static GitHub Pages artifact."""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


UPDATE_TIME_RE = re.compile(
    r"数据更新时间：\d{4}年\d{1,2}月\d{1,2}日 \| 数据来源：各平台公开榜单"
)


def china_today() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def render_page(source: Path, output_dir: Path, now: datetime | None = None) -> Path:
    html = source.read_text(encoding="utf-8")
    current = now or china_today()
    update_text = (
        f"数据更新时间：{current.year}年{current.month}月{current.day}日 | "
        "数据来源：各平台公开榜单"
    )

    html, replacements = UPDATE_TIME_RE.subn(update_text, html, count=1)
    if replacements != 1:
        raise ValueError("Could not update the page timestamp in index.html")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "index.html"
    output_path.write_text(html, encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("index.html"),
        help="Source HTML file to render.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("_site"),
        help="Directory for the generated GitHub Pages artifact.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = render_page(args.source, args.output_dir)
    print(f"Generated {output_path}")


if __name__ == "__main__":
    main()
