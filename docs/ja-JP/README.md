<img src="../../assets/icon.svg" width="64" height="64" alt="">

# skill-evolver

言語: [English](../../README.md) | 日本語

既存のClaude Code／Codexのログインで、テキストで答えるスキルの `SKILL.md` を改善できます。参考資料やテンプレートなどの補助ファイルは、読み取り専用で評価に使います。評価課題と合格基準を自動で作り、あなたが確認した後に、目的を保った改善を最大3回試します。改善候補と比較結果は、元のスキルとは別の場所に保存します。Python 3.11以降と標準ライブラリを使用し、この改善フローにはDockerや別のAPIキーは不要です。

## はじめに

準備は5分ほどです。改善の実行には、モデルによって数分から最大1時間かかります。

### 1. インストールとログイン

[uv](https://docs.astral.sh/uv/getting-started/installation/) を用意し、このリポジトリをインストールします。

```sh
uv tool install /path/to/skill-evolver   # このリポジトリのパス。更新後は --reinstall を付けて再実行
skill-evolver --help                     # 見つからない場合は uv tool update-shell を実行し、シェルを開き直す
```

`claude` または `codex` に、普段どおりサブスクリプションでログインしておきます。APIキーは不要です。

### 2. いちばん簡単な方法: エージェントに頼む

同梱のスキルをコピーすると、Claude Code／Codexの会話から使えます。

```sh
mkdir -p ~/.claude/skills && cp -R /path/to/skill-evolver/skills/improve-skill ~/.claude/skills/   # Claude Code
mkdir -p ~/.codex/skills && cp -R /path/to/skill-evolver/skills/improve-skill ~/.codex/skills/     # Codex
```

新しい会話で「`~/.claude/skills/my-skill/SKILL.md` を improve-skill で改善して」と頼みます（Codexでは `$improve-skill` でも呼べます）。エージェントが評価案を作って要約し、**あなたが承認してから**改善を実行し、結果の差分を見せます。元のスキルは、あなたが採用すると答えるまで変更しません。

Codexの既定のサンドボックスでは、このツールが起動する `codex`／`claude` がネットワークに接続できません。エージェントがサンドボックス外での実行の許可を求めたら、内容を確認して許可してください。

### 3. 自分で実行する場合（2コマンド）

```sh
# 評価案を作る（モデル呼び出し1回、通常2分以内）
skill-evolver improve prepare ~/.claude/skills/my-skill/SKILL.md \
  --provider claude --model sonnet --output ~/skill-evolver-runs/my-skill-$(date +%Y%m%d-%H%M%S)

# 評価案を表示し、y で改善を実行（表示された保存先を指定）
skill-evolver improve run ~/skill-evolver-runs/my-skill-20261025-120000
```

`run` の実行中は端末を閉じないでください（中断した実行は再開できません）。各コマンドは、現在の状態と次に打つコマンドを表示します。完了すると、課題ごとの得点、比較レポート（`comparison.md`）、改善が見られた場合は改善版（`candidate/SKILL.md`）と採用用の `diff`／`cp` コマンドを表示します。Codexでは `--provider codex --model <codexの /model に表示される完全なモデルID>` を指定します。指定したモデルが、評価案の作成、課題の実行、改訂案の作成、採点のすべてを行います。

### 評価案で確認すること

承認の前に `evaluation.md` を読み、次を確認します。改善の良し悪しは、この評価案で決まります。

- 課題が、そのスキルの実際の使い方に近いか
- 必須（required）の基準が本当に重要な点か。甘すぎたり、達成不可能だったりしないか
- 今のスキルがすでに全基準を満たしそうではないか（その場合、改善を示せません）。実際にそのスキルでうまくいかなかった依頼で課題を1件差し替える、見かけた失敗を必須基準として1件加える、といった方法で難しくします
- 最終確認用の課題が、改善用の課題と実質的に同じになっていないか

変える場合は `evaluation.json` を編集し、`skill-evolver improve report <保存先>` で表示を更新してから `run` します。`evaluation.md` は表示用なので、編集しても反映されません。課題は改善用・最終確認用とも3件ちょうどで、追加ではなく差し替えで編集します。`id`／`group_id` は重複できず、基準は3〜12件で、うち1件以上を必須（`required: true`）にします。これを満たさない評価案では `run` を実行できません。

## 詳しい仕様

**仕組み:** 元のスキルから、目的・維持する制約・合否基準・2組の課題（改善用と最終確認用、各3件）を作ります。改善用の課題で最大3回の改訂を試し、最も良い候補を最終確認用の課題で元のスキルと比べます。

**結果の読み方:** `observed_improvement`（確認した評価で改善を観測）、`no_observed_improvement`（改善を確認できず）、`incomplete`（評価未完了）の3種類です。必須基準に新たな失敗がなく、最終確認用の課題で得点が上がった場合にだけ改善と表示します。課題は各3件と少なく、作成も採点もモデルが行うため、1件の差でも改善と判定されます。一般的・統計的に優れていることの証明ではありません。

**CLIのバージョン:** 検証済みは Claude Code 2.1.310／Codex 0.164.0 です。それ以外のバージョンでは実行しません。試す場合は `prepare` に `--allow-untested-cli` を付けます。この場合も、必要なオプションの有無とサブスクリプションログインは確認し、レポートに未検証バージョンである旨を残します。`prepare` から `run` の完了までにCLIが更新されると、その実行は未完了になります。実行中はCLIを更新しないでください。Claudeの `sonnet` などの別名は、CLIが返す実モデル名で固定します。実モデル名が返らない場合は停止するので、完全なモデルIDを指定してください。

**承認の固定:** `run` で `y` と答えた時点の目的・評価・モデル・CLIバージョン・実行設定を固定します。その後に入力が変わった場合は、新しい保存先で準備し直します。完了済みの保存先で再実行しても、モデル呼び出しは繰り返しません。課題の実行と採点の呼び出しは何も変更しないため、時間切れのときだけ1回再試行します。それ以外の失敗・中断・結果不明の呼び出しは再試行しません。スクリプトから使う場合は `--json` を付け、`improve report --json` の `approval_hash` を `run --approve-evaluation` に渡します。終了コードは、0が正常、1がエラー・中止・対象外、2が未完了です。

**対応範囲:** 64 KiB以下の `SKILL.md` で、応答をテキストとして評価できるスキル。改善するのは本文の指示による出力の質だけで、frontmatter（`name`／`description`）や、スキルが呼び出されるかどうかは変更も評価もしません。

**補助ファイル:** `references/`・`assets/`・`scripts/` 以下のファイルと、本文が参照・リンクしているファイルを、準備の時点で読み取り専用のスナップショットにして、評価案の作成・課題の実行・採点・改訂案の作成に渡します。補助ファイル自体は改訂せず、改訂案は既存の補助ファイルしか参照できません。`scripts/` は文章として渡すだけで**実行しません**。スクリプトの実行やツール操作が目的に欠かせないスキルは対象外です。補助ファイルは UTF-8 テキストに限り、シンボリックリンク不可、最大32件・1件64 KiB・合計128 KiB です。参照先が実在しない場合や、準備後に補助ファイルが変わった場合は停止します。`app/assets/x` のように長いパスの一部として書かれた語は補助ファイルとして扱いません。

**上限と目安:** 実測では、`haiku` と小さなスキルで43回の呼び出し、約5分でした。元のスキルが改善用の課題で満点なら、改訂を省略して呼び出しを減らします。`prepare` が失敗した保存先は再利用できないので、削除して構いません。上限は改善3回、モデル呼び出し60回（準備の1回を含む）、1回120秒、全体1時間、1回の入力256 KiB、CLIの出力（標準出力・標準エラー）それぞれ1 MiB。CLIが返す使用量だけを記録し、不明な使用量・費用は不明のまま残します。トークン・請求額・サブスクリプション残量の厳密な上限は保証しません。時間切れになった呼び出しの使用量は、提供側で生成が続いていても観測できません。

**実行境界:** 実行設定はモデルによるシェル・MCP・Web・書き込みを制限し、想定外のツール要求を拒否します。管理ポリシーのフック、CLI自身の認証・内部状態の更新、ホスト読み取りの完全隔離は保証範囲外です。CLIのログイン情報はそのまま使い、子プロセスに渡す環境変数からAPIキーと接続先の指定を取り除きます。認証ファイルの読み取り・コピーやCLI設定の変更は行いません。

**インストールせずに実行:** リポジトリのルートで、`skill-evolver` を `PYTHONPATH=src uv run --locked python -m skill_evolver` に置き換えます。

## 比較実験（研究用・上級者向け）

スキルを改善するだけなら、この節は読まなくて構いません。改善手法どうしを比べるための、Dockerを使う別機能の説明です。

予算を固定した Docker 隔離環境で、遺伝的アルゴリズム（GA）、逐次改善、元のスキルを比較する CLI です。Python 3.11 以降と標準ライブラリを使用します。まず同梱のモックで実行手順を確認し、実モデルと未見のタスクでスキルの改善効果を測定します。

## 実験のクイックスタート

Python は uv 経由で実行します。リポジトリのルートでロック済み環境を同期し、`PYTHONPATH=src uv run --locked python -m skill_evolver` を使います。

```sh
uv sync --locked
PYTHONPATH=src uv run --locked python -m skill_evolver validate experiments/pilot/config.json
PYTHONPATH=src uv run --locked python -m skill_evolver smoke experiments/pilot/config.json --output /tmp/skill-evolver-smoke
PYTHONPATH=src uv run --locked python -m skill_evolver run experiments/pilot/config.json --output /tmp/skill-evolver-run
PYTHONPATH=src uv run --locked python -m skill_evolver resume /tmp/skill-evolver-run
PYTHONPATH=src uv run --locked python -m skill_evolver report /tmp/skill-evolver-run
PYTHONPATH=src uv run --locked python -m unittest discover -s tests -v
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --locked python -m unittest discover -s tests -v
```

uv のプロジェクト設定は `package = false` です。チェックアウトからの実行では、編集可能モードでインストールせず `src` を直接読み込みます。実行時の追加依存はありません。別の環境に通常の wheel をインストールし、コンソールコマンドを使う場合は、次を実行します。

```sh
uv venv /tmp/skill-evolver-cli
uv pip install --python /tmp/skill-evolver-cli/bin/python --no-build-isolation .
uv run --no-project --python /tmp/skill-evolver-cli/bin/python skill-evolver validate experiments/pilot/config.json
```

## 実行条件

試行の前に Docker を起動し、`alpine:3` をローカルに用意します。サンドボックスは不変のイメージ ID を固定し、実験全体で使います。`smoke` は探索ケースの先頭3件を実行するため、本実験とは別の出力ディレクトリを指定してください。`resume` は固定済みの設定と元スキルを照合し、記録済みの結果を再利用します。終了コード 2 は評価未完了、1 は設定不正または実行上のエラーです。`report.json` に全ケース・予算・差分を、`report.md` にレポートを出力します。

実験・ケース・グループの ID は `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` に一致する必要があります。実験は最初の試行前に不変の Docker イメージ ID を固定し、比較用のスコアを採用する前に、観測した環境ハッシュを固定します。再開時は同じイメージを照合し、新たな試行の前に、所有するリソースのうち永続記録にあるものだけを回収します。イメージ ID が固定されていない古い実行ディレクトリでは再開できません。新しい実験を開始してください。

レポートは、実行環境の固定がない場合、複数の環境が混在する場合、探索中の実行が未解決の場合に、最終試行が成功していても比較を未完了とします。基盤エラー後の再試行が成功すると、その試行のエラーは解消されます。使用量不明の提案や再試行後も残る基盤エラーがある場合は、元のスキルが最終試行を完了しても比較は未完了です。

データ分割、予算・再開、候補の固定、グループ単位の条件付きブートストラップは[実験プロトコル](experiments/protocol.md)で説明します。同梱データは各分割3件で、実行手順を検証します。品質評価では探索・選択・最終評価のデータセットを別々に用意してください。

## OpenAI を使う実験

OpenAI を使う場合は、`model` と `proposal_model` の両方に次の形式で設定します。

```json
{"provider":"openai","id":"YOUR_SUPPORTED_MODEL_ID","options":{},"api_key_env":"OPENAI_API_KEY","count_fee_acknowledged":true}
```

`pricing.version` に料金の記録バージョンを指定し、`pricing.per_token` に入力・出力の両方に適用する、余裕を見込んだ正の USD/token 単価を設定します。探索・選択・最終評価のトークン予算と金額予算は実行前に固定します。`validate` はキーやプロバイダーへの接続なしで設定を検査します。

呼び出しには、指定したホスト環境変数が必要です。ブローカーは固定の OpenAI Responses エンドポイントを使い、入力トークン数を事前に数え、出力に上限を設け、認証情報をホスト側で管理します。任意の HTTP エンドポイントや追加オプションは拒否します。提案はメタデータを保持し、探索時の失敗記録だけを受け取ります。GA の変異をモデルへ依頼する前に、コードでセクション単位の交叉を行います。応答と使用量は、候補を探索に使う前にジャーナルへ保存します。

台帳は、設定したトークン単価で計算した費用を制限します。予算を決める前にプロバイダーの料金とトークン計数リクエストの費用を別途確認してください。`count_fee_acknowledged` はこの確認要件への同意を記録します。実 API との互換性と実モデルでの改善効果は未検証です。改善を判定するのは、両方の役割に実モデルを使う実験だけです。どちらかがモックの場合、レポートは実行手順の検証結果に限定されます。

## ドキュメント

- [実験プロトコル](experiments/protocol.md)
- [OpenAI Responses アダプター](model-evidence.md)
- [Docker 実行環境](runtime-evidence.md)
- [実験の検証結果](experiments/report-001.md)

## ライセンス

[MIT](../../LICENSE)
