# ADR-0006: private原資料の個人pathをレビュー済みの限定例外で扱う

- status: Accepted
- date: 2026-10-06

## 背景と判断

取得時点の原資料には、公開された文章の中の絶対pathや取得環境のprovenanceが含まれる。private保管で原文のSHA-256を維持する必要と、公開向けの個人path検査が衝突する。一方、file全体やディレクトリの除外、正規表現の単語境界変更は、未知のpathや埋込みcredentialを見逃すため採用しない。

ADR-0005の明示指定JSONと内容hashの束縛を再利用する。同じ `--reviewed-secret-exceptions` のversion 2で個人pathの限定例外を表す。version 1のsecret誤検出契約は維持し、新しい台帳・自動生成器・除外ディレクトリは設けない。secret候補は公開URLや既存識別子として内容を確認したものだけを既存契約で扱い、通常のsecret正規表現は変えない。

個人pathの例外は、originから確定した対象repoがGitHubのlive読取りでprivateと確認できる場合だけ利用する。公開、所在不明、読取り不能、originとfull_name不一致を成功扱いにしない。例外に指定した内容が現在の共有範囲で保管可能かは人間が判断し、機械はその判断を生成しない。

## 束縛と停止条件

各entryをexact repo・相対path・生byte全体のSHA-256・検出rule・検出文字列のSHA-256・検出件数に束縛する。version 2ではUTC期限と非空の人間レビュー参照を必須とする。理由と参照へcredentialの生値を保存しない。期限は将来かつ90日以内とする。期限切れ・不正schema・重複・曖昧なidentity・可視性の不一致は停止する。

例外は明示指定した検査のみに適用し、自動探索しない。同じfile内の別の検出、別path、別blob、件数変化、追記されたsecretは残る。履歴の中間commitと削除済みfileも調べる。target diffでは各時点の全blob内の個人path検出が完全束縛を満たす場合だけ、そのfileに対する限定適用を認め、他fileの追加行検査を維持する。UTF-16・URL復号・binary・既存のCubism展開検査を迂回しない。展開できないfileに例外で成功を与えない。

## 検証と見直し

架空データと実Gitでprivate／public／unknown／offline、期限、内容・件数・path変更、別credential、履歴alias、中間commit後の削除、作業treeとuntrackedを検証する。通常の検出に加えた経路は同じCLI/API/intentから通す。

対象の公開・collaborator追加・運用主体変更は承認内容の再レビュー条件とする。private確認は公開・push・mergeの自動承認ではない。公開する上流実装には原資料、実際の検出文字列、対象者の絶対pathを含めない。

## JSON契約

最上位の `version: 2` と `entries` はADR-0005と同じ入口で明示指定する。version 1は従来のsecret専用形式として互換を維持する。

| キー | version 2の条件 |
|---|---|
| id / repo / path / content_sha256 / match_sha256 / occurrence_count / review_reason | ADR-0005と同じ完全束縛と型検査 |
| rule | 既存secret rule、または windows_user_path / macos_user_path / linux_home_path |
| expires_at | UTCの YYYY-MM-DDTHH:MM:SSZ。検査時点から将来かつ90日以内 |
| review_reference | 非空の人間レビュー参照。credentialの生値を含めない |

path ruleがあるときだけlive private確認を要求する。適用結果は `checks.personal_path_scan.reviewed_exceptions` にIDと件数を記録し、secret側の適用結果は従来どおり `checks.secret_scan.reviewed_exceptions` に分離する。例外設定は対象のprivate保管先でレビューし、実際のpathや検出の指紋を公開上流へコピーしない。
