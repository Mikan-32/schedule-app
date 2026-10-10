# スケジュール管理アプリ

Discord などに届いたメッセージ（文章・画像）をローカルLLMで精査し、予定を自動で登録するアプリです。
メッセージの受け取り方は「Windows の通知を直接読む（Bot 不要）」と「Discord Bot」の2通りで、併用もできます。

```
Windows 通知 ──▶ WindowsNotificationConnector ─┐
Discord      ──▶ DiscordConnector (Bot) ────────┼─▶ ScheduleService ──▶ OllamaClient (ローカルLLM)
                                                 │      │  予定の検証・重複判定
                                                 │      ▼
                                                 │  SQLite ──▶ Notifier ──▶ Discord DM / Web UI
                                                 │      │
                                                 │      └──▶ Google カレンダー（スマホで確認・通知）
```

## 仕様（プロジェクトのドキュメントより）

| 項目 | 内容 |
|---|---|
| 対象アプリ | 現段階は Discord（Windows 通知なら `WINDOWS_NOTIFY_APPS` に Slack / Zoom 等を足すだけで対象にできる） |
| 精査方法 | ローカルLLM（Ollama）の API を使用。インストール済みモデルから精度の高いものを自動選択 |
| 通知 | 予定追加時に「10月15日(木)14時から定例ミーティング」の形式で通知 |
| 重複 | 同日に予定が被った場合は確認通知（Discord DM のボタン / Web UI で「追加する・追加しない」） |
| 変更・取り消し | 「延期」「中止」などの連絡で登録済みの予定を更新。はっきりしていれば自動で反映（「元に戻す」可）、あいまいなら確認 |
| 同じ予定 | リマインドや別チャンネルでの再掲は1件にまとめる。足りない情報（時刻・場所など）だけ補い、新しい情報がなければ通知しない |
| UI | Apple のデザイン（Human Interface Guidelines）を参考にしたシンプルな Web UI |

## メッセージの受け取り方

| | Windows 通知（Bot 不要） | Discord Bot |
|---|---|---|
| 準備 | 不要（初回に通知へのアクセス許可） | Bot の作成・サーバーへの招待 |
| 読める範囲 | 通知が出るものすべて（個人 DM も可） | Bot が参加しているサーバーと Bot への DM |
| 他のアプリ | Slack / Teams / Zoom / LINE なども設定だけで対象 | Discord のみ |
| 画像の精査 | できない（通知に画像の中身は含まれない） | できる |
| 長文 | 通知に表示される範囲のみ | 全文 |
| 取りこぼし | 通知が出ないもの（開いているチャンネル・ミュート中）は読めない | なし |
| 動作条件 | Windows の PC が起動中 | PC が起動中（OS は問わない） |

おすすめは **Windows 通知を基本にして、DM 通知・確認ボタン・画像の精査が欲しければ Discord Bot を併用** する形です。
併用する場合は `.env` で `DISCORD_READ_CHANNELS=false` にすると、同じメッセージを二重に精査しません。

## 必要なもの

- Python 3.10 以上
- [Ollama](https://ollama.com/)（ローカルLLM の実行環境）
- Windows 10 / 11（Windows 通知を読む場合）
- Discord Bot（任意）

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

### 2. Windows 通知を読む設定（Bot 不要）

`.env` の既定値のままで、Windows では自動的に有効になります（`WINDOWS_NOTIFICATIONS=auto`）。

1. Discord（PC 版）の **ユーザー設定 → 通知 → デスクトップ通知を有効にする** を ON
2. Windows の **設定 → システム → 通知** で Discord の通知が ON になっていることを確認
3. 初回起動時に「通知へのアクセス」を求められたら許可します。拒否されている場合は
   **設定 → プライバシーとセキュリティ → 通知** でアクセスを許可してください
4. Slack なども対象にする場合は `.env` の `WINDOWS_NOTIFY_APPS=Discord,Slack,Teams,Zoom,LINE` のように追加

読み取りは次の順に試し、使えたものを使います（Web UI 下部に表示されます）。

- **通知API**: Windows 公式の通知読み取り API（UserNotificationListener）
- **通知データベース**: 上が使えない場合、通知センターのデータベース（`%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db`）を読み取り専用で参照

> 「応答不可（集中モード）」中でも通知センターに入った通知は読み取れます。
> Discord で今開いているチャンネルのメッセージや、ミュート中のチャンネルは通知が出ないため対象外です。

### 3. Discord Bot を作る（任意）

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** タブ → **Reset Token** でトークンを取得し、`.env` の `DISCORD_BOT_TOKEN` に設定
3. 同じ画面の **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT** を ON
4. **OAuth2 → URL Generator** で `bot` を選び、権限 `View Channels` `Read Message History` `Send Messages` を付けて生成した URL からサーバーに招待
5. Discord の設定 → 詳細設定 → **開発者モード** を ON にし、自分のアイコンを右クリック →「ユーザーIDをコピー」→ `.env` の `DISCORD_OWNER_ID` に設定
6. 監視するチャンネルを絞る場合は、チャンネルを右クリック →「チャンネルIDをコピー」→ `DISCORD_WATCH_CHANNEL_IDS`（カンマ区切り）

> Discord の規約上、Bot は「Bot が参加しているサーバーのチャンネル」と「Bot への DM」しか読めません。
> 個人間の DM などに届いた予定は、Windows 通知で読むか、そのメッセージを Bot に **転送** すると精査されます。
> Bot から DM を受け取るには、Bot と同じサーバーに参加している必要があります（自分専用のサーバーに招待すれば OK）。

### 4. 起動

```powershell
python run.py
```

ブラウザで http://127.0.0.1:8000 を開きます。ログは `data\app.log` にも保存されます。

### 5. Windows にサインインしたら自動で起動する（おすすめ）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
```

タスク スケジューラに「ScheduleApp」を登録し、すぐに起動します（管理者権限は不要。コンソール画面は出ません）。
登録できない環境では、スタートアップ フォルダのショートカットで代用します。

- 止める: `Stop-ScheduledTask -TaskName ScheduleApp`
- 解除する: `powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1`
- 既に起動していれば、`python run.py` を実行しても二重には起動しません

**スリープ対策**: アプリの実行中は、電源につないでいる間 PC がスリープしないようにしています（`PREVENT_SLEEP=ac`）。
電源設定そのものは変えず、アプリを終了すると元に戻ります。ノート PC のカバーを閉じたときもスリープさせたくない場合は、
Windows の **コントロール パネル → 電源オプション → カバーを閉じたときの動作** で「電源に接続」を「何もしない」にしてください。

### 6. Google カレンダーと同期する（任意・おすすめ）

確定した予定を、アプリ専用の Google カレンダー「スケジュール（自動登録）」に自動で登録します。
**PC が消えていても、スマホの Google カレンダーで予定を見られて、30 分前に通知も届きます。**
アプリが作ったカレンダーだけを操作する権限（`calendar.app.created`）を使うので、ほかのカレンダーの中身は見えません。

1. [Google Cloud コンソール](https://console.cloud.google.com/) でプロジェクトを作成する（名前は自由。例: `schedule-app`）
2. **API とサービス → ライブラリ** で **Google Calendar API** を検索して **有効にする**
3. **Google Auth Platform**（旧「OAuth 同意画面」）を開き、最初の設定をする
   - アプリ名（例: スケジュール管理）とサポートメール（自分のアドレス）を入力
   - 対象（ユーザーの種類）は **外部**
4. **対象**（Audience）で **アプリを公開** を押し、公開ステータスを **本番環境** にする
   - 「テスト」のままだと、Google の仕様で **7 日ごとに連携が切れます**
5. **クライアント** → **クライアントを作成** → アプリケーションの種類は **デスクトップ アプリ** → 作成
6. 作成したクライアントの **JSON をダウンロード** し、`data\google\credentials.json` という名前で保存する
7. アプリを再起動し、Web UI 下部の「Googleカレンダー: 未連携 **連携する**」を押す
8. Google の画面で自分のアカウントを選んで許可する
   - 「Google はこのアプリを確認していません」と出たら、**詳細 → （アプリ名）に移動** で進みます（自分で作ったアプリなので問題ありません）
9. 「Google カレンダーと連携しました」と表示されれば完了です。スマホの Google カレンダー アプリで
   「スケジュール（自動登録）」カレンダーが表示されているか確認してください（表示されない場合は、アプリの設定でそのカレンダーの同期を ON）

> 同期はアプリ → Google カレンダーの一方向です。Google カレンダー側で編集しても、アプリには反映されません
> （アプリ側でその予定が変わったときに上書きされます）。「確認待ち」の予定は、確定してから登録されます。
> 連携をやめるときは、Web UI 下部の「解除」を押します（登録済みの予定は Google カレンダーに残ります）。

## 使い方

- 「来週水曜15時から定例です」のようなメッセージの通知が届く（または Bot が監視中のチャンネルに投稿される）と、
  自動で予定に登録され、`10月14日(水)15時から定例` と通知されます（Web UI、Bot があれば Discord DM にも）。
  Bot を使う場合は、チラシや招待状のスクリーンショットなど画像も対象です。
- 同じ日の予定と時間が重なる場合は登録を保留し、DM に「追加する / 追加しない」ボタン付きの確認通知が届きます。Web UI の「確認待ち」タブからも選べます。
- 「定例は16日の15時に変更です」「明日のゼミは中止です」のような連絡が届くと、登録済みの予定を変更・取り消しします。
  - 対象の予定がはっきりしていて確信度も高ければ自動で反映し、`10月16日(金)15時から定例に変更しました（変更前: 10月15日(木)14時）` のように通知します。
    通知（Web UI / Discord DM）の「元に戻す」で取り消せます。取り消した予定は削除せずに残しているので、元に戻すと復活します。
  - どの予定のことか確定できないとき、確信度が低いとき、変更後の日時に別の予定があるときは「確認待ち」に入り、
    「変更する / 変更しない」（取り消しは「取り消す / 残す」）を選べます。
  - 元の予定が未登録のまま「○日に変更」と届いた場合は、変更後の内容を新しい予定として登録します。
- 同じ予定がリマインドや別のチャンネルで何度届いても、予定は1件のままです。
  - 同じ日・開始時刻が矛盾しない・タイトルが似ている（「定例ミーティング」「定例MTG」「定例会議」などの表記ゆれも同じとみなす）か、
    ローカルLLM が登録済みの予定と同じと判断した場合にまとめます。
  - 時刻・終了時刻・場所・補足のうち、登録済みの予定に足りない情報だけを補います（既にある情報は上書きしません）。
    補ったときは `同じ予定の連絡をまとめました: …に場所「会議室A」を追加` と「元に戻す」付きで通知し、新しい情報がなければ通知しません。
  - 同じ予定か判断できず「確認待ち」に入ったものは、「同じ予定（まとめる）」ボタンで既存の予定にまとめられます。
    開始時刻が同じ場合は「同じ予定があるかもしれません」と表示します。
- ローカルLLM（Ollama）が止まっているときに届いたメッセージは捨てずに保存し、つながったら自動で精査します（Web UI 下部に「精査待ち」の件数）。
  LLM の応答が壊れているなどで 3 回失敗したものは「精査できなかったメッセージ」になり、「再試行」でやり直せます。
- Web UI の「＋」から、メッセージの貼り付け解析や手動入力もできます。

## 設定（.env）

| キー | 既定値 | 説明 |
|---|---|---|
| `WINDOWS_NOTIFICATIONS` | `auto` | Windows 通知の読み取り。`auto`=Windows なら有効 / `true` / `false` |
| `WINDOWS_NOTIFY_APPS` | `Discord` | 対象アプリ（表示名の部分一致、カンマ区切り） |
| `WINDOWS_NOTIFY_BACKEND` | `auto` | `auto` / `winrt`（通知API）/ `database`（通知データベース） |
| `WINDOWS_NOTIFY_POLL_SEC` | `2` | 新着通知を確認する間隔（秒） |
| `WINDOWS_NOTIFY_BACKFILL_MIN` | `30` | 起動時に、直近何分以内の通知までさかのぼって精査するか |
| `DISCORD_BOT_TOKEN` | なし | 未設定なら Discord Bot は無効 |
| `DISCORD_READ_CHANNELS` | `true` | `false` にすると Bot はチャンネルを読まず DM 通知だけ行う |
| `DISCORD_OWNER_ID` | なし | DM 通知の送り先。Bot への DM はこのユーザーからのみ受け付け |
| `DISCORD_WATCH_CHANNEL_IDS` | 全チャンネル | 監視するチャンネルID（スレッドは親チャンネルIDでも可） |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | |
| `OLLAMA_MODEL` / `OLLAMA_VISION_MODEL` | `auto` | モデルを固定したい場合に指定 |
| `LLM_THINK` | `false` | 思考モード（対応モデルのみ） |
| `MIN_CONFIDENCE` | `0.5` | これ未満の確信度の予定は登録しない |
| `PREFILTER` | `true` | 日付・時刻らしき表現がない文章は LLM に送らない |
| `CONFLICT_MODE` | `overlap` | `overlap`=時間帯が重なるとき確認 / `same_day`=同日に予定があれば常に確認 |
| `DEFAULT_DURATION_MIN` | `60` | 終了時刻が不明な予定の長さ（重複判定用） |
| `CHANGE_MODE` | `auto_if_clear` | 予定の変更・取り消しの反映方法。`auto_if_clear`=はっきりしていれば自動・あいまいなら確認 / `confirm`=常に確認 / `auto`=常に自動（変更後の日時に別の予定がある場合だけ確認） |
| `CHANGE_AUTO_CONFIDENCE` | `0.8` | `auto_if_clear` のとき、この確信度以上なら自動で反映 |
| `QUEUE_RETRY_SEC` | `60` | 精査できなかったメッセージを何秒ごとに再精査するか |
| `QUEUE_MAX_ATTEMPTS` | `3` | LLM の応答エラーなどで何回失敗したらあきらめるか（接続できないだけなら数えない） |
| `PREVENT_SLEEP` | `ac` | アプリ実行中のスリープ防止（Windows）。`ac`=電源接続中だけ / `always` / `off` |
| `LOG_FILE` | `data/app.log` | ログファイル |
| `GOOGLE_CALENDAR` | `auto` | Google カレンダー同期。`auto`=credentials.json があれば有効 / `true` / `false` |
| `GOOGLE_CREDENTIALS` | `data/google/credentials.json` | Google Cloud でダウンロードした OAuth クライアント |
| `GOOGLE_TOKEN` | `data/google/token.json` | 連携後に保存されるトークン（他人に渡さないこと） |
| `GOOGLE_CALENDAR_NAME` | `スケジュール（自動登録）` | アプリが作る専用カレンダーの名前 |
| `GOOGLE_REMINDER_MIN` | `30` | 時刻のある予定の何分前にスマホへ通知するか（`0` で通知なし） |
| `GOOGLE_SYNC_SEC` | `60` | 同期の間隔（予定が変わったときはすぐ同期） |
| `TIMEZONE` | `Asia/Tokyo` | |

終日の予定は、同じ日の他の予定すべてと「被っている」とみなします。

## ディレクトリ構成

```
run.py                      起動スクリプト
app/
  main.py                   FastAPI + コネクタの起動
  config.py                 設定（.env）
  service.py                精査 → 検証 → 同じ予定の照合・重複判定 → 登録 / 変更・取り消し のパイプライン
  notifier.py               通知（Web UI + 各ツール）
  formatting.py             「○月○日(○)○時から○○」形式の文面
  db.py / models.py         SQLite / データモデル
  llm/ollama_client.py      Ollama API・モデル自動選択・画像の前処理
  llm/prompts.py            抽出プロンプト（基準日と参照カレンダーで相対日付を解決）
  connectors/base.py        連絡ツール共通インターフェース
  connectors/discord_bot.py Discord Bot（受信・DM通知・確認ボタン）
  connectors/windows_notifications.py  Windows 通知の読み取り（通知API / 通知データベース）
  gcal.py                   Google カレンダー同期（OAuth・専用カレンダー・一方向同期）
  power.py                  実行中のスリープ防止（Windows）
  background.py             定期実行タスク（再精査・同期）
  web/api.py                REST API
  static/                   Web UI（HTML/CSS/JS）
scripts/install_autostart.ps1    Windows サインイン時の自動起動を登録（uninstall_autostart.ps1 で解除）
tests/                      pytest
```

## Slack / Zoom を追加するには

Windows 通知で読む場合は `.env` の `WINDOWS_NOTIFY_APPS` にアプリ名を足すだけです。
それぞれの API（Slack Bot など）で全文や画像まで読みたい場合は、`app/connectors/base.py` の `Connector` を継承したクラスを作り、受信メッセージを `IncomingMessage` に変換して
`service.handle_message()` に渡します。そのツールにも通知を送る場合は `notify_added` / `notify_conflict` を実装して
`notifier.register()` で登録し、最後に `app/connectors/__init__.py` の `build_connectors()` に追加します。

## テスト

```powershell
pip install -r requirements-dev.txt
pytest
```
