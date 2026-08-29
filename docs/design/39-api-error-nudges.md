# 39. 外部 API のエラーに、次の一手を添える

## 症状

ツールは名前で呼べるようになり、PubMed は実データを返すようになりました。
残ったのは UniProt です。

```
{'success': False,
 'error': 'API error: 400 Client Error ...&fields=function',
 'response_url_error': '{"messages":["Invalid fields parameter value \\'function\\'"]}'}
```

## 原因

`query_uniprot` は、自然言語のプロンプトから REST の URL を組み立てます。
その URL を書くのも LLM です。UniProt の**返却フィールド名は、画面の
見出しと違います**。モデルは画面の言葉（`function`、`sequences`、
`expression`、`gene_ontology`）で書き、400 が返ります。

クエリの連結も間違えていました。

```
query=gene_exact:IHH&organism_id:9606      ← & で繋いでいる
query=gene_exact:IHH+AND+organism_id:9606  ← 正しくは +AND+
```

## 直し方

エラー本文には、**どのフィールドが無効かが列挙されています**。
それを使って、次の一手を具体的に返します。

```
[api] UniProt rejected these `fields` values: function, sequences.
      The simplest fix is to DROP the `fields=` parameter entirely -
      UniProt then returns its default fields, which is enough.
      If you keep it, only these are safe: accession, id, protein_name,
      gene_names, organism_name, cc_function.
      Also join query terms with `+AND+`, not `&`.
```

**「`fields=` を丸ごと外せ」を最初に置くこと。** 対応表を長々と出すより、
必ず通る道を 1 本示すほうが速く、こちらが名前を間違える余地もありません。
挙げる安全な名前は、確実なものだけに絞ります。

`success: False` を含むがフィールドの話ではない場合は、一般の助言にします。

```
[api] The last tool call failed. Do not repeat the same call unchanged.
      Either simplify it (drop optional parameters) or use a different tool
      or a different database.
```

## エスケープで当たらなかった

最初の正規表現は `Invalid fields parameter value 'function'` を見ていました。
実際に届くのは入れ子の JSON なので、引用符がエスケープされています。

```
"Invalid fields parameter value \\'function\\'"
```

エスケープ無しだけを見ていると**本番のテキストには当たりません**。
利用者の観測をそのまま貼ったテストで踏みました。
助言のための正規表現は、必ず実物のテキストで確かめること。

## ここまでの並び

ツールを呼べるようにする（§38）→ 呼び方を直させる（§38 付記）→
**呼んだ先のエラーから次の一手を出す（ここ）**。
どれが欠けても、モデルは同じところで足踏みします。
