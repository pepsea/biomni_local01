# 43. 集めた情報へのリンク

## 要望

> 集めた情報へのリンクがほしい

もっともです。**根拠を示すとは、識別子を書くことではなく、辿れるように
すること**です（§3）。`PMID:37821999` とだけ書いて「あとは検索してください」
では、示したことになりません。

## 何が足りなかったか

URL は最初から持っていました。`Evidence.url` も `Resource.url` もあり、
`citations.py` に組み立ての表もあります。

```python
url_template="https://pubmed.ncbi.nlm.nih.gov/{id}/"
url_template="https://www.uniprot.org/uniprotkb/{id}"
url_template="https://reactome.org/content/detail/{id}"
```

**画面に出していなかっただけ**です。根拠チップはボタンで、URL は
チップを押して開くドロワーの中にだけありました。一覧から直接は飛べません。

## 直したところ

### 根拠チップの隣に外部リンク

```
[✓ PMID:37821999] ↗
```

`↗` を**ボタンの外**に置きます。ボタンの中に `<a>` を入れると
入れ子の操作要素になり、どちらを押したのか分からなくなります。

### 「集めた情報」タブに一覧表

チップに加えて表を出します。識別子そのものがリンクで、
「開く ↗」の列からも飛べます。URL が無いものは「リンクなし」と明示します
（黙って空欄にすると、リンクを出し忘れたのか元々無いのか分かりません）。

| 識別子 | 種別 | 検証 | ステップ | リンク |
|---|---|---|---|---|
| [PMID:37821999](https://pubmed.ncbi.nlm.nih.gov/37821999/) | literature | verified | 3 | 開く ↗ |
| [P11362](https://www.uniprot.org/uniprotkb/P11362) | db_record | verified | 5 | 開く ↗ |

使用したデータ・ツールの表も、名前がそのままリンクになります。

### Markdown レポート

論点の根拠、使用リソース、検証に失敗した引用 ── いずれも
`[識別子](URL)` にしました。失敗した引用こそ辿れたほうが直しやすい。

## 確認

保存済みのランを実ブラウザで開き、リンク先を読み出しました。

```
GWAS Catalog ↗    https://www.ebi.ac.uk/gwas/                        target=_blank
PMID:37821999     https://pubmed.ncbi.nlm.nih.gov/37821999/          target=_blank
P11362            https://www.uniprot.org/uniprotkb/P11362           target=_blank
R-HSA-190236      https://reactome.org/content/detail/R-HSA-190236    target=_blank
```

すべて別タブで開きます。調査中の画面を失わないためです（§29）。

## 教訓

**持っているのに出していないデータは、無いのと同じです。**
URL は最初からありました。ドロワーの中という「1 手先」に置いただけで、
利用者からは無いのと変わりませんでした。
