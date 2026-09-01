# XLS regression fixtures

These unmodified fixtures come from the public `python-excel/xlrd` repository,
tag `2.0.1`, resolved to commit `b8d573e11ec149da695d695c81a156232b89a949`.
The copied [LICENSE](LICENSE) preserves the upstream copyright, redistribution
conditions and disclaimers. They are test data, not runtime dependencies.

| File | Original source | SHA-256 | Regression purpose |
| --- | --- | --- | --- |
| `issue20.xls` | [pinned original](https://raw.githubusercontent.com/python-excel/xlrd/b8d573e11ec149da695d695c81a156232b89a949/tests/samples/issue20.xls) | `06be33a16f611910678c43808ce190bcae1e421ecca6be3b28c9c6724441ef4d` | Empty physical rows must not renumber later values. |
| `ragged.xls` | [pinned original](https://raw.githubusercontent.com/python-excel/xlrd/b8d573e11ec149da695d695c81a156232b89a949/tests/samples/ragged.xls) | `a144c284163641c2cb7dfc17d0116006aeffc3dff2b4878f039d4dbab6e2b07e` | An empty header cell must not hide data in that column. |
| `LICENSE` | [pinned original](https://raw.githubusercontent.com/python-excel/xlrd/b8d573e11ec149da695d695c81a156232b89a949/LICENSE) | `b5a5dbce60265e305a815a6cb83ed07f24519d8ba644f2a307994488bced8815` | Required upstream notices. |

The current adapter tests use the separately locked xlrd runtime version. The
fixture version does not select or change the production parser dependency.
