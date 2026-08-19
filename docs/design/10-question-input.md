# 10. 調べたいことの入力

`biomni_hypo/question.py`。「何を調べたいか」を構造化して受け取り、
エージェントへ渡すプロンプトを組み立てる層。

## 10.1 なぜ自由記述だけにしないか

A1 のシステムプロンプトは 16k トークン前後（04 §4.5）ある。そこに
「TNBC の耐性について」のような一文を足しても、エージェントは何から手を付けるか決められず、
最初の数ステップを無駄にする。ローカルモデルではそのまま迷子になる。

対象・生物種・知りたい関係を埋めさせるだけで探索の初手が安定するので、
**入力を構造化して、埋まっていない項目を指摘する**。

## 10.2 入力モデル

| 項目 | 必須 | 用途 |
| --- | --- | --- |
| `text` | ✅ | 調べたいこと（自由記述） |
| `mode` | | `hypothesis` / `evidence_check` / `data_interpretation` |
| `organism` | | 生物種。無関係な種のデータを拾いにくくする |
| `context` | | 疾患・組織・細胞株・条件 |
| `focus` | | 注目する遺伝子・経路・薬剤 |
| `background` | | 既に分かっていること。同じ調査の繰り返しを避ける |
| `exclude` | | 避けたい方向性 |
| `dataset_ids` | | 解析する自前データ |
| `max_hypotheses` | | 仮説の上限 |

### モードごとにプロンプトの骨格が変わる

| mode | 課題文 | 期待する出力 |
| --- | --- | --- |
| `hypothesis` | 「この問いを調査し、検証可能な仮説を N 件提案せよ」 | 仮説 + 検証プラン |
| `evidence_check` | 「この主張の支持根拠と反証根拠を集めよ」 | 両論。データを見る前に結論を決めない指示を入れる |
| `data_interpretation` | 「このファイルを読み込んで解析し、解釈を N 件出せ」 | ファイル中の具体的な行・統計量を根拠にすることを必須にする |

どのモードでも共通の「守ること」を末尾に付ける。

```
- Ground every claim in data you actually retrieved or computed in this session.
- Prefer querying public databases and the local data lake over recalling facts from memory.
- When you cite a paper or a database record, make sure the identifier appears in the output of code you ran.
- Report contradicting evidence as well; do not only collect support.
```

3 番目が §03 の根拠モデルと直結する。トレースに現れない識別子は検証で落ちるので、
**最初から「実行結果に出てくるものだけを引け」と言っておく**ほうが歩留まりがよい。

### 指示文は英語、ユーザーの記述はそのまま

A1 のシステムプロンプトもツール説明も英語なので、指示の枠組みを英語に揃えるほうが
ローカルモデルの追従が安定する。ユーザーが書いた文は**翻訳せずそのまま埋め込む**
（訳した時点で意図がずれる）。`HYPO_PROMPT_LANGUAGE=ja` で日本語の骨格にも切り替えられる。

最終的な仮説 JSON は Extractor が日本語プロンプトで抽出するので、出力は日本語で返る。

## 10.3 入力の検査（Hint）

| 重大度 | 例 | 挙動 |
| --- | --- | --- |
| `error` | 課題が短すぎる / データ解釈モードなのにデータ未指定 | **実行させない**（API は 422） |
| `warning` | 生物種が空 / 対象も注目対象も空 / 注目対象が多すぎる | 実行はする。指摘は残す |
| `info` | 商用モードで弱い領域に触れている（§10.4） | 実行はする。代替を提示する |

`error` を実行前に止めるのは、15 分走った末に「入力が悪かった」と分かるのが
いちばん無駄だから。

## 10.4 商用モードで弱い領域を、実行前に知らせる

05 §5.1 の「除外領域を隠さない」を入力側で実装したもの。
質問文・対象・注目対象を走査して、商用モードで除外されたデータに依存しがちな
話題に触れていたら、限界と代替を先に出す。

| 話題 | 使えないもの | 代替 |
| --- | --- | --- |
| 遺伝子セット解析 / GSEA | MSigDB | GO、Reactome、mousemine |
| 希少疾患・遺伝性疾患 | OMIM、DisGeNET | Open Targets、ClinVar、GWAS Catalog、Monarch |
| miRNA-標的 | miRTarBase、miRDB | 商用可の同等品が乏しい（文献ベースになる） |
| 薬物相互作用 | DDInter | openFDA で部分代替 |
| 結合親和性 | BindingDB | ChEMBL、PubChem |

黙って浅い答えを返すより、限界を先に見せるほうが研究用途では価値がある。

## 10.5 3 つの入口

```
scripts/ask.py           ─┐
Web UI (/)               ─┼─> ResearchQuestion ─> to_prompt() ─> TracingRunner
notebooks/04             ─┘         │
                                    └─ hints() ─> 実行前に止める / 警告する
```

| 入口 | 特徴 |
| --- | --- |
| Web UI (`/`) | 依存なしの 1 ファイル。モード選択・テンプレート・モデル選択・SSE トレース |
| CLI (`scripts/ask.py`) | 対話入力と引数指定の両方。`--dry-run` でプロンプトだけ確認 |
| ノートブック | `ResearchQuestion` を直接組み立てる |
| API | `POST /api/question/preview` で確認 → `POST /api/runs` |

いずれも **実行前にプロンプトを見られる**。何を投げたか分からないまま結果だけ出てくる、
という状態を作らない。組み立てたプロンプトは `RunResult.prompt` に残り、レポートにも載る。

## 10.6 テンプレート

`GET /api/question/templates` が返す。入力欄の初期値になる。

| id | 用途 | mode |
| --- | --- | --- |
| `resistance` | 治療抵抗性の機序を探す | hypothesis |
| `gene_disease` | 遺伝子と疾患の関連を調べる | hypothesis |
| `target` | 創薬標的の妥当性を評価する | evidence_check |
| `deg` | 発現変動遺伝子リストを解釈する | data_interpretation |
| `claim` | 論文の主張を検証する | evidence_check |
