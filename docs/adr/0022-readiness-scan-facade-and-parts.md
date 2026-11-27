# ADR-0022: readiness_scan を窓口と部品に分け、照合値を部品全体で取る

状態: 提案。2026-11-07。

## 問題

`scripts/readiness_scan.py` は 4,888 行あり、`scan()` だけで 849 行ある。直近 90 日に 16 回変わり、scripts の中で最もよく変わる。直しやすさのために分けたい。

ただし、分け方を縛る約束が 4 つある。

- 利用者のリポジトリは、版を 40 桁の SHA で固定して `scripts/readiness_scan.py` をパスと引数で呼ぶ。
- Projects の送信入口は、このファイルを名前で読んで `main(...)` を直接呼び、照合値を受領記録に残す。
- 名義検査の合格控えは、`_scanner_source_sha256()` が返す値で検査器の版を見分ける。これまでは窓口 1 ファイルの SHA-256 だった。
- 試験は窓口をファイルの場所から読み、62 個の名前を直接使い、その一部を差し替える。

窓口 1 ファイルの照合値のままで中身を別ファイルへ移すと、2 つのことが起きる。移した部品を変えても、合格控えが使い回される。受領記録も、実際に動いた部品を覆わなくなる。2026-11-07 に、この 2 つを TLA+ の模型で確かめた。順番を問わない場合は、2 つの規則とも破れる。照合値の仕組みと送信入口の対応が入ってから分ける順番なら、どちらも守られる。

## 決定

- `scripts/readiness_scan.py` は、窓口として同じパスに残す。引数・終了コード・出力の JSON は変えない。
- 中身は、同じフォルダの `scripts/readiness_scan_*.py` に部品として移す。部品は窓口の `_part(name)` だけで読み込む。引数が 1 つで、読む場所は `Path(__file__).resolve().with_name(name + ".py")` に限る。`scripts/local_check.py` が試験を選ぶ時に追える形と合わせるためである。
- `_part` が読めるのは、`readiness_scan_` で始まり、小文字の英字・数字・下線だけでできた名前に限る。同じフォルダのほかのスクリプトは読めない。
- `_part` は部品のバイト列を 1 回だけ読み、その SHA-256 を取り、同じバイト列をそのまま実行する。照合値を取った中身と実行した中身がずれないようにするためで、バイトコードの控えは使わない。実行の前に、モジュール名 `"_" + name` で `sys.modules` に登録する。実行に失敗したら登録を外す。使い回すかどうかは、窓口の中の登録簿だけで決める。同じ名前で同じ SHA-256 なら使い回し、違えば読み直す。
- 照合値 `scanner_bundle_sha256()` は、窓口と、`_part` が読み込んだ部品の全体から取る。名前順に `ファイル名 + NUL + SHA-256 + 改行` をつなぎ、その SHA-256 を取る。部品の一覧はどこにも手で書かず、`_part` の読み込みから導く。窓口の SHA-256 は、窓口を読み込んだ時点の値に固定する。
- 同じフォルダに、まだ読み込んでいない `readiness_scan_*.py` があれば、照合値を作る時点で止める。照合値に入らない部品が動く状態を作らないためである。
- `_scanner_source_sha256()` はこの値を返す。部品が 0 個でも値の書き方が変わるので、既存の合格控えはこの版で 1 回だけ全部外れる。判定は変わらず、最初の検査に時間がかかるだけである。
- 部品は `__file__`・`importlib`・`runpy`・`subprocess`・`os.environ`・`os.getenv`・`sys.modules` を使わない。試験が差し替える名前と外部とのやり取りは窓口に残し、部品には引数で渡す。`tests/test_readiness_scan_bundle.py` が構文から検査する。
- 部品のファイル名を書いてよいのは窓口だけとする。ほかのスクリプトと試験は窓口を通る。
- `scripts/git_data_privacy_gate.py` が報告する照合値の項目は、この ADR では増やさない。報告は窓口のファイルだけを覆う、という範囲は変わらない。

## 退けた案

- 部品を編集し、配るのは合成した 1 ファイルにする案。外への影響は無いが、合成すると名前の空間が 1 つに戻るので、部品の境界を構造で守れない。合成器と、合成物との一致検査という保守対象も増える。
- 普通の import でパッケージにする案。Projects の送信入口はバイト列を実行するので、パッケージの相対 import が解けない。試験選択の前提も変わる。
- 1 ファイルのまま中を整える案。照合値の問題は起きないが、4,888 行の中の境界を構造で守れない。

## 移行

1. この ADR と、照合値の仕組みと契約試験を入れる。部品はまだ 0 個。
2. Projects の送信入口で、`readiness_scan*.py` 全体を実行の前後で比べ、部品全体の照合値を受領記録に足す。部品が 0 個の版では、結果は今と同じである。
3. 1 と 2 が両方入ってから、部品を 1 つずつ移す。1 つにつき 1 つの変更依頼にする。
4. 最初の部品として `scripts/readiness_scan_report.py` に、出力の約束の定数（`SCHEMA`・`GUARANTEES`・`NON_GUARANTEES`）、報告の整形（`enrich_report`・`format_human_report`・`sanitized_evidence_label`・`sanitized_ref_report` の本体など）、終了コード（`report_exit_code`・`dialogue_exit_code`）、`unexpected_exception_issue`、`scan()` の集計を純粋関数にした `gating`・`aggregate_status` を移した。差し替えられる名前と秘密情報の規則は窓口に残し、引数で渡す。
5. 次の部品では、秘密情報の規則と検出件数の集計を `readiness_scan_leak_rules.py` に移す。窓口から既存名を引き続き使え、部品は全体の照合値に含まれる。個人パスとの組み合わせやレビュー済み例外の判定は窓口が担当する。
6. 次の部品 `scripts/readiness_scan_cli.py` に、引数の定義（`build_parser`）・検査オプションの解決（`resolve_options`）・対話入力（`prompt_*`・`collect_interactive_options`）・`ScanOptions`・`AUDIENCE_CHOICES`・`MODE_CHOICES`・`valid_initial_private_scope`・`DEFAULT_CONSISTENCY_BASE_REF` を移した。部品は兄弟ファイルを読まないので、`dialogue_gate` の読み込みは窓口に残し、窓口の `build_parser`・`resolve_options`・`collect_interactive_options` が、`dialogue_gate` を読み込む関数と境界文を作る関数を部品の関数へ渡す。部品は、変更前と同じ枝でだけそれを呼ぶ。この 3 つの名前は試験が直接呼ぶので、同じ引数のまま窓口に置く。
7. 次の部品 `scripts/readiness_scan_media.py` に、媒体の種類の判定（`media_kind`・`media_candidate`）・`.cmo3` の展開（`cmo3_contents_xml`）・履歴の媒体メタデータ検査（`_inspect_history_media`・`_metadata_media_names`）・コミット済み媒体の一覧の絞り込み（`changed_committed_media_entries`）を移した。部品は git を直接起動しないので、窓口に小さな型 `GitPort` を置く。窓口が呼ぶたびに作り、`run_subprocess` が呼ばれた時点の窓口の `run_subprocess` へ転送する。部品は `GitPort` を import せず、受け取ったものを呼ぶだけにする。兄弟ファイルの読み込み（`media_metadata.py`・`media_identity.py`・`caff_archive.py`）は窓口の関数が担い、部品へは読み込む関数として渡す。`CMO3_MAX_XML_BYTES`・`_MEDIA_METADATA_DECODED_BUDGET_BYTES`・`secret_matches`・`personal_path_matches` は窓口に残し、呼ぶたびに引数で渡す。
8. 次の部品 `scripts/readiness_scan_identity.py` に、登録済み個人情報の本文検査（`PrivateIdentityScan` の判定のメソッド）・合格の控え（`IdentityScanCache` と定数）・コミットの名義の行の判定（`_is_coauthor_line`・`_coauthor_line_blocked`・`_identity_line_mismatch`・`_identity_line_allowed`・`effective_identity`）・`normalized_personal_path_matches`・`_guard_worktree_parent_components` を移した。`PrivateIdentityScan` の `__init__` は環境変数・`__file__`・provider の実行を使うので、窓口の派生クラスに残す。試験は `(repo, required)` で作り、`check` を差し替え、`__init__` を通さずにも作るので、派生クラスは窓口の判定への口 `IdentityPort` を class 属性 `port` に置く。`IdentityPort` は `GitPort` と同じく、呼ばれた時点の窓口の名前へ転送する。`IdentityScanCache` は同じ物を再公開し、`open` は環境変数を読む関数・`_scanner_source_sha256`・版の照会を引数で受け取って、変更前と同じ時点で呼ぶ。`identity_gate.py` と `identity_normalization.py` の読み込みは窓口の関数が担い、部品へは読み込む関数として渡す。`private_commit_identity_view` は呼び出し元が `history_hits` だけなので、履歴の部品と一緒に移す。
9. 次の部品 `scripts/readiness_scan_history.py` に、履歴の検査（`history_hits`・`fixed_history_revisions`・`changed_committed_entries`・`verified_parent_blob`・`submission_text`・`private_commit_identity_view`）と差分の検査（`diff_added_lines_have`・`comparison_parent`・`target_diff_personal_path_hits`・`_decode_scannable_text`）を移した。部品の関数は keyword 専用の引数 `port` を受け取り、窓口の名前を `port` の同じ名前の属性から読む。窓口の `HistoryPort` は `GitPort` を継ぎ、`_NAMES` に挙げた名前を、属性を読んだ時点の窓口の値として返す。試験が差し替える `run`・`run_subprocess`・`fixed_history_revisions`・`_HISTORY_BATCH_BUDGET_BYTES`・`text_has`・`load_identity_gate` も、ほかの部品の名前（`_zip_exact`・`_metadata_media_names`・`_guard_worktree_parent_components`）も、ここを通る。`subprocess.SubprocessError` は `HistoryPort.SubprocessError` として渡す。窓口には同じ引数の薄い関数を残し、呼ぶたびに `HistoryPort()` を作って渡す。部品の中の関数どうしの呼び出しは部品の中で閉じるので、窓口の `comparison_parent` や `diff_added_lines_have` を差し替えても、部品の `history_hits` などには届かない。作業ツリーのファイル一覧は、`scan()` と一緒に次の段で扱う。

## 残す課題

- 部品のファイル名を窓口以外が書いていないかの検査は、`scripts/`・`tests/`・`templates/`・`.github/`・`runtime/` の下の全部のフォルダの Python・シェル・YAML・JSON・TOML を見て、名前の文字列が含まれるかで判定する。文字列をつないで組み立てた参照は見つけられない。
- 照合値の書式（名前順に `ファイル名 + NUL + SHA-256 + 改行`）は、`Projects` の送信入口も同じ書式で計算して突き合わせる。書式を変える時は、送信入口の変更と対にする。食い違うと、送信入口は全部の公開 push を止める側に倒れる。
- 照合値が覆うのは、窓口と `readiness_scan_*.py` の部品だけである。窓口が別に読み込む兄弟ファイル（`identity_normalization.py`・`media_metadata.py`・`caff_archive.py`・`readme_release_gate.py`・`dialogue_gate.py` など）は覆わない。`scanner_bundle_sha256` を検査器全体の照合値と読まない。対象を広げるかは、後の段で決める。
- `git_data_privacy_gate.py` が報告へ記録する照合値は、窓口 `readiness_scan.py` 単体の SHA-256 までで、`readiness_scan_*.py` の部品を含まない。部品が変わっても記録は変わらない。`scanner_bundle_sha256` を `readiness_scan_bundle_sha256` として足すと、`.repo-preflight/identity-fixture-exceptions.txt` が固定する `tests/test_git_data_privacy_gate.py` の SHA-256 の更新が要るため、本人承認のある後の段で行う。

## 検証

- 部品が 0 個の版で、全部の試験が変更前と同じ結果になること。
- 一時フォルダに部品を置いて読み込むと、名前の一覧と照合値が増え、同じ中身なら使い回し、中身が変われば読み直し、失敗すれば登録されないこと。
- 部品を変えた時に、`local_check.py --plan-tests` が窓口を読む試験を選ぶこと。
