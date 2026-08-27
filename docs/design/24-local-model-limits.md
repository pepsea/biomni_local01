# 24. ローカルモデルが苦手な場所を切り分ける

## 24.1 症状

```
0 execute      from biomni.tool.database import query_uniprot
1 observation  {'success': False,
                'error': 'API error: 400 ... rest.uniprot.org/uniprotkb/search
                          ?...&fields=function,pathway,organism_id,protein_name',
                'response_url_error': '{"messages":[
                    "Invalid fields parameter value \\'function\\'",
                    "Invalid fields parameter value \\'pathway\\'"]}'}
```

LLM 呼び出しは**成功しています**（§21.18 の 400 とは別物）。
生成された URL のフィールド名を、UniProt が拒否しました。

## 24.2 スキーマは渡っていた

`query_uniprot` は `schema_db/uniprot.pkl` をシステムプロンプトに埋め込みます。
中身を確認したところ:

```
スキーマの大きさ: 7,877 文字（約 2,250 トークン）
  'cc_function'  がスキーマに載っているか: True
  'cc_pathway'   がスキーマに載っているか: True
  'protein_name' がスキーマに載っているか: True
```

**正しいフィールド名は渡っていました。** ローカルモデルがそれを無視して、
`function` / `pathway` という「それらしい名前」を書きました。

biomni の不具合でも、プロンプトの不備でもありません。
**モデルがスキーマに従えなかった**、それだけです。

## 24.3 どこが効くかは一様ではない

このアプリがモデルにさせていることは 3 種類あり、要求が違います。

| 用途 | 要求 | 小さいモデルの成否 |
|---|---|---|
| エージェント本体（何を調べるか決める） | 方針判断・タグ規約 | だいたい通る |
| **DB ツールの URL 生成** | **スキーマ厳守**（1 文字違えば 400） | **落ちやすい** |
| 仮説抽出（JSON 化） | スキーマ厳守 + 根拠 ID の制約 | 落ちやすい |

「Ollama だとうまくいかない」の実体は、**真ん中の行**です。
本体は動いているのに、外部データが取れないので根拠が集まらず、
最後にはエージェントが自分の記憶で書き始めます（§20.1）。

## 24.4 対処: 落ちやすい 1 か所だけ寄せる

抽出には既に `HYPO_EXTRACTOR_MODEL` があります。同じ考えを DB ツールにも。

```
HYPO_TOOL_QUERY_MODEL=claude-sonnet-5
```

- **エージェント本体はローカルのまま。** 実行の大半はローカルで回ります
- URL を作る一瞬だけ、指示追従の強いモデルを使います
- 空なら従来どおり（エージェントと同じモデル）

実装は `patch_biomni_get_llm()` の中です。あの経路は既に
`_query_llm_for_api` を横取りしているので、モデル名を差し替えるだけで済みます。

```
既定（未設定）                    → ChatOllama     qwen3:14b
HYPO_TOOL_QUERY_MODEL=claude-…    → ChatAnthropic  claude-sonnet-5
同上・オフラインモード             → ChatOllama     qwen3:14b
```

### 既定では有効にしない

質問由来の語（遺伝子名・疾患名）が Anthropic に送られます。
**黙って外に出さない。**明示的に設定したときだけ使います。

そして **`HYPO_OFFLINE_MODE=true` なら、設定されていても無視します。**
「質問文を一切外部に出さない」という約束のほうが上位です。
`tool_query_model_name` がその判断を 1 か所に持っています。

## 24.5 それでもローカルだけで通したいなら

| 手 | 効果 |
|---|---|
| 大きいモデル（qwen3:32b など） | スキーマ追従がかなり上がる |
| ポリシー許可の中から選ぶ | `make models` で確認（Apache-2.0 / MIT のもの） |
| DB ツールを使わない問い方にする | データレイクのファイル解析中心にする |
| 失敗を許容する | 400 は観測として戻るので、エージェントは別の手を試せる |

最後の行は重要です。**1 つのツールが落ちてもランは続きます。**
問題は、全部のツールが落ちて手が無くなったときです。

## 24.6 やらないこと

**フィールド名をこちらで補正する**（`function` → `cc_function` と書き換える）
ことはしません。UniProt だけでも数十のフィールドがあり、OpenTargets・
Ensembl・ClinVar と DB ごとに規約が違います。補正表を持てば、
**biomni と UniProt の両方の更新に追従し続ける**責任を負うことになります。
モデルを替えれば済む問題に、恒久的な保守を持ち込む価値はありません。
