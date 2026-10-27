# Runtime サポートと保証境界

この文書は、repo-preflight が **どの実行環境で何を保証するか** の正本です。

## 対象 runtime

| Runtime | サポート | 入口 | 機械検証 |
|---|---|---|---|
| CLI (直接) | 一次対応 | `python scripts/readiness_scan.py` | pytest + `runtime_smoke` + CI |
| Claude Code | 対応 | `runtime/claude-code/SKILL.md` または root `SKILL.md` | skill 存在・trigger 語・smoke |
| Grok (Grok Build / agents skills) | 対応 | `runtime/grok/SKILL.md` または root `SKILL.md` | 同上 |
| Codex / OpenAI agents | 対応 | `runtime/agents/openai.yaml` + root `SKILL.md` | metadata 検査 |

「対応」は **手順と CLI が同じ契約で動く** こと。各製品の内部モデル挙動そのものは保証しない。

## 保証すること

1. **CLI 契約**  
   Python 3.9+ と git があれば、Linux / macOS / Windows で同じ JSON schema  
   (`repo-preflight.scan/v3` / `repo-preflight.dialogue/v3`) を返す。
2. **intent 対話**  
   `create_repo` / `push` / `open_pr` / `merge` / `configure_settings` / `publish` / `release` で
   guarantees / non_guarantees / proposals / confirmations が機械生成される。
3. **Skill 入口**  
   Claude Code / Grok / Codex 向け adapter が root `SKILL.md` を正本として指す。  
   各 runtime で「PR 作る」「公開する」等の trigger 語が description に含まれる。  
   `configure_settings` も同様に `runtime/claude-code/SKILL.md` / `runtime/grok/SKILL.md` /
   `runtime/agents/openai.yaml` それぞれの description・intent 列挙・trigger 語に明示され、
   root `SKILL.md` と乖離しない（GitHub Settings の変更は行わず GET/比較/preview まで）。
4. **クロス OS CIの定義**
   `.github/workflows/ci.yml` どおり: ubuntu-latest と macos-latest で Python 3.9 / 3.11 / 3.13、  
   windows-latest で Python 3.13 のみ。3.9 は test-py39 ジョブで pytest だけを回す（ai-ratchet-gate と black は 3.11 以上のジョブで回す）。各ジョブを`PYTHONUTF8=1`で実行し、pytest（`pytest-xdist` で `-n auto` の並行実行）、`runtime_smoke`、原本のまま同梱したrelease wheelとSHA-256を`requirements-tools.txt`で固定した `ai-ratchet-gate` v0.1.1を実行する。  
   秘密情報検査の試験は本物の gitleaks を要るため、各ジョブ（Windows・macOS を含む）は最初に `scripts/install_pinned_gitleaks.py` で `references/gitleaks-pins.json` の版（preflight-gate と同じ 8.30.1）を入れる。OS ごとの配布物の SHA-256 と、取り出した本体の SHA-256 の両方が固定値と一致したときだけ置き、合わなければジョブを止める。  
   workflow の一覧は `jobs` の外では block 形式、matrix は `include` 形式で書く。consistency gate の Action 参照読み取り器は流れ形式やスカラーの列を読まずに免除を断るため、こう書かないと Dependabot の Action SHA 更新が関連 docs 不足で止まる。
5. **発火は skill 遵守前提**  
   エージェントが skill を読み、外部操作前に CLI を実行する運用を契約とする。
6. **GitHub 採用時の対話**  
   `create_repo` / `push` / `open_pr` / `merge` の直前に `--intent` を実行すると  
   `repo-preflight.dialogue/v3` が返り、質問が機械生成される。これが GitHub 採用時の契約。  
   clone や GitHub ページ閲覧だけでは対話は走らない。
7. **install 済みコピーの drift 検知**
   `install_runtime_skills.py --check` が、ホームへ配布した `SKILL.md` /
   `run_preflight.py` / `README.md` / `checkout` link を repo 正本と sha256 で
   突き合わせ、ずれていれば `drift` と exit code 1 を返す。書き込みはしない。
   配布先へ追加された余分な file は検出しない（directory の一覧は取らない）。

## AI憲法の入口保証

runtime skillの対応と、共通AI憲法が各製品の入口へ実際に届くことは別の契約です。
`scripts/ai_entry_contract.py` は、共通正本と入口の関係を次の3戦略で検査します。

- `pointer`: runtime固有のsource pointer/importまたは、正本パスだけでなく肯定的な読込指示を伴う明示的な正本読込指示を検査する場合
- `materialized`: import非対応runtime向けに生成した共通本文とsource hashを検査する場合
- `manual`: 製品UIや未確認仕様に依存し、自動完了扱いしない場合

検査は既定で読み取り専用です。Grok/Cursorへ新しいglobal設定を勝手に作らず、
未確認の入口は`blocked`または`human_review`として返します。詳細は
[AI憲法入口契約](ai-constitution-entry-contract.md)を参照してください。

このmanifestはruntimeの選択的な採用を表す契約であり、Codexを含むadapterの存在は
`repo-preflight` CLIの必須依存を意味しません。`required` はそのmanifestで宣言した
entry単位の必須性です。未導入または採用しないruntimeはentryを省略するか
`required: false` にできます。任意entryの状態もreportには残り、採用した必須entryの
判定だけがmanifest全体のstatusを決めます。全体statusは `tool_error` > `blocked` > `human_review` > `pass`
の優先順位で、`pass` (exit 0)・`blocked` (exit 1)・`tool_error` (exit 2)・`human_review` (exit 3)
のいずれかです。引数エラーはargparseのusageをstderrに出してexit 2で終わり、JSONは出ません。

## 保証しないこと

1. Claude Code / Grok が **skill を自動インストール**すること（導入は `install_runtime_skills.py` または手動）。
2. モデルが skill を **無視して** push / PR / 公開を実行しないことの物理強制（hook が無い環境では運用契約）。
3. Claude Code Cloud / リモート sandbox に git やローカル path が無い場合の完全動作。
4. 各製品 UI のバージョン差分・権限ダイアログ・ネットワーク制限。
5. GitHub 設定の自動適用。`configure_settings`は`gh api`のGET、profile比較、個別previewまでを保証するが、変更は別承認・別工程。
6. github.com の repository ページが対話 UI になること。

## 導入 (Claude Code / Grok)

リポジトリを **好きな場所** に clone したうえで:

```bash
# 事前確認 (書き込まない)
python scripts/install_runtime_skills.py --repo .

# ホーム skills へ portable skill を書く (明示 --apply が必要)
python scripts/install_runtime_skills.py --repo . --apply
```

### path の持ち方（重要）

| やること | OK? |
|---|---|
| 各自が clone → 各自が `--apply` | OK |
| skill 隣の `run_preflight.py` と `checkout/` link を使う | OK（推奨） |
| 環境変数 `REPO_PREFLIGHT_ROOT` を自分で設定 | OK |
| 他人の skill フォルダをコピー | NG（壊れる） |
| skill 本文に他人の絶対 path を焼く | NG（廃止） |

install が skill ディレクトリに置くもの:

- `SKILL.md` — 手順（絶対 path なし）
- `run_preflight.py` — root 自動解決 launcher
- `checkout/` — clone への symlink / junction（失敗時は `ROOT_PATH.txt`）

起動例:

```bash
python ~/.claude/skills/repo-preflight/run_preflight.py --repo /path/to/app --intent open_pr --human
```

既定の配布先:

| Runtime | 既定 path |
|---|---|
| Claude Code | `~/.claude/skills/repo-preflight/` |
| Grok / shared agents | `~/.agents/skills/repo-preflight/` |
| Grok (user skills) | `~/.grok/skills/repo-preflight/` (ディレクトリが存在するとき) |

### 正本の更新と drift（重要）

`git pull` で追従するのは `checkout/` link **だけ**です。`SKILL.md` /
`run_preflight.py` / `README.md` は install 時の**物理コピー**なので、clone 側を
更新してもホーム側は古いまま残ります。

コピーが正本からずれていないかは、書き込まずに検査できます:

```bash
# install 済みコピーを repo 正本と sha256 で突き合わせる (read-only)
python scripts/install_runtime_skills.py --repo . --check
```

`status: drift` なら `--apply` で再配布してください。検査対象と検出名:

| 検査対象 | 検出名 |
|---|---|
| `SKILL.md`（`REPO_PREFLIGHT_ROOT=` 行を落とした射影で比較） | `skill_md_drift` / `skill_md_missing` / `skill_md_unreadable` |
| `run_preflight.py` | `run_preflight_drift` / `run_preflight_missing` / `run_preflight_unreadable` |
| `README.md`（install が作りうる link mode のいずれかと一致すれば ok） | `readme_drift` / `readme_missing` / `readme_unreadable` |
| `checkout/` link | `checkout_missing` / `checkout_dangling` / `checkout_foreign` |
| skill 配布先そのもの | `dest_unexpected`（存在するが directory ではない） |

`ROOT_PATH.txt` の値が相対 path のときは `checkout_foreign` です。install は絶対
path だけを書くため、相対値を `--check` の cwd で resolve して ok にはしません。
Windows の junction は `ROOT_PATH.txt` を見る前に `is_junction()` で識別します
（junction 先の repository が `ROOT_PATH.txt` を持っていても path-file と取り違えない）。

**`README.md` の検査は `checkout/` の状態に依存しません。**install 時の link mode は
どこにも記録されていないため、今の `checkout/` から検出した mode で期待値を作ると、
`checkout/` を壊しただけで無傷の README が `readme_drift` になり、`checkout/` を消すと
README が一切検査されなくなります。install が作りうる mode（`symlink` / `junction` /
`path-file`）のいずれかと一致すれば ok とします。

`*_unreadable` は install 済み file が UTF-8 として読めない場合です。1 file の破損で
run 全体を止めず、JSON を返して残りの target も検査します。

exit code は drift 検出で `1`、正常および未 install で `0`、`--check --apply`
同時指定など引数エラーで `2`。`not_installed` は配布先が**存在しない**ときに限ります。
配布先が file や dangling symlink など directory 以外として存在するときは
`dest_unexpected` の drift（exit 1）です。`--repo` が repo-preflight checkout でない
場合は `missing_adapter` になり、**`status` は `pass` ではなく `tool_error`**、
exit code は `2` です（何も検査できなかった run を `pass` と書くと、JSON を読む側が
fail-open するため）。`status: drift` の `next` は、呼び出した script の絶対 path と
検査した `--repo` を含む再配布コマンドです。

同じ検査は `runtime_smoke.py` からも呼ばれます（`check_one` を直接 import して実行）。
smoke での扱いは次のとおりで、`ok` / `not_installed` 以外を pass に丸めません。

| `--check` の status | smoke の扱い | smoke の `errors` |
|---|---|---|
| `ok` | pass | — |
| `not_installed` | pass（CI など配布していないマシン） | — |
| `drift` | fail | `runtime_skills_drift:<runtime>:<findings>` |
| `missing_adapter` / 未知の status | fail | `runtime_skills_tool_error:<runtime>:<status>` |
| 検査自体の例外・`SystemExit`（installer 不在、installer の `sys.exit` など） | fail | `runtime_skills_tool_error:<例外名>:<内容>` |

CI には install 済みコピーが存在しないため、CI 上の smoke では常に
`not_installed` として pass します。drift を拾えるのは、配布済みのマシンで
smoke を実行したときです。検査するホームは `--home` で差し替えられます（tests 用）。

`drift` のとき smoke の `notes` には `runtime_skills_next=` として、`--check` の
`next` と同じ再配布コマンドが載ります。

**install 済みのマシンでは、次の場合も smoke が drift で失敗します（再 install するまで）。**

- install した clone とは別の clone / worktree を `--repo` にして smoke を実行した
  （`checkout/` link が別の場所を指すので `checkout_foreign`。`SKILL.md` /
  `run_preflight.py` の中身が違えば file の drift も出る）
- `runtime/*`（adapter の `SKILL.md` や `run_preflight.py`）を編集している branch で
  smoke を実行した（配布済みコピーは編集前の内容のままなので file の drift）

どちらも「今 `--repo` で指している正本と、ホームのコピーが一致しない」という
正しい報告です。その checkout を使い続けるなら `--apply` で再配布してください。
CI には配布先が無く `not_installed` になるので、この挙動の影響を受けません。

**検査の限界:** `check_one` は既知の 4 対象（`SKILL.md` / `run_preflight.py` /
`README.md` / `checkout/`）だけを見て、配布先 directory の一覧は取りません。
install 済みコピーに**後から追加された余分な file は検出しません**。

プロジェクト限定にしたい場合:

```text
<your-project>/.claude/skills/repo-preflight/   # Claude Code project skill
```

へ同様に install してもよい。

## 動作確認コマンド

```bash
python scripts/runtime_smoke.py --repo .
python -m pytest -q
```

`runtime_smoke` が exit 0 なら、そのマシン上で CLI + skill 契約の最小保証は満たす。
ホームへ install 済みの skill コピーがあれば、その既知の 4 対象が正本から drift して
いないことも含む（`install_runtime_skills.py --check` と同じ検査。未 install は pass。
コピーへ追加された余分な file は対象外）。

## エージェント向け最小契約 (全 runtime 共通)

1. 外部操作直前に  
   `python <repo-preflight>/scripts/readiness_scan.py --repo <target> --intent <intent> --human`
2. stdout JSON の `status` が `needs_human_input` / `blocked` なら操作しない
3. guarantees / non_guarantees を短く人間へ示す
4. proposals を番号付きで聞き、yes だけ実行
5. secret に ignore を出さない

この 5 点は Claude Code でも Grok でも同じ。

## CIの起動確認と非公開設定

`runtime_smoke.py`は一時サンプルのCLI確認に限り、架空の名義policyと識別子リストを渡します。結果には`sample_policy=synthetic`と記録します。利用者の実policyやhomeへの設定は変更せず、本番の個人情報検査を通過した証拠にはしません。CIのようにskill未導入のhomeを検査する場合は`--home`へ空の検証用ディレクトリを指定できます。通常のhome検査で旧版の配置が見つかった場合は、従来どおりdriftとして停止します。

CI定義があることと、対象commitのジョブが開始・成功したことは別です。実行が制限されている場合は、ローカル試験の結果とGitHub上の未実行を分けて報告します。


検証済みpixelや数値表に意図的に隠した文字列の不存在は保証しません。画像全byteのliteral不存在が必要な利用要件には、この境界は適合しません。Git在庫の相対名は名前専用contextで確認し、本文の絶対path・名前のidentity literal・埋込みhost pathの検査は維持します。

### PNGを含むprivate identity検査

PNGは[ADR-0012](adr/0012-image-binary-and-repository-name-semantics.md)に従い、構造・CRC・展開・scanlineを検証したcoding・pixel領域をbinaryとして扱い、文字としてのliteral・path照合から分けます。文字metadata（tEXt・zTXt・iTXt）は[PNG仕様](https://www.w3.org/TR/png-3/#11textinfo)に沿って読み、圧縮文も展開して既存の静的文字列連結・URL表記の検査へ渡します。未知の付加chunkも既存の文字viewで検査します。

chunk境界・CRC・必要なheaderを確認し、壊れた構造、読めない文字metadata、展開上限超過は検査不能として停止します。PNGの全画像復号・OCRを保証する検査ではありません。既存のtext入力の厳格な正規化と、不明をpassにしない境界は維持します。

静止PNGはzlib streamの終端・余剰data、IHDRに対応するscanline長、filter byteを照合してから画素のコード解析を省略します。Adam7の各passも同じ条件で確認します。復号量は上限付きです。frame単位の復号を未検証のAPNGは検査不能として停止し、静止画の免除を流用しません。

### JPEGを含むprivate identity検査

JPEGは拡張子ではなく実signatureで判定し、[ADR-0010](adr/0010-strict-jpeg-private-scan.md)のstrict復号を終えてから画素をコード解析と分けます。検証済みcoding・entropy・JFIF thumbnail・Adobe数値項目は[ADR-0012](adr/0012-image-binary-and-repository-name-semantics.md)に従いbinaryとして扱います。COMなど文字metadataの既存文字検査は保持します。未解析のAPP payloadはsource連結せず、raw照合だけを維持します。APP1のXMP本文だけは文字として連結検査します。破損・警告・時間や容量の上限超過・依存不足は検査不能です。今回の送信（比較元からHEADまでの差分と、未commitの追加・変更）に含まれるJPEGの検査不能は走査全体を止めます。送信に含まれない既存JPEGの構造不正（`private_identity_jpeg_unverified`）は、そのfileを未展開として数え、送信全体は止めません。読めるfileの個人識別子と、送信に含まれるJPEGは止めます。範囲は [ADR-0011](adr/0011-existing-jpeg-unverified-does-not-block-submission.md) です。

JPEG検査のruntimeには、任意の`images`追加依存の`simplejpeg`を導入した仮想環境を使用します。`python -m venv .venv`で分離し、その環境の`python -m pip install ".[images]"`で依存を入れて、同じ環境のPythonから既存CLIを起動してください。hookや外部wrapperから呼ぶ場合も、呼出元がその環境のPythonを選んでいることを別途確認します。標準ライブラリだけの旧実行経路はJPEGを成功扱いにせず、`private_identity_jpeg_decoder_unavailable`で停止します。homeのhookやsystem Pythonを自動変更する処理は含みません。

### PRレビューの収束確認

GitHub CLIで認証済みの環境では、CI、review、inline threadを同じPR HEADへ束縛して確認できます。

```bash
python scripts/github_pr_review_snapshot.py \
  --repo owner/name \
  --pr 123 \
  --expected-head <40文字のcommit SHA> \
  --required-reviewer chatgpt-codex-connector \
  --grace-seconds 30
```

通常コメント、review概要、CI、GraphQL review threadを別々のsurfaceとして読みます。HEAD不一致は
`tool_error`、未解決threadやCI失敗は`blocked`、review待ちやCI実行中は`pending`です。
`pass`はreview snapshotの機械検査結果であり、merge承認ではありません。

## PSDを含む個人情報検査

PSD検査はPython 3.11以降で `.[images]` の `psd-tools` を使って形式を読みます。Python 3.9・3.10ではPSDの依存が未導入として停止し、テキスト・PNGの検査は維持します。圧縮された画素をプログラムの文字列式として解析せず、全バイトの直接照合、合成画像および非表示を含む全レイヤー・マスクの展開後の直接照合、画像リソース・レイヤー名・追加メタデータの文字列検査を分けます。メタデータの文字列連結・パーセント符号化も既存の正規化器で検査します。OCRは行いません。

PSD（バージョン1）が対象で、PSB（バージョン2）は未対応です。ファイル128MiB、展開後の合成画像と全レイヤーの累計128MiB、メタデータ文字列8MiB、メタデータ走査10万要素が上限です。構造不正・上限超過・追加依存不足・未確認の型は検査不能として停止します。旧版PSDで起きた `private_identity_normalization_incomplete` を、ファイルの除外や検査の無効化で回避しません。

## GIFを含む個人情報検査

`.[images]` のPillowを使い、GIF87a/89aの構造を検証した後、拡張ブロックの本文と全フレームを検査します。コメントのsubblockを連結してから既存の文字列検査へ渡します。フレームは単独で展開し、透明・差分合成で元のパレット番号を消さず、番号と色の両方を直接照合します。画像の画素をプログラムとして解析しません。OCRは行いません。

入力128MiB、1フレームのRGBA換算128MiB、全フレーム累計512MiB、512フレーム、拡張本文8MiBが上限です。構造不正・画像展開失敗・追加依存不足・切れた画像を許す設定・元の番号を失う強制RGB設定は検査不能にします。Pillowの版とデコーダ設定は検査キャッシュの鍵に含めます。


## CubismのCMO3（CAFF）を含む個人情報検査

CAFF v0.0.0 / format `----` のファイル表と各ファイル固有のXORキーを読み、圧縮画素を文字列式として解析しません。内部名・main_xml・全埋込みPNG・PNGのpreviewを既存の検査器へ渡します。中央directory付きZIPはPythonの標準ライブラリ、Cubismのstreaming ZIPは既存の局所header・deflate終端・descriptor・CRC検証を使います。未知の型、壊れたentry、重複名、範囲外や重なったoffsetは検査不能として停止します。

入力128MiB、展開したentryの合計256MiB、main_xml 64MiB、entry 1万件、XML要素10万件が上限です。XMLはUTF-8のみで、DTD・外部entityを読みません。補助デコーダのSHA-256を検査キャッシュ鍵へ含めます。埋込み画像の再検査の控えも検査方針へ束縛します。旧来のCMO3パス検査も同じ読取りを再利用します。

形式を確認した一次実装は [stretchystudioのCAFF読み取り](https://github.com/MangoLion/stretchystudio/blob/master/docs/live2d-export/scripts/cmo3_decrypt.py) と [書き込み](https://github.com/MangoLion/stretchystudio/blob/master/docs/live2d-export/scripts/caff_packer.py) です。独自のZIP圧縮器や暗号処理を作り直さず、標準のZIP・zlib・CRCを再利用します。`rewrite_main_xml` は全entryの再読取りを行い、XML以外の内容がbyte単位で同じことを確認してから結果を返します。本人情報の削除を自動で承認する機能ではありません。


Git履歴の通常batch予算16,000,000 byteは維持します。現在の作業treeで同一byteをCAFFデコーダの上限内で完全に形式検査したblobだけ、SHA-1/SHA-256のGit object IDへ証拠を束縛し、予算超過分を単独batchで読み直します。読み直した本文のOID・private/secret/path検査は省略しません。同じデコーダ・方針hashのclean cacheも同一byteの証拠を引き継ぎます。未検証の大容量objectや、現在treeにない過去の大容量blobは従来どおりunknownです。

## PDF・MP3とMP4のprivate identity境界

同梱のtrusted adapterはPDF・MP3・限定H264 MP4を形式検証して本文・metadata・coverを既存identity callbackへ渡す。extensionとmagicの不一致、不正・未知形式・依存不足はtool_errorになる。MP4はHachoir3.4.0と固定mov decoderによる1本のH264 videoだけを扱い、audio/字幕/cover/他codecは停止する。同一hashへ全decode・trace・exact packet/container範囲を結合し、SEIのUUID/payload/結合viewも検査する（ADR-0014）。復号文字viewのsecretも既存secret_scanへ別集計する。外部の明示providerにはこのbinary exemptionを与えない。

worktreeと到達可能なGit blobは同じdispatchを使い、blobの全path aliasもextension照合に使う。媒体はdecoder環境をlookup前に完全束縛するまでclean-pass cacheのlookup/recordを行わない。他の文書・画像のcache契約は維持する。reportにはadapter SHAと実decoder versionsを付ける。これらの証拠は利用側CIや原本参照の採用を代替しない。

PDFの完全原本検査はPython 3.10+のoptional機能（pypdf 6.21.2 / pdfminer.six 20260107）。coreのPython 3.9契約は変えず、同環境のPDFは検査不能として停止する。原本I/Oとinitialized stream identityを束縛し、圧縮領域以外のheader・comment・free object等も検査する。既存tokenizerは位置だけに使い、文字列/hex/nameの値はpypdf primitiveが確定する。依存source hashの変化・range不整合・不完全EOF・未知filter chainは停止する。全linked xref componentを同じ原本で再読し、旧版も意味検査する。XRef streamはW/Indexとexact decoded length・完全消費を確認し、cycle/補正/partial overlapは停止する。未圧縮binaryをPDF構文として読むのではなく、raw照合とその実decoderのmetadata検査を行う。

限定null互換は、xref_free_entryで明示freeかつlive xref/object streamに存在しない正確なgeneration/idだけに適用する。その他のstrict読取り失敗は維持し、元free bytesの検査と数値の適用回数を残す。mailtoは非空targetのinert metadataとして原文・percent復号を検査し、外部へ送信しない。文字列件数は既存object上限100000に整合し、2MB等の他上限は維持する。

Type1はfontTools4.67.0の固定実装を短命child内でguardし、Length1/2/3・eexec開始・readstring→PS object→CharStrings/Subrsを正確に結ぶ。raw/decryptedの全文字領域を検査し、完全なcodeword decode後のbinary区間以外は免除しない。operation/割当前allocation/stack/depth/output/I/O予算を超えれば停止する。PatternType1/FunctionType4は既存parserでinert graphics sourceを確認する。checksumだけの不一致は数値diagnosticとして残すが、全table/glyph/metadataはstrict decompile必須で、元checksum整合性の保証をしない。

埋込みfontの固定fontTools4.67.0は配布metadataのRequires-Pythonに従いPython3.11+で導入する。Python3.10のPDF字句・原本検査は維持し、font decoderがない入力はdependency unavailableで停止する。core3.9やoptional依存不足を検査成功とは扱わない。

媒体依存のPython 3.9互換版はMutagen 1.47.0（上流Requires-Python >=3.7）、3.10+は1.48系（>=3.10）を使う。FFmpeg/FFprobe不足は検査成功ではなくdependency unavailableで停止する。fontTools 4.67.0のsource pinは既存LF SHAを維持し、配布SHAを照合した公式Windows wheelの完全一致CRLF SHAだけを追加する。任意の改行正規化・混在改行・コード変更は受理しない。

媒体coverのPNG/JPEGは、既存parserが意味を検査できるmetadataだけでobserver完了を返し、復号した文字をidentityとsecretの両検査へ渡す。PNGは検証済みIHDR/PLTE/IDAT/IENDと解析済みtEXt/zTXt/iTXtだけを扱い、宣言済みXMP・iCCP・Exif・数値項目を含む未解析の付加chunkは停止する。JPEGは既存の形状検証済みJFIF/Adobe項目とCOMを扱い、単一segmentを含む全ICC・Exif・Photoshop・XMP・未知APP metadataは停止する。observer未対応も停止し、検証済みの画素・codewordを文字検査へ戻さない。この限定はobserverを使う媒体cover経路だけで、従来の2引数画像検査APIは維持する。PDFのXMLは上限付きExpatで文字参照・本文・属性・namespaceを検査し、DTD・外部entity・不正XMLを拒否する。 PDFのICCBased色空間から参照されるICC v4は、desc/cprtの全locale文字列（mluc）、白色点・原色（XYZ）、色順応行列（sf32）、パラメトリック曲線（para）に限定して解析する。タグ表・範囲・予約領域・余剰バイト・成分数を検査し、未知タグ、部分的な重なり、範囲外、非ゼロの未解析領域は停止する。単にICCヘッダーに一致する未参照streamは許可しない。これは色変換の正しさを保証するものではなく、画像coverのICC未対応方針も変更しない。CFFは未使用glyph名を含むName/String INDEXもmetadataとして検査する。


### PDFの抽出テキスト量

解析済みのcontent/Form/pattern/function streamでは、数値と既知のPDF演算子を文字列として再解釈しません。hex・literal文字列は既存parserで復元した値を検査し、その表記上の桁やescapeを別の本文として重複保持しません。名前・コメント・未知tokenは保持し、NULを含む曖昧なcontentは元bytesをそのまま検査します。[ISO 32000-1の数値・文字列・演算子の定義](https://opensource.adobe.com/dc-acrobat-sdk-docs/standards/pdfstandards/pdf/PDF32000_2008.pdf)に従う構文分類であり、数値を偶然UTF-16へ変換して生じる文字は本文と扱いません。

PDFの元bytesは内部packetへ一度だけ格納し、親processでUTF-8/Latin-1/UTF-16 LE・BEと両alignmentの観測を逐次行います。2,000,000-byteの予算は、通常の文字列と、各bytes sourceの最大UTF-8 viewの合計に適用し、親側でも再計算します。全viewの合計を複数の本文として保持する方式から変更しました。件数・出力・画像・時間・復号量の上限と、真に大きい本文の停止は維持します。MP4のbyte-text処理は変更しません。

FontDescriptorから参照されるCIDSetは最大65,536 CIDのbit表として、encoding以外の辞書項目・サイズを検証したうえで全byte-text viewも検査します。content/font/CMap/ICCと同じstreamを兼用する場合は停止します。未分類streamに一般的な除外を追加するものではありません。

ページの文字抽出・画像確認・operand確認では同じ解析結果を再利用し、そのページの確認が終わると解放します。全ページの解析treeは保持しません。元readerの辞書と元stream bytesは変更せず、配列や複数の宣言roleを持つstreamは個別検査を維持します。
