# ADR-0017: 旧履歴のregex役割と合成負例signatureを限定して扱う

- status: Proposed（ローカル実装候補。親が独立レビューを別担当へ依頼する）
- date: 2026-10-29
- scope: 既存normalizerの役割判定修復と、既存v3予約集合への有限4signature追加。

## 理由と確認した出自

公開releaseの全履歴検査で6旧blobがprivate_identity_scanに残った。実denylistのSHA-256は`dfc434c8da6dd385f58c2bc5b849c4a5d367505b74648a83ba1b2771068a4516`。値を表示せず固定原本と既存normalizerへ渡すと、全6件がliteral_match_count=0、local_path_match=true、reconstructed_path_match=trueだった。2件は検出器のre.compile引数、4件はパスを拒否するための合成負例で、変数名だけで合成認定していない。負例を生成し拒否を検査する構文上の出自、原本hash、非公開語一致0を独立診断した。

同名4ファイルのmain先端では同じ検出がなく、6 blobはmain履歴から到達する。先端を変更するだけでは全履歴の問題を解消できない。公開履歴書換、履歴の除外、tests全体免除、denylist削減は採用しない。

## 旧規則と修復

ADR-0013の標準re.compile役割は単一位置引数のみを認識し、root直後の限られた汎用username classだけをspan化していた。第2位置引数が標準re.I/re.Mである呼出しと、名前付きcapture内の有限class、既存共有システムディレクトリの有限な境界assertionを追加認識する。既存のimport/binding/属性変更/動的評価拒否、regex検証、容量上限、source tokenへの位置束縛は維持する。一般本文、具象username、後付けの具象suffix、private literalは従来どおり検出する。

ADR-0008のv3は3個の予約signatureだけを登録可能としていた。今回、下の合成負例に必要なlinux_home_pathの4個のmatch SHA-256だけを追加する。生のfixture値、人物名、メール、利用者ホームはこの記録にも新しい予約集合にも書かない。

- `2ce1dda1084edaefc6f33285f9259285b887f0eae16957495c6413f70ce72c85`
- `01c9f309dc1e667a5404ad9d98fb3e96c4d9c109d5b6d63d83adaf41448c38ec`
- `6d06e07163cde8fac80c1941aed3c558499ef602b963201c2d43c681e76257df`
- `b1cbc5f347543a03701354923cd95344068142e4aa08c74d259fbfe7c62984fc`

既存normalized_personal_path_matchesは役割なしstatic_string_viewsを使い、normalizer本体との結果差が残っていた。private_path_viewsを同じ_private_text_analysisから投影し、既存の正規化path照合にも同じ役割spanを渡す。raw byte検出と公開static_string_viewsの二要素tuple契約は維持する。未知の旧providerには従来viewを使う。

有限集合の追加は自動例外ではない。v3のrepo/path/content SHA/rule/match SHA/count/expiry/review reference/live publicの全条件を変更しない。secret rule及びliteral deny語は免除しない。既存のnormalized=True経路で復元された検出も同じ完全束縛へ渡す。

## 固定6 blobの証拠

| path | Git blob | 原本SHA-256 | 出自category |
|---|---|---|---|
| scripts/identity_gate.py | 83b0888ec7631a95afdcbb1385ca6f3b69d341a1 | 5d220ec4291a23f3c6ca1c7215cfee24c2122edb364095b50e276723da50a39f | regex役割 |
| tests/test_commit_hook_templates.py | d44827608ea4de4c1761aa3c8e76cd249e734037 | d430d8e0f9e91d27f637fecf1fb5afc3d1e66732bdd0f6a73202531f169c36d7 | 合成負例 |
| tests/test_identity_fixture_exceptions.py | 39b3c6cc4832951ffa974778a7f7ff3d90fe642f | 3e27047375feb36208d0b271ca03a6cbebe0db391d4111fcd6d9d4f60fcbe470 | 合成負例 |
| tests/test_public_identity_policy.py | 0446121a7a6d7b3a3b66fc5bd52a9d540725db9a | 0149b93fec4b1c582479254eb33cdb93a389b2b93f3ffabc138f35082fd15526 | 合成負例 |
| tests/test_identity_fixture_exceptions.py | fe39b142a0c5d7cb24d6756d883dad4bc93e0596 | 6c481d6f11c02b57afad1a1bd831dcb95ff95e160760ede32d23bc6d233216d7 | 合成負例 |
| scripts/identity_gate.py | 939c619b3c2eb2b10c83885842e45e646e81cbab | 1be5657828a6a4f555a9afe2e708d54672641f26a719deea72d7816c4a9a1eed | regex役割 |

## 移行方法

1. 親が本候補の差分と局所検証を独立レビューする。既存settingや運用gateはその場で変更しない。
2. regex修復は新しい検査器で旧2 blobを直接再検査する。fileや変数名による特例は登録しない。
3. fixtureは利用者側private scratchの明示v3候補JSONへ、原本ごとのrepo/path/content hash・署名・件数・UTC期限・review参照を記録する。4 blobに5entryを要し、同じpathの旧2版も別content hashへ束縛する。policyはrepoへ同梱せず、自動読込もしない。
4. 実originがliveでpublicであることを既存loaderで確認する。mockされた公開応答は局所検証の証拠に限り、実運用のlive確認の代用にしない。unknown/private/offlineは停止する。
5. 正式な採用判断後、明示policy付きの同じ全履歴release scanを行う。期限切れ、alias、内容・件数変更、未知署名、追加secret・private literalは停止する。旧検査器にpolicyだけ渡しても追加signatureは受理されないため、検査器revisionも記録する。

## 非保証と停止線

合成出自のレビュー責任、任意の別fixtureの安全性、人物情報の完全不存在、GitHub CI成功、別端末への導入、公開やreleaseの承認は保証しない。局所6件の解消をfullscan成功に読み替えない。parentによる独立レビュー・運用policyの採用・公開実行は別段階。新しい検出器も明示policyなしでは合成負例4件を検出する。


## 独立レビューで確認したP1と修復（2026-10-29）

初回候補はgeneric atom後の区切りだけでrootをmaskし、後続の親componentを見ていなかった。名前付きcaptureと標準flagsを認識する拡張により、escaped literal dotの親参照と具象usernameを連結したpatternが具象homeへ正規化されても、path gateがpassするP1を独立レビューで確認した。初回の425件通過及び6旧blobの解消は、この反例を含まない旧候補の証拠として保持する。

mask前に、literalなregex escape及びURL表記を戻したviewを既存のparent-component文法へ接続する。dot、path separatorを表すescapeだけを還元し、source quoteの多重化も16段以内で確認する。上限まで還元が継続する場合はmaskしない。対象sourceを実行したり、非公開語を追加したりしない。さらにgeneric spanを仮に除いた後も、後続pathに解釈しきれないregex演算子が残る場合はrootを保持してfail-closedにする。文字classや反復が親componentを生成する場合を、具体文字列でないから安全とは扱わない。未知の複雑なpath suffixを新たに免除しない。

通常のgeneric atom・標準flags・安全なliteral suffix・共有system-directoryの必要な既存役割は対応試験で維持する。literal/escaped/hex/Unicode/octalのdot、文字classと反復、source quote・静的連結、POSIX/Windows separatorを組み合わせ、runtimeだけで作る具象値が正規化でhomeへ戻ること、normalizer・normalized_personal_path_matches・PrivateIdentityScanがともに止めることを確認する。個人のhome/usernameや生成sourceはfixture・report・logへ保存しない。v3予約4signature及び全exact束縛の変更は行わない。

この修復hashは親側の独立再レビューを要する。旧候補のfull release scan完走結果を、新候補の全体安全性又はrelease承認へ読み替えない。

## 保存したレビュー設定

[既存合成負例のv3設定](../../.repo-preflight/reviewed-synthetic-fixtures.json)を明示指定して使用します。自動適用は行いません。期限・内容hash・対象・件数・公開状態の条件を維持し、期限後は再レビューします。秘密情報・private literalの検出は除外しません。

承認元は後継の[ADR-0021](0021-public-fixture-policy-trust.md)に従います。任意の外部台帳は採用せず、固定した検査器のcommitに同梱された内容と一致する場合だけ認めます。前節のscratchで作った候補は、登録前の検査・本文レビューを経て同梱されるまで有効になりません。

## Windowsのliteral区切りの互換性

generic username atomに続く安全なliteral suffixでは、regexの二重backslashを区切りとして扱います。単一escape、backreference、文字classや反復などの未解決操作、親componentは引き続きmaskを認めません。Python tokenは復号後のpatternを検査し、PowerShell互換viewも既存のregex役割とliteral解析を維持します。raw・repr・JSON・静的連結の表記と、正当なsuffix・未解決操作を実APIで対照検証し、本文の実値を免除する規則は追加しません。
