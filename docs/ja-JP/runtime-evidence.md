# Dockerランタイムの検証結果

言語: [English](../runtime-evidence.md) | 日本語

2026-10-25に、uvで管理するPython標準ライブラリとDocker Engineサーバー29.8.2（Docker Desktop）を使ってローカルで検証しました。Alpineの `alpine:3` は、ローカルで `sha256:f7bbaec1bd0330a8dba26f03e05f421bb3c8b807bb41873df3c3b0c3d1083d8f` に解決されました。試行を始める前に、イメージをローカルに準備してください。コンテナーの起動にはすべて--pull neverを使い、イメージがなければ実行を停止します。

## 公開APIと固定された環境

`TrialRunner(config).run(skill_text, case, trial_id=...)` は、status、outputs、tokens、duration_seconds、termination_reason、model（provider/id/options）、environment_hash、skill_loaded、transcriptを返します。成功は収集に成功したことを意味し、正しさの判定は外部の評価器が担います。公開ケースの境界を越えるのは、id、group_id、prompt、files、output_paths、operationだけです。expectedは一切読み取りません。

`prepare_environment(pinned_image_id=None)` は、ローカルの変更不能なイメージIDを1つ解決し、内部のサンドボックスイメージに設定して、そのIDを返します。後続の呼び出しではそのIDを保持し、矛盾するIDが渡された場合は失敗します。再開時は、マニフェストにある正確なIDを指定してください。そのIDがローカルに存在し、同じIDに解決される必要があります。各サンドボックスは、設定された変更不能なイメージを検証し、固定後に元の可変タグを再解決することはありません。コアはマニフェストの永続保存と、比較の受け入れ判定を担います。環境ハッシュには、固定したイメージ、ハーネスのバージョン2、固定フラグ、タイムアウト、実行時のストレージ上限、コレクターの制限、スーパーバイザーと候補の実行IDを含めます。nonceラベルとホストのステージングパスは所有権データであり、比較用の識別情報から除外します。

モックは実際のDocker読み取りツールで/SKILL.mdを読み、実際のシェル変換を行ってから終了します（3ステップ）。operation=uppercaseはtrを、copyはcpを呼び出します。input.txtがあればそれを使い、なければ最初の公開入力を使います。出力パスはちょうど1つ必要です。デフォルトのパスは/input/input.txtと/output/result.txtです。ツールのループの検証には、この決定論的で課金のないモックを使ってください。実モデルの実行には、[model-evidence.md](model-evidence.md)で説明するアダプターを設定してください。

`reservation_tokens(config)` は、厳密にmax_steps*(max_input_tokens+max_output_tokens)と等しくなります。モックはシリアライズしたリクエストとレスポンスのUTF-8バイト数を数え、各ステップの上限を適用します。実モデルモードでは、バイト数の推定ではなく、トークンの事前確認と報告された使用量を使います。使用量が不明な場合は、予約枠全体を保持します。

## ストレージとプロセスの隔離

ホストからマウントするのは、専用の読み取り専用入力とSKILL.mdだけです。プロジェクト、正解、評価器、他の候補、Dockerソケットはマウントしません。ネットワークはnone、rootfsは読み取り専用とし、すべてのcapabilityを削除してno-new-privilegesを有効にします。CPU、メモリーとスワップ、PID、/tmpのtmpfsのバイト数とinode数に上限を設定します。モデルのキーはホスト側に保持します。

出力には、一意のDocker localドライバーの **tmpfs** ボリュームを使い、type=tmpfs、device=tmpfs、o=size=<cap>,nr_inodes=<bound>,uid=65534,gid=65534,mode=0770を設定します。使用前にドライバーオプションを検査し、マウントされたファイルシステムがtmpfsで、対応する割り当て単位が4096バイトであることを確認します。未対応のストレージは、モデル実行前に安全側に倒して拒否します。カーネルによる割り当てバイト数の上限はceil(max_total_bytes/4096)*4096です。このページ単位の切り上げは明示的な仕様です。論理上限が1ページ未満でも、実行中は1ページの割り当てを許可します。スパースファイルの論理長は、カーネルの割り当てバイト数上限とは異なります。収集時には、ファイル単位と全体の正確な論理サイズを別途制限します。inode数の上限は、ルートディレクトリを含めてmax_files+1です。入れ子のディレクトリもこの同じ枠を消費するため、使用可能なファイル数が減ります。

メインコンテナーの信頼されたroot PID1は、`/bin/sleep <timeout_seconds>` だけを実行します。候補の各ツールはdocker exec --user 65534:65534を使います。rootでのexecを公開するツールはなく、非rootの候補はCAP_KILLなしではrootのスーパーバイザーへシグナルを送れません。スーパーバイザーがデーモン側で終了すると、ホストのPythonによるクリーンアップとは独立に、候補のすべての子孫プロセスを終了させます。
別の信頼されたrootのkeeperが同じ出力ボリュームをマウントし、試行のタイムアウトに固定の60秒の収集猶予を加えた時間だけスリープします。メインが停止している間も、keeperがtmpfsのマウントを維持するため、Docker cpで最終成果物を読み取れます。keeperの寿命は有限で、所有者が異常終了しても独立に終了します。すべてのマウントが解放されると、tmpfsのデータは消えます。keeperは候補のコマンドを受け取りません。メインは明示的にクリーンアップするまで停止したままです。所有者の異常終了後は、ボリュームのメタデータとステージングにもジャーナルによる復旧が必要です。

停止処理はkillの前にState.Runningを確認し、後でfalseになったことを確認します。自然な期限切れと繰り返しの停止は、冪等に処理します。収集にはさらに稼働中のkeeperが必要で、停止済みコンテナーに対する `docker cp ID:/output/. -` を-Lなしで使います。アーカイブのバイト数と処理時間を制限し、tarのストリーミングモードで各メンバーを検証します。extractallは使いません。パストラバーサル、絶対パス、重複名、リンク、スパースファイルや特殊ファイル、想定外または欠落した出力、不正なUTF-8、論理バイト数・ファイルサイズ・個数の超過は拒否します。返す文字列は切り離されたコピーであり、後のファイルシステムへの書き込みでは変更できません。

## リソースの厳密なジャーナル記録と復旧

`TrialRunner.resource_recorder` は呼び出し可能なプロパティで、デフォルトはNoneです。コアは実行前にStore.appendを設定します。ステージング、ボリューム、コンテナーを作成する前に、サンドボックスはresource_id=<UUID hex>、論理的なtrial_id、resources={main,keeper,volume,labels,staging}を含むresource_allocatedを出力します。名前は、そのnonceから厳密に導出します。ラベルにはskill-evolver.nonceを含め、ステージングには一時ディレクトリの正確なパス、nonce、ホストUIDを記録します。その.ownerマーカーにはnonceを記録します。イメージの検査ではリソースを作成しません。

`recover_resources(events)` は、対応する割り当てと解放を集約し、対応する解放のない正確な記録だけを復旧します。削除前に、正規の名前・パス・UID、既存のコンテナーとボリュームのnonceラベル、ステージングのマーカーと所有権を検証します。コンテナーはプレフィックス検索ではなく、検査した変更不能なIDを指定して削除します。ラベルのないリソースや不一致のリソースは、安全側に倒して拒否します。完全なクリーンアップに成功すると、同じresource_id/resourcesでresource_releasedを出力します。名前とラベルをDockerでの作成前に記録しているため、作成レスポンスが失われても復旧できます。広範囲の列挙、無関係なコンテナーへのアクセス、プレフィックスによるクリーンアップは行いません。

## 検証

```sh
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --no-project python -m unittest discover -s tests -p test_sandbox.py -v
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --no-project python -m unittest discover -s tests -p test_runner.py -v
uv tool run --from ruff==0.14.14 ruff check --select F,E9 src/skill_evolver/sandbox.py src/skill_evolver/runner.py tests/test_sandbox.py tests/test_runner.py
uv tool run --from mypy==1.19.1 mypy --check-untyped-defs --ignore-missing-imports src/skill_evolver/sandbox.py src/skill_evolver/runner.py
```

ストレージテストでは実際のDockerストレージを使います。上限4096バイトでは、8192バイトの書き込みが失敗し、ファイルへの割り当ては最大4096バイトにとどまります。使用可能なファイル用inodeが3つの場合、4つ目の空ファイルの作成は失敗します。これらの確認用ファイルを置き換えた後も、keeperがマウントを保持している間、停止済みメインからの収集は安定したテキストを返します。

所有者となるサブプロセスがメイン、keeper、30秒間動作する候補の子孫プロセスを起動し、その後SIGKILLを受けます。テストでは検証時間を抑えるため、メインのタイマーを3秒、keeperの猶予を1秒にします。4.2秒後、メインとkeeperはRunning=false、Pid=0を報告します。fsync済みの割り当てジャーナルからの復旧により、対象のコンテナー、ボリューム、ステージングを厳密に特定して削除し、解放を出力します。クリーンアップを繰り返しても安全です。他のテストは、入力の読み取り専用化、ホストのシークレットとパスの非公開、ループバックだけのネットワーク、非rootのツール、rootタイマーへのシグナル拒否、実際の変換とコピー、毎回新しい状態と会話、悪意ある成果物、所有権不一致の厳密な拒否、イメージの固定と再開時の動作を対象にしています。これらのランタイムテストでは、実APIや有料APIの呼び出しも、実際の認証情報の読み取りも行いません。

## 制約

これらの結果は、Docker Engine 29.8.2と検証したローカル設定に適用されます。実モデルの実験には、実プロバイダーでの検証と請求の確認が別途必要です。
