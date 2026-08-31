# Knowledge reference redesign — 2026-08-31

final result: passed

## Scope and source authority

The user's latest eight screenshots govern the visual direction for the existing Knowledge module: compact type, blue selected states, gray filled controls, a split creation workspace, a right-side segment editor, and label/control columns on Settings. The earlier right-side Search redesign remains included. Existing global and knowledge navigation, full workspace width, permissions, API contracts, and actual capabilities are preserved. This is an adaptation to ActWeave, not a clone of unsupported features from the reference product.

Artifact directory:
`/Users/jiangfeng/.codex/visualizations/2026/08/31/01a05614-6f61-7450-abe3-3bff9add0e45/`

The user-provided reference bitmaps were copied unchanged:

| File                             | Dimensions  | Reference surface                   |
| -------------------------------- | ----------- | ----------------------------------- |
| `reference-wizard-parent.png`    | 1729 × 1011 | Expanded parent/child configuration |
| `reference-wizard-general.png`   | 1711 × 989  | General mode and split preview      |
| `reference-wizard-retrieval.png` | 1723 × 993  | Model and retrieval configuration   |
| `reference-wizard-finish.png`    | 1714 × 992  | Completion summary and next steps   |
| `reference-segment-editor.png`   | 1732 × 1001 | Dense list and right-side editor    |
| `reference-child-preview.png`    | 1725 × 998  | Populated child chunk preview       |
| `reference-settings-top.png`     | 1666 × 1055 | Settings field and option layout    |
| `reference-settings-bottom.png`  | 1692 × 901  | Retrieval controls and bottom Save  |

## Combined comparison evidence

Final desktop implementation captures are 1962 × 1009 CSS pixels at device pixel ratio 1. Each `comparison-reference-*.png` contains source and implementation in the same input; the matching `compare-reference-*.html` records exact crops and proportional scales. No reference bitmap was painted over, altered, or regenerated.

| Combined comparison                  | Final implementation capture            | Normalization                                                                                           |
| ------------------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `comparison-reference-settings.png`  | `reference-after-settings.png`          | Crop source x=20, width=910; implementation x=528, width=1040; compare label/control areas              |
| `comparison-reference-wizard.png`    | `reference-after-general.png`           | Exclude the implementation's preserved 240px global sidebar                                             |
| `comparison-reference-parent.png`    | `reference-after-parent.png`            | Exclude 240px global sidebar; use actual parent/child preview                                           |
| `comparison-reference-retrieval.png` | `reference-after-wizard-configured.png` | Compare lower independently scrolled configuration areas                                                |
| `comparison-reference-finish.png`    | `reference-after-finish.png`            | Compare source x=300,width=1240 and implementation x=500,width=1200                                     |
| `comparison-reference-editor.png`    | `reference-after-editor.png`            | Compare source x=1000,width=732 and implementation x=1250,width=712, including surrounding list context |

The broad wizard comparisons establish layout fidelity; original-size implementation captures were also inspected for field labels, controls, preview text, and focus states. The Settings and editor comparisons provide larger field-level detail. Different document contents, counts, processing states, absent unsupported options, preserved app navigation, and comparison canvas margins are excluded from pixel judgments. The two extra parent/settings references were reviewed as supplementary detail of the same surfaces.

## Final design and resolved findings

1. Creation uses a compact centered step indicator, selectable mode cards with parameters expanded inside the selected mode, gray filled controls, blue primary actions, and approximately equal-width configuration/preview columns. Only step 2 constrains its desktop height; its two panes scroll independently while the footer and preview header remain available. Steps 1 and 3 keep natural height.
2. Parent/child preview uses the actual returned child contents, numbered compact chips, and a disclosure of the unchanged complete parent text. The first rendering preserved blank lines inside chips and became too tall; child previews now collapse whitespace visually. Stored text and the full-parent view retain original content.
3. Completion shows real document statuses and the frozen submission configuration with a next-step panel. An initially stretched full-width summary was corrected to a centered maximum of 1152px, with a 288px next-step column and 180px desktop summary labels. No artificial percentages, completed counts, upgrade banners, or unsupported API links were added.
4. Segment browsing uses compact flat rows, file-type icons, three-line previews, and a dedicated full-content action. View, edit, and add open 560px right-side sheets, or full-width sheets on narrow screens. Full text and editor preserve original whitespace. Preview whitespace was tightened to prevent blank lines consuming the entire preview.
5. The first editor capture exposed the shared default 50% black overlay, obscuring the list compared with the reference. Knowledge sheets now request a 5% slate overlay through the optional `SheetContent.overlayClassName`; other sheets retain the default. Modal, focus trap, Escape/outside dismissal, and shared close-button behavior are unchanged. The editor footer stays visible, while textarea content scrolls.
6. Settings now uses a 160px label column with a flexible control column capped at 820px, separated sections, gray inputs, semantic/hybrid radio cards, and one blue Save at the bottom. Base fields, the independent embedding action, and retrieval defaults have clear boundaries. On mobile labels stack above controls. The long-description and narrow-name problems of the earlier stretched form are resolved.
7. Search retains the preceding compact query/results split and history panel, with full details available behind truncated snippets. Its score explanation uses a touch/keyboard-accessible native disclosure; submitting does not drift to the bottom of long results.

No actionable P0/P1/P2 visual issues remain in the inspected scope. Layout uses the existing font stack, 16px page titles, 13px controls/body, 12px helper text, soft borders, and module-local blue/gray variables. Existing Lucide and document-type icons supply functional visuals. No new bitmap decoration, logo substitution, or global palette change was introduced.

## Capability boundaries and intentional differences

- Backend and frontend contracts support `general`/`parent_child` and `semantic`/`hybrid`. No economic index, Q&A mode, full-document parent option, lexical-only route, ranking weight controls, visibility picker, or editable icon was invented.
- Chunking belongs to each document and is frozen at upload, so Settings does not pretend to expose a knowledge-base chunk mode. Existing document reparse behavior remains separate.
- Top K and threshold remain in Settings. Reranker selection/clearing remains available there, and the creation wizard now also binds the optional reranker through the existing create API. This corrects a missed frontend entry point; no backend capability or request field was invented.
- Settings numeric controls and its bottom Save explicitly associate with the same unique form id, preserving native validation and Enter submission. Re-embedding stays outside the PATCH form and retains its separate confirmation and actual accepted/skipped result.
- The segment API supports content edits; no attachment upload, summary input, child-edit operations, fake recall counts, or unsupported save-and-regenerate action was added.
- Short previews are visual only. Full-content access remains available to read-only members, who receive no mutation controls.

## Live and responsive checks

The user authorized creating validation knowledge bases. Two new bases contain only clearly labeled fictional Markdown documents:

- `界面参考验证 · 父子文档`, id `432b0649-dc56-4650-847f-3b7f52359bd7`: two documents, both reached ready. Parent size 1000, overlap 100, child size 100, hybrid route.
- `界面参考验证 · 通用文档`, id `52d9c791-906e-45ab-b820-288b718f41db`: one document, reached ready. General size 1000, overlap 100, semantic route.

Both used the existing SiliconFlow embedding-model option. The business base and the prior UI validation base were not modified during this reference pass. New preview requests and the two authorized create/upload flows were real; live document editing, settings saving, and re-embedding were not performed. Their mutations were covered with mocked browser tests.

Verified in the running app: general auto-preview, parent/child switching, stale preview indication, explicit refresh, real child output, model selection, name/description input, create/upload/status progression, documents navigation, editor open/cancel, and Settings navigation. Desktop screenshot measurements confirm a 560px sheet and no horizontal overflow. At 390 × 844 the sheet measures exactly 390px wide and keeps Cancel/Save visible. Settings and wizard content remain reachable without horizontal page overflow. At 1280 × 900 Settings retains readable fields and has no horizontal overflow.

Responsive evidence: `reference-editor-mobile.png`, `reference-settings-mobile.png`, `reference-settings-1280.png`, `reference-wizard-mobile.png`. Viewport overrides were reset. The comparison tab and temporary comparison server were closed/stopped; the user's real services were left running.

## Baseline redesign verification (before alignment follow-up)

- `cd frontend && pnpm check`: passed on final source.
- `cd frontend && pnpm test`: **203 files / 1121 tests passed**, no failures, skips, or snapshot changes.
- Production build in an isolated frontend copy: **exit 0**, 69 pages generated; the running development `.next` was not overwritten.
- Complete `tests/e2e/project-knowledge.spec.ts`, system Chrome/Chromium: **53 passed (2.0m), exit 0**, no failures or skips.
- All 15 source/test files in the final browser/build validation manifest match SHA-256 values in the main workspace, including the shared Sheet change. Manifest: `/tmp/knowledge-complete-validation-manifest.json`.
- Existing tests were updated for actual radios, read-only full-content Sheet plus Escape, native invalid-threshold protection, and Enter submission. The full spec also covers scoped permissions, preview races, immutable upload parameters, retries, reranker binding/clearing, rebuild confirmation, result provenance, document location, and conflicts.
- A separate final read-only diff review found no important reachable permission, request-payload, form-association, or Sheet-state regression.
- Focused formatting and `git diff --check` passed. README and the frontend owning guide describe the changed interaction surfaces and the optional overlay patch.

Reproduce the browser run from the built isolated frontend with an explicitly managed server on port 3126, `ACT_WEAVE_AUTH_DISABLED=1`, and `ACT_WEAVE_INTERNAL_GATEWAY_BASE_URL=http://127.0.0.1:9`:

```sh
PLAYWRIGHT_SKIP_WEB_SERVER=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3126 PLAYWRIGHT_USE_SYSTEM_CHROME=1 pnpm exec playwright test tests/e2e/project-knowledge.spec.ts --config=playwright.config.ts --project=chromium --workers=1 --reporter=line --timeout=30000 --global-timeout=300000
```

Business API requests were mocked in that suite. Its server was stopped afterward. The explicit server avoids the earlier automatic-webServer teardown hang; no test configuration workaround was committed.

## Alignment and copy follow-up

The user subsequently identified input-row misalignment and requested removal of the explanatory copy beneath chunk parameters. The prior visual pass missed that detail; it is corrected in this follow-up.

- Root cause: stretched outer grid items contained inner grids with unequal helper-text heights. Their auto tracks distributed surplus height above inputs. Before the fix, general-mode separator input y=226 versus size/overlap y=232.65625.
- Minimal implementation: add `items-start` to the two parameter grids in the wizard and the two matching grids in the upload dialog. Remove the four visible parameter helper spans in each surface, as requested. Labels, validation constraints, event handlers, modes, columns, field order, preprocessing controls, and immutable-upload notice remain unchanged.
- Live desktop after: general inputs all y=226 and height=36; parent inputs all y=336; child inputs both y=438. At 1280px width the parent inputs all y=356 and child inputs both y=458. At 390px width parent inputs stack as before and child inputs both y=723; no horizontal overflow.
- Upload dialog after: size/overlap both y=499.5 and child inputs both y=654.5. No files or knowledge bases were submitted in this follow-up; the wizard and upload dialog were canceled after inspection.
- Visual evidence: `chunk-alignment-user-source.png` (unchanged user screenshot), `chunk-settings-aligned.png`, `chunk-settings-general-aligned.png`, `chunk-settings-aligned-mobile.png`, and `chunk-upload-aligned.png`. The source and corrected parent controls were inspected together; parameter helpers are absent and each input row shares its baseline.
- Final follow-up validation: `pnpm check` passed; `pnpm test` passed 203 files / 1121 tests with zero skips. Isolated production build passed (69 pages). Six affected Chromium tests passed in 11.3s, covering wizard creation, pending-create freeze, parent/child preview/upload, upload-dialog parameters, multi-file retry, and accepted formats. This is a focused rerun; the earlier 53-case result above belongs to the preceding baseline.
- All 15 files in the updated validation manifest match the workspace. The test server was stopped, the browser viewport restored, and no unrelated changes were touched.

## Section-description placement follow-up

The user clarified that section-level explanations belong directly below their large headings and above controls. In the wizard, the immutable-chunk note now follows “分段设置”, and the model-change note follows “Embedding 模型”. Each heading/note pair is grouped with a 4px gap. The cards variant of the retrieval field likewise puts its existing group-level hint between the legend and choices. Previously removed parameter-level hints remain absent.

Live screenshot: `chunk-section-descriptions-under-headings.png`. DOM inspection confirms each wizard heading is immediately followed by its original explanation and then the relevant controls. Existing column alignment is retained. Settings passes `showLabel=false`/`showHint=false`, so the shared cards adjustment does not change its visible layout. No API, validation, mode, radio, or disabled behavior changed. This follow-up is a presentation-only relocation. On the current source, `pnpm check` passed and `pnpm test` passed 203 files / 1121 tests with zero skips. The build and six-case browser result above belong to the preceding alignment snapshot; those gates were not rerun for this text relocation.

## Reranker entry and vector naming follow-up

The user identified a missing Rerank selector and an unclear vector-search label. Source inspection confirmed that the backend already accepts `reranker_model_id` on creation (`backend/app/knowledge/gateway.py:316,933`) and uses a bound reranker for both semantic and hybrid retrieval. The creation wizard had omitted the existing field; treating that omission as a capability boundary in the first pass was incorrect.

- Added the optional model selector inside the selected retrieval card. Its content is outside the radio label, with its own accessible combobox label. Switching modes retains the selected model. Empty model registries show an explicit message and still allow “No reranking”.
- The model id is captured in the submission snapshot and sent in the existing create request. Default or cleared selection omits the id entirely and never sends an update-only clear flag. Submission disables the selector, and the completion summary shows the frozen choice.
- Renamed the display label from “仅语义检索”/“Semantic only” to “向量检索”/“Vector search”, including the per-query override. The persisted/API value remains `semantic`.
- Actual supported routes remain vector (`semantic`) and vector-plus-keyword (`hybrid`). The lexical recall path exists within hybrid; the API does not expose a standalone full-text mode. No nonfunctional third choice or ranking-weight control was added.
- Live model options include `SiliconFlow · Qwen/Qwen3-VL-Reranker-8B`. Selecting it, switching Vector → Hybrid without losing it, and clearing it were checked in the running UI. No live base creation or rerank provider call was performed in this pass. The wizard was canceled, and mobile viewport overrides were reset.
- Visual evidence: `reference-reranker-selection.png` and `reference-vector-retrieval.png` preserve the supplied references; `wizard-vector-hybrid-reranker.png`, `wizard-reranker-mobile.png`, and `vector-hybrid-reranker-final.png` show the implementation. The final card structure follows the reference's selected-card expansion while only exposing supported controls. At 390px the model control fits x=31..344 and the document has no horizontal overflow.
- Final verification: `pnpm check` passed; `pnpm test` passed 203 files / 1121 tests with zero skips. Isolated production build passed, generating 69 pages. The complete Knowledge mocked Chromium spec passed **54 tests in 2.1m**, exit 0, including binding, default omission, explicit clearing, switching modes, and pending-submit freezing. All 15 files in the final manifest match the main workspace SHA values; the test server was stopped.

## Unconfigured empty-base follow-up

The user requested that empty-base creation not require a model or retrieval choice. This is implemented as a genuinely unconfigured database-backed base, rather than hiding an automatically selected model.

- Empty creation now collects and submits only name/description. It does not fetch model options. The response can contain `embedding_model_id: null`; the base retains the existing status enum and displays “Not configured”. The stored retrieval default remains semantic but is not a user-facing creation choice.
- First upload opens `KnowledgeBaseSetupDialog`. Its initial PATCH atomically binds Embedding, retrieval mode, and optional Reranker. Only a successful response opens the original upload dialog. Failure retains choices and performs no upload. Settings opens the same setup without uploading; read-only users never receive setup controls.
- Backend create/API/view contracts accept an absent model. PATCH accepts an embedding id only for an unconfigured base with no documents; configured bases must still use rebuild to change models and cannot clear the binding. Upload admission rejects an unconfigured base under its row lock before reserving a document, object, or task. Search excludes only unconfigured bases before budgets/model materialization; a configured base with an unavailable model still fails explicitly.
- ORM, Schema V1 SQL, comments, catalog signature/digest, and schema tests were updated together. Schema marker remains `schema_v1`. The exact database change is removal of NOT NULL from `knowledge_bases.embedding_model_id` plus the table/column descriptive comments; no data deletion or default model assignment is required by the code.
- Frontend validation: `pnpm check` passed; `pnpm test` passed 203 files / 1121 tests, zero skips. The isolated production build passed and all **58 Knowledge mocked Chromium tests passed (2.1m)**. All 17 source/test files matched the main workspace SHA values. New coverage includes metadata-only creation with no model-options call, delayed upload until successful setup, configuration failure with no upload, read-only restrictions, and Settings-only setup.
- Backend final evidence: the complete core gate passed **5,242 tests, zero failures or skips, exit 0, 527.58s**; six real-provider integration cases were excluded by the core command. The affected 201 tests, 61 schema tests, isolated first-configuration concurrency cases, and 17 isolated MinIO tests also passed. Formatting, lint, schema comments, and diff checks passed. The blocking-IO scanner exited 0 and reported 90 repository findings, with no hits in this change. All 19 backend files match `/tmp/knowledge-unconfigured-backend-sha.json`.
- The running database was not altered. Read-only `make check-db` against local `127.0.0.1:9432/deerflow` reports `recreate_required` because its old catalog is still NOT NULL. The Gateway currently does not pass readiness, so this feature is not yet usable in that installation. The repository's backend guide disallows in-place migration and requires explicit target approval for destructive recreation. No reset, ALTER, schema stamping, service workaround, or automatic model selection was performed.
- Visual QA additionally inspected `empty-base-create-mocked.png` and `empty-base-setup-mocked.png`: these are screenshot-instrumented isolated tests with mocked API responses, not the unavailable live Gateway. Both captures show complete settled dialogs; the temporary instrumentation was removed, the copied spec restored byte-for-byte, and port 3126 stopped. Two screenshot cases passed in 3.4s.
- The user has been asked to authorize a one-time, data-preserving migration exception after backup, limited to the column nullability and two comments. Database application and subsequent live validation remain pending that authorization.

## Limits

This verifies the frontend unit suite and the complete Knowledge Chromium spec, not the entire repository, all Playwright specs, other browser engines, external model providers, or deployment targets. Dark mode and physical touch hardware were not visually verified. The earlier visual passes changed no backend behavior; the unconfigured empty-base follow-up above now includes an explicit schema contract change. The unrelated untracked model-provider plan was preserved. This new redesign remains uncommitted; the user's earlier push request had already been fulfilled for the preceding version.


## Existing-base upload wizard — accepted 2026-08-31

The current request replaces the upload dialog with the same three-step workspace used to create a base. Earlier entries above are historical validation snapshots; the database was subsequently reset with explicit authorization and the normal local service is running.

- **Acceptance:** the user confirmed “我已经验证了，改好了”. No additional functionality or browser reruns were pursued after that confirmation.
- **Behavior:** both upload entry points open the shared full-page wizard. Configured bases preserve their models and retrieval mode; unconfigured bases perform their first atomic configuration in step 2. Uploading never creates another base. Results contain only this batch’s returned document IDs, and retries retain the frozen parameters and only retry failed files. Cancel returns to the prior base view; browser history changes clear the transient upload session.
- **Visual sources:** `upload-dialog-user-before.png` and `upload-wizard-source-reference.png` in the artifact directory above. The source reference is 1336×766 pixels. `upload-wizard-step1-live.png` is a same-size 1336×766 content crop from a 1640×800 CSS viewport, inspected together with the reference. `upload-wizard-mobile-live.png` records the 390px viewport; DOM scroll width was also 390px.
- **Fidelity:** the comparison checked typography, spacing, colors, assets, and copy. Compact type, blue step state, a 640px file area, a light dashed drop target, and the right-aligned Next action follow the reference. Existing navigation and design tokens remain. Icons come from the existing library; there are no new raster assets. Supported formats and the actual 50 MB limit are preserved; the reference product’s upgrade advertisement and 15 MB restriction are intentionally omitted. The first-step controls were legible at full comparison scale, so an additional focused crop was unnecessary. No actionable visual P0/P1/P2 remained in that capture.
- **Checks completed on this change:** frontend `pnpm check`; 203 unit-test files / 1,124 tests passed with zero skips; isolated production build generated 69 pages; whitespace checks passed.
- **Automated browser limitation:** the 61-case system-Chrome run did not return a final result and was stopped. A bundled-Chromium visual run reached the configured-upload UI, then exposed an overly exact test selector for the Parent-child radio’s accessible name. That selector was corrected to match the existing label-plus-description control, but the browser suite was not rerun after the user confirmed acceptance. Do not report the 61 browser cases as passed. The complete flow is user-verified; only the first-step desktop/mobile views were screenshot-verified by the agent in this iteration.
- **Cleanup:** only the isolated port-3126 server and temporary visual-test copy were removed. The normal port-2026 service remains running. No business data was created or changed by this UI verification.

final result: passed
