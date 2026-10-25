<img src="../../assets/icon.svg" width="64" height="64" alt="">

# skill-evolver

言語: [English](../../README.md) | 日本語

予算を固定した Docker 隔離環境で、遺伝的アルゴリズム（GA）、逐次改善、元のスキルを比較する CLI です。Python 3.11 以降と標準ライブラリを使用します。まず同梱のモックで実行手順を確認し、実モデルと未見のタスクでスキルの改善効果を測定します。

## クイックスタート

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

uv のプロジェクト設定は `package = false` です。チェックアウトからの実行では、編集可能インストールを行わず `src` を直接読み込みます。実行時の追加依存はありません。別の環境に通常の wheel をインストールし、コンソールコマンドを使う場合は、次を実行します。

```sh
uv venv /tmp/skill-evolver-cli
uv pip install --python /tmp/skill-evolver-cli/bin/python --no-build-isolation .
uv run --no-project --python /tmp/skill-evolver-cli/bin/python skill-evolver validate experiments/pilot/config.json
```

## 実行条件

試行の前に Docker を起動し、`alpine:3` をローカルに用意します。サンドボックスは不変のイメージ ID を固定し、実験全体で使います。`smoke` は探索ケースの先頭3件を実行するため、本実験とは別の出力ディレクトリを指定してください。`resume` は固定済みの設定と元スキルを照合し、記録済みの結果を再利用します。終了コード 2 は評価未完了、1 は設定不正または実行上のエラーです。`report.json` に全ケース・予算・差分を、`report.md` にレポートを出力します。

実験・ケース・グループの ID は `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` に一致する必要があります。実験は最初の試行前に不変の Docker イメージ ID を固定し、比較可能なスコアを採用する前に観測した環境ハッシュを固定します。再開時は同じイメージを照合し、新たな試行の前に、永続記録された所有リソースだけを回収します。イメージ ID が固定されていない古い実行ディレクトリでは再開できません。新しい実験を開始してください。レポートは、実行環境の固定がない場合、複数の環境が混在する場合、探索中の実行が未解決の場合に、最終試行が成功していても比較を未完了とします。基盤エラー後の再試行が成功すると、その試行のエラーは解消されます。使用量不明の提案や再試行後も残る基盤エラーがある場合は、元のスキルが最終試行を完了しても比較は未完了です。

データ分割、予算・再開、候補の固定、グループ単位の条件付きブートストラップは[実験プロトコル](experiments/protocol.md)で説明します。同梱データは各分割3件で、実行手順を検証します。品質評価では探索・選択・最終評価のデータセットを別々に用意してください。

## OpenAI を使う実験

OpenAI を使う場合は、`model` と `proposal_model` の両方に次の形式で設定します。

```json
{"provider":"openai","id":"YOUR_SUPPORTED_MODEL_ID","options":{},"api_key_env":"OPENAI_API_KEY","count_fee_acknowledged":true}
```

`pricing.version` に料金の記録バージョンを指定し、`pricing.per_token` に入力・出力の両方をカバーする保守的な正の USD/token 単価を設定します。探索・選択・最終評価のトークン予算と金額予算は実行前に固定します。`validate` はキーやプロバイダーへの接続なしで設定を検査します。実際の呼び出しには、指定したホスト環境変数が必要です。ブローカーは固定の OpenAI Responses エンドポイントを使い、入力トークン数の事前計数、出力上限、ホスト側での認証情報管理を適用します。任意の HTTP エンドポイントや追加オプションは拒否します。提案はメタデータを保持し、探索時の失敗記録だけを受け取ります。GA の変異をモデルへ依頼する前に、コードでセクション単位の交叉を行います。応答と使用量は、候補を探索に使う前にジャーナルへ保存します。

台帳は、設定したトークン単価で計算した費用を制限します。予算を決める前にプロバイダーの料金とトークン計数リクエストの費用を別途確認してください。`count_fee_acknowledged` はこの確認要件への同意を記録します。実 API との互換性と実モデルでの改善効果は未検証です。改善を判定するのは、両方の役割に実モデルを使う実験だけです。どちらかがモックの場合、レポートは実行手順の検証結果に限定されます。

## ドキュメント

- [実験プロトコル](experiments/protocol.md)
- [OpenAI Responses アダプター](model-evidence.md)
- [Docker 実行環境](runtime-evidence.md)
- [実験の検証結果](experiments/report-001.md)

## ライセンス

[MIT](../../LICENSE)
