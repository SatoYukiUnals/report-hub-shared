# report-hub

AI が出した**調査結果・作業計画・実施結果**を手元のブラウザで読み、ページ内の**確認事項にその場で回答**するためのローカル専用サーバー。回答はファイルに残り、AI がそれを読んで作業を続ける。

特定のプロジェクトに属さない共通の置き場として、リポジトリの外（`~/report-hub/` のような場所）に置く。複数のプロジェクトを横断して使う。

---

## 使い方

```bash
docker compose up -d        # 起動。http://localhost:5180/
docker compose logs -f      # アクセスログ
docker compose down         # 停止
```

`http://localhost:5180/` にプロジェクト別のレポート一覧が出る。レポートを開き、確認事項に答えて画面下の［回答する］を押すと、同じ場所の `.answers.json` に保存される。同じ設問には何度でも答えられ、**最後の回答だけが残る**。

一覧は 1 分ごとに中身を見に行き、変わっていれば自動で描き直す（開いたままにしておける）。中身が同じ間は何もしない。

一覧はプロジェクトを大項目にして、その下にレポートをぶら下げる形。レポートの先頭には「← レポート一覧」があり、一覧のそのプロジェクトの位置へ戻る。

一覧では次が分かる。

- **未回答 n** … そのプロジェクトに答えていない設問がいくつ残っているか。未回答があるプロジェクトが上に来る。
- **新着 n** … 前回開いたあとに更新されたレポートの数。開けば消える（記録は `reports/.read.json`）。
- レポート 1 件ごとに `未回答 2 / 6`・`回答済 6 / 6`・`設問なし` のいずれかが出る。

## セットアップ（AI に使わせる）

サーバーを起動しただけでは AI は使ってくれない。**「調査結果・作業計画・実施結果は HTML レポートで出す」という運用ルールを、AI が読む `CLAUDE.md` に入れて初めて効く。**

入れる作業は `/report-hub-setup` スキルがやる。使う人は **① スキルを入れる → ② 呼ぶ → ③ 質問に答える** の 3 つだけ。

### ① clone してスキルを入れる（最初の 1 回だけ）

```bash
git clone <このリポジトリ> ~/report-hub && cd ~/report-hub
python3 bin/setup-claudemd.py install-skill --yes    # /report-hub-setup を使えるようにする
docker compose up -d                                  # http://localhost:5180/
```

`install-skill` は `~/.claude/skills/report-hub-setup` から repo の中のスキルを symlink で参照させるだけ。
実体は repo に残るので、**`git pull` すればスキルもルールも新しくなる**。

入れ終わったら **Claude Code を開き直す**（起動時に読み込まれるため）。

### ② `/report-hub-setup` と打つ

「report-hub をセットアップして」のように話しかけても呼ばれる。

### ③ 聞かれることに答える

スキルは**どの階層の `CLAUDE.md` に入れるか**を聞いてくる。ここだけが決めどころ。

| 選ぶ階層 | 効く範囲 |
| :--- | :--- |
| `~/.claude` | そのマシンの全プロジェクト |
| `~/ghq/github.com/<org>` のような親ディレクトリ | その配下のリポジトリすべて |
| リポジトリのルート | そのリポジトリだけ |

選ぶと、**何が書き込まれるかを差分で見せてから**「入れてよいか」を聞いてくる。承認すると書き込み、最後にサーバーが動いているかまで見て、止まっていれば起動する。

### 何が入るのか

触るのは**選んだ階層の `CLAUDE.md` 1 ファイルだけ**。入るのはルール本体を読み込む十数行で、本文そのものはコピーされない。

```markdown
<!-- report-hub:begin この区画は report-hub が書く。ルールを直すなら repo の rules/report-hub.md -->
# 成果物は HTML レポートで出す（report-hub）

調査結果・作業計画・実施結果・レビュー・確認事項は、チャットの箇条書きで済ませず
report-hub の HTML レポートとして出す。運用ルールの本体は次の 1 行で読み込む。

@~/report-hub/rules/report-hub.md
...
<!-- report-hub:end -->
```

- **CLAUDE.md が無ければ**その数行だけのファイルができる。**既にあれば**既存の中身はそのまま残り、末尾に区画が足される（書き換える前の控えが `CLAUDE.md.bak-<日時>` に残る）。
- **もう入っていれば**区画の中だけが入れ替わる。二重にならないし、区画の外に自分で書いたルールは触らない。
- 効き始めるのは**次にセッションを開いたとき**から。

### 入れた後

- **ルールを直したいとき**は `rules/report-hub.md` を直す。配った先は `git pull` するだけでよく、各マシンの `CLAUDE.md` を書き換えて回る必要はない。
- **repo を移したり消したりすると**ルールが読めなくなる。移したらもう一度 `/report-hub-setup` を実行する。
- 効かせる階層を増やしたい・外したいときも `/report-hub-setup` を実行する（外すのは区画を消すだけなので手でもよい）。

## 置き場所

```
report-hub/
├── docker-compose.yml
├── server.py                        ← 一覧・配信・回答の保存（標準ライブラリのみ）
├── mdlib.py                          ← 成果物ビューア（/d/…）で .md を HTML に直す
├── bin/
│   ├── watch-answers.py             ← 回答が入るまで待つ（AI が使う）
│   └── setup-claudemd.py            ← CLAUDE.md から運用ルールを読ませる（セットアップ）
├── rules/
│   └── report-hub.md                ← 運用ルールの本体。CLAUDE.md から読み込まれる（直すのはここ）
├── skills/
│   └── report-hub-setup/            ← セットアップ用のスキル（/report-hub-setup）
├── assets/
│   ├── report.css / tokens.css / nav.css / doc.css / index.css
│   ├── index.js
│   └── answers.js                   ← 回答の保存（末尾の［回答する］を差し込む）
├── templates/                       ← 用途別の雛形。複製して使う
│   ├── qa.html / review.html / survey.html / plan.html / result.html
│   └── wbs-status.html / wbs-change.html
└── reports/
    ├── .read.json                   ← 開いた時刻（新着の判定用・サーバーが書く）
    └── <プロジェクト>/
        ├── <名前>.html              ← AI が書く（進行中）
        ├── <名前>.answers.json      ← サーバーが書く（回答）
        └── done/                    ← 片が付いたものをここへ移す（完了）
            ├── <名前>.html
            └── <名前>.answers.json
```

## 進行中と完了

レポートの状態は**置き場所**で表す。ファイルの中に状態は書かない。

| 状態 | 置き場所 | 一覧での見え方 |
| :--- | :--- | :--- |
| 進行中 | `reports/<プロジェクト>/` | 大項目の下にそのまま並ぶ |
| 完了 | `reports/<プロジェクト>/done/` | 「完了 n 件」に畳まれる |

「未回答 n」「新着 n」は進行中のぶんだけ数える。完了へ移すと一覧が静かになる。移しても読めるし（`/r/<プロジェクト>/done/<名前>.html`）、回答も残る。

```bash
cd report-hub/reports/<プロジェクト>
mkdir -p done
mv <名前>.html <名前>.answers.json done/   # HTML と回答は対で移す
mv done/<名前>.* .                          # 差し戻されたら戻す
```

## テンプレート

`templates/` の雛形を `reports/<プロジェクト>/<YYYY-MM-DD>_<名前>.html` へ複製し、中身を書き換えて使う。一覧の末尾からブラウザで下見できる（`/t/<名前>.html`。下見では回答は保存されない）。

| ファイル | 用途 |
| :--- | :--- |
| `qa.html` | 質問・確認事項 |
| `review.html` | レビュー（RV）。指摘ごとに対応可否を聞く |
| `survey.html` | 調査結果 |
| `plan.html` | 作業計画（対応内容・修正内容・懸念点） |
| `result.html` | 実施結果 |
| `wbs-status.html` | タスク管理ツールから取得した状況の出力（使わないなら削除してよい） |
| `wbs-change.html` | タスク管理ツールを更新する前の確認（現在値 → 変更後。使わないなら削除してよい） |

タスク管理ツールの値を使う場合、値はレポートに焼く。サーバーはツールの API を叩かない（ローカル専用・認証なしのため）。取得時刻をページに必ず書く。

プロジェクト名・レポート名に使えるのは半角英数字・`.`・`_`・`-` のみ。日本語やスペースを含む名前はサーバーが弾く。

## URL

| メソッド・パス | 内容 |
| :--- | :--- |
| `GET /` | レポート一覧（プロジェクトを大項目に、その下へレポート） |
| `GET /r/<プロジェクト>/done/<名前>.html` | 完了へ移したレポート |
| `GET /r/<プロジェクト>/` | 一覧のそのプロジェクトの位置へ戻す（302） |
| `GET /api/signature` | 一覧の中身を表す文字列（自動更新の判定に使う） |
| `GET /r/<プロジェクト>/<名前>.html` | レポート本体（開いた時刻を記録する） |
| `GET /t/<名前>.html` | テンプレートの下見 |
| `GET /assets/<ファイル>` | 共通の css / js |
| `GET /api/answers/<プロジェクト>/<名前>` | 回答の取得（再読み込み時の復元に使う。完了ぶんは `<プロジェクト>/done/<名前>`） |
| `POST /api/answers/<プロジェクト>/<名前>` | 回答の保存（`{answers: [{qa_id, question, choice, note}, ...]}`。1 件だけの `{qa_id, ...}` も受ける） |
| `GET /d/` | 成果物ビューア。読めるプロジェクトの一覧 |
| `GET /d/<プロジェクト>/<パス>` | リポジトリ内のファイル。`.md` は HTML に直して出す |
| `GET /d/<プロジェクト>/<パス>?bare=1` | 同上。サイドバーと余白を外す（レポートへ埋め込むとき） |

## 成果物ビューア（/d/…）

**レポートで触れた現物を、その場で読めるようにするための入口。** レポートに結論だけ書いて
「詳しくはリポジトリの md を見て」となると、Web だけでレビューが終わらないため。

- 公開する範囲は `docker-compose.yml` の `volumes` で決める。**読み取り専用（`:ro`）で足す。**

  ```yaml
  - ~/path/to/your-repo:/srv/sources/your-repo:ro
  ```

  `sources/<名前>` がそのまま URL（`/d/<名前>/…`）になる。増やすときは 1 行足して `docker compose up -d`。
  使わないなら、この行ごと削除してよい（`/d/` は空の一覧になるだけで、他の機能に影響しない）。
- `.md` は `mdlib.py` で HTML に直して出す（見出し・表・箇条書き・チェックボックス・引用・コード・リンク）。
  md 内の相対リンクは、ビューアの URL に読み替えるので**ドキュメントどうしを辿れる**。
- `.txt` `.csv` `.json` `.py` `.png` などはそのまま返す。**`.html` `.js` は開けない**（レポートと混ざらないようにするため）。
- `sources/` の外は指せない（`..` やシンボリックリンクはパスの検査で落ちる）。`sources/` は git に入れない。

レポートから使うときは、リンクか `<details>` + `<iframe>`（`?bare=1`）で埋め込む。

```html
<details class="doc">
  <summary>設計メモ<a class="open" href="/d/your-repo/docs/設計メモ.md" target="_blank">別の窓で開く ↗</a></summary>
  <iframe src="/d/your-repo/docs/設計メモ.md?bare=1" loading="lazy"></iframe>
</details>
```

## レポート HTML の書き方

`<head>` で共通の見た目を読み、`</body>` の前で共通スクリプトを読む。確認事項は `div.qa` として書く。

```html
<link rel="stylesheet" href="/assets/report.css">
...
<p class="back"><a href="../">← レポート一覧</a></p>
<div class="qa" data-qa-id="q-1" data-question="どちらの案にするか">
  <h3>案 A と案 B、どちらで進めるか</h3>
  <p class="why">案 A は速いが手動確認が要る。案 B は自動化できるが実装が重い。</p>
  <label><input type="radio" name="choice" value="A">案 A で進める</label>
  <label><input type="radio" name="choice" value="B">案 B で進める</label>
  <textarea name="note" placeholder="補足（任意）"></textarea>
</div>

<script src="/assets/answers.js"></script>
```

- **送信ボタンは書かない。** `answers.js` が画面末尾に［回答する］を 1 つ差し込み、ページ内の全設問をまとめて保存する。残り件数もそこに出る。
- 選択肢の `name` は全部 `choice` でよい。`answers.js` が設問ごとに `choice:<qa_id>` へ付け替えるので、設問をまたいで 1 つしか選べなくなることはない。
- `data-qa-id` は回答の突き合わせに使う。**一度公開したら変えない**（変えると過去の回答と結び付かなくなる）。
- `data-question` は回答ファイルに設問名として残る。後から読み返すときの手掛かり。
- 設問は静的な HTML で書く。JavaScript で組み立てると一覧の未回答件数に数えられない。
- `<pre>`・`<code>`・コメントの中に書いた `data-qa-id` は設問として数えない（書き方の説明を載せても件数が狂わない）。

## 回答の待ち受け（AI 側）

回答が入るまで待ち、入った時点で続きを進めるための待ち受けスクリプト。1 行 1 件で出力する。

```bash
python3 bin/watch-answers.py <プロジェクト> <名前> [--done] [--interval 2]

[待受] 2026-08-05_pending-decisions：6 問中 6 問が未回答
[回答] q-1 → A　／　自動化できるほうを優先したい
[完了] 6 件すべてに回答が入った
```

AI エージェント（Claude Code など）はこれを呼び、回答が届いた時点で起こされて続きを実行する、という形で使う。待ち受けは会話が動いている間だけ。閉じても回答自体は残るので、次のセッションで `.answers.json` を読めば拾える。

## 回答の読み方（AI 側）

```bash
cat reports/<プロジェクト>/2026-08-05_pending-decisions.answers.json
# または
curl -s http://localhost:5180/api/answers/<プロジェクト>/2026-08-05_pending-decisions
```

ファイルが無ければ、まだ一件も回答されていない。設問より回答が少なければ未回答が残っている。

## 回答ファイルの形

```json
[
  {
    "qa_id": "q-1",
    "question": "どちらの案にするか",
    "choice": "B",
    "note": "自動化できるほうを優先したい",
    "answered_at": "2026-08-05 14:32:10"
  }
]
```

## 前提と割り切り

- **ローカル専用**。認証を持たない。公開は `127.0.0.1` のみで、LAN の他端末からは見えない。
- **共有はできない**。他人に見せる報告は別の手段（アーティファクト等）を使う。
- **コンテナが動いている間だけ見える**。`restart: unless-stopped` を付けてあるので、Docker が起動していれば自動で復帰する。
