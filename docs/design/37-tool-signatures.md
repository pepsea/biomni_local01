# 37. 「そんな引数はありません」を捨てない

## 症状

```
Observation error: query_reactome() got an unexpected keyword argument 'max_result'
```

## 何が起きているか

スキーマは正しく、モデルの捏造でした。確かめました。

```
$ read_module2api() の query_reactome
required_parameters: prompt
optional_parameters: endpoint, download, output_dir, verbose
```

`max_result` はどこにもありません。にもかかわらずモデルが付けるのは、
**biomni のツールは名前が似ているのに引数が揃っていない**ためです。
`max_result` を持つツールの書き方を、持たないツールにも当ててしまいます。

エージェントは観測としてこの `TypeError` を受け取るので、理屈の上では
自分で直せます。しかし返ってくるのは「そんな引数は無い」だけで、
**では何ならあるのか**が書かれていません。ローカルモデルは同じ呼び方を
繰り返し、1 ステップずつ捨てていきます。

## 直し方

観測に「そんな引数は無い」が出たら、**実物の署名**を添えて言い直させます。
`FormatReminderLLM`（形式の念押しと深さの押し戻しを入れている包み）に
もう 1 つ足すだけで済みます。biomni 本体には触りません。

```
[tool] `query_reactome` has no parameter `max_result`.
       Its parameters are: prompt, endpoint, download, output_dir, verbose.
       Call `query_reactome` again with only those parameters.
       Do not guess parameter names.
```

引数は `inspect.signature` で**関数そのもの**から取ります。
スキーマ（module2api）はたいてい正しいのですが、最終的に呼ばれるのは
関数なので、食い違ったら関数が正しいからです。

署名を引けないもの（そもそも存在しない関数名）には何も言いません。
当てずっぽうの助言は、間違った方向に押すだけです。

## 教訓

**エラーは「何が駄目か」しか言いません。「では何ならよいか」は、
こちらが足せます。** 足せる情報を持っているのに黙っていると、
モデルは同じ失敗を繰り返します。§30・§31 と同じ形です。
