#!/usr/bin/env python3
"""カイゼンベースのYouTubeチャンネルの新着動画を検出する。

YouTubeが公開しているチャンネルRSSフィードを読み、前回までに見た動画IDを
記録した状態ファイルと突き合わせて、新しく公開された動画だけを出力する。
標準ライブラリのみで動く（GitHub Actionsのubuntu-latestでそのまま実行可能）。

使い方:
    python3 scripts/check_new_videos.py                 # 新着を検出して状態を更新
    python3 scripts/check_new_videos.py --seed          # 通知せず現在の動画を既読にする
    python3 scripts/check_new_videos.py --dry-run       # 状態ファイルを書き換えない
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "config.json")
DEFAULT_STATE = os.path.join(REPO_ROOT, "state", "seen_videos.json")

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
USER_AGENT = "kaizen-base-youtube-watcher/1.0 (+github actions)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# 通知に出す日時は日本時間で表示する。JSTはサマータイムがないので固定オフセットでよい。
JST = timezone(timedelta(hours=9), "JST")

# 状態ファイルに残す動画IDの上限。フィードは最新15件しか返さないので、
# これだけ残しておけば「一度見た動画をまた新着扱いする」ことはまず起きない。
MAX_SEEN = 500


def to_jst(published):
    """RSSのpublished（UTCのISO8601）を日本時間の表示用文字列にする。"""
    if not published:
        return ""
    text = published.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # 想定外の形式ならそのまま出す（通知が落ちるより読めない方がマシ）
        return published
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M (JST)")


def log(message):
    print(message, file=sys.stderr)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def resolve_channel_id(config):
    """config の channel_id をそのまま使う。空ならチャンネルページから解決する。"""
    channel_id = (config.get("channel_id") or "").strip()
    if channel_id:
        return channel_id

    channel_url = (config.get("channel_url") or "").strip()
    if not channel_url:
        raise SystemExit("config.json に channel_id か channel_url のどちらかが必要です")

    log(f"channel_id が未設定のため {channel_url} から解決します")
    html = fetch(channel_url)
    match = re.search(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{22})"', html)
    if not match:
        raise SystemExit(f"{channel_url} からチャンネルIDを取得できませんでした")
    log(f"チャンネルIDを解決しました: {match.group(1)}")
    return match.group(1)


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    feed_title = root.findtext("atom:title", default="", namespaces=NS)

    videos = []
    for entry in root.findall("atom:entry", NS):
        group = entry.find("media:group", NS)
        description = ""
        if group is not None:
            description = group.findtext("media:description", default="", namespaces=NS) or ""
        published = entry.findtext("atom:published", default="", namespaces=NS)
        videos.append(
            {
                "video_id": entry.findtext("yt:videoId", default="", namespaces=NS),
                "title": entry.findtext("atom:title", default="", namespaces=NS),
                "url": entry.find("atom:link", NS).get("href", ""),
                "published": published,
                "published_jst": to_jst(published),
                "description": description,
            }
        )
    return feed_title, videos


def matches(video, match_config):
    """担当動画の絞り込み。include_all が true なら全件を通す。"""
    if match_config.get("include_all", True):
        return True

    title = video["title"].lower()
    description = video["description"].lower()

    for keyword in match_config.get("title_keywords", []):
        if keyword.lower() in title:
            return True
    for keyword in match_config.get("description_keywords", []):
        if keyword.lower() in description:
            return True
    return False


def load_json(path, fallback):
    if not os.path.exists(path):
        return fallback
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def emit_github_output(new_videos):
    """GitHub Actions のステップ出力に結果を渡す。"""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"count={len(new_videos)}\n")
        handle.write("videos<<__EOF__\n")
        handle.write(json.dumps(new_videos, ensure_ascii=False))
        handle.write("\n__EOF__\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="通知せずに現在フィードにある動画をすべて既読として記録する",
    )
    parser.add_argument("--dry-run", action="store_true", help="状態ファイルを書き換えない")
    args = parser.parse_args()

    config = load_json(args.config, None)
    if config is None:
        raise SystemExit(f"設定ファイルが見つかりません: {args.config}")

    channel_id = resolve_channel_id(config)
    try:
        xml_text = fetch(FEED_URL.format(channel_id=channel_id))
    except urllib.error.HTTPError as error:
        raise SystemExit(f"フィードの取得に失敗しました (HTTP {error.code}): {error.reason}")
    except urllib.error.URLError as error:
        raise SystemExit(f"フィードの取得に失敗しました: {error.reason}")

    feed_title, videos = parse_feed(xml_text)
    log(f"チャンネル: {feed_title} / フィード件数: {len(videos)}")

    state = load_json(args.state, {})
    seen = state.get("seen_video_ids")
    first_run = seen is None
    seen_set = set(seen or [])

    if first_run or args.seed:
        # 初回はまとめて何十件も通知しても嬉しくないので、既読化するだけにする。
        log("初回実行（またはseed）のため、現在の動画をすべて既読として記録します")
        new_videos = []
    else:
        new_videos = [
            video
            for video in videos
            if video["video_id"] not in seen_set and matches(video, config.get("match", {}))
        ]
        # 新しい順のフィードを、古い順に通知する
        new_videos.reverse()

    for video in new_videos:
        log(f"新着: {video['title']} ({video['published_jst']}) — {video['url']}")
    if not new_videos and not first_run and not args.seed:
        log("新着はありません")

    # フィルタに合わない動画も既読にしておく（毎回照合し直す意味がないため）
    updated_seen = [video["video_id"] for video in videos if video["video_id"]]
    updated_seen += [video_id for video_id in (seen or []) if video_id not in set(updated_seen)]

    if not args.dry_run:
        write_json(
            args.state,
            {
                "channel_id": channel_id,
                "channel_title": feed_title,
                "updated_at": datetime.now(JST).isoformat(timespec="seconds"),
                "seen_video_ids": updated_seen[:MAX_SEEN],
            },
        )

    emit_github_output(new_videos)
    print(json.dumps(new_videos, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
