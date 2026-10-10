# スケジュール管理アプリ

Discord に届いたメッセージ（文章・画像）をローカルLLMで精査し、予定を自動で登録するアプリです。
Slack・Zoom などは今後コネクタとして追加できる構成になっています。

```
Discord ──▶ DiscordConnector ──▶ ScheduleService ──▶ OllamaClient (ローカルLLM)
                                     │  予定の検証・重複判定
                                     ▼
                                 SQLite ──▶ Notifier ──▶ Discord DM / Web UI
```

## 仕様（プロジェクトのドキュメントより）

| 項目 | 内容 |
|---|---|
| 対象アプリ | 現段階は Discord のみ（Slack / Zoom は `app/connectors/` に追加予定） |
| 精査方法 | ローカルLLM（Ollama）の API を使用。インストール済みモデルから精度の高いものを自動選択 |
| 通知 | 予定追加時に「10月15日(木)14時から定例ミーティング」の形式で通知 |
| 重複 | 同日に予定が被った場合は確認通知（Discord DM のボタン / Web UI で「追加する・追加しない」） |
| UI | Apple のデザイン（Human Interface Guidelines）を参考にしたシンプルな Web UI |

## 必要なもの

- Python 3.10 以上
- [Ollama](https://ollama.com/)（ローカルLLM の実行環境）
- Discord Bot（任意。なくても Web UI の「メッセージを解析」で動作確認できます）

## セットアップ（Windows / PowerShell）

```powershell
cd schedule-app
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # 中身を編集
```

### 1. ローカルLLM を用意する

```powershell
ollama pull qwen3-vl:8b     # 画像対応・日本語OK。多くのPCで動く標準的な選択
```

`OLLAMA_MODEL=auto`（既定）の場合、インストール済みのモデルから次の優先順で自動選択します
（`app/llm/ollama_client.py` の `MODEL_PREFERENCE`）。画像付きメッセージは画像対応モデルで処理します。

| 優先 | モデル | 目安のVRAM | 備考 |
|---|---|---|---|
| 1 | `qwen3.8:27b` | 24GB〜 | 最新世代・画像対応。最も精度が期待できる |
| 2 | `qwen3.6:35b` / `qwen3.6:27b` | 24GB〜 | 画像対応 |
| 3 | `qwen3-vl:32b` / `qwen3-vl:30b` | 24GB〜 | 画像内文字認識(OCR)が強い |
| 4 | `gemma3:27b` | 24GB〜 | 画像対応 |
| 5 | `qwen3-vl:8b` | 8GB〜 | **一般的なノートPC向けのおすすめ** |
| 6 | `gemma3:12b` / `qwen3:14b` | 8〜12GB | |
| 7 | `qwen3-vl:4b` / `qwen3:8b` / `gemma3:4b` / `qwen3-vl:2b` | 4GB〜 | 低スペック向け |

リストにないモデルはパラメータ数の大きい順に選ばれます。モデルを追加したら Web UI 下部の「再検出」を押してください。
精度を上げたい場合は `.env` で `LLM_THINK=true`（思考モード。遅くなります）。

### 2. Discord Bot を作る

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** タブ → **Reset Token** でトークンを取得し、`.env` の `DISCORD_BOT_TOKEN` に設定
3. 同じ画面の **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT** を ON
4. **OAuth2 → URL Generator** で `bot` を選び、権限 `View Channels` `Read Message History` `Send Messages` を付けて生成した URL からサーバーに招待
5. Discord の設定 → 詳細設定 → **開発者モード** を ON にし、自分のアイコンを右クリック →「ユーザーIDをコピー」→ `.env` の `DISCORD_OWNER_ID` に設定
6. 監視するチャンネルを絞る場合は、チャンネルを右クリック →「チャンネルIDをコピー」→ `DISCORD_WATCH_CHANNEL_IDS`（カンマ区切り）

> Discord の規約上、Bot は「Bot が参加しているサーバーのチャンネル」と「Bot への DM」しか読めません。
> 個人間の DM などに届いた予定は、そのメッセージを Bot に **転送** すると精査されます。

### 3. 起動

```powershell
python run.py
```

ブラウザで http://127.0.0.1:8000 を開きます。

## 使い方

- 監視中のチャンネルに「来週水曜15時から定例です」のようなメッセージが投稿されると、自動で予定に登録され、あなたに DM で
  `10月14日(水)15時から定例` と通知されます。チラシや招待状のスクリーンショットなど画像も対象です。
- 同じ日の予定と時間が重なる場合は登録を保留し、DM に「追加する / 追加しない」ボタン付きの確認通知が届きます。Web UI の「確認待ち」タブからも選べます。
- Web UI の「＋」から、メッセージの貼り付け解析や手動入力もできます。

## 設定（.env）

| キー | 既定値 | 説明 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | なし | 未設定なら Discord 連携なし（Web UI のみ） |
| `DISCORD_OWNER_ID` | なし | DM 通知の送り先。Bot への DM はこのユーザーからのみ受け付け |
| `DISCORD_WATCH_CHANNEL_IDS` | 全チャンネル | 監視するチャンネルID（スレッドは親チャンネルIDでも可） |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | |
| `OLLAMA_MODEL` / `OLLAMA_VISION_MODEL` | `auto` | モデルを固定したい場合に指定 |
| `LLM_THINK` | `false` | 思考モード（対応モデルのみ） |
| `MIN_CONFIDENCE` | `0.5` | これ未満の確信度の予定は登録しない |
| `PREFILTER` | `true` | 日付・時刻らしき表現がない文章は LLM に送らない |
| `CONFLICT_MODE` | `overlap` | `overlap`=時間帯が重なるとき確認 / `same_day`=同日に予定があれば常に確認 |
| `DEFAULT_DURATION_MIN` | `60` | 終了時刻が不明な予定の長さ（重複判定用） |
| `TIMEZONE` | `Asia/Tokyo` | |

終日の予定は、同じ日の他の予定すべてと「被っている」とみなします。

## ディレクトリ構成

```
run.py                      起動スクリプト
app/
  main.py                   FastAPI + コネクタの起動
  config.py                 設定（.env）
  service.py                精査 → 検証 → 重複判定 → 登録 のパイプライン
  notifier.py               通知（Web UI + 各ツール）
  formatting.py             「○月○日(○)○時から○○」形式の文面
  db.py / models.py         SQLite / データモデル
  llm/ollama_client.py      Ollama API・モデル自動選択・画像の前処理
  llm/prompts.py            抽出プロンプト（基準日と参照カレンダーで相対日付を解決）
  connectors/base.py        連絡ツール共通インターフェース
  connectors/discord_bot.py Discord Bot（受信・DM通知・確認ボタン）
  web/api.py                REST API
  static/                   Web UI（HTML/CSS/JS）
tests/                      pytest
```

## Slack / Zoom を追加するには

`app/connectors/base.py` の `Connector` を継承したクラスを作り、受信メッセージを `IncomingMessage` に変換して
`service.handle_message()` に渡します。そのツールにも通知を送る場合は `notify_added` / `notify_conflict` を実装して
`notifier.register()` で登録し、最後に `app/connectors/__init__.py` の `build_connectors()` に追加します。

## テスト

```powershell
pip install -r requirements-dev.txt
pytest
```
