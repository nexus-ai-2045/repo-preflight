# ADR-0001: GitHub ActionのSHA-only更新を限定的に免除する

- Status: Accepted
- Date: 2026-08-16
- Decision owners: repository maintainers

## Context

`impact_map` は、workflow実装が変わったときに関連docsまたはtestsの更新を要求します。一方、Dependabotなどが同じGitHub Actionの完全SHAだけを更新する場合、workflowの構造やrepo固有ロジックは変わりません。pathだけを見る判定では、この機械的な依存更新も仕様変更として止まり、実質的な説明を持たないdocs更新を要求していました。

ただし、作成者名やbranch名だけを信頼して免除すると、Action名の差し替え、可変ref、workflowロジック変更、symlink化などを見逃す安全上の問題があります。行単位の正規表現だけでも、Action step外の `uses` 文字列を誤認できます。

## Decision

各impact ruleが `allow_github_action_ref_updates: true` を明示した場合だけ、次の条件をすべて満たす変更を関連docs・tests要件から免除します。

1. 対象は `.github/workflows/*.yml` または `.yaml` の既存通常fileである。
2. baseからworking treeまでGit file modeが変わらず、symlinkではない。
3. YAMLのindentとkey構造を厳格に走査し、`jobs.<job>.steps[*].uses` または reusable workflowの `jobs.<job>.uses` と一意に確認できる。
4. 更新前後とも `owner/repository[/path]@<40桁SHA>` である。
5. Action識別子は同一で、変えてよいのは完全SHAと、空白で区切られた行末コメントだけである。
6. 行追加・削除、別のworkflowロジック変更、同じimpact ruleに該当する別file変更が混在しない。

判定はactor、Dependabot名義、branch名、labelに依存しません。曖昧、parse不能、mode取得不能の場合は免除せず、従来の関連docs・tests要件を適用します。

## Alternatives considered

### Dependabot作成PRを一律免除する

採用しません。identityやGitHub上の状態に依存し、差分内容を保証できないためです。

### workflow fileの変更をすべて免除する

採用しません。権限、trigger、shell command、Action名などの意味的変更まで通してしまいます。

### 行単位の正規表現だけで判定する

採用しません。`env.uses` や `run: |` 内の文字列を、実際のAction参照と区別できないためです。

### 依存更新でも毎回docsまたはtestsを変更する

採用しません。内容のない追随変更を誘発し、人間レビューの信号対雑音比を下げるためです。

## Consequences

- 同一Actionの完全SHA更新は、repo固有ロジックを変えない限り自動検査を通せます。
- 完全SHAの供給元、release内容、互換性、CI結果のレビューは別途必要です。この免除は依存先の安全性を保証しません。
- 外部runtime dependencyは追加しません。対応するYAML構造を限定し、indentやcontextを一意に確認できない場合はfail-closedです。
- 新しいGitHub Actions構文へ対応する場合は、許可範囲を広げる前に回帰テストとこのADRの見直しが必要です。

## Verification

少なくとも次を回帰テストします。

- step-levelおよびreusable workflowの同一Action SHA更新は通る。
- Action名変更、可変ref、workflowロジック変更、行追加・削除は止まる。
- staged / unstagedのfile mode変更とsymlink化は止まる。
- `#` の前に空白がない値は止まる。
- `env.uses` と `run: |` 内の文字列はAction参照として扱わない。
