# カイゼンベース YouTube 新着通知

カイゼンベースのYouTubeチャンネル（[現場の人材教育・カイゼンチャンネル / KAIZEN BASE](https://www.youtube.com/c/Kaizen-base)）に
新しい動画が公開されたら通知します。

GitHub Actions が1時間ごとにチャンネルのRSSフィードを確認し、新着が見つかったら
このリポジトリに Issue を作成します。GitHub から Issue 作成のメールが届くので、それが通知になります。
Slack Webhook を設定すれば Slack にも同時に通知できます。

## 仕組み

| ファイル | 役割 |
| --- | --- |
| `config.json` | 監視するチャンネルと、通知対象を絞り込む条件 |
| `scripts/check_new_videos.py` | RSSフィードを取得し、既読の動画IDと突き合わせて新着を抽出 |
| `state/seen_videos.json` | 通知済みの動画ID（ワークフローが自動でコミット） |
| `.github/workflows/youtube-new-videos.yml` | 1時間ごとの実行、Issue作成、Slack通知 |

## セットアップ

**設定済みです。**このブランチがリポジトリのデフォルトブランチなので、
1時間ごとの定期実行はすでに動いています。初回のならし運転（seed）も実行済みで、
その時点の動画は既読になっています。次の新着から通知されます。

残っているのは1つだけです。

### メール通知を受け取れるようにする

リポジトリの **Watch → All Activity**（または Custom → Issues）を選んでください。
これで Issue が作られるたびに GitHub からメールが届きます。

### 既読をリセットしたいとき

Actions タブから **カイゼンベース YouTube 新着通知** を選び、
`Run workflow`（`seed` を `true`）で実行してください。
その時点でフィードにある動画がすべて既読になります。

## 「自分が担当した動画だけ」に絞り込む

初期設定は**新着すべてを通知**します（`config.json` の `include_all: true`）。
担当した動画だけに絞るには、タイトルか概要欄に含まれるキーワードを指定してください。

```json
{
  "match": {
    "include_all": false,
    "title_keywords": ["IE実践編", "時間研究"],
    "description_keywords": ["担当:サキ"]
  }
}
```

- `title_keywords` … 動画タイトルに含まれる文字列（部分一致・大文字小文字は無視）
- `description_keywords` … 概要欄に含まれる文字列
- どれか1つでも一致すれば通知されます

担当動画をタイトルや概要欄から機械的に見分けられない場合は、
`include_all: true` のまま全件受け取り、メールの件名で判断するのが確実です。

## Slack にも通知する

リポジトリの **Settings → Secrets and variables → Actions** で
`SLACK_WEBHOOK_URL` に Slack の Incoming Webhook URL を登録してください。
未設定ならこのステップは自動的にスキップされます。

## 通知の時刻について

通知に出る「公開日時」は**日本時間（JST）**で表示します。

なお、通知が届く時刻は動画の公開時刻とは一致しません。以下の遅れが乗ります。

- チェックは1時間に1回なので、公開から最大1時間ほど待ってから通知されます
- GitHub Actions のスケジュール実行は混雑時に数分〜十数分ずれます（GitHub側の仕様）
- YouTubeのRSSフィードへの反映自体にも数分かかることがあります

「もっと早く知りたい」場合は下の `cron` を短い間隔にしてください。
ただし GitHub Actions のスケジュール実行は5分より短い間隔にはできません。

## チェック間隔を変える

`.github/workflows/youtube-new-videos.yml` の `cron` を編集してください（UTC指定）。

```yaml
    - cron: "17 * * * *"    # 1時間ごと
    - cron: "17 */6 * * *"  # 6時間ごと
    - cron: "17 23 * * *"   # 毎日 日本時間 8:17
```

## 手元で試す

```bash
python3 scripts/check_new_videos.py --dry-run   # 状態を書き換えずに新着を確認
python3 scripts/check_new_videos.py --seed      # 通知せず現在の動画を既読にする
```

## チャンネルIDについて

`config.json` の `channel_id` は `UCK-PjhRfdAnzY2FthVIXB6A` を初期値にしています。
もし別のチャンネルを見てしまう場合は `channel_id` を空文字にしてください。
`channel_url` のページから自動でチャンネルIDを解決します。
最初の実行ログに取得したチャンネル名が出るので、そこで確認できます。
