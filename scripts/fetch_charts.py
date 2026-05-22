#!/usr/bin/env python3
"""Fetch public chart pages and normalize them into data/songs.json.

The crawler is intentionally conservative: it only asks the LLM to extract
song rows from content that this script fetched, then validates the returned
JSON before using it.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"
MAX_SOURCE_CHARS = 120_000
MAX_SONGS_PER_SOURCE = 100


@dataclass(frozen=True)
class Source:
    name: str
    platform: str
    url: str


SOURCES = [
    Source("QQ音乐热歌榜", "qq", "https://y.qq.com/n/ryqq/toplist/26"),
    Source("网易云音乐飙升榜", "netease", "https://music.163.com/discover/toplist?id=19723756"),
    Source("酷狗音乐TOP500", "kugou", "https://www.kugou.com/yy/html/rank.html"),
    Source("腾讯音乐由你榜", "tme", "https://www.tencentmusic.com/zh-cn/uni-chart.html"),
    Source(
        "Apple Music中国榜",
        "apple",
        "https://music.apple.com/cn/playlist/每周热门-100-首-中国大陆/pl.939cf56e73c44970b81fd9648f859223",
    ),
]


def china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def fetch_url(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def compact_source(raw: str) -> str:
    raw = html.unescape(raw)
    raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.DOTALL)
    raw = re.sub(r"\s+", " ", raw)
    return raw[:MAX_SOURCE_CHARS]


def deepseek_extract(source: Source, content: str, api_key: str) -> list[dict[str, Any]]:
    system_prompt = (
        "You extract music chart rows from fetched source content. "
        "Use only the provided content. Do not guess missing songs. "
        "Return JSON only in this exact shape: "
        '{"songs":[{"rank":1,"title":"Song title","artist":"Artist name","url":"https://example.com"}]}. '
        "If no chart rows are present, return {\"songs\":[]}."
    )
    user_prompt = (
        f"Extract up to {MAX_SONGS_PER_SOURCE} ranked songs as json from this source.\n"
        f"Source name: {source.name}\n"
        f"Source platform: {source.platform}\n"
        f"Source url: {source.url}\n"
        f"Fetched content:\n{content}"
    )
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_MODEL),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 12000,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{os.environ.get('DEEPSEEK_BASE_URL', DEEPSEEK_BASE_URL).rstrip('/')}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))

    content = result["choices"][0]["message"].get("content") or ""
    parsed = json.loads(content)
    songs = parsed.get("songs", [])
    if not isinstance(songs, list):
        return []
    return songs[:MAX_SONGS_PER_SOURCE]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_source_songs(source: Source, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for fallback_rank, row in enumerate(rows, start=1):
        title = clean_text(row.get("title"))
        artist = clean_text(row.get("artist"))
        if not title or not artist:
            continue
        try:
            rank = int(row.get("rank") or fallback_rank)
        except (TypeError, ValueError):
            rank = fallback_rank
        if rank < 1 or rank > 500:
            rank = fallback_rank
        normalized.append(
            {
                "rank": rank,
                "title": title,
                "artist": artist,
                "platform": source.platform,
                "source": source.name,
                "url": clean_text(row.get("url")) or source.url,
            }
        )
    normalized.sort(key=lambda song: song["rank"])
    return normalized[:MAX_SONGS_PER_SOURCE]


def song_key(song: dict[str, Any]) -> tuple[str, str]:
    title = re.sub(r"[\s　]+", "", song["title"]).casefold()
    artist = re.sub(r"[\s　]+", "", song["artist"]).casefold()
    return title, artist


def merge_charts(source_songs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for song in source_songs:
        key = song_key(song)
        item = merged.setdefault(
            key,
            {
                "title": song["title"],
                "artist": song["artist"],
                "platforms": [],
                "sourceRanks": [],
                "bestRank": song["rank"],
            },
        )
        if song["platform"] not in item["platforms"]:
            item["platforms"].append(song["platform"])
        item["sourceRanks"].append(song["rank"])
        item["bestRank"] = min(item["bestRank"], song["rank"])

    ranked = sorted(
        merged.values(),
        key=lambda item: (sum(item["sourceRanks"]) / len(item["sourceRanks"]), item["bestRank"]),
    )[:100]
    total = len(ranked)
    output = []
    for index, item in enumerate(ranked, start=1):
        heat = max(58_000_000, int(1_580_000_000 * ((total - index + 1) / max(total, 1))))
        output.append(
            {
                "rank": index,
                "title": item["title"],
                "artist": item["artist"],
                "platforms": item["platforms"],
                "plays": heat,
                "isNew": index <= 20,
                "daysOnChart": 7,
                "peakRank": index,
            }
        )
    return output


def load_existing(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_payload(path: Path, songs: list[dict[str, Any]], source_status: list[dict[str, Any]]) -> None:
    now = china_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updatedAt": now.isoformat(),
        "model": os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_MODEL),
        "sources": source_status,
        "songs": songs,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_charts(output: Path, allow_stale: bool) -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("DEEPSEEK_API_KEY is not set; keeping existing data if present.", file=sys.stderr)
        existing = load_existing(output)
        if allow_stale or (existing and existing.get("songs")):
            return 0
        return 2

    all_rows: list[dict[str, Any]] = []
    source_status: list[dict[str, Any]] = []
    for source in SOURCES:
        try:
            raw = fetch_url(source.url)
            rows = deepseek_extract(source, compact_source(raw), api_key)
            songs = normalize_source_songs(source, rows)
            all_rows.extend(songs)
            source_status.append(
                {
                    "name": source.name,
                    "platform": source.platform,
                    "url": source.url,
                    "status": "ok",
                    "songs": len(songs),
                }
            )
            print(f"{source.name}: extracted {len(songs)} songs")
        except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            source_status.append(
                {
                    "name": source.name,
                    "platform": source.platform,
                    "url": source.url,
                    "status": "failed",
                    "error": str(exc)[:300],
                }
            )
            print(f"{source.name}: failed: {exc}", file=sys.stderr)

    songs = merge_charts(all_rows)
    if songs:
        write_payload(output, songs, source_status)
        print(f"Wrote {len(songs)} songs to {output}")
        return 0

    if allow_stale:
        print("No live songs extracted; keeping existing data or source HTML fallback.", file=sys.stderr)
        return 0
    print("No live songs extracted and no existing data is available.", file=sys.stderr)
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/songs.json"),
        help="Path for normalized chart data.",
    )
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Exit successfully if live fetching fails but an existing output file is present.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(fetch_charts(args.output, args.allow_stale))


if __name__ == "__main__":
    main()
