#!/usr/bin/env python3
"""スプレッドシートに書いた投稿を、予定時刻になったらInstagramへ自動投稿する。

Googleスプレッドシートを「ウェブに公開(CSV)」した URL を読み、投稿日時が来ている行を
Instagram Graph API で投稿する。投稿済みの行は state/posted_instagram.json に記録し、
二重投稿しないようにする。標準ライブラリのみで動く。

使い方:
    python3 scripts/post_to_instagram.py              # 予定時刻が来た投稿を実行
    python3 scripts/post_to_instagram.py --dry-run    # 投稿せず、対象だけ表示する
    python3 scripts/post_to_instagram.py --list       # シートの中身を全部表示する

必要な環境変数:
    IG_ACCESS_TOKEN   Instagram Graph API の長期アクセストークン
    IG_USER_ID        InstagramビジネスアカウントのユーザーID
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "instagram_config.json")
DEFAULT_STATE = os.path.join(REPO_ROOT, "state", "posted_instagram.json")

JST = timezone(timedelta(hours=9))
USER_AGENT = "instagram-scheduled-poster/1.0 (+github actions)"

# Instagramの制限。超えるとAPIがエラーを返すので、投稿前に自分で弾く。
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_CAROUSEL_ITEMS = 10

# 動画は変換に時間がかかる。FINISHEDになるまで待つ上限。
CONTAINER_POLL_INTERVAL = 5
CONTAINER_POLL_TIMEOUT = 300

VIDEO_EXTENSIONS = (".mp4", ".mov")

# 日時の書き方はぶれるので、よくある形は全部受け付ける。
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)

# シートの列名。表記ゆれを吸収するため、それぞれ複数の候補を持つ。
COLUMN_ALIASES = {
    "scheduled_at": ("投稿日時", "日時", "予定日時", "scheduled_at", "datetime"),
    "media": ("メディアURL", "画像URL", "動画URL", "メディア", "media_url", "media"),
    "caption": ("キャプション", "本文", "文章", "caption", "text"),
    "skip": ("スキップ", "除外", "skip"),
}


def log(message):
    print(message, file=sys.stderr)


def redact(text):
    """ログやエラーにアクセストークンが混ざらないようにする。"""
    token = os.environ.get("IG_ACCESS_TOKEN")
    if token and token in text:
        text = text.replace(token, "***")
    return text


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


# ---------------------------------------------------------------- シート読み込み


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def build_column_map(fieldnames):
    """シートの見出し行から、列名→実際の見出し の対応を作る。"""
    normalized = {}
    for name in fieldnames or []:
        if name:
            normalized[name.strip().lower().replace(" ", "")] = name

    mapping = {}
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            actual = normalized.get(alias.lower().replace(" ", ""))
            if actual:
                mapping[key] = actual
                break
    return mapping


def parse_scheduled_at(text, tz):
    text = text.strip().replace("年", "-").replace("月", "-").replace("日", "")
    text = " ".join(text.split())
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def parse_rows(csv_text, tz):
    """CSVを読んで、投稿1件ぶんの辞書のリストにする。"""
    reader = csv.DictReader(io.StringIO(csv_text))
    mapping = build_column_map(reader.fieldnames)

    missing = [key for key in ("scheduled_at", "media", "caption") if key not in mapping]
    if missing:
        labels = {key: COLUMN_ALIASES[key][0] for key in missing}
        raise SystemExit(
            "シートに必要な列が見つかりません: "
            + "、".join(labels.values())
            + f"\n見つかった見出し: {reader.fieldnames}"
        )

    rows = []
    for number, raw in enumerate(reader, start=2):  # 2行目からがデータ
        scheduled_text = (raw.get(mapping["scheduled_at"]) or "").strip()
        media_text = (raw.get(mapping["media"]) or "").strip()
        caption = (raw.get(mapping["caption"]) or "").strip()
        skip_text = ""
        if "skip" in mapping:
            skip_text = (raw.get(mapping["skip"]) or "").strip()

        # 3つとも空の行はシートの余白なので黙って飛ばす
        if not scheduled_text and not media_text and not caption:
            continue

        rows.append(
            {
                "row_number": number,
                "scheduled_text": scheduled_text,
                "scheduled_at": parse_scheduled_at(scheduled_text, tz),
                "media_urls": [part.strip() for part in media_text.split("|") if part.strip()],
                "caption": caption,
                "skip": skip_text.lower() in ("true", "1", "yes", "y", "はい", "○", "x", "✓"),
            }
        )
    return rows


def row_key(row):
    """行を一意に識別するキー。日時とメディアURLから作る。

    キャプションは投稿後に直したくなることがあるので、あえてキーに含めない。
    含めてしまうと、投稿済みの行のキャプションを直しただけで別の行と見なされ、
    二重投稿になる。
    """
    material = row["scheduled_text"] + "\n" + "|".join(row["media_urls"])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 検証


def detect_media_type(media_urls):
    if len(media_urls) > 1:
        return "CAROUSEL"
    if media_urls and media_urls[0].lower().split("?")[0].endswith(VIDEO_EXTENSIONS):
        return "REELS"
    return "IMAGE"


def count_hashtags(caption):
    return sum(1 for word in caption.split() if word.startswith("#") and len(word) > 1)


def validate_row(row):
    """投稿する前に、APIに弾かれると分かっている問題を先に見つける。"""
    problems = []

    if row["scheduled_at"] is None:
        problems.append(f"投稿日時を読み取れません: 「{row['scheduled_text']}」（例: 2026-09-20 19:00）")
    if not row["media_urls"]:
        problems.append("メディアURLが空です")
    for url in row["media_urls"]:
        if not url.startswith("https://"):
            problems.append(f"メディアURLはhttpsで公開されている必要があります: {url}")
    if len(row["media_urls"]) > MAX_CAROUSEL_ITEMS:
        problems.append(
            f"カルーセルは最大{MAX_CAROUSEL_ITEMS}枚までです（{len(row['media_urls'])}枚指定されています）"
        )
    if len(row["caption"]) > MAX_CAPTION_CHARS:
        problems.append(
            f"キャプションが長すぎます: {len(row['caption'])}文字（上限{MAX_CAPTION_CHARS}文字）"
        )
    if count_hashtags(row["caption"]) > MAX_HASHTAGS:
        problems.append(f"ハッシュタグが多すぎます（上限{MAX_HASHTAGS}個）")

    return problems


# ---------------------------------------------------------------- Instagram API


class GraphError(RuntimeError):
    """Graph APIが返したエラー。メッセージはそのまま人に見せられる形にしてある。"""


def graph_request(config, path, params, method):
    version = config.get("api_version", "v21.0")
    url = f"https://graph.facebook.com/{version}/{path}"
    params = dict(params)
    params["access_token"] = os.environ["IG_ACCESS_TOKEN"]
    encoded = urllib.parse.urlencode(params).encode("utf-8")

    if method == "GET":
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": USER_AGENT},
            method="GET",
        )
    else:
        request = urllib.request.Request(
            url, data=encoded, headers={"User-Agent": USER_AGENT}, method="POST"
        )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        message = body
        try:
            detail = json.loads(body).get("error", {})
            message = detail.get("error_user_msg") or detail.get("message") or body
            if detail.get("code") == 190:
                message += "（アクセストークンが無効か期限切れです。再発行してください）"
        except (ValueError, AttributeError):
            pass
        if error.code == 400 and "version" in message.lower():
            message += (
                f"\nヒント: instagram_config.json の api_version（現在 {version}）が"
                "古い可能性があります。新しいバージョンに変えて再実行してください。"
            )
        raise GraphError(redact(f"HTTP {error.code}: {message}")) from None
    except urllib.error.URLError as error:
        raise GraphError(redact(f"通信に失敗しました: {error.reason}")) from None


def graph_post(config, path, params):
    return graph_request(config, path, params, "POST")


def graph_get(config, path, params):
    return graph_request(config, path, params, "GET")


def wait_until_ready(config, container_id):
    """コンテナが公開可能になるまで待つ。動画は変換に数十秒かかることがある。"""
    deadline = time.time() + CONTAINER_POLL_TIMEOUT
    while True:
        result = graph_get(config, container_id, {"fields": "status_code,status"})
        status = result.get("status_code", "")

        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise GraphError(
                f"メディアの処理に失敗しました (status={status}): {result.get('status', '')}"
            )
        if time.time() >= deadline:
            raise GraphError(
                f"メディアの処理が{CONTAINER_POLL_TIMEOUT}秒以内に終わりませんでした"
                f" (status={status})。動画が長すぎるか重すぎる可能性があります。"
            )
        time.sleep(CONTAINER_POLL_INTERVAL)


def create_container(config, ig_user_id, row):
    """投稿内容からメディアコンテナを作り、そのIDを返す。"""
    media_type = detect_media_type(row["media_urls"])

    if media_type == "IMAGE":
        result = graph_post(
            config,
            f"{ig_user_id}/media",
            {"image_url": row["media_urls"][0], "caption": row["caption"]},
        )
        return result["id"], media_type

    if media_type == "REELS":
        result = graph_post(
            config,
            f"{ig_user_id}/media",
            {
                "media_type": "REELS",
                "video_url": row["media_urls"][0],
                "caption": row["caption"],
            },
        )
        container_id = result["id"]
        wait_until_ready(config, container_id)
        return container_id, media_type

    # カルーセル: 1枚ずつ子コンテナを作ってから、親コンテナでまとめる
    children = []
    for url in row["media_urls"]:
        params = {"is_carousel_item": "true"}
        if url.lower().split("?")[0].endswith(VIDEO_EXTENSIONS):
            params["media_type"] = "VIDEO"
            params["video_url"] = url
        else:
            params["image_url"] = url
        child = graph_post(config, f"{ig_user_id}/media", params)
        children.append(child["id"])

    for child_id in children:
        wait_until_ready(config, child_id)

    parent = graph_post(
        config,
        f"{ig_user_id}/media",
        {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": row["caption"],
        },
    )
    return parent["id"], media_type


def publish(config, ig_user_id, container_id):
    result = graph_post(config, f"{ig_user_id}/media_publish", {"creation_id": container_id})
    return result["id"]


def permalink(config, media_id):
    """投稿のURL。media_idとは別の短縮コードなので、APIから取得する。"""
    try:
        return graph_get(config, media_id, {"fields": "permalink"}).get("permalink", "")
    except (GraphError, KeyError):
        return ""


def remaining_quota(config, ig_user_id):
    """24時間あたりの投稿上限の残り。取得できなければNoneを返す。"""
    try:
        result = graph_get(
            config, f"{ig_user_id}/content_publishing_limit", {"fields": "config,quota_usage"}
        )
        entry = (result.get("data") or [{}])[0]
        limit = (entry.get("config") or {}).get("quota_total")
        used = entry.get("quota_usage")
        if limit is None or used is None:
            return None
        return limit - used
    except (GraphError, KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------- 投稿対象の選別


def classify(rows, now, config, state):
    """全行を「今出す/まだ先/期限切れ/済み/問題あり」に振り分ける。"""
    posted = state.get("posted", {})
    failed = state.get("failed", {})
    expired_seen = set(state.get("expired", []))
    max_attempts = config.get("max_attempts", 3)
    catchup = timedelta(hours=config.get("catchup_hours", 24))

    buckets = {"due": [], "future": [], "expired": [], "done": [], "invalid": [], "skipped": []}

    for row in rows:
        key = row_key(row)
        row["key"] = key

        if row["skip"]:
            buckets["skipped"].append(row)
            continue
        if key in posted:
            buckets["done"].append(row)
            continue

        problems = validate_row(row)
        if problems:
            row["problems"] = problems
            buckets["invalid"].append(row)
            continue

        attempts = failed.get(key, {}).get("attempts", 0)
        if attempts >= max_attempts:
            row["problems"] = [
                f"{attempts}回失敗したため、これ以上自動では試しません: "
                + failed.get(key, {}).get("last_error", "")
            ]
            buckets["invalid"].append(row)
            continue

        if row["scheduled_at"] > now:
            buckets["future"].append(row)
        elif row["scheduled_at"] < now - catchup:
            # 予定時刻をだいぶ過ぎている。いま出すと的外れなので出さない。
            if key not in expired_seen:
                buckets["expired"].append(row)
        else:
            buckets["due"].append(row)

    buckets["due"].sort(key=lambda row: row["scheduled_at"])
    return buckets


def post_row(config, ig_user_id, row):
    container_id, media_type = create_container(config, ig_user_id, row)
    media_id = publish(config, ig_user_id, container_id)
    return {"media_id": media_id, "media_type": media_type, "permalink": permalink(config, media_id)}


def emit_github_output(payload):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in payload.items():
            if isinstance(value, (list, dict)):
                handle.write(f"{key}<<__EOF__\n")
                handle.write(json.dumps(value, ensure_ascii=False))
                handle.write("\n__EOF__\n")
            else:
                handle.write(f"{key}={value}\n")


def describe(row):
    head = row["caption"].splitlines()[0] if row["caption"] else "(キャプションなし)"
    return f"{row['scheduled_text']} 「{head[:40]}」"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--csv", help="CSVのURLの代わりにローカルファイルを読む（動作確認用）")
    parser.add_argument("--dry-run", action="store_true", help="投稿せず、対象だけ表示する")
    parser.add_argument("--list", action="store_true", help="シートの中身を全部表示して終了")
    args = parser.parse_args()

    config = load_json(args.config, None)
    if config is None:
        raise SystemExit(f"設定ファイルが見つかりません: {args.config}")

    tz = timezone(timedelta(hours=config.get("utc_offset_hours", 9)))
    now = datetime.now(tz)

    if args.csv:
        with open(args.csv, encoding="utf-8") as handle:
            csv_text = handle.read()
    else:
        csv_url = (config.get("csv_url") or "").strip()
        if not csv_url:
            raise SystemExit(
                "instagram_config.json の csv_url が空です。\n"
                "スプレッドシートを「ファイル → 共有 → ウェブに公開」でCSVとして公開し、"
                "そのURLを設定してください。"
            )
        try:
            csv_text = fetch(csv_url)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise SystemExit(f"スプレッドシートを取得できませんでした: {error}")

    rows = parse_rows(csv_text, tz)
    state = load_json(args.state, {})
    buckets = classify(rows, now, config, state)

    if args.list:
        for name, label in (
            ("due", "今すぐ投稿"), ("future", "予定"), ("done", "投稿済み"),
            ("expired", "期限切れ"), ("invalid", "要修正"), ("skipped", "スキップ"),
        ):
            for row in buckets[name]:
                print(f"[{label}] {describe(row)}")
                for problem in row.get("problems", []):
                    print(f"         → {problem}")
        return

    log(
        f"現在 {now:%Y-%m-%d %H:%M} / 行数 {len(rows)} / "
        f"投稿対象 {len(buckets['due'])} / 予定 {len(buckets['future'])} / "
        f"投稿済み {len(buckets['done'])} / 要修正 {len(buckets['invalid'])}"
    )

    for row in buckets["invalid"]:
        log(f"要修正: {describe(row)}")
        for problem in row["problems"]:
            log(f"  → {problem}")
    for row in buckets["expired"]:
        log(f"期限切れ（投稿されませんでした）: {describe(row)}")

    limit = config.get("max_posts_per_run", 3)
    targets = buckets["due"][:limit]
    if len(buckets["due"]) > limit:
        log(f"今回は{limit}件までにします（残り{len(buckets['due']) - limit}件は次回）")

    posted_now, failed_now = [], []

    if targets and not args.dry_run:
        for name in ("IG_ACCESS_TOKEN", "IG_USER_ID"):
            if not os.environ.get(name):
                raise SystemExit(f"環境変数 {name} が設定されていません")
        ig_user_id = os.environ["IG_USER_ID"]

        remaining = remaining_quota(config, ig_user_id)
        if remaining is not None:
            log(f"24時間あたりの投稿上限の残り: {remaining}件")
            if remaining <= 0:
                raise SystemExit("24時間あたりの投稿上限に達しています。次回の実行で再開します。")
            targets = targets[:remaining]

        for row in targets:
            log(f"投稿します: {describe(row)}")
            try:
                result = post_row(config, ig_user_id, row)
            except (GraphError, KeyError) as error:
                message = redact(str(error))
                log(f"  失敗: {message}")
                failed_now.append({"key": row["key"], "summary": describe(row), "error": message})
                continue
            log(f"  完了: media_id={result['media_id']}")
            posted_now.append(
                {
                    "key": row["key"],
                    "summary": describe(row),
                    "scheduled_at": row["scheduled_text"],
                    "media_id": result["media_id"],
                    "media_type": result["media_type"],
                    "permalink": result["permalink"],
                }
            )
    elif targets:
        for row in targets:
            log(f"[dry-run] 投稿対象: {describe(row)}")

    if not args.dry_run:
        posted = state.get("posted", {})
        failed = state.get("failed", {})
        for entry in posted_now:
            posted[entry["key"]] = {
                "summary": entry["summary"],
                "media_id": entry["media_id"],
                "posted_at": now.isoformat(timespec="seconds"),
            }
            failed.pop(entry["key"], None)
        for entry in failed_now:
            record = failed.get(entry["key"], {"attempts": 0})
            record["attempts"] += 1
            record["last_error"] = entry["error"]
            record["last_attempt_at"] = now.isoformat(timespec="seconds")
            failed[entry["key"]] = record

        expired = list(state.get("expired", []))
        expired += [row["key"] for row in buckets["expired"]]

        write_json(
            args.state,
            {
                "updated_at": now.isoformat(timespec="seconds"),
                "posted": posted,
                "failed": failed,
                "expired": expired[-500:],
            },
        )

    emit_github_output(
        {
            "posted_count": len(posted_now),
            "problem_count": len(failed_now) + len(buckets["invalid"]) + len(buckets["expired"]),
            "posted": posted_now,
            "problems": (
                [{"summary": e["summary"], "detail": e["error"]} for e in failed_now]
                + [
                    {"summary": describe(r), "detail": " / ".join(r["problems"])}
                    for r in buckets["invalid"]
                ]
                + [
                    {"summary": describe(r), "detail": "予定時刻を過ぎたため投稿されませんでした"}
                    for r in buckets["expired"]
                ]
            ),
        }
    )

    if failed_now:
        raise SystemExit(f"{len(failed_now)}件の投稿に失敗しました")


if __name__ == "__main__":
    main()
