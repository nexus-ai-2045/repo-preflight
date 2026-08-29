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
   Python 3.11+ と git があれば、Linux / macOS / Windows で同じ JSON schema  
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
4. **クロス OS CI**  
   `.github/workflows/ci.yml` どおり: ubuntu-latest と macos-latest で Python 3.11 / 3.13、  
   windows-latest で Python 3.13 のみ。各ジョブを`PYTHONUTF8=1`で実行し、pytest、`runtime_smoke`、公開release wheelのURLとSHA-256を`requirements-tools.txt`で固定した `ai-ratchet-gate` v0.1.1を実行する。
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
判定だけがmanifest全体の `blocked` / `pass` を決めます。

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

**`README.md` の検査は `checkout/` の状態に依存しません。**install 時の link mode は
どこにも記録されていないため、今の `checkout/` から検出した mode で期待値を作ると、
`checkout/` を壊しただけで無傷の README が `readme_drift` になり、`checkout/` を消すと
README が一切検査されなくなります。install が作りうる mode（`symlink` / `junction` /
`path-file`）のいずれかと一致すれば ok とします。

`*_unreadable` は install 済み file が UTF-8 として読めない場合です。1 file の破損で
run 全体を止めず、JSON を返して残りの target も検査します。

exit code は drift 検出で `1`、正常および未 install で `0`、`--check --apply`
同時指定など引数エラーで `2`。install していないマシンでは `not_installed` を
返して `pass` になります。`--repo` が repo-preflight checkout でない場合は
`missing_adapter` になり、**`status` は `pass` ではなく `tool_error`**、exit code は `2` です
（何も検査できなかった run を `pass` と書くと、JSON を読む側が fail-open するため）。

CI ゲートには載せていません。CI には install 済みコピーが存在しないため、
そこで検査しても常に `not_installed` にしかならないからです。

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

## エージェント向け最小契約 (全 runtime 共通)

1. 外部操作直前に  
   `python <repo-preflight>/scripts/readiness_scan.py --repo <target> --intent <intent> --human`
2. stdout JSON の `status` が `needs_human_input` / `blocked` なら操作しない
3. guarantees / non_guarantees を短く人間へ示す
4. proposals を番号付きで聞き、yes だけ実行
5. secret に ignore を出さない

この 5 点は Claude Code でも Grok でも同じ。
