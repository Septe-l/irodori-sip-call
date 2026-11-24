# irodori-sip-call

ローカルLLMが原稿を書き、TTSが読み上げ、SIPでスマホの Linphone に電話をかけてくれるモーニングコールです。
iPhone では Linphone が CallKit に対応しているので、普通の電話と同じように着信します。応答すると音声が流れ、読み終わったら自動で切れます。

Docker コンテナ1つで動き、設定用の WebUI、定時発信のスケジューラ、外部から予約発信できる REST API / MCP サーバーを含みます。

```
LLM（OpenAI互換）──原稿──▶ TTS（Irodori）──音声──▶ baresip ──SIP──▶ sip.linphone.org ──▶ Linphone（スマホ）
```

## 必要なもの

- Docker が動く Linux マシン（`network_mode: host` を使うため Linux 推奨）
- OpenAI 互換の Chat Completions API を持つ LLM サーバー（LM Studio、Ollama など）
- TTS サーバー：[Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server)（Irodori-Autospeak など、同じ `/v1/audio/speech` を持つものでも可）
- [sip.linphone.org](https://subscribe.linphone.org/) のアカウントを2つ
  - 発信用：このコンテナが使う
  - 着信用：スマホの Linphone アプリでサインインしておく

## 使い方

```bash
git clone https://github.com/Septe-l/irodori-sip-call.git
cd irodori-sip-call
docker compose up -d --build
```

ブラウザで `http://<ホスト>:45000` を開き、次を設定して保存します。

| 項目 | 内容 |
| --- | --- |
| 発信先 | スマホ側の SIP アドレス（例 `sip:yourname@sip.linphone.org`） |
| 発信用ユーザー名 / パスワード / SIPドメイン | 発信用アカウント（ドメインは `sip.linphone.org`） |
| LLM URL / トークン / モデル | 例 `http://192.168.x.x:1234`。モデルはURLを入れるとプルダウンに出ます |
| TTS URL / トークン / 声 | 例 `http://192.168.x.x:8088` |
| スケジュール | 発信時刻・曜日・祝日（日本）を鳴らさないか・有効にするか |

「今すぐ実行」で原稿生成から発信まで一通り試せます。

設定・ログ・生成した音声・予約は `./data` に保存されます（`config.json` はパスワードを含むので権限 600 で保存）。

### プロンプトの置き換え文字

| 文字 | 中身 |
| --- | --- |
| `{month}` `{day}` `{weekday}` `{time_text}` | 発信する日時（`time_text` は「8時ちょうど」のような形） |
| `{hour}` `{minute}` | 原稿を生成した時刻（発信の数分前） |

### 原稿生成で MCP ツールを使う

設定の「原稿生成で使うMCPサーバー」に MCP サーバー（Streamable HTTP）の URL を1行に1つ書くと、原稿を作るときに LLM がそのツールを呼べます。LLM サーバーがツール呼び出し（OpenAI 形式の `tools`）に対応している必要があります。

原稿は発信の数分前に作られるので、ニュースを取得する MCP を登録しておけば、前の晩に「明日の7時にニュースを伝えて」と予約しても、**発信直前のニュース**になります（予約するときにニュースを取って `message` に入れると、予約した時点のニュースになってしまいます）。

```bash
curl -X POST http://<ホスト>:45000/api/calls -H 'Content-Type: application/json' \
  -d '{"at": "明日 7:00", "prompt": "おはようの挨拶のあと、最新ニュースを3つ短く伝えてください"}'
```

ツールの呼び出しや取得に時間がかかる場合は「何分前から準備するか」を長めにしてください。

## Linphone 以外の SIP サービスで使う

設定は一般的な SIP の項目（ユーザー名・パスワード・ドメイン・発信先）なので、他の SIP サービスのアカウントでも登録・発信できます。ただし次の2点に注意してください。

| 設定 | 既定値 | 他のサービスで |
| --- | --- | --- |
| コーデック | Opus | 050番号のサービスや電話回線につながる SIP サービスの多くは G.711（PCMU/PCMA）しか受け付けません。「488 Not acceptable here」で失敗する場合は **G.711** にしてください（音声は 8kHz モノラルになります） |
| 通話の暗号化 | srtp-mand（必須） | Linphone は暗号化必須ですが、SRTP に対応していないサービスもあります。つながらない場合は **srtp**（できれば暗号化）か **none** にしてください |

また、着信側のアプリが通知で起きて普通の電話のように鳴るか（iPhone の CallKit など）は、そのアプリとサービス次第です。Linphone + sip.linphone.org はプッシュ通知で確実に鳴るため既定にしています。

## 予約発信 API / MCP

OpenClaw などのエージェントから「指定の時刻に電話をかけて、これを読み上げて」と頼めます。

### MCP

Streamable HTTP で `http://<ホスト>:45000/mcp` に接続します。

| ツール | 内容 |
| --- | --- |
| `schedule_call` | 予約する。`at`（下の表）または `in_minutes`、読み上げ文 `message` か LLM への指示 `prompt`、`target`、`voice`（すべて省略可。省略すると今すぐ設定のプロンプトで発信） |
| `list_calls` | 予約の一覧 |
| `cancel_call` | 予約の取り消し |

`at` に使える書き方（時刻を省略すると設定の発信時刻）:

| 書き方 | 例 | 意味 |
| --- | --- | --- |
| 時刻だけ | `07:30` | 次に来るその時刻 |
| 年なしの日付 | `11-23 07:30` `11/23 7:30` | 今年（過ぎていれば来年） |
| 今日・明日・明後日 | `明日 7:30` `明後日 7時半` `tomorrow 07:30` | |
| 曜日 | `金曜 7時` `friday 7:30` | 今日より後で次に来るその曜日 |
| 今週・来週・再来週の曜日 | `来週の月曜 8:00` `next week monday 07:30` | 週は月曜始まり |
| ISO 8601 | `2026-11-23T07:30+09:00` | |

予約結果の `at_text` に曜日つきで実際の日時が入るので、意図どおりか確認できます。

### REST

```bash
# 7:30 に決まった文を読み上げる
curl -X POST http://<ホスト>:45000/api/calls -H 'Content-Type: application/json' \
  -d '{"at": "07:30", "message": "おはようございます。会議の時間です。"}'

# 10分後に、LLMに書かせた原稿でかける
curl -X POST http://<ホスト>:45000/api/calls -H 'Content-Type: application/json' \
  -d '{"in_minutes": 10, "prompt": "洗濯物を取り込むよう短く伝えてください"}'

curl http://<ホスト>:45000/api/calls              # 一覧
curl -X DELETE http://<ホスト>:45000/api/calls/<id>  # 取り消し
```

API の詳細は `http://<ホスト>:45000/docs` にあります。

- 原稿と音声は発信の数分前（「何分前から準備するか」）から作り始めます
- 予定より10分以上遅れた予約（コンテナが止まっていた等）は発信せず `missed` になります
- 定時発信と重なった場合は、先の発信が終わってからかけます

## 注意

- **認証はありません。** LAN 内で使う前提です。ポート 45000 をインターネットに公開しないでください。
- baresip 1.1.0（Debian パッケージ）を使っています。動作確認で分かった注意点：
  - `stun.linphone.org` は ICE で失敗するため、既定は `stun.l.google.com:19302` と `medianat=stun`
  - Linphone は通話の暗号化が必須のため、既定は `srtp-mand`
  - baresip は音声を変換しないため、コーデックは1つだけ有効にし、それに合わせて音声を変換してから流しています（Opus: 48kHz ステレオ、G.711: 8kHz モノラル）
- SIP 5070/TCP・UDP と RTP 46000–46200/UDP をホストで使います。

## ライセンス

MIT License（[LICENSE](LICENSE)）
