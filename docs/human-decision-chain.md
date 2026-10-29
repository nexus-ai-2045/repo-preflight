# CI変更に人間判断を結び付ける

`base-guard`は、保護パスの変更を一律に禁止する代わりに、登録された承認元の設計判断と具体的な差分判断を検証できます。設定が無い環境では、従来どおり保護変更を停止します。既存の必須検査を弱める例外ではありません。

## 人間判断を入れる境界

設計時に、目的、対象、許可する操作、人間へ戻す分岐を決めます。その範囲での調査、実装、テスト、通常の修正はAIへ任せます。対象・目的・権限の拡大は新しい判断へ戻します。

具体的な保護差分を確認したら、設計判断を親に持つ差分判断を署名します。同じPRで保護差分が変わらない再実行や、非保護ファイルだけのHEAD更新では再利用できます。新しいHEADの通常検査は省略しません。非保護コード、push、merge、release、公開まで承認したとは扱いません。

現在の形式は単一PRの判断連鎖です。別PRへの流用は拒否します。複数PRにまたがる設計方針の委任は、この形式では実装していません。

## 信頼の起点をPRから分離する

検証器はbaseのGit objectから`.repo-preflight/decision-trust.json`を読みます。PR側が追加した公開鍵や、CLIの承認フラグ、コメント、ラベルを信頼の起点にしません。

信頼設定は次の項目を持ちます。

| 項目 | 意味 |
| --- | --- |
| `schema` | `repo-preflight.decision-trust/v1` |
| `repository_id`、`repository`、`base_ref` | 対象の不変ID、owner/name、比較元branch |
| `max_validity_seconds` | 本人が採用する判断の最大有効期間。ツールは期間を既定で決めない |
| `signers` | 承認元IDから公開Ed25519 SSH鍵への対応。秘密鍵を置かない |
| `revoked_payload_sha256` | 個別判断の撤回 |
| `revoked_signers` | 承認元の失効 |

この設定の導入・変更は通常の差分承認で免除できません。既存の人間レビュー境界で別に扱います。登録鍵の失効情報はbaseから読み直します。設定全体のhashを判断に結び付けるため、設定の更新時は既存の判断も失効します。

署名が証明するのは、登録された鍵が決定文へ署名したことです。その場で人間が操作した証明ではありません。人間の承認元として運用する場合は、AIやCIが承認鍵を自由に使えないことを登録時に確認します。本ツールは本番鍵を生成・探索・読取り・署名せず、承認元を自動登録しません。

## 設計判断と差分判断の形式

PRの固定ファイル`.repo-preflight/decision-bundle.json`には、次の包みを最大8件置きます。

```json
{
  "schema": "repo-preflight.decision-bundle/v1",
  "decisions": [
    {
      "payload_base64": "正規化した決定文bytesのbase64",
      "signature": "SSHSIG形式の署名"
    }
  ]
}
```

決定文はJSONをキー順で並べ、余分な空白を除き、`ensure_ascii=True`で表したbytesです。署名と意味検査には同じbytesを使います。重複キー、浮動小数、未知フィールド、非正規形を拒否します。

| 決定文の項目 | 意味 |
| --- | --- |
| `schema` | `repo-preflight.protected-decision/v1` |
| `decision_id`、`kind`、`signer`、`purpose` | 一意の判断ID、`design`または`change`、承認元、目的 |
| `parent_payload_sha256` | 親の決定文hash。IDだけの参照ではない。根は`design`のみ |
| `scope` | 対象・操作・保護差分・信頼設定への束縛 |
| `issued_at`、`expires_at` | UTCの発行・失効時刻。発行時刻は署名者の申告 |

`scope`の全項目は`repository_id`、`repository`、`base_ref`、`pull_request`、`operation`、`paths`、`protected_delta_sha256`、`policy_sha256`です。操作は`protected-change-check`に固定します。設計判断の差分hashは`null`、末端の差分判断は現在の保護差分hashと完全一致させます。設計判断だけでは通しません。

子のパスと有効期間は親の範囲内に限ります。具体的差分を承認した親からは、別の差分や広い設計権限へ戻せません。欠落・循環・取消・期限切れの親は拒否します。全ノードの署名を検証します。

署名検証は[OpenSSHの`ssh-keygen -Y verify`](https://man.openbsd.org/ssh-keygen.1)を使い、principalとnamespace `repo-preflight-protected-change`を固定します。`check-novalidate`は使いません。新しい暗号ライブラリ、署名サービス、CI内の秘密鍵は不要です。署名作成は登録した承認元側の操作であり、本ツールは代行しません。

## 確認する差分を取り出す

信頼済みの作業場所から、実際の対象を指定します。

```bash
python3 scripts/base_guard.py --repo target --base-ref origin/main --head-ref HEAD \
  --repository example/project --repository-id 123 --pull-request 7 \
  --base-branch refs/heads/main --decision-request
```

この読取りは対象のbase・HEAD、変更された保護パス、差分hash、baseの信頼設定hashをJSONで返します。署名・承認・外部送信は行いません。信頼設定が読めない場合も、承認済みとは扱いません。

保護差分hashには、merge-baseからHEADへの変更をパス順に並べ、変更前後のblobとmode、追加・削除を含めます。renameは削除と追加です。循環を避けるため固定の署名carrierだけを除外します。それ以外の保護パスを除外しません。APIの呼出元は、変更された保護パスの全件を渡す必要があります。

## 通さない変更と現在値の照合

callerの削除、別owner、SHA不一致、未公開の参照先、読めない定義は、署名があっても停止します。既存caller workflowの意味変更は、検証済みの固定版更新以外を許可しません。`base-guard` jobを含むworkflowの変更、承認元設定の変更も通常の差分判断で免除しません。

導入テンプレートは`pull_request_target`から、固定されたtoolとbaseだけを実行します。PRのコードをcheckout・実行せず、Git objectとして読みます。判定の前後にGitHub APIの現在のrepo・PR・base branch・base SHA・head SHAへ照合し、不一致・取得失敗は停止します。増やす権限は`pull-requests: read`だけです。

ローカルCLIだけでは指定したbaseの鮮度を保証しません。チェック完了後のbase変更や取消が過去の緑を自動更新する保証もありません。merge直前に最新状態で再検証し、必要なチェックの発行主体とbranch保護を別に確認します。

## 導入状態を区別する

コードと模擬署名の検査成功は、承認元の登録や利用先への反映成功ではありません。旧固定版からの移行、信頼設定の初回導入、実際の承認元の選択は、具体的な差分を示して人間が判断します。旧guardが自身の更新を止める初回移行を、自動で設定解除して通してはいけません。
