"""biomni_hypo — Biomni を使った仮説構築のコアパッケージ.

Jupyter ノートブックからの検証と FastAPI ワーカーからの実行が、
まったく同じコードパスを通るようにするための共有ライブラリ。
ノートブックにロジックを書かないこと。ここに書いてノートブックから import する。
"""

from biomni_hypo.version import __version__

__all__ = ["__version__"]
