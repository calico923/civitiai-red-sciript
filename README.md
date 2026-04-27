# civitiai-red-sciript

[Civiti.red](https://civitai.red) の `/api/v1/models` を **Newest** 順にカーソルページング（`metadata.nextPage` 追従）し、**LoRA / Checkpoint（Illustrious・NoobAI）/ Embedding** など **`stats.thumbsUpCount / stats.downloadCount`** が指定パーセント以上のモデルを **JSONL** に書き、`--preset all` 時は **HTML レポート**（表＋バー）も出せます。

**ログイン限定・NSFW を含む一覧向けに、API キー（Bearer）が必須です。**

## 要件

- Python 3.9+（標準ライブラリのみ）
- Civitai / Civiti.red の **API key**（アカウント設定から発行）

## 認証

**推奨: このディレクトリに `.env` を置く**（dotenv と同様の `KEY=value` 形式。`#` 行コメント可）

```bash
# civitiai-red-sciript/.env
CIVITAI_API_KEY=your_token_here
```

- 読み込み順: **`--env-file` で指定したファイル**（既定はカレントディレクトリの **`.env`**）→ 既に環境に入っている値は上書きしません。
- 別パスなら `python3 scrape_newest_ratio.py --env-file /path/to/.env ...`

シェルだけで運用する場合:

```bash
export CIVITAI_API_KEY='your_token_here'
```

または毎回 `--api-key` を渡します。

## 使い方

### 3 カテゴリまとめて取得 + グラフィカル HTML

`--out-dir` に `hits_lora.jsonl` / `hits_checkpoint.jsonl` / `hits_embedding.jsonl` と **`report.html`**（タブ切替・いいね/DL/比率%/閾値%/リンク・バー）が出ます。

```bash
python3 scrape_newest_ratio.py --preset all --out-dir ./out --max-pages 20 --sleep 0.3
open ./out/report.html
```

Checkpoint 枝は API の `baseModels` に **Illustrious** と **NoobAI**（Civiti.red 上の表記）を付けます。変えたい場合は `--base-models Illustrious,NoobAI`。

### 1 カテゴリだけ / 従来の custom

```bash
# LoRA のみ → hits.jsonl
python3 scrape_newest_ratio.py --preset lora --max-pages 10 --out hits.jsonl

# Checkpoint（Illustrious + NoobAI）のみ + HTML
python3 scrape_newest_ratio.py --preset checkpoint --max-pages 10 --out hits.jsonl --html report.html

# Embedding（TextualInversion）のみ
python3 scrape_newest_ratio.py --preset embedding --out emb.jsonl

# 標準出力へヒットのみ（進捗・ハートビート・サマリは stderr）。custom は従来どおり --types
python3 scrape_newest_ratio.py --preset custom --max-pages 2 --out hits.jsonl

# ハートビート間隔（秒）。0 で無効
python3 scrape_newest_ratio.py --heartbeat-sec 15 --max-pages 50 --out hits.jsonl
```

### 主なオプション

| オプション | 説明 |
|------------|------|
| `--env-file` | 読み込む `.env` パス（既定: `.env`。無ければ無視） |
| `--api-key` | Bearer トークン（**必須**。`.env` / 環境変数 `CIVITAI_API_KEY` でも可） |
| `--base-url` | 既定 `https://civitai.red` |
| `--limit` | 1ページあたり件数（既定 100） |
| `--preset` | `custom`（既定）/ `lora` / `checkpoint` / `embedding` / `all` |
| `--out-dir` | `--preset all` 用: 出力先ディレクトリ（既定 `scrape_output`） |
| `--html` | 単独 preset 時に HTML レポートのパス（例 `report.html`） |
| `--base-models` | `checkpoint` と `all` の Checkpoint 枝用（既定 `Illustrious,NoobAI`） |
| `--types` | `--preset custom` のときのみ（既定は従来どおり 3 種混在） |
| `--lora-tags` | `lora` / `all` の LoRA 枝: `tag=` 走査をカンマ区切りで（既定 `style,concept,pose`） |
| `--lora-exclude-tags` | 上記のヒットのうち、モデル `tags` と**完全一致**（大小無視）のものを除外（既定で `character` 等。空文字 `""` で無効） |
| `--min-thumb-pct` | 既定 `15`（`downloadCount > 0` のとき `(thumbsUpCount/downloadCount)*100 >= この値`） |
| `--max-pages` | 取得ページ上限 |
| `--max-items` | スキャンするアイテム総数の上限（0 で無制限） |
| `--nsfw` | `true`（**既定**） / `false` / `omit` |
| `--heartbeat-sec` | **stderr** に進捗を N 秒ごとに出力（既定 `30`。`0` でオフ）。長い HTTP 待ち中も「死んでいない」ことが分かります |
| `--sleep` | リクエスト間の秒数 |
| `--timeout` | HTTP タイムアウト（秒） |

各行は JSON（`thumbsUpCount` / `downloadCount`／いずれも**モデル全体の累計**として返る `stats`）`thumb_ratio_pct` / `threshold_pct` / `latestVersionPublishedAt`（全バージョンの `publishedAt` のうち**最新**）/ `modelCreatedAt`（初回目安・最古の `createdAt`・詳細取得時のみ）など）。`--preset all` 終了時は stderr に各パスとサマリ JSON が出ます。

## HTML レポートの再生成

**API に再度アクセスせず**、既存の JSONL だけから `report.html` を書き直します。次のようなときに使います。

- `scrape_newest_ratio.py` 内のレポート用 HTML / JS / CSS を変更したあと、同じ採取結果で見た目だけ試したい
- `out/hits_*.jsonl` を手で直したあと、表を取り込み直したい

### 手順

1. プロジェクト直下（`scrape_newest_ratio.py` があるディレクトリ）で実行する。
2. 入力は固定で **`out/hits_lora.jsonl`**、**`out/hits_checkpoint.jsonl`**、**`out/hits_embedding.jsonl`**。いずれかが無い場合はその分は空として扱われます。
3. 出力は **`out/report.html`**（`out` が無ければ作成されます）。

```bash
cd civitiai-red-sciript
python3 regenerate_report.py
```

終了時に stderr に `Wrote .../out/report.html`、stdout にそのパスが出ます。ブラウザでは **強制再読み込み**（キャッシュ回避）で開き直すと確実です。**LoRA タブ**ではツールバーの **Style / Concept / Pose** チェックで表示を絞り込めます（JSONL を変えずにクライアント側のみ）。

**注意:** JSONL に無い項目（例: 後からスクリプトで追加した `tagCategory` など）は、**再採取していない**限り再生成でも埋まりません。データそのものを更新する場合は `scrape_newest_ratio.py` を再度実行してください。

## 注意

- 公開 API のカーソル実装は厳密な全件整合を保証しない場合があります。同じ `id` が再出現した場合は警告を出します。
- 短い間隔・小さい `--max-pages` から試し、サイトの負荷とブロックに配慮してください。

## ライセンス

MIT — 全文は [LICENSE](LICENSE) を参照してください。
