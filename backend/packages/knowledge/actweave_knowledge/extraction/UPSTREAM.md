# Dify extraction source provenance

Upstream repository: https://github.com/langgenius/dify

Pinned commit: `9c16c865977e9d89a9ec7ae0536e893f4385a758`.

The table below was regenerated with `git show COMMIT:PATH` against the local
upstream checkout and SHA-256 of the exact bytes, then checked against the
controller's original-byte artifacts. Upstream originals stay in the task
artifacts; later tasks adapt only the files listed here, not the Dify backend.
`patches.md` records the per-file adaptation owner and intended changes.
Registry, processor, and signature preflight are ActWeave glue; they are not
claims that Dify's original admission policy has been copied unchanged.

| Original path | SHA-256 of original bytes |
| --- | --- |
| `api/core/rag/extractor/extractor_base.py` | `e4d53e3e08c753dcaea21be79ef8784f5100dc73977f8be06d27d0205e61e326` |
| `api/core/rag/extractor/helpers.py` | `a96871cdec723666dc84d429230c8d2f6aad63c04cf427c1e63ec8a1b5f3fd37` |
| `api/core/rag/extractor/text_extractor.py` | `05e79ac4d3b92a5958468c101a8fd80caf2bbf804dc4ce493760ca7d79091e7b` |
| `api/core/rag/extractor/markdown_extractor.py` | `91f27ab52f15618ae79410167ed66449b31365458e9f10e219c519d915d8f605` |
| `api/core/rag/extractor/pdf_extractor.py` | `7e3a1d97055ce6e2f797e4403469a891b5056d5c96f5cd838a2034e5d8e21331` |
| `api/core/rag/extractor/word_extractor.py` | `e918b5d3619e0a47181bbb484b1a04337ba31669004164d2d3ce78e1762b76d2` |
| `api/core/rag/extractor/excel_extractor.py` | `159fd312be101141f6d3b871a0df6d0988aac95da80d83514bf6b92ca23a01e1` |
| `api/core/rag/extractor/csv_extractor.py` | `6e68fabe3f5729d65e8cf528fcaa4867a299705925cc1aed980ce9cbced20d58` |
| `api/core/rag/extractor/html_extractor.py` | `d6ca2146886bf638b64edaf3e02cf18f117cd5499ea6065def2cd778fa4b844b` |
| `api/core/rag/extractor/unstructured/unstructured_pptx_extractor.py` | `dcf65ad47e81dacc3b23fc9d09f2db0b1335ea125a9484a7a254e5a94a1e7607` |
| `api/core/rag/extractor/unstructured/unstructured_epub_extractor.py` | `258e4506af08f02ea0dcfcb2773c3cff36c62853d6b717faf444cb69fc505d8a` |
| `api/core/rag/extractor/unstructured/unstructured_markdown_extractor.py` | `468ebf6c1746f50356ce1f88d26051b299cf4c35905adb17bad78cbac3de5184` |
| `api/core/rag/extractor/unstructured/unstructured_eml_extractor.py` | `ad99a11388ac45c1f87f4bfc1cbb2e6440585577dbeb414e6cfaead3e88a444a` |
| `api/core/rag/extractor/unstructured/unstructured_msg_extractor.py` | `07c46c40c3cb4a88fdb697fe876dd214c55c09f2be2f354a38bd297ab1ac6e6a` |
| `api/core/rag/extractor/unstructured/unstructured_xml_extractor.py` | `e470e5aeea9a57cc7860216afe52e33227820875baca07c4e6192b87628c9cf5` |
| `api/uv.lock` | `26698df595d66f8c7cb5a4983953eec18402b08bcc766e3db44e651d1131d1d1` |
| `LICENSE` | `232cf91474932d5110ed304e53b6b742a58463857c571fae803fdf2ac36d7bb3` |

## Dependency boundary

Exact direct versions are declared in `packages/knowledge/pyproject.toml`; the
workspace `uv.lock` locks the complete resolved dependency graph. The local
extra uses `unstructured[epub,md,pptx]`; it has no invented EML/XML extras and no
`all-docs` extra. Existing pypdf and ebooklib remain for the old ingestion path.
Pypdfium2 stays at the existing ActWeave 5.7.1 instead of downgrading to Dify's
5.6.0 candidate. No API service, runtime download, or source-code execution is
used as a fallback. Installing the local extra does not prove binary/resource
availability or offline parsing; P1-T7 and P1-T8 own those gates.

`extractor_version` includes the fixed upstream commit, `adapter-v1`, and the
canonical runtime manifest digest: installed parser dependency versions plus
actual parsing/NLP/native resource hashes and the local network policy revision.
Resources are compared against the checked platform entry before partition
imports. It excludes ingestion Tokenizer/ChunkProfile. P1-T7 provides the six
local adapters and the macOS resource entry; P1-T8 owns actual OS isolation, and
a Linux entry is not claimed until its independent build and sandbox gates pass.
See `patches.md` for the exact dependency source audit and provisioning steps.

## License notice and complete upstream LICENSE

Dify uses a **modified Apache License 2.0 with additional conditions**, not plain
Apache-2.0. The exact upstream LICENSE bytes are preserved in the following
block, including the multi-tenant condition and the frontend-specific notice.
Local development and source preservation do not establish that a particular
deployment is licensed. Deployment/licensing clearance remains a separate
review; this document does not resolve whether an intended service needs a
commercial license.

```text
# Open Source License

Dify is licensed under a modified version of the Apache License 2.0, with the following additional conditions:

1. Dify may be utilized commercially, including as a backend service for other applications or as an application development platform for enterprises. Should the conditions below be met, a commercial license must be obtained from the producer:

a. Multi-tenant service: Unless explicitly authorized by Dify in writing, you may not use the Dify source code to operate a multi-tenant environment.
    - Tenant Definition: Within the context of Dify, one tenant corresponds to one workspace. The workspace provides a separated area for each tenant's data and configurations.

b. LOGO and copyright information: In the process of using Dify's frontend, you may not remove or modify the LOGO or copyright information in the Dify console or applications. This restriction is inapplicable to uses of Dify that do not involve its frontend.
    - Frontend Definition: For the purposes of this license, the "frontend" of Dify includes all components located in the `web/` directory when running Dify from the raw source code, or the "web" image when running Dify with Docker.

2. As a contributor, you should agree that:

a. The producer can adjust the open-source agreement to be more strict or relaxed as deemed necessary.
b. Your contributed code may be used for commercial purposes, including but not limited to its cloud business operations.

Apart from the specific conditions mentioned above, all other rights and restrictions follow the Apache License 2.0. Detailed information about the Apache License 2.0 can be found at http://www.apache.org/licenses/LICENSE-2.0.

The interactive design of this product is protected by appearance patent.

© 2025 LangGenius, Inc.
```

## Verified Unstructured NLP resource prerequisite

Installed Unstructured 0.21.5 uses spaCy (not NLTK). Its
`unstructured/nlp/tokenize.py` fixes `en_core_web_sm` 3.8.0 and wheel SHA-256
`1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85`.
Upstream attempts to download/install the wheel when absent. The local adapters
instead require the official wheel installed at build time, verify actual model
and library bytes against the platform lock, then replace the exact NLP loader
with a local-load-only function. Missing resources or a load failure never reach
that installer. Actual OS network/filesystem isolation is independently gated
by P1-T8; build metadata alone is not its evidence.

## P1-T7 MSG fixture provenance

The public `python-oxmsg` fixture is copied byte-for-byte from
`scanny/python-oxmsg` commit `d0ee4645d4a8bf6d18517d33bc1f7dcb23e7620b`,
`tests/test_files/no-attachments.msg` (8,704 bytes), SHA-256
`44f38934c6bbad9ada1b00c09149e22749609c8d5ed2e6432a8a273718ec6a88`.
Its real parsed HTML body is `This is a message`. Exact upstream MIT license
bytes and per-file provenance are preserved beside the fixture under
`backend/tests/knowledge/fixtures/python-oxmsg/`. No MSG source fixture is fetched
at runtime; tests verify its SHA before parsing.

## P1-T4 XLS fixture provenance

Official `python-excel/xlrd` tag 2.0.1 resolves to commit
`b8d573e11ec149da695d695c81a156232b89a949`. Unmodified files and the exact
redistribution license are kept in `backend/tests/knowledge/fixtures/xlrd/`:

| Upstream path | Bytes | SHA-256 |
| --- | ---: | --- |
| `tests/samples/issue20.xls` | 6144 | `06be33a16f611910678c43808ce190bcae1e421ecca6be3b28c9c6724441ef4d` |
| `tests/samples/ragged.xls` | 6656 | `a144c284163641c2cb7dfc17d0116006aeffc3dff2b4878f039d4dbab6e2b07e` |
| `LICENSE` | 3771 | `b5a5dbce60265e305a815a6cb83ed07f24519d8ba644f2a307994488bced8815` |

The original source URLs are rooted at
`https://raw.githubusercontent.com/python-excel/xlrd/b8d573e11ec149da695d695c81a156232b89a949/`.
Fixtures are build/test assets, never runtime downloads.
