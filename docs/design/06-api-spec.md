# 06. API 仕様

ベース URL: `http://localhost:8000/api`。認証なし（単一ユーザーのローカル運用前提、§01.3）。

## 6.1 エンドポイント一覧

| メソッド | パス | 用途 |
| --- | --- | --- |
| POST | `/runs` | ラン開始 |
| GET | `/runs` | ラン一覧（履歴） |
| GET | `/runs/{id}` | ラン詳細（ステップ・仮説・根拠を含む） |
| GET | `/runs/{id}/events` | **SSE**。ライブトレース |
| POST | `/runs/{id}/cancel` | 中断 |
| GET | `/runs/{id}/report` | レポート出力（`?format=md\|json`） |
| GET | `/runs/{id}/artifacts/{artifact_id}` | 図・生成ファイル取得 |
| GET | `/models` | 使用可能な LLM 一覧（ポリシー適用済み、§05.2） |
| GET | `/datasets` | データレイクの状態（取得済み / 未取得 / ライセンス） |
| POST | `/datasets/fetch` | 許可リスト内データセットの取得 |
| POST | `/uploads` | ユーザーデータ（CSV/TSV）アップロード |
| GET | `/policy` | 適用中のリソースポリシーの要約 |
| GET | `/health` | Ollama 接続・ワーカー状態・キュー長 |

## 6.2 ラン開始

```http
POST /api/runs
Content-Type: application/json

{
  "question": "トリプルネガティブ乳がんで PARP 阻害剤耐性を規定する因子の候補は？",
  "mode": "hypothesis",            // hypothesis | evidence_check | data_interpretation
  "model": "qwen3:14b",
  "options": {
    "temperature": 0.7,
    "num_ctx": 32768,
    "offline_mode": false,
    "use_tool_retriever": false,
    "max_steps": 60,
    "wallclock_limit_sec": 1800,
    "timeout_seconds": 600,
    "max_hypotheses": 5,
    "dataset_ids": ["upload_a1b2c3"]     // 任意。agent.add_data() で組み込む
  }
}
```

```http
202 Accepted
{ "run_id": "r_01HXYZ...", "status": "queued", "queue_position": 0 }
```

`mode` の違いはシステムプロンプトのテンプレートと Extractor のスキーマ：

| mode | 目的 | 出力の重み |
| --- | --- | --- |
| `hypothesis` | 新しい仮説を出す | 仮説 + 検証プラン |
| `evidence_check` | 既存の主張の裏付け・反証を集める | 支持/反証根拠の網羅性 |
| `data_interpretation` | アップロードデータの解釈 | データ由来の根拠を必須にする |

**バリデーション**: `model` はポリシー許可リスト内であること（外れたら 422）。
`options.dataset_ids` は既存アップロードであること。

## 6.3 SSE ストリーム

```http
GET /api/runs/{id}/events
Accept: text/event-stream
```

再接続に対応するため `Last-Event-ID` を受け付け、`seq` 以降を再送する。
イベントは DB に永続化してから配信するので、接続していなくても取りこぼさない。

### イベント種別

```
event: status
data: {"seq":1,"status":"running","phase":"retrieval"}

event: resources_selected
data: {"seq":2,"tools":["query_opentarget","query_gwas_catalog"],
       "datasets":["gwas_catalog.pkl","DepMap_CRISPRGeneEffect.csv"],
       "libraries":["gseapy"],"know_how":[]}

event: step
data: {"seq":3,"idx":0,"kind":"think","text":"まず GWAS Catalog から..."}

event: step
data: {"seq":4,"idx":1,"kind":"execute",
       "code":"from biomni.tool.database import query_gwas_catalog\n...",
       "tools":[{"name":"query_gwas_catalog","module":"biomni.tool.database"}],
       "datasets":["gwas_catalog.pkl"]}

event: step
data: {"seq":5,"idx":2,"kind":"observation","text":"rs2981582 FGFR2 p=2e-76 ...",
       "citations":[{"kind":"db_record","identifier":"rs2981582"}],
       "artifacts":[{"id":"a_1","kind":"image","mime":"image/png"}],
       "duration_ms":4210}

event: step
data: {"seq":6,"idx":3,"kind":"policy_blocked",
       "text":"query_kegg はポリシーによりブロックされました（商用ライセンス必要）"}

event: phase
data: {"seq":7,"phase":"extracting"}

event: hypotheses
data: {"seq":8,"hypotheses":[ /* §6.4 の Hypothesis[] */ ]}

event: verification
data: {"seq":9,"verified":35,"unverified":4,"failed":3}

event: done
data: {"seq":10,"status":"succeeded","duration_ms":412000}

event: error
data: {"seq":10,"status":"failed","error":"ollama connection refused",
       "hint":"OLLAMA_BASE_URL を確認してください"}
```

`kind` は `think | execute | observation | solution | policy_blocked | error`。

## 6.4 ラン詳細レスポンス

```jsonc
{
  "run": {
    "id": "r_01HXYZ",
    "question": "...",
    "status": "succeeded",
    "mode": "hypothesis",
    "llm_model": "qwen3:14b",
    "temperature": 0.7,
    "biomni_version": "0.0.8",
    "commercial_mode": true,
    "offline_mode": false,
    "policy_version": 1,
    "started_at": "2026-08-18T09:00:00Z",
    "finished_at": "2026-08-18T09:07:12Z"
  },
  "resources_considered": { "tools": [...], "datasets": [...], "libraries": [...] },
  "resources_used": [
    {"id":"res_1","kind":"dataset","name":"gwas_catalog.pkl","license":"Apache-2.0",
     "attribution":"EBI GWAS Catalog","commercial_ok":true,"step_idxs":[1,5]}
  ],
  "steps": [ /* §6.3 の step と同形 */ ],
  "hypotheses": [
    {
      "id": "h_1",
      "statement": "FGFR2 の発現上昇が TNBC における PARP 阻害剤耐性に寄与する",
      "rationale": "...",
      "confidence": "medium",
      "novelty": "emerging",
      "is_supported": true,
      "evidence": [
        {
          "id": "ev_1",
          "stance": "supports",
          "kind": "db_record",
          "resource_id": "res_1",
          "identifier": "rs2981582",
          "locator": "step:1",
          "excerpt": "rs2981582 | FGFR2 | breast carcinoma | p=2e-76",
          "claim_span": "FGFR2 の発現上昇",
          "why": "GWAS Catalog で FGFR2 座位が乳がんリスクと強く関連する",
          "verification_status": "verified",
          "strength": 0.7
        }
      ],
      "assumptions": ["..."],
      "test_plan": {
        "experiment": "...", "readout": "...", "controls": ["..."],
        "feasibility": "high", "estimated_effort": "3 週間"
      }
    }
  ],
  "unsupported_ideas": [ { "statement": "...", "note": "根拠が紐付かなかった着想" } ],
  "failed_citations": [
    {"identifier":"PMID:99999999","reason":"PubMed に存在しない","step_idx":8}
  ],
  "verification_summary": {"verified":35,"unverified":4,"failed":3},
  "licenses": [
    {"resource":"gwas_catalog.pkl","license":"Apache-2.0","attribution":"EBI GWAS Catalog"}
  ]
}
```

`failed_citations` を**レスポンスから隠さない**のが設計意図。何が捨てられたかが見えることが信頼につながる。

## 6.5 その他

### `GET /api/models`

```jsonc
{
  "models": [
    {"name":"qwen3:14b","size_gb":9.3,"license":"Apache-2.0","allowed":true,"loaded":true},
    {"name":"llama3.1:8b","size_gb":4.9,"license":"Llama Community",
     "allowed":false,"reason":"商用利用ポリシーにより不可"}
  ],
  "ollama": {"base_url":"http://ollama:11434","reachable":true,"version":"..."}
}
```

Ollama の `/api/tags` を叩いてポリシーと突き合わせる。**不許可モデルも理由付きで返す**
（黙って消すと「モデルが出てこない」という問い合わせになる）。

### `POST /api/uploads`

`multipart/form-data` で CSV / TSV / Parquet を受ける。保存後、ラン開始時に
`agent.add_data({filename: description})` でデータレイクに登録する。
`description` はユーザーが入力（リソース検索とプロンプトに使われるため、質の良い説明が精度に直結する旨を UI で伝える）。

### `GET /api/health`

```jsonc
{
  "api": "ok",
  "worker": {"status":"idle","running_run_id":null,"queue_length":0,"restarts":3},
  "ollama": {"reachable":true,"models_loaded":["qwen3:14b"]},
  "datasets": {"allowed":41,"present":12,"missing":29},
  "policy_version": 1
}
```

## 6.6 エラー設計

| HTTP | 状況 | ボディ |
| --- | --- | --- |
| 422 | モデルがポリシー違反 / 質問が空 | `{"error":"policy_violation","detail":"..."}` |
| 409 | 同一ランが実行中で新規受付不可（キュー上限） | `{"error":"queue_full","queue_length":5}` |
| 503 | Ollama 未到達 | `{"error":"llm_unavailable","hint":"ollama serve を起動してください"}` |
| 500 | ワーカークラッシュ | ランを `failed` にし、最後のステップまでは保持する |

**ラン失敗時も、そこまでのステップと抽出済み根拠は保存して見せる。**
15 分走った末に何も残らないのが最悪の体験。
