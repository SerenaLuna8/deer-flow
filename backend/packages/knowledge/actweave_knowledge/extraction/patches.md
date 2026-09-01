# Dify adapter patch matrix

The authoritative upstream identity and complete modified license are in
[UPSTREAM.md](UPSTREAM.md). Original paths below are relative to
`api/core/rag/extractor/`; destination paths are relative to this directory.
The P1 adapters below are implemented; the focused test files name their local
verification boundaries. Deployment, database integration and production Linux
isolation remain separate gates. Comparable upstream classes/functions are
retained where compatible with the approved local contracts.

| Original | Local destination | Implemented adaptation | Owner / verification node or gate |
| --- | --- | --- | --- |
| `extractor_base.py` | `base.py`, `contracts.py` | Replace Dify Document with immutable local DTOs; inject local setting/context/sink; no host import. | P1-T1 / `test_extraction_contracts.py` |
| `helpers.py` | `encoding.py`, `normalizer.py`, `tabular.py`, `images.py` | Move only required helper logic; preserve source offsets and safe image bytes. No generic Dify host utilities. | P1-T3/T4/T5 / encoding, normalization, table and image focused gates |
| `text_extractor.py` | `dify/text_extractor.py` | Remove host imports; bounded encoding detection and strict decoding. | P1-T3 / text and encoding gate |
| `markdown_extractor.py` | `dify/markdown_extractor.py` | Preserve Markdown literal angle brackets, fenced/inline code and MDX source; execute nothing; do not fetch remote images. | P1-T3 / Markdown literal preservation gate |
| `pdf_extractor.py` | `dify/pdf_extractor.py` | Remove Dify cache/storage/database binding; P2 owns cache. Use injected image sink, retain page spans; no OCR or image-text substitution. | P1-T6 / PDF pages, images and no-indexable-text gate |
| `word_extractor.py` | `dify/word_extractor.py` | Remove host file/storage imports; retain body order, nested-table order and significant spaces; image occurrences use the injected sink. | P1-T6 / Word mixed/nested-order and image gate |
| `excel_extractor.py` | `dify/excel_extractor.py` | Retain empty columns and original sheet/row/column positions; deterministic per-sheet headers. XLS and XLSX have separate image capabilities. | P1-T4 / Excel field-binding, location and XLS gate |
| `csv_extractor.py` | `dify/csv_extractor.py` | Keep strings/leading zeros, reject malformed rows, bounded encoding detection; first ten rows only for automatic header selection. | P1-T4 / CSV string, bad-row and header gate |
| `html_extractor.py` | `dify/html_extractor.py` | Remove active HTML safely while keeping structure/text; forbid URL fetching, scripts and external images. | P1-T3 / HTML structure and no-fetch gate |
| `unstructured/unstructured_pptx_extractor.py` | `unstructured_local/unstructured_pptx_extractor.py` | Remove partition API branch; retain elements without page_number with truthful location/warning instead of discarding them. No invented page number. | P1-T7 / actual element metadata, missing-page and offline gates |
| `unstructured/unstructured_epub_extractor.py` | `unstructured_local/unstructured_epub_extractor.py` | Remove API/download_pandoc and Dify segmentation config; use verified bundled Pandoc and actual chapter/element metadata. | P1-T7 / EPUB offline-resource and chapter gates |
| `unstructured/unstructured_markdown_extractor.py` | `unstructured_local/unstructured_markdown_extractor.py` | Local partition only, preserve source structure and literals; no second-stage title chunking. | P1-T7 / local Markdown/literal gates |
| `unstructured/unstructured_eml_extractor.py` | `unstructured_local/unstructured_eml_extractor.py` | Local partition only; do not Base64-decode already decoded body text a second time. | P1-T7 / actual email element and repeated-decoding gates |
| `unstructured/unstructured_msg_extractor.py` | `unstructured_local/unstructured_msg_extractor.py` | Local partition and OLE stream validation; remove API path and repeated body decoding. | P1-T7 / MSG OLE/email and offline gates |
| `unstructured/unstructured_xml_extractor.py` | `unstructured_local/unstructured_xml_extractor.py` | Local partition only; forbid external entities/network loads and preserve actual metadata. | P1-T7 / XML external-entity and offline gates |

## ActWeave admission differences (implemented in P1-T2)

`registry.py` is the unique datasource/ETL/extension mapping and capability
source; unsupported dimensions/extensions fail closed. PPTX and EPUB remain
registered in both modes to retain ActWeave's existing format surface. Dify's
API-based DOC/PPT paths are excluded. ODT is unsupported. Missing optional
local dependencies never trigger text parsing or an API fallback.

`processor.py` validates the frozen parser identity/version before dependency
probing, source size, signature and factory invocation. It checks cumulative
returned text size, does not tokenize or split, and uses the original filename
extension even when the authorized staging path has no suffix. Resource quota
failures retain `KNOWLEDGE_QUOTA_EXCEEDED`; format failures use safe parse reason
codes. Parser implementations must also enforce budgets while accumulating
content; this final check alone does not bound peak memory.

`signatures.py` validates PDF/OLE magic, OOXML actual main-part declarations and
EPUB identity, rejects unsafe ZIP member paths, symlinks, duplicate names and
encryption, and checks cumulative declared expansion before format-library
loading. Text-format admission scans for binary NULs (except BOM-marked UTF-16)
and rejects ZIP/OLE/PDF identities. Preflight never unpacks members. It does not
prove a correct OLE stream layout, a valid full document, or a process memory
ceiling; the format libraries and P1-T8 runtime enforce those later boundaries.

P1-T2 focused nodes live in `tests/knowledge/test_extractor_registry.py`, covering
unique routes, long-tail local-only behavior, missing dependency/profile
identity rejection, signature/container identity, ZIP path/symlink/expansion,
and source/text budget ordering. These are admission tests, not format parsing
tests.

## Dependencies and platform evidence

The fixed Dify API lock is provenance, not ActWeave's dependency graph.
Pypdfium2 stays at the installed 5.7.1; pypdf and ebooklib remain for the legacy
profile pending P3 compatibility review. `extraction-local` installs
Unstructured 0.21.5 with epub/md/pptx extras, python-oxmsg, pypandoc-binary,
python-magic and the pinned official spaCy model wheel. No all-docs or invented
extras are used. Hatch explicitly permits the pinned direct wheel reference.

Capability probes now check package versions and actual resource hashes against
`resources.lock.json`, before lazy partition imports. The parser version is
`<dify-or-unstructured_local>:<full-upstream-commit>:adapter-v1:<runtime-digest>`.
The runtime digest includes installed dependencies, parsing/NLP/native resources
and the exact local network policy revision. It excludes ingestion Tokenizer
and ChunkProfile. Missing resources, load prerequisites or unverified platforms
remain unavailable; no old parser/API/runtime-download fallback exists.

## P1-T1/T3 — contracts, source fidelity and shared image recognition

- Frozen DTOs and stable canonical manifest encoding are ActWeave contracts,
  not imported Dify model/host code. `SourceSpan` offsets address the current
  output string; `context_prefix` denotes generated context, not original text.
  Manifest validation checks strict canonical image syntax, logical SHA refs,
  exact `(ref,start,end)` occurrence multiplicity and exact inventory closure.
  Paths, host IDs and authority are excluded from persistent metadata.
- `TextExtractor` retains upstream load-to-Document flow. Shared bounded
  decoding replaces constructor flags and duplicate retry logic. Original
  whitespace and source line/encoding spans survive; errors disclose no paths.
- `encoding.py` retains charset-normalizer best-candidate selection, but calls
  `from_bytes` on at most 1 MiB instead of unbounded `from_path`. It restores
  `SIGALRM`/`ITIMER_REAL` after a 5-second detection budget rather than waiting
  on executor shutdown. BOM takes precedence; full-file decoding is strict.
- Dify Markdown retains `extract -> parse_tups -> markdown_to_tups` and its
  incremental line/current-header/fence organization. It removes global hash
  deletion, angle-tag regex deletion, stripping and synthetic ancestor copying.
  ATX headings apply outside fences; opening/closing fence character and length
  must match. MDX/code is inert; source text and heading paths are not flattened.
- The shared `markdown_images.py` source locator is used by both normalization
  and manifest closure. CommonMark block maps restrict inline parsing to real
  inline regions, and map de-indented list/blockquote text back to physical
  offsets, including tabs. Exact code-span delimiter/escape behavior prevents
  code examples from substituting for actual logical image occurrences.
  Fenced/indented code, raw HTML and bounded lexical MDX expressions/attributes
  are protected; braces inside separate code spans cannot hide an intervening
  real image. Unmappable source positions fail closed.
- Literal masks determine whether an image rule may start; the entered image
  rule reads original unmasked labels/destinations and restores masking in
  `finally`. Thus `<IP>`, `{topology}` and original bracket syntax survive,
  without allowing invalid bracket forms to manufacture images. Normalization
  adjusts source/attachment offsets only for actual edits, preserving idempotence.
- HTML retains the original bytes -> BeautifulSoup `html.parser` seam, but
  replaces `get_text()/strip` with explicit safe Markdown structures. Nested
  lists/quotes/wrappers preserve paragraph/code boundaries. Inline siblings
  concatenate before block-boundary trimming so emphasis/span/link edge spaces
  do not fuse words. Active elements/events/scripts are removed; external
  images become positioned placeholders, and URLs are never fetched.

## P1-T4 — CSV/Excel field integrity and image-loss recovery

CSV retains `extract -> _read_from_file -> row Documents`, replacing pandas
`on_bad_lines='skip'`, type/NA inference and trimming with strict stdlib rows
and the shared tabular builder. Strings, leading zeros and empty physical
columns remain bound to their actual row/column; header auto-selection examines
at most ten rows, and explicit/headerless rules never invent source headers.

Excel retains extension dispatch, openpyxl non-read-only workbook/sheet/cell
traversal, hyperlink handling, anchor discovery and `_data()` bytes loading.
Host/session/storage/file-key code and traceback logging are removed. Shared
row binding replaces filtered-column headers and pandas XLS `dropna`; actual
sheet/row/column locations, raw headers and formula-cache warnings survive.
Workbook closure is in `finally`, and every image anchor becomes a distinct
occurrence delivered to the sole normalization sink. XLS has no embedded-image
capability; these adapters no longer need pandas even though its existing
locked dependency remains.

Known openpyxl image-drop warnings are captured without leaking media paths.
DrawingML relationships/anchors are inspected without decoding the media to
recover the actual location of a corrupt image. A recoverable corrupt image
gets `IMAGE_CORRUPT`, an inert visible placeholder and `context_prefix` span at
its real sheet/row/column/image-index, with no fabricated attachment URI.
Unknown lost locations get an unpositioned placeholder rather than a guessed
row. Unrelated library warnings retain their original classification.

## P1-T5 — raster normalization and parent IPC ownership

Raster input formats are allowlisted before decode; dimensions and image/work
budgets precede allocations. The sink applies EXIF orientation, copies pixels
into a metadata-free RGBA PNG, bounds encoding, and hashes normalized bytes.
GIF/TIFF/WebP uses the first frame with `IMAGE_FIRST_FRAME_ONLY` per occurrence.
Bytes deduplicate while occurrences remain separate. Decoder corruption/image
quota violations degrade through typed rejection; filesystem/workdir exhaustion
and unrelated sink failures propagate. The uploaded-document byte cap is not
misapplied to extracted raster intermediates; actual image/work budgets still
apply, including source-plus-output accounting.

Parent IPC traverses child paths using directory FDs and nofollow/nonblocking
opens, rejecting traversal, symlinks, hardlinks, FIFOs, sockets and directories.
It copies bounded bytes into an exclusive parent-owned file, validates actual
SHA/size/PNG structure/dimensions/metadata/full pixel decode, and publishes only
the checked parent copy. Duplicate refs still undergo validation; subsequent
child-path replacement cannot change accepted parent bytes.

## P1-T6 — Word physical order and PDF image fidelity

Word retains `extract -> parse_docx`, body `iter_inner_content`, run/hyperlink/
legacy field handling and local image relationship extraction. Paragraphs and
nested table content are visited in physical XML order; only repeated views of
the same underlying merged cell are deduplicated. Run spaces/repeated strings
survive. Heading paths, numeric `table_path`, cell-local paragraph numbering and
per-occurrence drawing/pict spans refer to actual source. Safe hyperlink targets
are escaped, unsafe schemes remain visible text, and external images are never
fetched. Sink identity replaces host storage/UploadFile/session logic.

Only actual `w:tblHeader` is header authority; unmarked tables use `列N：` lists.
Marked tables emit a header Document followed by ordered row Documents. Joined
fragments form the table; individual row Documents are deliberately not full
standalone tables. P3 may group/repeat headers as `context_prefix`; P1 does not
silently duplicate cell text or image occurrences. Nested tables flush outer
row fragments at their actual position. Inside marked tables, `w:br`, `w:cr`
and literal cell newlines become `; ` before span/offset assignment; body and
unmarked-table newlines retain their original behavior.

PDF retains `extract/load/parse`, PDFium page text and image-object iteration.
Each page, including empty/image-only pages, yields one positioned Document;
image occurrences append at page end, without claiming 2-D reading order.
Plaintext cache/host storage/database/Blob branches are removed. Text and image
budgets precede append/allocation, and all native Document/Page/Text/Image/
Bitmap/PIL handles close in `finally`, including sink failure and cancellation.

Direct DCT/JPX extraction remains preferred for opaque images. PDFium image
extraction omits separate soft masks, so bounded `get_bitmap(render=True)`
renders only the image object and supplies PNG when its actual alpha contains
transparency. Raw and Flate DeviceCMYK images whose PNG fallback raises
`OSError` now continue through that bounded bitmap path; valid CMYK no longer
aborts the document. Literal source Markdown metacharacters are escaped before
span assignment so source fences cannot absorb appended logical references.
No OCR, page screenshots, image vectors or persistent cache were introduced.

## P1-T7 — six installed Unstructured local branches and resources

- All six original files in `api/core/rag/extractor/unstructured/` retain their
  local `partition_*` dispatch. Constructor API URL/key, `partition_via_api`,
  host config imports, and host-specific `Document` are removed; the input is
  `ExtractSetting`, and output is the shared immutable `Document` contract.
- PPTX retains source order rather than dropping elements without page metadata
  into a page-keyed dictionary. Only positive actual integer page metadata is
  mapped to `slide`; unavailable slide positions get a warning without a guess.
- EPUB removes the unconditional `pypandoc.download_pandoc()` call. Installed
  `pypandoc-binary==1.17`'s executable is hash checked and selected with its
  documented `PYPANDOC_PANDOC` variable. No guessed chapter/page is emitted.
- Markdown's real `partition_md(filename=...)` operates on token-map-delimited
  local source. Code, HTML/MDX, angle generics and placeholders are protected.
  Source block delimiters must return exactly once and in order; otherwise
  parsing fails. Protected literal markers must also remain inside their own
  source block delimiters, preventing cross-block reassignment. Restored original source intervals retain Markdown punctuation,
  `heading_path`, and line-level spans. This deliberate fidelity side channel
  compensates for the library's HTML intermediate losing punctuation and source
  coordinates; it is not a default-parser fallback. Shared normalizer handles
  real external images only after source restoration.
- EML removes speculative `base64.b64decode` of already-decoded element text.
  Mail bodies remain local; EML/MSG disable the upstream default attachment
  auto-partitioner, which would broaden this six-format/OCR boundary.
- XML first uses `XMLParser(resolve_entities=False, load_dtd=False,
  no_network=True)` on original bytes, honoring encoding/BOM. Any DTD/entity is
  rejected, including UTF-16; only safe UTF-8 serialization reaches partition.
- Element conversion uses actual `category`, `category_depth`, `page_number`
  and `text_as_html`, preserving order. HTML tables use the shared HTML converter
  with the outer element's source coordinates. Text-only tables warn rather
  than fabricate columns. No token-based/chunking claims or host settings remain.

### Exact installed-source resource audit

The checked dependency is Unstructured **0.21.5**, not an NLTK-based version.
`unstructured/nlp/tokenize.py` defines model name `en_core_web_sm`, version
`3.8.0`, and wheel SHA-256
`1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85`.
`_get_nlp -> _load_spacy_model -> _install_spacy_model ->
_download_with_timeout -> urllib.request.urlopen` is the inspected fallback.
There is no claimed environment switch disabling it. After resource preflight,
`prepare_local_parser` substitutes exactly `_load_spacy_model` with a local
`spacy.load`-only implementation that returns a safe dependency error on load
failure. No requests/socket monkeypatch is used in production. OS-level network
and filesystem denial remain the P1-T8 boundary.

`partition/md.py` and `partition/html/partition.py` have explicit URL branches;
our adapters pass local `filename`/library-local text only. The inspected six
partition branches, metadata decorators and NLP source contain no telemetry
request path. `languages=[""]` uses the locked library's supported language
inference disable option. This is a code-path audit, not proof of OS isolation.

`pypandoc/__init__.py::_ensure_pandoc_path` otherwise searches PATH/home/system
locations; setting `PYPANDOC_PANDOC` and calling its public
`clean_pandocpath_cache()` pins the checked binary in repeated-process tests.
`magic/loader.py::load_lib` finds Homebrew/system libmagic. The selected shared
library's actual loaded `magic_version` symbol is resolved with POSIX `dladdr`
and checked against the locked bytes (rather than assuming a Linux soname is
an absolute filename), and `MAGIC` selects the checked compiled database. No build-machine paths are serialized in manifests.

### Reproducible provisioning and evidence boundary

The `extraction-local` extra pins the official spaCy wheel URL and uv records its
exact SHA. `uv sync --all-packages --extra extraction-local` is a **build-time**
installation action; it is never called by extraction. The model wheel, not
arbitrary model downloads, is the supported NLP resource supply chain.

The present macOS arm64 resource entry uses Homebrew libmagic **5.48** (new
formula installation only, no dependencies, no upgrades, no sudo); bottle SHA
`c8c01258938e218cf9dcff85eaf7580b299821fab53f4d6706679d41d55b476b`.
Install only during environment/image preparation. Native library/database,
Pandoc and codec hashes are in `resources.lock.json`; native probe output is
recorded separately from the runtime fingerprint.

After build dependency installation, run:

```sh
.venv/bin/python scripts/build_extraction_resources.py --output packages/knowledge/actweave_knowledge/extraction/resources.lock.json
```

The builder reads an explicit package/resource scope allowlist and does not
fetch anything. Generated canonical JSON contains package versions, relative
logical resource names, byte hashes, platform and adapter/network-policy
revisions. It excludes timestamps, absolute paths, installation metadata,
bytecode, user names, ingestion Tokenizer and ChunkProfile. Gateway and Worker
must ship the same reviewed platform entry. Unknown platforms or missing,
changed, mismatched resources fail closed; the macOS entry does not certify
Linux installation or network isolation. A Linux builder must supply native
resources, generate/review its own entry and pass the P1-T8 OS gates.

### macOS Pandoc architecture caveat

The PyPI `pypandoc_binary-1.17-py3-none-macosx_11_0_arm64.whl` metadata says
arm64, but the actual packaged `pypandoc/files/pandoc` bytes are a Mach-O x86_64
executable (`file` inspection), version 3.9. The checked current machine executes
it via existing Rosetta. Its linked libraries are Apple's libSystem, libz,
libiconv, libffi, libcharset and Security.framework. The macOS sandbox therefore
needs the existing read-only Rosetta runtime under `/Library/Apple/usr/libexec/oah`
and `/Library/Apple/usr/lib`; P1-T8 owns the actual permission/functional probe.
No Rosetta installation, dependency pin change or library broad write permission
is implied by the macOS manifest. Missing execution prerequisites remain a
platform readiness failure; Linux uses its own native packaged binary and lock.
