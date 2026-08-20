# 14. 調査履歴の検索と閲覧

「前に似た質問をしたはずだが、どのモデルで、どの条件で回したか思い出せない」を
なくすための機能。**結果だけでなく条件も検索できる**ことが要件。

## 14.1 なぜ payload の JSON だけでは足りないか

v1 の `runs` テーブルは `(id, payload)` の 2 列で、`payload` に `RunResult` を
JSON で丸ごと入れていた。一覧は `created_at` の降順に 50 件返すだけ。

これだと「qwen3:14b で回した、ヒト由来の、証拠検証に失敗したラン」を探せない。
JSON を全行ロードして Python で絞る手はあるが、ラン数が増えると線形に遅くなるうえ、
ファセット（絞り込みの選択肢）を出すのに毎回全件読むことになる。

そこで **payload は真実の source of truth のまま残し、検索したい値だけを列に射影する**。
列が壊れても payload から再構築できるので、スキーマ変更が怖くない。

## 14.2 射影する列

`backend/app/store.py::RUN_COLUMNS`。条件と結果を分けて持つ。

| 区分 | 列 | 用途 |
|---|---|---|
| 基本 | `question` `status` `created_at` `updated_at` | 一覧表示・並び替え |
| 条件 | `provider` `model` `mode` `organism` `context` `focus` `offline_mode` `policy_version` | 「どういう設定で回したか」の検索 |
| 結果 | `answer` `hypothesis_count` `unsupported_count` `evidence_verified` `evidence_failed` `step_count` `duration_sec` | 「どういう結果だったか」の検索 |
| 検索 | `search_text` | 全文の部分一致用 |

`policy_version` を条件に含めているのは、ポリシー（`config/resource_policy.yaml`）を
変えると同じ質問でも使えるデータソースが変わるため。**条件が変わったのに結果を
比較してしまう事故**を、あとから気づけるようにしておく。

`search_text` には次を小文字化して連結する。1 つの LIKE で横断できるようにするため:

- 質問文 / 回答本文
- 対象生物・背景・注目点（`organism` / `context` / `background` / `focus`）
- モデル名・プロバイダ・モード
- 各仮説の主張文
- 使ったリソース名（データセット・ツール）
- 証拠の識別子（PMID、アクセッション番号など）

つまり「rs2981582」や「GWAS Catalog」でも過去のランに当たる。証拠の識別子まで
入れているのは、**根拠から遡って探せる**ようにするため（§3 の証拠モデルの延長）。

## 14.3 既存 DB の移行

`_migrate()` は `PRAGMA table_info(runs)` で現在の列を見て、足りないものを
`ALTER TABLE ADD COLUMN` する。1 列でも足したら全行の `payload` を読み直して
`_extract(run)` の結果で `UPDATE` する。

- 破損した行は例外を握って**そのまま残す**（消さない）
- 追加のみなので、古いバイナリが新しい DB を読んでも壊れない
- `created_at DESC` / `provider` / `model` にインデックスを張る

## 14.4 検索 API

```
GET /api/runs?q=&provider=&model=&mode=&status=&organism=&since=&until=&limit=&offset=
```

- `q` は空白区切りの **AND**。各語が `search_text LIKE %語%` に落ちる
- `provider` `model` `mode` `status` `organism` は完全一致
- `since` / `until` は `created_at` の ISO 文字列を前方比較する（`2026-08` で月指定できる）
- 返り値は `{"runs": [...], "total": n, "facets": {...}}`

`facets` は**現在の絞り込みを適用したうえで**、各列に実際に存在する値と件数を返す。
0 件の選択肢を出さないため。UI のプルダウンはこれをそのまま描く。

```
DELETE /api/runs/{id}
```

実行中のランは消せない（409）。止めてから消す。

## 14.5 UI

「履歴」タブ 1 枚。上部に検索欄と 5 つのファセット、下にカード一覧。

- 入力は 250ms のデバウンス。1 文字ごとに叩かない
- カードには**条件（provider/model・モード・対象生物・文脈・注目点・オフライン）と
  結果（状態・仮説数・検証済/失敗の証拠数・手順数・所要時間・時刻）を必ず並べる**。
  結果だけ出すと「なぜこの結果になったのか」が分からず、比較の役に立たない
- カードをクリックすると `showResult(id)` で全タブを復元する。トレースも
  保存済みの `steps` / `prompt` から描き直すので、**あとからでも手順を追える**

## 14.6 やらないこと

- 全文検索インデックス（FTS5）は使わない。ラン数が 10^4 に届くまでは LIKE で足りる。
  必要になったら `search_text` を FTS5 の外部コンテンツテーブルに載せ替えるだけで済む
- 複数ランの差分ビューは v2。まず「条件込みで見つかる」ことを満たす
