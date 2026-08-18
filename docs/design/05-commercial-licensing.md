# 05. 商用利用限定の設計

**方針: 本アプリは商用利用可能なリソースのみを使う。** データセット・LLM・ソフトウェアライブラリ・
外部 API のすべてに適用し、実行時に強制する。非商用ライセンスのリソースは
「設定で有効化できる」のではなく、**そもそも到達できない**ようにする。

> 免責: 以下はコードと Biomni 同梱の `license_info.md` を読んだ上での技術設計であり、法的助言ではない。
> 実運用前に各ライセンス原文の法務レビューを行うこと。本アプリは「レビュー対象を機械的に絞り込み、
> 使用実績を記録に残す」ことで、そのレビューを可能にする。

## 5.1 Biomni の `commercial_mode` — 必要だが十分ではない

`A1(commercial_mode=True)` を指定すると Biomni は以下を行う。

| 対象 | 挙動 |
| --- | --- |
| データレイク | `env_desc.py`（76 件）ではなく `env_desc_cm.py`（41 件）を使う |
| ライブラリ | 113 件 → 111 件（`PyLabRobot`, `nnunet` を除外） |
| ノウハウ文書 | メタデータの `commercial_use` が「❌ / Not Allowed / Non-Commercial」の文書を除去 |
| **ツール** | **フィルタされない**（全ツールが使える） |

### 除外される 35 データセット

COSMIC、MSigDB（全 9 コレクション）、OMIM、DisGeNET、BindingDB、DDInter（全 7）、
miRTarBase（全 4）、miRDB、McPAS-TCR、Enamine、EveBio（全 8）。

**これは仮説構築の実力に直結する。** 特に痛いのは：

| 失うもの | 影響 | 商用可の代替 |
| --- | --- | --- |
| MSigDB Hallmark / C2 / C5 | 経路エンリッチメント解析の主力遺伝子セット | `go-plus.json`（GO, CC BY 4.0）、Reactome API（CC0）、`mousemine_*_geneset`（CC BY 4.0, マウス）、WikiPathways（CC0）を追加検討 |
| OMIM / DisGeNET | 疾患–遺伝子関連の主力 | `query_opentarget`（Open Targets, CC0）、`query_monarch`、`query_clinvar`（NCBI, パブリックドメイン）、`gwas_catalog.pkl`（Apache-2.0） |
| BindingDB | 化合物–標的の結合親和性 | `query_chembl`（CC BY-SA 3.0, 帰属＋継承に注意）、`query_pubchem`（パブリックドメイン） |
| miRTarBase / miRDB | miRNA–標的 | 商用可の同等品が乏しい。**miRNA 仮説は v1 のスコープ外と明示する** |
| DDInter | 薬物相互作用 | `query_openfda`（パブリックドメイン）で部分代替 |

**設計判断**: 失った領域を隠さない。UI で「このクエリは商用モードで除外されたデータ領域に該当します」と
警告し、代替リソースを提示する。黙って弱い答えを返すより、限界を明示する方が研究用途では価値が高い。

### `commercial_mode=True` でも残る、要注意のデータセット

| データセット | ライセンス | 注意点 |
| --- | --- | --- |
| `proteinatlas.tsv` (HPA) | CC BY-SA 3.0 | **継承（ShareAlike）**。派生物の配布形態に制約が及ぶ可能性 |
| `affinity_capture-*`, `two-hybrid` 他 (BioGRID) | OSL 3.0 | コピーレフト系。SaaS 提供時の扱いを要確認 |
| `gtex_tissue_gene_tpm.parquet` (GTEx) | 集計値は公開、個票は dbGaP 管理アクセス | 本アプリが扱うのは集計値のみであることを明示 |
| `czi_census_datasets_v4.parquet` | CC BY 4.0 | 帰属表示が必要 |
| `DepMap_*` | CC BY 4.0 | 帰属表示が必要 |
| `broad_repurposing_hub_*` | CC BY 4.0 | 帰属表示が必要 |

→ **`commercial_mode` に依存しきらず、本アプリ側で独自の許可リストを持つ。**

## 5.2 リソースポリシーファイル（本アプリの追加レイヤ）

`config/resource_policy.yaml` を単一の情報源とし、起動時に読み込んで強制する。

```yaml
policy_version: 1
mode: commercial_only

datasets:
  # 明示的に許可したものだけがワークスペースに存在しうる
  allow:
    - name: DepMap_CRISPRGeneEffect.csv
      license: CC-BY-4.0
      attribution: "DepMap, Broad Institute"
      url: https://depmap.org/
    - name: gwas_catalog.pkl
      license: Apache-2.0
      attribution: "EBI GWAS Catalog"
    - name: go-plus.json
      license: CC-BY-4.0
      attribution: "Gene Ontology Consortium"
    # ...
  review_required:            # 取得はするが、UI で注意バッジを出す
    - name: proteinatlas.tsv
      license: CC-BY-SA-3.0
      note: "ShareAlike。出力の再配布形態を法務確認"
    - name: affinity_capture-ms.parquet
      license: OSL-3.0
      note: "コピーレフト系。SaaS 配布時に要確認"
  deny: "*"                   # 上記以外はすべて拒否（既定拒否）

tools:
  # commercial_mode はツールを絞らないため、ここで塞ぐ
  deny:
    - name: query_kegg
      reason: "KEGG は学術利用のみ無償。商用は要ライセンス契約"
    - name: query_scholar
      reason: "Google Scholar のスクレイピングは ToS 違反の懸念"
    - name: search_google
      reason: "同上。必要なら正規の Search API 契約に置換"
  review_required:
    - name: query_cbioportal
      reason: "収載元データセットごとにライセンスが異なる"
  allow: "*"                  # 上記以外は許可

libraries:
  deny:
    - PyLabRobot              # Biomni 側でも commercial_mode 時に除外
    - nnunet
  # GPL/AGPL のパッケージは環境構築時にライセンススキャンで検出（§5.4）

models:
  allow_licenses: [Apache-2.0, MIT, BSD-3-Clause]
  allow:
    - qwen3:14b
    - qwen3:32b
    - gpt-oss:20b
    - qwen2.5-coder:14b
  deny_note: "Llama Community License / Gemma Terms of Use は追加条項があるため既定で不可"
```

### 強制のかけ方（4 重）

1. **データ取得時**: `scripts/fetch_datasets.py` は `allow` + `review_required` にあるファイルしか S3 から取らない。
   `A1(expected_data_lake_files=<許可リスト>)` を渡し、Biomni の一括ダウンロードを封じる（§4.4）
2. **ツール登録時**: A1 構築後に `agent.tool_registry` と `agent.module2api` から `deny` のツールを削除し、
   システムプロンプトにも載らないようにする
3. **コード実行前**: `<execute>` のコードを静的検査し、拒否ツール名・拒否データセット名が現れたら
   実行せず `<observation>` にポリシー違反メッセージを返す（エージェントは代替手段を探して継続する）
4. **モデル選択時**: `/api/models` は Ollama の全モデルではなく、ポリシーの `allow` と交差した集合のみ返す

3 の「実行前ブロック」が最後の砦。1・2 をすり抜けても（例: LLM が直接 URL を叩くコードを書いた）ここで止まる。

## 5.3 ライセンス情報を出力に載せる

**根拠提示アプリなのだから、根拠のライセンスも出す。** `Resource` テーブルに `license` /
`attribution` / `commercial_ok` を持たせ（§03.1）、以下に反映する。

- 仮説カードの根拠チップに、ライセンスのバッジ（`CC BY 4.0` など）を表示
- 根拠ドロワーに帰属表示（attribution）と出典 URL
- エクスポートしたレポート末尾に **「使用データとライセンス」セクション**を自動生成。
  CC BY 系の帰属義務をここで機械的に満たす
- `review_required` のリソースを使ったランには、レポート冒頭に注意ブロックを出す

```markdown
## 使用データとライセンス

| リソース | 種別 | ライセンス | 帰属 | 商用 |
| --- | --- | --- | --- | --- |
| DepMap_CRISPRGeneEffect.csv | dataset | CC BY 4.0 | DepMap, Broad Institute | ✅ |
| Open Targets (query_opentarget) | tool | CC0 1.0 | Open Targets | ✅ |
| proteinatlas.tsv | dataset | CC BY-SA 3.0 | Human Protein Atlas | ⚠️ 継承条項 |
```

## 5.4 スタック全体のライセンス

| 構成要素 | ライセンス | 商用 |
| --- | --- | --- |
| Biomni 本体 | Apache-2.0 | ✅ |
| LangChain / LangGraph | MIT | ✅ |
| FastAPI / Uvicorn / SQLModel / Pydantic | MIT | ✅ |
| React / Vite / TanStack Query / Tailwind | MIT | ✅ |
| Ollama（ランタイム） | MIT | ✅ |
| LLM 重み | §5.2 の `models.allow_licenses` で限定 | ✅ |

**Biomni の E1 conda 環境（`biomni_env/setup.sh`）はフルインストールしない。**
生命科学系パッケージには GPL / 非商用のものが混ざるため、本アプリで実際に使うライブラリだけを
ピン留めした最小環境を作り、CI で `pip-licenses` によるライセンススキャンを回して
拒否リスト（GPL-3.0 / AGPL / 非商用）に該当したらビルドを落とす。

## 5.5 外部 API への送信

商用利用では「どのデータがどこへ出たか」の記録が要る。§02.6 のオフラインモードに加えて：

- ラン設定に `offline_mode`（外部通信を Ollama のみに制限）を持ち、既定値をユーザーが選べる
- オンライン時は、外部 API に送ったクエリ文字列を `Step` に記録し、レポートの
  「外部送信ログ」セクションに出す。機密性の高い課題を扱う際の監査材料になる
- 各外部 API の利用規約（レート制限・帰属・商用可否）を `resource_policy.yaml` の `tools` に
  `reason` として書き残し、レビューの起点にする
