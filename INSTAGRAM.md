# Instagram 予約投稿

Googleスプレッドシートに「投稿日時・メディアURL・キャプション」を書いておくと、
時刻になったら GitHub Actions が自動でInstagramに投稿します。

投稿し忘れをなくすのが目的なので、**書いた時点で完了**にできます。
アプリを開いて予約画面まで進む作業がなくなります。

## 仕組み

| ファイル | 役割 |
| --- | --- |
| `instagram_config.json` | シートのURLとAPIバージョンなどの設定 |
| `scripts/post_to_instagram.py` | シートを読み、時刻が来た行を投稿 |
| `state/posted_instagram.json` | 投稿済みの記録（ワークフローが自動でコミット） |
| `.github/workflows/instagram-scheduled-posts.yml` | 15分ごとの実行、結果のIssue通知 |

15分ごとに実行し、予定時刻を過ぎた行を投稿します。
投稿できたとき・問題があったときは Issue が作られるので、メールで届きます。

## スプレッドシートの書き方

1行目は見出し、2行目からが投稿です。

| 投稿日時 | メディアURL | キャプション | スキップ |
| --- | --- | --- | --- |
| 2026-09-20 19:00 | https://example.com/a.jpg | 今日のカイゼン<br>#現場 #改善 | |
| 2026-09-21 12:00 | https://.../1.jpg\|https://.../2.jpg | 2枚まとめて | |
| 2026-09-22 08:00 | https://example.com/movie.mp4 | リールです | TRUE |

- **投稿日時** … 日本時間。`2026-09-20 19:00` / `2026/09/20 19:00` / `2026年9月20日 19:00` のどれでも可
- **メディアURL** … 公開されている `https://` のURL。`|` で区切ると複数枚（カルーセル、最大10枚）
- **キャプション** … 2200文字まで、ハッシュタグ30個まで
- **スキップ** … `TRUE` と書いた行は投稿しません

`.mp4` / `.mov` のURLは自動でリール投稿になります。

### 画像の置き場所

Instagram は「外部から見えるURL」しか受け付けないので、スマホの中の画像は直接使えません。
このリポジトリはパブリックなので、`media/` に画像を入れてコミットすれば、そのまま使えます。

```
https://raw.githubusercontent.com/vbsaki0317/my-project/<デフォルトブランチ名>/media/a.jpg
```

Googleドライブに置く場合は、共有を「リンクを知っている全員」にして次の形にします。

```
https://drive.google.com/uc?export=download&id=<ファイルID>
```

## セットアップ

### 1. スプレッドシートを作ってCSVで公開する

上の表の見出しでシートを作り、**ファイル → 共有 → ウェブに公開**を開きます。
シートを選び、形式を **カンマ区切り形式(.csv)** にして公開。出てきたURLを
`instagram_config.json` の `csv_url` に貼ってください。

> このURLを知っている人はシートの中身を読めます。キャプションは公開される前提の文章なので
> 通常は問題になりませんが、未公開情報は書かないでください。

### 2. Instagram側の準備

1. Instagramを**プロアカウント**にする
2. Facebookページを作り、Instagramアカウントと連携する
3. [Meta for Developers](https://developers.facebook.com/) でアプリを作成する
4. Instagram Graph API の権限（`instagram_basic`、`instagram_content_publish`、
   `pages_read_engagement`）を付けた**長期アクセストークン**を発行する
5. InstagramビジネスアカウントのユーザーID（数字）を控える

### 3. GitHubに登録する

**Settings → Secrets and variables → Actions** で2つ登録します。

| 名前 | 中身 |
| --- | --- |
| `IG_ACCESS_TOKEN` | 長期アクセストークン |
| `IG_USER_ID` | InstagramビジネスアカウントのユーザーID |

トークンはリポジトリには絶対に書かないでください。Secretsはパブリックリポジトリでも安全です。

### 4. デフォルトブランチに入れる

GitHub Actions の定期実行は**デフォルトブランチのワークフローしか動きません**。
このブランチをデフォルトブランチにするか、デフォルトブランチにマージしてください。

### 5. 動作確認

Actions タブから **Instagram 予約投稿** を選び、`Run workflow` の
`dry_run` を `true` にして実行します。投稿せずに対象行だけ確認できます。

## 手元で試す

```bash
python3 scripts/post_to_instagram.py --list      # シートの中身を全部表示
python3 scripts/post_to_instagram.py --dry-run   # 投稿せず対象だけ表示
python3 scripts/post_to_instagram.py --csv sample.csv --list   # ローカルCSVで確認
```

## 設定

`instagram_config.json`

| 項目 | 既定値 | 意味 |
| --- | --- | --- |
| `csv_url` | `""` | ウェブに公開したシートのCSV URL |
| `api_version` | `v21.0` | Graph APIのバージョン |
| `utc_offset_hours` | `9` | シートに書く日時のタイムゾーン（9=日本時間） |
| `catchup_hours` | `24` | 何時間前までの遅れを取り戻して投稿するか |
| `max_posts_per_run` | `3` | 1回の実行で投稿する最大件数 |
| `max_attempts` | `3` | 同じ行を何回まで再試行するか |

## 安全のための仕組み

- **二重投稿しない** … 投稿済みの行は記録に残り、次回以降は対象外
- **古い投稿を蘇らせない** … 予定時刻から `catchup_hours` 以上過ぎた行は投稿せず、Issueで知らせる
- **一気に投稿しない** … 1回の実行で `max_posts_per_run` 件まで
- **失敗し続けない** … 同じ行が `max_attempts` 回失敗したら止めて、Issueで知らせる
- **投稿前に弾く** … 日時の書式、httpsかどうか、文字数、ハッシュタグ数を事前に確認

## うまくいかないとき

| 症状 | 原因と対処 |
| --- | --- |
| `アクセストークンが無効か期限切れです` | トークンを再発行して `IG_ACCESS_TOKEN` を更新 |
| `api_version が古い可能性があります` | `instagram_config.json` の `api_version` を新しい版に変更 |
| `シートに必要な列が見つかりません` | 見出し行が `投稿日時` `メディアURL` `キャプション` になっているか確認 |
| `メディアの処理に失敗しました` | 動画の長さ・形式・サイズを確認（URLが公開されているかも） |
| 何も起きない | デフォルトブランチにワークフローが入っているか確認 |

## 制限

- 投稿は24時間あたり50件までです（Instagram側の制限）
- 実行は15分ごとなので、予定時刻より**最大15分ほど遅れて**投稿されます。
  GitHub側が混んでいるとさらに数十分ずれることがあります。分単位の精度が必要な用途には向きません
- ストーリーズの投稿には対応していません
