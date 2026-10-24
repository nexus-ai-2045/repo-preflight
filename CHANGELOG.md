# Changelog

## Unreleased

## 0.10.0 - 2026-10-24

- 新しいリポとして作り直した初版（履歴は1コミット）。
- 名義の検査（足した行の形と読めないファイル）から、人が中身を確かめた試験データだけをファイル単位で外せるようにした。一覧は `.repo-preflight/identity-fixture-exceptions.txt`（1行1ファイル、SHA256 と理由つき）。検査は固定した repo-preflight 側の一覧だけを読み、そのコミット（またはステージ）での中身の SHA256 が一致するときだけ外す。中身が変われば、また止まる。フォルダ・ワイルドカードは書けず、tests/ の新しいファイルはこれまでどおり検査する。今回は試験データ15件・カナリア脚本1件・保全した wheel 1件の計17件（2026-10-24 に人が確認）。
- 対応する Python を 3.9 以上に下げ、共通フック（M2）とそろえた（`requires-python = ">=3.9"`、black の対象も py39）。3.9 で動かない書き方を洗い出し、型注釈の `X | Y` を評価しないよう `from __future__ import annotations` を足し、試験の `tomllib` は 3.10 以下で `tomli` を使う。試験の依存は版ごとに分けた（3.9 では pytest 8 と Pillow 11）。組み立て用の setuptools も版ごとに分けた（3.10 以上は 84 以上、3.9 は 77.0.3 以上 81 未満。84 は 3.10 以上が要るため）。CI に `test-py39`（ubuntu と macOS、pytest と runtime_smoke だけ）を足した。保全した ai-ratchet-gate の wheel と black は 3.11 以上が要るので、その試験は 3.9 では飛ばし、3.11 以上のジョブで回す。

- 敵対的レビュー（残り4件）を直した。M2: Mac 標準の Python 3.9 でも検査が `tool_error` で止まらないよう、`readiness_scan.py` の `zip(strict=True)` を同じ働きの関数に置き換えた。共通フックは 3.9 以上の Python を新しい順に探し、無ければ版を確かめるよう理由を出して止める。M3: `.gitattributes` でフック・スクリプト・ワークフロー・ひな形の改行を LF に固定し、Windows の `core.autocrlf=true` でも CRLF にならないようにした。M7: pre-push は、送る commit の基準値ファイルを送り先の基準値（送り先の commit、無ければ送り先を追う手元の ref、どれも無ければ全件 0）と比べ、手で件数を上げた基準値や commit に入っていない基準値では止める（`scripts/push_baseline_guard.py`、検査は回さない）。M8: AI のシェル実行の前に呼ぶ `scripts/ai_command_guard.py` を追加し、フックを飛ばす指定（no-verify と `git commit` の短い n）・`core.hooksPath`・`git commit-tree`・検査が頼る環境変数（`REPO_PREFLIGHT_*`・`PATH`・`PYTHON*`・`GIT_*`、表示まわりは除く）の上書きを拒む。Claude Code と Codex の PreToolUse のひな形（`templates/ai/`）を置いた（各機械の実際の AI 設定は変えない）。共通フックは Python を `-E -s` で呼び、`PYTHONPATH` などの差し替えを読まない。

- 共通 pre-push は全履歴ラチェットを回さない（H5）。同じ判定は必須チェック preflight（CI）が PR・merge_group・push のすべてで回す。手元では基準値ファイルの有無だけ見て、無ければ今まで通り止める。足した行の写し（符号化を戻す・NFKC など）は変わり得るときだけ作り、1件で足した行が400万字を超える commit は読まずに人の承認待ちで止める（push の時間の上限を守る）。
- 品質判定の「公開前に必ず直す」を直した。H1: セッション行は行頭に限らず、箇条書き・強調・空白区切り・全角コロンでも止め、Codex・ChatGPT・Cursor の作業 URL と Claude Code のプロジェクトの URL も止める（`.gitleaks.toml` の規則も同じ形に広げた。全履歴のラチェットの型は数を比べるので広げない）。H2: 足した行にも名簿にないメールの判定を掛け、全角の `＠`・伏せ字（`[at]`・`(dot)`）・国や地域で末尾が変わる個人向けの大手も見る。H3: URL 符号化を何重にでも戻し、JSON の `\u`・`\/`、HTML 実体、文字列の連結、大文字小文字違い、WSL の `/mnt/<drive>/` を見る。画像・圧縮・PDF・ノートなど中身を形で読めないファイルを足すと人の承認待ちで止める。H4: pre-push と pre-commit の gitleaks は、`references/gitleaks-pins.json` のハッシュと一致する版だけを使い、送る範囲の gitleaks は入口の中で見る。H6: `[skip ci]` 系と `skip-checks: true` を止める。H7: pre-push で注釈タグの作者と本文を見る。M1: `GitHub <noreply@github.com>` は記録者（committer）だけに許す（`committer_only`）。M4: 呼び出し側のひな形 `templates/github-actions/public-privacy-preflight.yml` を、再利用ワークフロー `preflight-gate.yml@SHA`・`preflight-ref`・`all-green`・`edited` の形に差し替えた。M5: base-guard の別ワークフローで、PR 側の呼び出しの SHA と自分の固定 SHA が repo-preflight の版のタグ（`v*`）から辿れるかを見る（フォーク網の SHA を弾く）。M6: 署名行は `〜-by:` をすべて名簿と照合する。あわせて pre-push で、相手に無い根の commit（別の履歴）を送ろうとしたら止める（H8）。
- 敵対的レビュー（重大）を直した。C1: 必須チェック `base-guard` を `pull_request_target` の別ワークフロー（`templates/github-actions/preflight-guard.yml`）にした。定義は比較元の版が動き、PR のコードは checkout も実行もせず、PR の commit を git の中身として読むだけ。job 名と必須チェック名は同じ `base-guard`。C2: 名簿（許可名義の一覧）はリポに置かず、固定した repo-preflight に同梱の一覧だけを使う。`.repo-preflight/identity-policy.json` は手元でも CI でも読まない。検査を変えるパス（`references/protected-paths.json`: `.github/`・`.githooks/`・`.repo-preflight/`・`.gitleaks.toml` など）を変える PR は base-guard が赤にする。C3: `scripts/base_guard.py` は PyYAML の安全ローダーで読み、重複キー・アンカー・別名・明示タグ・壊れた YAML は「導入前」ではなく赤。呼び出し先は `nexus-ai-2045/repo-preflight` の preflight-gate と大文字小文字を区別せず完全一致だけ通す（別 owner・似た名前・手元の写しは赤）。PR 側の ref を渡すと、PR 側の定義と変わったパスも見る。PyYAML はハッシュ固定（`requirements-guard.txt`）で入れる。
- 再利用ワークフローで、PR だけでなく push・merge_group でも足した範囲を 公開モードの `identity_gate.py` で見る（H6）。gitleaks を版とハッシュで固定して入れ、足した範囲（初回は全履歴）を repo-preflight 側の設定で見る（H4）。
- 設計抜け点検 12-3 の穴5件を直した。(1) コミット文・PR本文・範囲の本文で、名簿にないメール（会社・組織のドメインを含む）を止める。予約ドメイン（example.*・.invalid・.test・localhost）は通す。(2) pre-push は基準値ファイルが無ければ飛ばさず止める。(3) 比較元に基準値が無い PR も、全件 0 と比べて緩めを止める。(4) 再利用ワークフローの `preflight-ref` を必須の 40桁 SHA にし、既定 `main` を外した。取り出した版が SHA と一致するかも見る。(5) 公開名簿から `github-actions[bot]` を外した（ワークフローは push しない方針）。
- base-guard（`scripts/base_guard.py`）を追加。比較元（保護された側）の呼び出し定義を `git show` で読み、`uses` の `@` と `preflight-ref` が同じ 40桁 commit SHA かを job ごとに見る。ずれ・枝名やタグ・`preflight-ref` の欠落は止める。PR 側の定義は見ないので、PR が自分の定義を書き換えても判定は変わらない。再利用ワークフローの最後の step で、初回 push 以外は毎回走る（比較元に呼び出しが無い導入前は見送る）。値は出さず種類だけ出す。
- `templates/hooks/commit-msg`（上限1秒）と `templates/hooks/pre-commit`（上限2秒）を追加。どちらも共通の入口 `identity_gate.py`（公開モード）を呼ぶだけで、入口が見つからなければ止める。pre-commit 用に `identity_gate.py` へステージ検査（これから作る commit の名義・ステージした差分の形・gitleaks）を追加。gitleaks が無ければ止める。

- 開発道具が本文へ付ける `Claude-Session` 行（行頭の見出しとコロン）と、Claude Code のセッションURLを止める対象に追加。コミット本文（commit-msg フック用の本文検査、範囲検査、公開名義検査 `public_attribution`）、PR題名・本文・コメント（`identity_gate.py` のPRイベント検査）、差分と履歴の秘密検査（規則 `claude_session`）のどれでも、値を出さず件数だけで止める。`public_attribution` の失敗は基準値で免除しない。既存の履歴に残る行は消さない（書き換えは別の手順）。
- 閉じない引用の終端を各位置から探し直して本文検査が長時間止まる問題を修正。同じliteral文法の終端位置を一度計算し、回復解析の入口・本体・一文字区切り候補の探索で共有する。非公開方針、検出対象、原資料やbaselineの例外は変更しない。

- 共通CIの通常pushで比較元が未指定となり、`master`の利用側で整合性検査が停止する問題を修正。PR・merge queue・pushのイベントSHAを取得した固定remote refと、基準値比較を同じcommitへ接続する。欠落・不正・force・削除・無関係履歴を拒否し、初回の単一rootだけは既存の初期ツリー契約を再利用する。
- 非公開の利用側で比較元fetchが無認証にならないよう、read tokenを一命令の環境だけへ供給。checkoutの資格情報非永続化、検査processへのtoken非継承、権限の最小化を維持する。
- 個人識別子の本文検査で、合格した内容のSHA-256を利用者だけが書ける控え（`${XDG_CACHE_HOME:-~/.cache}/repo-preflight/identity-scan/`）に残し、同じ内容の再検査を省く。控えるのは同梱検出器で例外の助けなしに合格した本文だけで、不合格・検査不能・reviewed例外で通った本文、本文やpathそのものは控えない。検査器・方針・正規化器・reviewed例外・remote・Pythonの版のどれかが変わる、控えが壊れている・所有者や権限が違う・repo内にあるときは全体を読み直す。`REPO_PREFLIGHT_IDENTITY_SCAN_CACHE=off` で無効化できる。

## 0.9.0 - 2026-10-23

- 公開ガードのフックをユーザー設定へ登録するとき、リンク経由の実行でもリンクのパスで登録し、POSIXではホーム配下を `${HOME}` で書く。登録が古い場所を指していれば書き直し、重複した古い登録は消す。
- CIのテストを `pytest-xdist` で並行に走らせる。
- Pythonとして読めない文章は、引用の直後に `+` が来る連結があるときだけ文字単位の回復解析を行う（注釈は入力の長さに比例する1回の走査で読み飛ばす）。全引用の走査は、文字列配列か、区切り（空白かエスケープ）が3つ以上ある引用があるときだけ行う。本文のliteralと絶対pathの照合は残します。
- 個人識別子は作業treeの在庫全体を検査したまま、送信に含まれない既存JPEGの構造不正だけは、そのfileを未展開として送信全体を止めない。送信に含まれるJPEG、容量上限、依存不足、PNGの検査不能は止めます。
- 公開名義検査が `import identity_gate` で sys.path に依存し、配布済みランチャーが古いと `unexpected_exception:ModuleNotFoundError` になる問題を、同梱 file からの読込みへ変更して修正。読込み失敗時は中途半端な module を残さず、次の検査も構造化された `tool_error` にする。
- 作者欄の区切り文字で名義検査をすり抜ける問題を、共通の原本commit解析へ変更して修正。制御文字・不正な名義ヘッダーは値を表示せず拒否し、本文全体と名義subsetの検査範囲は維持します。
- 検査器とCI接続を二段階に分離。配布workflowは未稼働のtemplateとして保存し、正本がmainに残った後、その確定SHAへ置換して導入します。
- PR内の公開検査が変更されても、信頼済みのGitデータ検査で秘密情報・個人パスを独立に判定します。forkのPR一覧照合とmerge queueの合成SHAに対応します。
- 同名の通常Actionsジョブによる代用を防ぐため、専用GitHub Appの発行元を照合する導入候補に変更。鍵・照合設定はmain限定の保護環境で管理し、未設定や異なる発行元では成功になりません。実App・環境・必須チェックの適用は人間レビュー後です。
- 信頼済み検査器が bare Git のデータだけを読み、登録済みの個人情報と名義を検査する入口を追加。設定欠落・不完全履歴・上限超過は失敗にします。
- 公開プリフライトと非公開照合の composite action を追加。SHA 一か所で検査器を選び、非公開照合結果を対象コミットの check に記録する導入候補です。運用有効化・必須チェック化は人間レビュー後に別途検証します。
- インストール済みランチャーを別ディレクトリから実行した場合も、同梱の名義検査 module を解決するように修正。README の詳細は専用ガイドへ移動しました。
- JPEGを実signatureで認識し、strict復号と文字metadata検査で、圧縮画素のコード誤解析を修正。JPEG用の任意追加依存`images`がない経路は検査不能で停止する。
- 未解析のJPEG APP payloadはsource連結の対象から外し、バイナリが不完全なソースに似ていても検査全体を止めない。rawのliteralと絶対path照合は維持する。APP1のXMP本文だけは文字として連結検査へ戻す。

- PNGの圧縮画素を静的コードとして解析して停止する不具合を修正。raw照合を維持し、文字metadataと圧縮文は既存のprivate identity検査へ接続する。
- PNG構造・CRC不正、metadataの解釈失敗・展開上限超過は検査不能で停止し、安全な診断コードを返す。
- PNG変更の個別検証はPNG回帰と直接の既存拒否ケースを対象とする。全体の検証結果は各CIを参照する。

## 0.8.0 - 2026-10-22

### 公開版の再構成と検査の接続

- 公開履歴を再構成した版です。以前の公開版のコミットSHA・タグへの互換参照は保証しません。導入先は公開後の `v0.8.0` または公開main上の確定SHAへ固定し直してください。
- このrepository自身にも共通CIを接続し、9項目すべて0件の基準値から指摘の増加を判定します。GitHubの必須チェック・main保護の適用は、workflowの配置とは別に確認します。
- `PUBLIC_READY.md` に公開対象と確認の入口を追加しました。
- 初回push通知のゼロ比較元は、非shallowで全参照が単一root commitの場合だけ既存の初期ツリー検査へ接続します。複数commit・shallowの履歴は拒否します。
- CIの起動確認用サンプルにだけ合成policyを渡し、利用者の非公開設定がない環境でもCLI契約を検証します。実運用の照合設定や配布済みskillのずれの検査は維持します。
- 依存検査ツールの原本wheelを `vendor/` に同梱し、既存SHA-256・LICENSEを保持しました。hash必須の通信なしインストール、改変拒否、Gitの改行変換を通したバイト保持を検証します。
- 伏せ字 `REDACTED` は登録名義として拒否し、禁止メールの正規化SHA-256照合を非公開設定から行います。公開名義の全履歴検査はラチェット件数で免除しません。

### その他の変更

- `docs/publish-and-ci-gate.md` に、共通 CI ゲートの `@` は repo-preflight の `main` 上の commit かタグにする決まりを足す。PR ブランチの SHA に pin した repo で、squash merge と枝削除のあと CI が検査スクリプトを取れずに止まったため。
- 公開コマンド `publish_gate.py` は、HEAD に共通 CI ゲート（`preflight-gate.yml` を呼ぶ workflow）とラチェットの基準値ファイルが無ければ公開しない（`ci_gate_missing` / `ratchet_baseline_missing`）。全項目 pass で公開しても、ゲートが無いと公開後の push と PR で検査が一度も走らなかったため。
- `.github/workflows/preflight-gate.yml` が PR と merge queue で必ず `tool_error` になっていたのを直す。比較元を生の SHA で渡していたが、`readiness_scan.py` は `refs/remotes/origin/` 配下の名前しか受け付けないため。比較元は `origin/<比較先ブランチ>` で渡し、基準値の比較は従来どおり SHA で行う。
- AI エージェントが repository を直接公開するのを止める Claude Code の PreToolUse フック `scripts/publish_guard_hook.py` を追加。公開は公開コマンドへ案内する。設定は [docs/publish-and-ci-gate.md](docs/publish-and-ci-gate.md) の「4.」。`--install` でユーザー設定へ1行で入れられる（macOS・Linux・Windows 共通）。

- 全 CLI の標準出力・標準エラーを UTF-8 に固定。Windows の既定コードページ（cp1252 / cp932）でパイプへ日本語を出すと落ちていた（`local_identity_guard.py` の pre-push フックを含む）。出力の文字コードを ASCII に絞った回帰テストを追加。
- 個人パス検査が、JSON やソース内でエスケープされた Windows のパス（区切りが `\\`）を見逃していたのを修正。照合エンジン側と同じ規則にそろえた。
- レビュー済み例外の内容 digest を、作業ツリーが CRLF（Windows の autocrlf）でも LF の digest と照合するようにした。

## 0.7.0 - 2026-10-17

- `.github/workflows/release.yml` を追加。手動起動で、版の整合（pyproject・CHANGELOG・`docs/releases/vX.Y.Z.md`）を確かめてから tag と GitHub Release を作る。任意で、開いた PR の無い不要なリモートブランチを先に消す。手順は OPERATIONS.md。
- 公開・CI・ラチェットの3点セットを追加（[docs/publish-and-ci-gate.md](docs/publish-and-ci-gate.md)、ADR-0009）。`scripts/publish_gate.py` は全体検査が `pass` で、手元と origin の全 ref が一致し、人が `--apply --confirm OWNER/NAME` を打ったときだけ公開へ切り替える（既定は dry-run）。`.github/workflows/preflight-gate.yml` は各リポジトリから `uses:` 1行で呼べる reusable workflow。`scripts/preflight_ratchet.py` は検査ごとの指摘件数を基準値ファイルと比べ、増加・基準値の緩和・締め忘れを失敗にする。公開の判定は基準値を使わない。
- 名義検査（`identity_gate`）を全体の必須停止判定へ接続。設定の欠落・不正や履歴取得の失敗は値を出さずに止め、全参照の本文検査とリリース時の名義照合範囲を区別する。
- `local_identity_guard.py exec` が親から継承して子へ渡す照合値も出力の伏せ字対象に含め、子の出力への素通りを防ぐ。

- `scripts/identity_normalization.py`（汎用の個人名義照合エンジン）を `codex/privacy-preflight-final-20261016` から単独で取り込む。private側の identity guard がこのmoduleを読むため、無いと照合リストを読めずに止まっていた。readiness_scan への組み込みは含めない。

- `scripts/local_identity_guard.py` を追加。持ち主の個人名義（OSのアカウント名、ホーム名、氏名、gitのglobal名義、端末名、そのrepoの履歴で私用メールを使ったコミットの名義）を実行中の環境から自動で集め、コミット名義・メッセージ・追加行・ファイル名を照合する。人が名前を書く照合リストを前提にせず、値は表示も保存もしない（報告はhashの先頭だけ）。コミット名義はGitHubのnoreplyとbotだけを許す。`exec` は既存のidentity guardへ値を環境変数で渡し、出力に値が出たら伏せ字にする。
- `AGENTS.md` に、個人名義をチャット・PR・コミット・ファイルに書かない、ユーザーに書かせる案を出さないルールを追加。
- `scripts/identity_gate.py` を追加。作者・コミッター名義の許可リスト、Co-authored-by / Signed-off-by 行、コミット本文の個人の型を、基点から HEAD までの全コミットで検査する。個人の型はリポに書かず、非公開設定（`PREFLIGHT_PRIVATE_PATTERNS_FILE` または `PREFLIGHT_PRIVATE_PATTERNS`）から読む。設定が無ければ fail。見つけた値は出力せず件数だけ返す。
- `readiness_scan.py` の結果に `identity_gate` 欄を追加。今は報告のみで、全体の判定（status）には入れない。必須化は別途決める。

## 0.6.0 - 2026-10-16

- 公開の合成fixtureをversion 3の明示policyで扱う。予約済み合成値だけに限定し、repo・path・blob・検出hash・件数・UTC期限・レビュー参照を固定する。任意の個人pathとsecretは登録できず、v2のprivate限定条件と全履歴検査を維持する。

- 公開説明のパス例を仮のホーム表記へ変更し、合成エラー値とテストのコメントから実ユーザーホーム形式を除いた。空白入りパスの処理と例外型による秘匿の検証は維持する。過去のコミットは変更しない。

### 修正

- 検査対象の `.git/config` に `core.fsmonitor` が設定されていると、`readiness_scan.py` と `consistency_gate.py` の
  `git status` などがそのコマンドを実行していた。git 呼び出しへ `-c core.fsmonitor=`（空の値。Git 2.36 以降は無効、
  それより前は hook なしとして扱われる）を渡す。あわせて `GIT_OPTIONAL_LOCKS=0` にし、`git status` が検査対象の
  `.git/index` を書き戻さないようにした。filter driver など
  名前が任意の設定は一括で無効化できないため、受け取った repository は新しく clone した複製を検査する旨を
  `docs/threat-model.md` に明記した。
- UTF-8 / UTF-16 として全体を復号できないファイル（Shift_JIS・Latin-1・binary）の中の secret と個人pathを
  検出していなかった。復号できない場合も生byteで照合する。作業tree・削除済み履歴・`--base-ref` の差分の
  全経路が対象。検出は増える方向だけで、正規表現の処理時間は増える。
- `configure_settings` が default branch の保護を ruleset からしか読まず、classic branch
  protection だけで保護された repository を「保護なし」の必須変更と誤判定していた。
  `branches/{default}/protection` も読み、ruleset と累積して評価する（GitHub は両方が掛かると全規則を
  強制し、同じ規則は厳しい方が効く）。保護の無い branch は本文 `Branch not protected` の 404、権限不足は
  `Not Found` の 404 で区別し、権限不足は `false` と推測せず `unavailable` にする。classic 側の
  `enforce_admins` 無効と PR 要件の bypass 許可は bypass actor として数える。設定名
  （`default_branch_ruleset`、`ruleset_*`）は packet schema v1 の互換のため変えず、
  `enforced_by` に由来、`sources_unavailable` に確認できなかった情報源を出す。
- 必須 status check の照合先を、default branch の HEAD から、直近に merge された PR（新しい順に最大 5 件）の
  head commit の check-runs と commit status へ変えた。PR でしか走らない check（`pr-body-hygiene` など）を
  HEAD では観測できないため、必須 check が空洞化している誤判定になっていた。merge 済み PR が無い、または
  取得できない場合は `unavailable` にする。
- Actions 設定（`actions/permissions`、`actions/permissions/workflow`、`selected-actions`）の変更案が、観測時の
  値を固定 body に埋めていたため、順に実行すると先の変更を戻していた。実行直前に取り直した現在値へ、
  承認された項目の変更だけを重ねて 1 回 PUT する手順（`fresh_read` / `copy_from_fresh_read` / `overlay`）で
  示す形に変えた。`allowed_actions` を `selected` へ変える案は、`all` の間 `selected-actions` が 409 を返すため
  「切り替え → 許可 list 設定」の 2 段にし、許可 list は default branch の `.github/workflows` の `uses:` から導く
  （GitHub 製は `github_owned_allowed`、それ以外は `OWNER/REPO@*`）。`selected_actions_patterns` も、導出した
  list と照合し、全消去の案を出さない。導出できない構文の workflow は推測せず `unavailable` にする。
- 設定ガイドと packet の推奨度の食い違いを揃えた。PR 経由・branch を最新にする・review thread 解決・承認数・
  `can_approve_pull_request_reviews` は推奨（必須ではない）。`can_approve_pull_request_reviews` が ON のとき、
  `GITHUB_TOKEN` による PR の作成も止まる旨と、「ON を維持して理由を記録する、または GitHub App token へ移して
  から OFF にする」例外を packet に出す（release-please などは workflow から検出する）。ガイドと packet の推奨度の
  対応は `tests/test_github_settings_guide.py` が機械で照合する。
- `references/github-settings.md` を 2026-10-09 の確認内容で更新した（`sha_pinning_required` を有効にする前の
  順序、CodeQL の必須化、immutable releases、workflow execution protections と `pull_request_target` の既定
  rule、private vulnerability reporting の件数制限と構造化 form、classic protection の ruleset 変換、Copilot code
  review の承認）。
- 「取れない・読めない時に、満たしている側へ倒れる」経路を塞いだ。
  - workflow の `uses:` を読む走査器を、job id・job の key・step の key の 3 段を追う形に
    書き直した。job 名が `container` や `env` でも中身を読み飛ばさない。`uses:` の値が block
    scalar、最上位が flow 記法や引用符付きの key、閉じない引用符、anchor・alias・tab などは、
    `[]` を返さず読み取り不能（`workflow_scan_refused`）にする。読み取り不能の間は、selected へ
    切り替える案を `ready: false` にし、既存の許可 list を消す案を出さない。最上位の `jobs` を
    見なかった文書（文書全体の字下げなど）と、LF 以外で行を切る文字（CR 単独、NEL、U+2028、
    U+2029、`\x0b`、`\x0c`、`\x1c` から `\x1e`）を含む文書も、読み取り不能にする。
  - 許可 list の判定を、集合の等値から「使用中の参照を既存の pattern が覆うか」に変えた。許可側の
    pattern は、`*` が `/` を越えず、大文字と小文字を区別する保守的な照合にした。`!` の拒否 pattern は
    逆に広く読む（`*` が `/` も越え、大文字と小文字を区別せず、`?` `+` `[` を含めば一致とみなす）。
    拒否は順序に関係なく優先する。どの使用中の参照にも一致しない pattern（`unused_patterns`）と、
    owner に `*` を含む、または `**` を含む pattern（`overly_broad_patterns`）は、覆っていても満たして
    いない扱いにする（solo では推奨、high_risk_public では要判断）。足りない参照があるときは、その分だけを
    追加する案にし、既存の pattern は消さず、SHA 固定の pattern を `@*` へ緩めない。使われていない・
    広すぎる pattern を消す案は出さない。照合は戻らない方式で、`*` の多い pattern でも固まらない。
    `allowed_actions` が `local_only`（selected より厳しい）のときは緩める案を出さない。workflow が
    読めないときは `unavailable` にする。
  - selected への切り替え案は、導出できない参照（`docker://`、式）や、走査していない local action
    （`./`、`$/`）があるとき `ready: false` と理由（`allow_list_needs_review:...`）を出す。
  - ruleset の詳細に `bypass_actors` が無いとき（書き込み権限が無いと返らない）は、bypass が無い
    とは読まず `unavailable` にする。
  - 必須 check の照合は、情報源のどれかが確認不能の間は、照合できても満たしたと言わず
    `unavailable` にする。
  - ruleset の一覧は `per_page=100` で読み、上限まで埋まっていたら `unavailable` にする。
    `~DEFAULT_BRANCH` だけを include し exclude が空の active な ruleset だけを default branch の
    保護として数え、それ以外の対象指定（`~ALL`、`refs/heads/<既定>`、glob、exclude 付き）は数えず
    `unavailable` にする（正しい解釈は別の作業）。
  - `unavailable` なのに理由が空になる箇所（classic の項目欠落、許可 list の導出失敗など）に理由を
    付けた。

### packet の形の変化

- Actions の変更案（`actions/permissions`、`actions/permissions/workflow`、`selected-actions`）は、
  観測値を埋めた固定の `body` をやめ、`fresh_read`・`copy_from_fresh_read`・`overlay`・
  `put_once` を持つ形になった（`body` は持たない）。`allowed_actions` を `selected` へ変える案は
  `SEQUENCE`（`steps`、`ready`、`blocked_reason`）になり、その 2 段目（許可 list の設定）だけは、
  workflow から導いた許可 list の `body` を持つ（導出できないときは `body: null`）。
- `body_basis` の値の名前が変わった。ruleset 系は `fresh_all_effective_ruleset_bodies_required` から
  `fresh_all_effective_protection_bodies_required` へ、rollback の `requirement` は
  `capture_each_fresh_ruleset_body_before_change` から
  `capture_each_fresh_protection_body_before_change` へ。Actions 系は
  `fresh_get_then_overlay_approved_changes_only`、許可 list の 2 段目は
  `derived_from_default_branch_workflows` または `derivation_unavailable` になった。
- `recommended_value` の文字列が変わった。`default_branch_ruleset` は
  `active_ruleset_for_default_branch` から `ruleset_or_classic_branch_protection_on_default_branch`
  へ、`required_status_checks` は `all_required_check_contexts_currently_emitted` から
  `all_required_check_contexts_emitted_on_recent_merged_pr_heads` へ。
- `required_status_checks` の `observed_value` は、`observed_on_default_branch` が
  `observed_on_recent_merged_pr_heads` と `evidence_pull_requests` になった。
  `strict_required_status_checks_policy` の `observed_value` は一覧から真偽値になった。
  `default_branch_ruleset` は、classic だけで保護されているとき `classic_branch_protection` を返す。
- `ruleset_bypass_actors` の `observed_value` の要素に、classic 由来の `{source, actor}` の形が
  混ざる。ruleset 由来は従来どおり `{ruleset_id, actor_type, actor_id, bypass_mode}`。
- 追加された項目: 設定ごとの `enforced_by` と `sources_unavailable`（対象は
  `default_branch_ruleset`、`ruleset_deletion_protection`、`ruleset_non_fast_forward_protection`、
  `ruleset_pull_request`、`required_review_thread_resolution`、
  `strict_required_status_checks_policy`）、`selected_actions_patterns` の `derived_patterns`・
  `missing_references`・`unused_patterns`・`overly_broad_patterns`・`derivation`（拒否 pattern が
  使用中の参照に一致するときは `conflicting_negated_patterns`、workflow が読めないときは
  `derivation_unavailable_reason`）、`required_status_checks` の
  `observed_value.sources_unavailable`（情報源の一部が確認不能のとき）、
  `can_approve_pull_request_reviews` の `exception`。
- 推奨度が required から recommended になった項目: `ruleset_pull_request`、
  `required_review_thread_resolution`、`strict_required_status_checks_policy`、
  `required_approving_review_count`、`can_approve_pull_request_reviews`。足りなくても
  `needs_human_input` で止まらなくなる。

### 先行して準備したv0.6.0の変更

### リリース前の確認と履歴検査

- release intentで、直近の到達可能なリリースtagからREADME本文が更新されているかを必須検査する。空白だけの変更は更新扱いにせず、履歴が不足して比較元を決められない場合は停止する。公開前に確認したtagを `--consistency-base-ref` へ明示し、候補tagの作成後も比較基準を保持できる。
- releaseに限り `--identity-base-ref` を追加し、今回のコミット名義を明示したremote refから照合する。既存全履歴の名義不一致は別に報告し、secretと個人パスの全履歴検査を狭めない。省略時の従来の全履歴名義照合は維持する。
- `--base-ref`を指定しない全履歴検査では、shallow cloneを検出して `shallow_history_unavailable` で停止する。取得済みの範囲だけを全履歴検査済みと扱わない。判定できない場合は `git_history_inventory_failed` にする。`actions/checkout` の既定（`fetch-depth: 1`）で全履歴検査をしているCIは `fetch-depth: 0` への変更が必要。
- 引用符で終わるルートだけの合成パス例を誤検知しないようにした。相対パスとの境界、ユーザー名を含むWindows/macOS/Linux絶対パスの検出、限定例外のhash照合は維持する。
- READMEへ現在のリリース確認と、必須レビュー記録の先頭markerの説明を追加した。GitHubへの書込み、公開、tag、releaseの操作は引き続き人間レビューと明示承認を要する。

### 追加

- レビュー済み限定例外のversion 2でprivate原資料の個人pathを扱えるようにした。originとliveのprivate可視性を照合し、全内容hash・検出hash・件数・期限・人間レビュー参照を必須にする。既存のsecret検出とversion 1互換を維持し、未承認の候補は停止する。

### 修正

- 既存の限定例外テストとADRを個人の広いsecret除外から明示的に保護し、trackedかつignoredの矛盾を増やさない。
- `secret_scan` の `openai_key` 規則に前側の区切りを入れ、`task-orchestra-…` や `elon-musk-…` のような英単語の途中の `sk-` を鍵として検出しないようにした (直前が英数字・`_`・`-` の `sk-` は意図して対象外。行頭・空白・引用符・`=`・`/`・escape された改行などの後は従来どおり検出する)。
- `personal_path_scan` が Live2D Cubism の編集ファイル `.cmo3` の中を検査するようにした。
  `.cmo3` は全byteを 0xE3 で XOR した zip entry に XML を raw deflate で入れているため、
  生byteにも UTF-8/UTF-16 の全体decodeにも個人pathが現れず、`psdFile` 要素に
  Mac の絶対pathが入ったfileを pass させていた。repo全体modeでは作業treeの `.cmo3` を
  展開し、data descriptor の CRC-32 と展開後サイズを照合したうえで既存の個人path判定を
  かける。展開・照合に失敗した・展開量が上限を超えたfileは pass にせず、
  `personal_path_scan` を `unknown` にして `unscanned_files` に出す。
  履歴上の `.cmo3` と、base を指定した差分検査での `.cmo3` は展開しない (非保証に追記)。
- `runtime_smoke.py` が install 済み runtime skill コピーの drift を検査するようにした。
  #45 の `install_runtime_skills.py --check` はどこからも呼ばれておらず、drift は
  手で実行したときしか気付けなかった。`ok` / `not_installed` は pass、`drift` は
  `runtime_skills_drift:*`、`missing_adapter`・未知の status・検査の例外は
  `runtime_skills_tool_error:*` として smoke を失敗させる。CI には配布先が無いので
  `not_installed` で pass する。tests が実ホームに依存しないよう `runtime_smoke.py` に
  `--home` を追加した。
- `install_runtime_skills.py --check` が path-file の相対 `ROOT_PATH.txt` を
  cwd 依存で ok にせず `checkout_foreign` にするようにした。Windows junction は
  先に `is_junction()` で識別し、リンク先の `ROOT_PATH.txt` を path-file と
  取り違えない。配布先が file / dangling symlink のときは `not_installed` ではなく
  `dest_unexpected` の drift とし、`--apply` はその node を除いて再配布できる。
  drift 時の `next` は呼び出した script と検査した `--repo` を保持する。
- `--repo` に repository root ではない path を渡したとき、囲っている repository へ
  暗黙に対象を広げるのをやめ、`repo_path_is_not_repository_root` で閉じるようにした。
  `git rev-parse --show-toplevel` は「その path を含む repository」を返すため、
  従来は利用者が指したのと別の repository の判定を、指した path の判定として
  返していた。大きな monorepo の一部を指した場合は全履歴走査に入り、無出力のまま
  数分待たされる形で表面化する。
- 親プロセスの `GIT_DIR` / `GIT_WORK_TREE` 等で別 repository の object DB を指して
  いても、`--repo` で指した path から見える repository 以外の判定を返さないように
  した。subdirectory の暗黙拡大と同じ取り違えの変種で、secret 判定や HEAD まで
  差し替わっていた。
- `ai_entry_contract.py` (#40) のレビュー指摘を、#41 の振る舞いを保ったまま吸収した。
  - marker を最初の出現で決め打ちせず、header / begin / end が各1個・正しい順序・
    header と begin の間が空白だけのときだけ生成ブロックとして扱う。従来は source に
    marker があると再 apply ごとに末尾へ残骸が増え、overlay が marker を引用すると偽 stale の
    うえ引用行から begin までの本文を無警告で削除し、改ざんした複製ブロックは検査されずに
    pass していた。違反は `projection_markers_ambiguous` (検査は stale、apply は書かない)、
    source 側の marker は `source_contains_projection_markers` で止める。
  - source と target が同じファイルに解決される apply を `source_target_identical` で
    拒否する。正本に marker をネストして書き込んでいた。
  - 区切りの改行を 1 個だけ剥ぐようにした。`lstrip` で source 先頭の空行まで剥いで
    恒久的に mismatch になっていた。
  - `ai_entry_contract.py` の manifest パスで `~` を展開しない。`--home` を迂回して
    実 home に解決していた。`~` は `tilde_unsupported_use_home_placeholder`、未知の
    placeholder は `template_placeholder_unresolved`、空のパスは `path_value_invalid`。
  - import pointer を部分文字列一致ではなく `@<path>` のパス解決と `samefile` で判定する。
    フェンス・inline code・HTML コメント内の例示や `@<source>.backup` による false green、
    `@~/` や相対パスでの false red、大小文字の扱いを `os.name` で代理したことによる
    macOS での false red をなくした。instruction pointer の判定 (#41) は変えていない。
  - exit code を 0=pass / 1=blocked / 2=tool_error / 3=human_review に分けた。required の
    失敗が manual の確認待ちだけなら `human_review`。apply 成功時は `applied_entry` を返し、
    manual entry の結果には `evidence` を載せる。
  - 自前定義の code 以外の例外は型名だけに丸め、username を含む絶対パスを JSON に出さない。
  - 既存 target を置き換えるときにファイルモードを引き継ぐ。`mkstemp` の 0600 が
    `os.replace` 後に残っていた (POSIX)。
  - `--apply` なしの `--entry-id` を `entry_id_requires_apply` で拒否する。何もしていなかった
    `--json` は削除した (未 release)。
  - テストが `from scripts import ...` に依存し、素の pytest で collection error になっていた。
  - 自前 code かどうかをメッセージの形ではなく専用の例外型で判定する。entry id に `/` や
    `\` を含む manifest で、#41 の `manifest_runtime_missing:<id>` などが
    `manifest_invalid:ValueError` に潰れていた。
  - import pointer のコード除外を CommonMark に合わせた。`~~~` の中の ```` ``` ````、長い
    フェンスの中の短いフェンス、2 連 backtick の inline code、インデントコードブロック、
    blockquote 内のフェンス、複数行にまたがる inline code の中の例示で pass していた。
  - NUL 文字を含む manifest パスを `path_value_invalid` で止める。検査では `entry_missing`
    と誤読され、apply では `os.replace` の `ValueError` が username 入りの絶対パスを含む
    traceback と exit 1 になっていた。書き込み時の `ValueError` は
    `target_write_failed:ValueError`、想定外の例外は `internal_error:<型名>` (exit 2) にする。
  - required entry が entry 単位で `tool_error` (`entry_unreadable` 等) のとき、全体が
    `blocked` (exit 1) になり exit code 表の 2=gate 自体の問題と矛盾していた。全体 status の
    優先順位を `tool_error` > `blocked` > `human_review` > `pass` に統一した。
  - 未クォートの空白入りパスの import (`@C:/<home with spaces>/AI-CONSTITUTION.md`) が、#40/#41 では
    pass だったのに stale になっていた。空白で切ったトークンが解決できないときは `@` から行末
    (末尾の空白・句読点を除く) も候補にする。コード・HTML コメントと `user@host` の除外は維持。
  - 深い入れ子の manifest で `json.loads` の `RecursionError` が `internal_error` に落ちていた。
    検査と apply の両方で `manifest_json_invalid` (tool_error) に丸める。
  - 読み取り専用の既存 target の置換に失敗したとき、モードを引き継いだ一時ファイルが
    Windows で unlink できず残っていた。後始末で書き込み可能に戻してから消す。
  - 引数エラー (未知の flag・manifest 指定の欠落) は argparse の usage を stderr に出して exit 2、
    JSON は出ないことを docs に明記した。

### 追加

- release intent の最終確認に「Release本文（日本語・非空）を用意したか」を追加した。
- 日本語READMEのクイックスタートを、AIへ貼る URL と危険レビュー指示にする契約を warning として検知する。
- 図が画像自身ではなく ADR / 再現テストへリンクされているかを warning として検知する。空のクイックスタート、否定形の危険レビュー、fence内の見出し/画像例、reference-style画像、pip3、HTMLコメント内URL、英語READMEの図も対象にする。CI/License などの遠隔バッジは図ではないので対象外。
- PRを出す前のセルフレビュー `docs/pr-self-review.md` を追加した。複数リポジトリの
  レビュー指摘を横断で一般化した停止条件 R1〜R14 と、差分に当てる20項目からなる。
  `CONTRIBUTING.md` の開発ルールを展開したもので、出典PR番号も非公開repository名も
  含まない。
- 上記は外部生成の配布物なので、`documentation-contract` で本文hashと `rules_version`
  の一致を検査し、手編集を止めるようにした。判断根拠はADR-0002に記録した。
- PRのCI、review、inline threadをexact HEADへ束縛してread-only判定するreview snapshotを追加した。
- 未解決thread、古いHEADのreview、CI待ちをreview完了と誤認しないfail-closed契約を追加した。
- `install_runtime_skills.py --check` を追加した。ホームへ配布した skill コピーが
  repo 正本から drift していないかを sha256 で検査する。書き込みをしない read-only
  検査で、標準ライブラリのみを使う。比較前に install と同じ射影
  (`REPO_PREFLIGHT_ROOT=` 行の除去) を通すため、install 直後に誤検知しない。
- 併せて `docs/runtime-support.md` の誤りを訂正した。`git pull` で追従するのは
  `checkout/` link だけで、`SKILL.md` / `run_preflight.py` / `README.md` は物理
  コピーのため追従しない。

### 保証境界

- 保証: `docs/pr-self-review.md` がこのrepositoryで改変されていないこと。
- 保証: `install_runtime_skills.py --check` は install 済みコピーの 4 対象
  (SKILL.md / run_preflight.py / README.md / checkout link) を repo 正本と
  sha256 で突き合わせる。`README.md` の検査は `checkout/` の状態に依存しない。
  path-file の相対 target は `checkout_foreign`。junction は fallback marker より
  先に判定する。配布先が存在するが directory でないときは `dest_unexpected`。
  drift 時の `next` は invoked script と `--repo` を含む。
- 保証: install 済み file が UTF-8 として読めない場合も traceback にせず
  `*_unreadable` を返し、残りの target の検査を続ける。
- 保証: 何も検査できなかった run (`missing_adapter`) の `status` は `pass` に
  ならない (`tool_error`・exit 2)。JSON を読む側が fail-open しないため。
- 非保証: `install_runtime_skills.py --check` は配布先が存在しないマシンの状態を
  検査しない (`not_installed` を返して pass)。repo 正本そのものの正しさ、および
  CI 上での検査も対象外 (CI に install 済みコピーは存在しない)。
- 保証: GitHub 採用（`create_repo` / `push` / `open_pr` / `merge`）の直前に `--intent` を付けると `dialogue/v3` の質問リストが機械生成されること。
- 非保証: 生成元の正本が正しいこと、配布物が最新の正本から生成されたこと。
  後者は正本側の配布台帳が担当する。
- 非保証: github.com のページが対話 UI になること。エージェントが skill を無視したときの物理停止。
- 保証: `ai_entry_contract.py` は marker が一意でない target を pass にも書き換えもしない。
  apply は source 自身と非生成ファイルへ書かない。import pointer は gate の `--home` と
  entry のディレクトリだけを基準に解決する。gate が生成する finding には source 本文と絶対パスを
  載せない。manual entry の `evidence` は manifest 作成者が書いた文をそのまま返す。
  想定外の例外でも traceback ではなく tool_error の JSON (exit 2) を返す。
- 非保証: import pointer のコード除外は CommonMark のフェンス・インデントコード・
  inline code・HTML コメント・blockquote に合わせた近似で、各 AI 製品の Markdown
  パーサーとの完全一致ではない (リスト項目の行頭に書いたフェンス等は未対応)。
- 非保証: instruction pointer の読込指示はキーワードの近傍判定で、文意の理解ではない。
  manual entry の確認そのものと、AI 製品が入口を実際に読むことは保証しない。

## 0.5.1 - 2026-08-24

### 追加

- `configure_settings` intent と `solo_public` / `team_public` / `high_risk_public` profileを追加した。
- GitHub SettingsをGETだけで実測し、現在値、推奨値、外部影響、rollback、個別API操作previewを `repo-preflight.github-settings-review/v1` で返すようにした。
- review済みの空baselineと、公開release wheelのURL・SHA-256を固定した `ai-ratchet-gate` v0.1.1を全CI jobへ追加し、trackedかつignoredなpathの新規増加を止めるようにした。

### 修正 / 改善

- Actions permissionsのPUT previewに必須`enabled`とfreshな関連fieldを含め、実行不能な一項目bodyを返さないようにした。
- 認証account、複数rulesetの累積効果、bypass actor、required check名、advanced CodeQLをfail-closedに確認するようにした。
- `high_risk_public`のselected-action patternを未確認のままpassせず、Dependabot security updatesのrequired/recommended境界をガイドと一致させた。
- Windows CIをUTF-8へ固定し、公開release wheelの日本語出力を全OSで実行できるようにした。

### 保証境界

- 保証: Settingsのread-only取得、profile比較、403/404の`unavailable`分類、設定ごとのpreview分離。
- 非保証: GitHub設定の自動変更、organization/enterprise policyの全体把握、設定変更・push・PR・merge・公開・releaseの承認。

## 0.5.0 - 2026-08-17

### 追加

- README可読性ゲートの判定を、公開を止めるエラーと人間確認へ回す警告に分離した。
- Mermaid図の日本語化候補を `Localize Diagram` として、図の追加候補とは別に提示するようにした。
- GitHub Actionsの完全SHA更新だけを限定的に免除するconsistency gateと、判断根拠を記録するADR-0001を追加した。
- DependabotのGitHub Actions向けversion updates設定を追加した。

### 修正 / 改善

- 名前付きstep、reusable workflow、版コメントを伴うAction SHA更新を構造的に判定するよう改善した。
- file mode変更、symlink化、`env`・`run`内の疑似`uses`、YAML flow collection・property・複数行scalar、workflow配下のtemplateを免除しないfail-closed判定へ強化した。
- READMEの表幅、Mermaid label抽出、fence判定、全件報告を改善し、修正と再検査の往復を減らした。
- CIで利用する`actions/checkout`を7.0.1、`actions/setup-python`を7.0.0へ更新し、参照は完全SHAで固定した。

### 保証境界

- 保証: 通常のworkflow構造にある同一Actionまたはreusable workflowの完全SHAだけが変わる場合に限り、明示opt-inされた関連文書・テスト要件を免除する。
- 非保証: 任意のYAML構文を完全解析すること、Action更新の安全性そのもの、GitHub設定・merge・releaseを自動承認すること。

## 0.4.0 - 2026-08-16

### 追加

- `--base-ref`（`push` / `open_pr` / `merge`）により、指定した差分範囲へ検査対象を限定できる preflight mode を追加 (#14)。結果 JSON の `scan_scope.mode` は内部値 `target_diff`。CLI フラグ名としての `--target-diff` はない。
- repository 全体の文書・設定・実装の食い違いを検出する consistency gate を追加 (#15)。
- README の理解順序、表の幅、Mermaid 図のラベルを含む日本語可読性ゲートを追加 (#16, #19)。

### 修正 / 改善

- README を日本語で読みやすい構成へ整理し、詳細説明を `docs/` へ分離 (#13, #17, #18)。
- 空 diff、改行を含む path、浅い・特殊な Git 履歴でも history inventory が安全に完了するよう修正 (#17, #20)。
- sparse checkout では、実際に materialize された `skip-worktree` file を検査対象に含めるよう修正 (#20)。
- consistency JSON と symlink fixture を Windows / Linux / macOS 間で再現可能にした (#14, #15)。

### 保証境界

- 保証: 対象差分と repository 全体の検査範囲を分離し、read-only の scan / consistency / README gate を CI で検証する。
- 非保証: GitHub 設定の自動変更、公開・merge・release の自動承認、依存関係の脆弱性が存在しないこと。

## 0.3.2 - 2026-08-06

### 修正 / 改善

- skill に絶対 path を焼かない portable install に変更。
  - install 先に `run_preflight.py` + `checkout/` link を置く
  - root 解決: `REPO_PREFLIGHT_ROOT` → `checkout/` → cwd 探索
- 他人の skill フォルダをコピーすると壊れる問題を設計上避ける（各自 `install_runtime_skills.py --apply`）
- Claude Code / Grok adapter と runtime-support 文書を更新

## 0.3.1 - 2026-08-06

### 追加

- Claude Code / Grok / Codex 向け runtime adapter と保証境界文書 (`docs/runtime-support.md`)。
- `scripts/runtime_smoke.py`: CLI 対話契約 + skill 入口の最小 smoke。
- `scripts/install_runtime_skills.py`: ホーム skills への pointer 配布 (dry-run 既定、`--apply` で書込)。
- CI を `ubuntu-latest` + `macos-latest` の matrix に拡大し、runtime smoke を必須化。

### 保証境界 (明示)

- 保証: 同一 CLI 契約が Linux/macOS CI で通る。Claude Code/Grok adapter が正本 SKILL を指す。
- 非保証: 各製品が skill を自動導入すること、モデルが skill を無視しないことの物理強制。

## 0.3.0 - 2026-08-06

### 追加

- **AI 実装フロー向け intent 対話ゲート** (`--intent create_repo|push|open_pr|merge|publish|release`)。
  エージェントが repo 作成 / push / PR / merge / 公開 / release に進む直前に自動発火し、
  不足文書・未設定・推奨設定を「設定しますか？」形式の `proposals` / `confirmations` として返す。
- dialogue schema `repo-preflight.dialogue/v3` と `scripts/dialogue_gate.py`。
- **次から出さない / snooze**: 推奨質問に `dismiss_30d` / `dismiss_90d` / `dismiss_forever` を付与。
  採用先 `.repo-preflight.json` に記録 (`--record-dismissal`)。secret 等 fail-closed は抑止不可。
- **GitHub 設定ガイド鮮度**: `references/github-settings.md` の `last_reviewed` 期限切れを検知し、
  更新確認の proposal を出す。リアルタイム自動追従は非保証、鮮度検知と更新確認は保証。
- 保証すること / 保証しないことを scan と dialogue の両方に常に同梱する。
- scan schema `repo-preflight.scan/v3`、`--human`、`--audience`。
- コンソール補助の `--interactive` (本体は intent 対話。TTY メニューは任意)。

### 互換

- 素の検査 (`--repo` のみ、stdout JSON) は維持する。
- `status: pass` と `publication_decision: blocked_human_review_required` の分離は変えない。
- secret 検出時に ignore 選択肢を出さない。
- 旧 `--json` は引き続き受け付けない (v0.2.0 と同様)。

## 0.2.0 - 2026-08-01

### 破壊的変更

- package名、skill名、agent interface keyを`public-readiness`から`repo-preflight`へ変更。旧名は永久欠番とし、再利用しない。
- 必須成果物を`PUBLIC_READY.md`から`PREFLIGHT.md`へ改名。`REQUIRED`が検査対象repositoryに要求するファイル名が変わるため、既存の`PUBLIC_READY.md`は改名が必要。
- README release gateのschema識別子を`repo-preflight.readme-release-gate/v1`へ変更。
- `readiness_scan.py`から`--json`を削除。定義されていたがどこからも参照されておらず、「付けないとJSONにならない」という誤解を生んでいた。出力は以前から常にJSONであり、実際の挙動は変わらない。`--json`を付けて呼んでいた場合はexit 2になるため、フラグを外す。

旧名は「public化がゴール」と読めるが、private保存、PRまで、mergeまでを正規の完了地点として扱う本来の設計と矛盾していた。検査項目は公開可否と無関係に効くため、チーム共有、客先納品、外部協力者への受け渡しでも同じ手順を通す。

### 追加

- release準備時にREADMEの短さ、理解順序、Quickstart、制約、見出し階層を検査するread-onlyゲート。
- `readiness_scan.py --release` からREADME設計ゲートを自動実行する経路。
- READMEの不足内容に応じて必要なデザインskill/pluginだけを提案するrouting。
- 公開repositoryのruleset、merge方式、Actions権限、security機能を選ぶための設定ガイド。

### 修正

- git identityが未設定の環境で全scanが`tool_error`終了していた問題。現在名義のprobeは`--expected-identity`指定時のみ実行し、失敗時はscan全体を落とさず`commit_identity`を`unknown`として返す。CI container上での実行が回復する。
- `i18n.logOutputEncoding`が非UTF-8のrepositoryで作者名が壊れ、誤ったidentity mismatchを報告していた問題。git側の出力encodingをUTF-8に固定してから読む。
- 片方のidentity probeが失敗したとき、成功した側が示すmismatchまで`unknown`へ丸めていた問題。probeを個別に評価し、既知の不一致を隠さない。
- 予期しない例外が終了コード1を返し、`blocked`と区別できなかった問題。`tool_error`として終了コード2へ写像し、path/secretを含み得る例外messageは出力しない。
- Windowsのcp932環境でgit出力が文字化けし得た問題。subprocess出力をUTF-8として明示的にdecodeし、stdoutもUTF-8にする。

### 改善

- secret scanの検出結果に秘密値そのものを表示しない回帰防止。
- release gateのpassと、人間レビュー・release承認を別状態として維持。
- READMEとSKILL.mdに、public化が到達点のひとつであり唯一の終点ではないことを明記。
- package versionとCHANGELOG先頭見出しの整合テストが、古い見出しの残存で通過し得た点を修正。先頭のrelease見出しを解析して比較する。

## 0.1.0 - 2026-07-22

- Gitリポジトリの公開準備をread-onlyで検査する初回release。
