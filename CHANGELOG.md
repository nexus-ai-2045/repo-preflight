# Changelog

## 0.2.0 - 2026-08-01

### 追加

- release準備時にREADMEの短さ、理解順序、Quickstart、制約、見出し階層を検査するread-onlyゲート。
- `readiness_scan.py --release` からREADME設計ゲートを自動実行する経路。
- READMEの不足内容に応じて必要なデザインskill/pluginだけを提案するrouting。
- 公開repositoryのruleset、merge方式、Actions権限、security機能を選ぶための設定ガイド。

### 改善

- secret scanの検出結果に秘密値そのものを表示しない回帰防止。
- release gateのpassと、人間レビュー・release承認を別状態として維持。

## 0.1.0 - 2026-07-22

- Gitリポジトリの公開準備をread-onlyで検査する初回release。
