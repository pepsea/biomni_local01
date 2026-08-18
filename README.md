# biomni_local01 — Biomni 仮説構築 Web アプリ

生物医学の研究課題を入力すると、[Biomni](https://github.com/snap-stanford/biomni) エージェントが
文献・公共データベース・データレイクを調べ、**検証可能な仮説**と**その根拠（使用データ・出典・実行過程）**を
セットで返す Web アプリケーション。

LLM はまず **Ollama（ローカル実行）** で動かす。API キー不要・データがローカルから出ないことを初期要件とする。
また、**商用利用可能なリソースのみを使う**（データセット・モデル重み・ライブラリ・外部 API のすべて）。

## いまの状態

設計フェーズ。実装コードはまだ無く、`docs/design/` に設計書一式がある。

## 設計書

| ドキュメント | 内容 |
| --- | --- |
| [01-overview.md](docs/design/01-overview.md) | 目的・スコープ・ユースケース・非機能要件 |
| [02-architecture.md](docs/design/02-architecture.md) | システム構成・コンポーネント・実行シーケンス |
| [03-evidence-model.md](docs/design/03-evidence-model.md) | **根拠モデル**（データモデル・抽出・検証） |
| [04-ollama-integration.md](docs/design/04-ollama-integration.md) | Ollama 統合と Biomni 側の既知の落とし穴 |
| [05-commercial-licensing.md](docs/design/05-commercial-licensing.md) | **商用利用限定**の設計（リソースポリシー） |
| [06-api-spec.md](docs/design/06-api-spec.md) | REST / SSE API 仕様 |
| [07-ui-design.md](docs/design/07-ui-design.md) | 画面設計 |
| [08-roadmap.md](docs/design/08-roadmap.md) | フェーズ計画・受け入れ基準・リスク |

まず [01-overview.md](docs/design/01-overview.md) → [02-architecture.md](docs/design/02-architecture.md) →
[03-evidence-model.md](docs/design/03-evidence-model.md) の順に読むのがおすすめ。

## 設計の要点（3行）

1. Biomni A1 エージェントの実行トレース（生成コード・実行結果・呼ばれたツール）を**構造化して保存**し、仮説とトレースを紐付ける。
2. 仮説の生成（探索）と JSON 抽出（構造化）を**別フェーズに分離**し、ローカル LLM でも壊れにくくする。
3. 抽出した引用（PMID / DOI / アクセッション / ファイル）は**実在検証を通してから**表示する。幻覚引用を UI に出さない。
4. 商用利用は `A1(commercial_mode=True)` だけでは不十分なため、独自のリソースポリシーで**既定拒否**を敷く（[05](docs/design/05-commercial-licensing.md)）。
