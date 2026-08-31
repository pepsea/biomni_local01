# 44. 文献検索を PubMed だけにしない

## 指摘

> 論文検索は PubMed のみですか？ EuropePMC や Google Scholar も含めるべきでは？

そのとおりでした。**文献に関してだけ、§3 の「独立した情報源を複数当たる」が
成り立っていませんでした。**

## 現状の整理

biomni の文献ツールは 4 つあり、実際に使えるのは 1 つだけでした。

| ツール | 状態 | 理由 |
|---|---|---|
| `query_pubmed` | 使える | |
| `query_arxiv` | ほぼ役に立たない | 生物医学の主要誌を収載していない |
| `query_scholar` | **拒否** | Google Scholar に公式 API が無い |
| `search_google` | **拒否** | 同上 |

### Google Scholar を入れない理由

これは今回の判断ではなく、`config/resource_policy.yaml` に最初から
書いてあります。

```yaml
- name: query_scholar
  reason: Google Scholar のスクレイピングは ToS 違反の懸念
- name: search_google
  reason: 同上。必要なら正規の Search API 契約に置換
```

Google Scholar には公式 API がありません。`scholarly` は画面をスクレイプ
します。**商用利用を前提にしている以上、ToS の問題は避けられません。**
技術的にも、少し叩けば CAPTCHA で止まります。

正規の契約（Google Custom Search API など）を結ぶなら、その時点で
`deny` から外して差し替えられます。判断が変わったときに 1 行で戻せるよう、
理由ごと残してあります。

## Europe PMC を足した

biomni にはありません。自前で書きました（`biomni_hypo/extra_tools.py`）。

- 公開 REST API。**鍵が要りません。**
- PubMed の内容に加えて、**プレプリント（bioRxiv / medRxiv）・特許・書籍**を
  収載しています。PubMed に無い文献が引けます。
- 返すのは、タイトル・識別子（PMID / PMCID / DOI）・抄録。
  識別子が本文に現れないと根拠として検証できません（§3）。

### 登録の仕方

`agent.add_tool()` は使いません。あれは docstring を LLM に解析させるので、
モックのモデルでは落ちます。**スキーマを自分で書いて `module2api` に
直接入れます。** `agent.configure()` の**前**に入れること。後から入れると
システムプロンプトの一覧に載らず、案内されません。

ポリシーの判定も通します。足す側で素通しすると、拒否したはずのものが
裏口から入ります。

### 併せて直したところ

- **PMCID を引用として拾う**ようにしました。プレプリントは PMID を
  持たないことがあり、PMCID が唯一の識別子になります。
  リンク先は `https://europepmc.org/article/PMC/{id}`。
- **プロンプトの規則**に「文献は両方引くこと」を入れました。
  片方で 0 件でも、もう片方にはあることがあります。
- **落ちたときの代わり**（§39 の対応表）を
  `query_pubmed ⇄ query_europepmc` に繋ぎました。

## 確認

```
query_europepmc がプロンプトの一覧にある: True
名前空間で呼べる: True
システムプロンプトに載っている: True
REPL: True
```

API 応答をモックしたテストで、識別子が本文に出ること、PMID を持たない
プレプリントでも PMCID が残ること、長い抄録が切られること（§40）、
`max_papers=1000` でも 25 件で頭打ちになること、通信の失敗が例外ではなく
観測として返ることを見ています。

**スキーマと実物の署名がずれていないこと**も縛りました（§37 で踏んだ形を
自分のツールで繰り返さないため）。
