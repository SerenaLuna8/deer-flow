# Design QA Reports

## Skills Markdown Preview

## Evidence

- Source visual: `docs/pr-evidence/skills-markdown-preview/source-mockup.png`
- Final desktop implementation: `docs/pr-evidence/skills-markdown-preview/implementation-desktop.png`
- Final mobile implementation: `docs/pr-evidence/skills-markdown-preview/implementation-mobile.png`
- Desktop iteration 1 comparison (source left, implementation right): `docs/pr-evidence/skills-markdown-preview/comparison-desktop-01.png`
- Final desktop comparison (source left, implementation right): `docs/pr-evidence/skills-markdown-preview/comparison-desktop-02.png`
- Final focused header comparison: `docs/pr-evidence/skills-markdown-preview/comparison-header-02.png`
- Final focused body comparison: `docs/pr-evidence/skills-markdown-preview/comparison-body-02.png`

The desktop comparison uses the same `1808 x 870` page viewport and the same open
`academic-paper-review` Sheet state. The mobile capture uses `390 x 844` with the
same skill open.

## Comparison history

### Iteration 1

- P0: none.
- P1: the reused Markdown renderer produced a `30px` H1, `24px` H2, and `16px`
  body/list copy. In the focused body comparison this was visibly too large and
  sparse for the compact reading rhythm established by the approved source.
- Fix: added preview-scoped typography overrides only at the `MarkdownContent`
  call site. The shared Markdown renderer and its raw-HTML-disabled pipeline were
  not changed.
- P2: none requiring a correction. The live skill has a longer real description
  and no license value, while the mock uses abbreviated sample copy and `MIT`;
  the implementation correctly renders API data rather than inventing metadata.

### Iteration 2 — final

- P0: none.
- P1: none.
- P2: none.
- Markdown hierarchy now follows the target density: H1 `20px/28px`, H2
  `18px/28px`, and body/list copy `14px/24px`.
- The Sheet remains the approved wide desktop treatment (`800px` maximum) and
  full viewport width on mobile.

## Final visual checks

- Typography and rhythm: pass. Header hierarchy, compact Markdown headings,
  paragraph spacing, list rhythm, and readable line length match the intended
  right-side document viewer.
- Spacing and density: pass. The header has a clear metadata/content boundary;
  the scroll body keeps consistent horizontal padding and a `72ch` reading
  measure.
- Tokens and shell fidelity: pass. The implementation uses DeerFlow background,
  border, muted text, badge, focus-ring, Sheet shadow, and standard modal overlay
  tokens. The live workspace sidebar and breadcrumb remain intact; the source
  mock intentionally isolates the feature surface.
- Icons and assets: pass. No new decorative assets were invented. Row affordance
  uses the existing Lucide chevron and the Sheet keeps its existing close icon.
- Copy and metadata: pass. `SKILL.md`, category, enabled state, description, and
  conditional license use localized UI copy plus live API data. YAML frontmatter
  is not visible in the rendered document.
- Selected row and background: pass. The selected row receives restrained border
  and muted-background treatment while the existing modal overlay preserves clear
  focus separation.
- Accessibility: pass. The row trigger and Switch remain separate controls;
  close-button and Escape paths both returned focus to the exact
  `academic-paper-review` trigger with `aria-expanded="false"`.

## Responsive and interaction checks

- At `390 x 844`, measured Sheet bounds were `x=0`, `width=390`, `right=390`.
- Document and dialog horizontal overflow were both zero
  (`root scrollWidth=390`, dialog `scrollWidth=389`).
- Metadata and the full live description wrapped without clipping.
- Table and code containers retain the existing internal `overflow-x-auto`
  boundary; the E2E fixture also verifies a 320-character token and wide code
  block without page overflow.
- Row open, close control, Escape, lazy loading, Switch isolation, distinct
  403/404/500 errors with Retry, stale-selection protection, and raw-HTML safety
  are covered by the 12-case browser E2E suite.
- Browser console after desktop and mobile interaction contained no warnings or
  errors.

---

## Memory Inbox

- Source visual truth: `/Users/jiangfeng/.codex/generated_images/019f4af4-2a5d-7f51-99f5-cf604f2c63ec/exec-5828b811-6528-4af0-9785-772f75976687.png`
- Rendered implementation: `/Users/jiangfeng/deer-flow/.worktrees/memory-inbox-redesign/.superpowers/sdd/memory-qa-desktop-1672x941.png`
- Full-view comparison: `/Users/jiangfeng/deer-flow/.worktrees/memory-inbox-redesign/.superpowers/sdd/memory-qa-comparison-full.png`
- Mobile evidence: `/Users/jiangfeng/deer-flow/.worktrees/memory-inbox-redesign/.superpowers/sdd/memory-qa-mobile-390x844.png`
- Viewport: `1672 × 941` desktop and `390 × 844` mobile, DPR 1
- State: Chinese, light theme, populated memory, All filter, summaries collapsed, management menu closed for the comparison capture

## Full-view comparison evidence

The source and browser-rendered implementation were placed in one native-size comparison board. The implementation preserves the selected composition: compact header with secondary management and primary add actions, one overview strip, search with segmented filters, a full-width divided fact list, and a collapsed full-width smart-summary disclosure.

The app shell and the stored-memory schema explain the visible intentional differences. DeerFlow keeps its workspace rail and breadcrumb; facts expose one stored content field rather than the mockup's invented title/description pair and source-count button. The implementation therefore uses the raw stored content as the only primary text and keeps learned-source navigation in metadata, preserving the product contract.

No separate focused crop was required: both full views were captured at the same native viewport and remain readable at original resolution; there are no raster illustrations, logos, photographs, or dense charts requiring an asset-level crop.

## Required fidelity surfaces

- Fonts and typography: existing DeerFlow font stack and optical weights are retained. Heading, body, metadata, and control hierarchy remain clear; mobile text wraps without document overflow.
- Spacing and layout rhythm: the five-layer vertical hierarchy matches the source. Existing capability-page width and app chrome are retained deliberately. Shared list chrome and dividers remove the previous 36/64 imbalance and large empty panel.
- Colors and visual tokens: warm-neutral background, card, border, muted text, primary charcoal, and destructive tokens come from the existing theme; no raw light-only palette, gradient, or glass treatment was added.
- Image quality and asset fidelity: the target contains no required photographic or illustrative assets. Visible icons use the existing Lucide family; no handcrafted SVG, CSS art, emoji, or placeholder imagery was introduced.
- Copy and content: Chinese page labels, actions, overview labels, filters, empty/loading/error copy, and summary disclosure are coherent. Dynamic category values remain stored strings by design.
- Responsiveness and accessibility: the `390 × 844` capture has `scrollWidth === clientWidth`; controls wrap and remain usable. Loading is a named live status, icon-only actions retain labels, and the management menu uses the existing Radix keyboard/focus behavior.

## Interaction and runtime evidence

- In-app Browser opened the production build through an in-memory QA API proxy, without contacting or mutating the real Gateway memory.
- Management menu opened and exposed Import, Export, and destructive Clear actions.
- The page produced no browser console errors during the QA run.
- Automated acceptance additionally covers add/edit/delete, import/export/clear, learned-source navigation, summary search/filter behavior, 390px overflow, and persisted dark mode.

## Findings

No actionable P0, P1, or P2 visual mismatch remains.

P3 follow-up polish:

- The reference mockup is visually roomier because it contains descriptive fact text that does not exist in `UserMemory`; the implementation correctly avoids synthesizing or persisting absent data.
- On narrow screens, recent focus shares the overview's compact grid and may truncate earlier than the desktop reference. It remains readable and its View summaries action remains available, but a future polish pass could give it a full-width row.
- Standard category keys remain visible as stored English strings in Chinese locale. Localizing known keys would improve polish but must retain arbitrary custom-category support.

## Comparison history

Pass 1: desktop and mobile browser captures were compared against the approved source. No P0/P1/P2 mismatch was identified, so no visual-fix iteration was required. The intentional schema and app-shell differences above were classified as acceptable product constraints; the remaining items are P3 polish.

## Implementation checklist

- [x] Fact-first full-width list
- [x] Summary disclosure collapsed by default
- [x] Header, overview, toolbar, empty/loading/error states
- [x] Core operation contracts preserved
- [x] Desktop and 390px browser evidence
- [x] Console-error check
- [x] Source and implementation in one comparison input
      final result: passed

---

## 产品体验重构一期（进行中）

### 对照目标与当前证据

- 设计基准：
  - 新会话：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-3e736424-1068-447c-a09e-8da09ad557e0.png`
  - Agent 选择器：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-e7b1c651-f3a0-4335-9467-70eb51d67401.png`
  - 正常执行：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-b969e19e-bff2-4bcd-81a8-edcb7992deac.png`
  - Human input：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-549b08d3-0103-410e-ae4f-84cee8269c57.png`
  - 文件预览：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-6d2e3e4b-5601-4111-98d9-301a17c4db0d.png`
  - 文件源码：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-2a6dd5ef-cd4d-4bf7-b00b-590d1112f155.png`
  - Sidecar：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-59eb953b-a203-4d5d-b6b2-11fc66d64c9b.png`
  - Agent 列表与抽屉：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-112b307b-fc35-49b7-b275-8cf413b2ea2c.png`
  - Skill 列表与抽屉：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-75faf3bb-8823-48ef-95d5-48f8a972304d.png`
  - MCP 列表与抽屉：`/Users/jiangfeng/.codex/generated_images/019f8357-97f6-79a0-b62f-c0477a4fe1bc/exec-c092d86a-0c74-4571-84b7-1611e5471434.png`
- 当前浏览器截图：`design-qa-artifacts/*.png`，桌面视口均为 `1672 × 941`。
- 同画面对照（左侧设计，右侧实现）：
  - `design-qa-artifacts/comparison-chat-new.png`
  - `design-qa-artifacts/comparison-chat-agent-selector.png`
  - `design-qa-artifacts/comparison-agents-drawer.png`
  - `design-qa-artifacts/comparison-mcp.png`
- 聚焦对照：
  - `design-qa-artifacts/comparison-chat-new-focus.png`
  - `design-qa-artifacts/comparison-chat-selector-focus.png`
  - `design-qa-artifacts/comparison-chat-nav-focus.png`
  - `design-qa-artifacts/comparison-agents-drawer-focus.png`

当前截图来自真实项目与真实 API 数据。设计图中的示例会话、项目 Agent、模型、工具组、Skill/MCP 数量、依赖名称、在线状态或运行指标均不能作为需要补齐的字段；接口没有返回的数据不得伪造。

### Findings

- [P1] Agent 选择器把依赖未满足的系统 Agent 标成“可立即启用”
  - 位置：新会话 Agent 选择器。
  - 证据：`comparison-chat-selector-focus.png` 中实现提供“启用 Project Assistant 并开始对话”，但当前项目没有满足该版本所需的 MCP/Skill 绑定；真实请求会被后端依赖校验拒绝。
  - 影响：用户会进入必然失败的主路径，且 UI 对实际业务状态作出错误承诺。
  - 修复：选择器应基于系统 Agent 精确版本依赖与当前项目已绑定版本计算可用性；加载失败时 fail closed。依赖未满足时展示“需要先完成依赖配置”及到 Agent 页的入口，不发起启用请求。

- [P2] 项目主导航缺少设计基准中的层级和当前位置反馈
  - 位置：项目侧边导航。
  - 证据：`comparison-chat-nav-focus.png` 中设计通过 DeerFlow 品牌、工作/能力/项目管理分组和黑底激活项建立层级；当前实现是无分组的等权列表，当前 Chats 也没有激活反馈。
  - 影响：会话、能力资产和治理入口混在同一层，用户难以判断自己在哪里，也放大了 Agent/Skill/MCP “像同一页”的认知问题。
  - 修复：保留现有真实路由与权限过滤，只重排为工作、能力、项目管理三组；增加 DeerFlow 品牌与当前路由激活态，不新增没有业务逻辑的菜单。

- [P2] 会话入口与其余中文导航的命名不一致
  - 位置：项目侧边栏和会话列表标题。
  - 证据：`comparison-chat-nav-focus.png` 中当前主导航使用 `Chats`，会话栏标题使用“会话”；设计基准统一为“会话”。
  - 影响：同一概念出现两个名称，降低定位效率。
  - 修复：仅统一可见标签为“会话”，不修改路由或 API 名称。

### 状态差异与非问题

- `comparison-chat-new-focus.png` 左侧是“已存在可执行 Agent 的新会话”，右侧是“项目没有可执行 Agent”的前置空状态。两者不是同一业务状态，不能据此要求右侧伪造 Composer、快捷操作或 Agent 名称；修复依赖可用性后需重新捕获相同状态。
- 设计图中的历史会话和多个项目 Agent/Skill/MCP 是示例内容；当前数据库为空或只有一个系统资产时，空列表和单条真实资产是正确表现。
- Agent 抽屉当前只拿到固定 Skill/MCP 版本 ID 时，展示真实 ID 并说明接口边界优于臆造依赖名称。名称解析可作为后续增强，但不阻塞本期验收。
- MCP 设计基准是“项目自建资产 + 审批/错误状态 + 抽屉打开”，当前 `mcp-desktop.png` 是“健康系统资产 + 抽屉关闭”。状态不一致，当前对照只能验证页面结构，不能用于判断抽屉字段缺失。

### Required fidelity surfaces

- 字体与排版：现有 DeerFlow 字体栈、标题/正文/元数据层级和对话框字重没有发现可确认的 P1/P2 偏差；最终需在相同状态截图上复核换行与截断。
- 间距与布局节奏：三栏会话骨架、居中对话框和右侧抽屉方向与基准一致；导航分组与激活态仍需修复。抽屉宽度差异可接受，前提是长 prompt、版本 ID 与说明不造成页面级横向溢出。
- 颜色与视觉 token：暖白背景、细边框、黑色主操作和 muted 文本均沿用现有主题，当前没有需要新增颜色或渐变的证据。
- 图片与图标：目标没有摄影、插画或其他必须生成的位图。现有图标来自统一图标库，没有用 emoji、CSS art 或占位图替代目标资产。
- 文案与内容：动态资产、依赖、版本、状态和会话内容必须来自 API；只修复逻辑错误和静态标签不一致，不补齐设计图里的演示字段。

### 验收仍需补齐的证据

- 在 Agent 依赖就绪或明确受阻的真实状态下，重新捕获新会话与 Agent 选择器，证明 UI 不再发出必然失败的启用请求。
- 捕获正常执行、Human input、文件预览、文件源码与 Sidecar；验证文件预览和 Sidecar 复用同一右侧槽位而非同时常驻。
- 捕获 Skill 列表 + 抽屉、MCP 列表 + 抽屉的同状态证据；当前只有 Agent 抽屉可以直接比较。
- 捕获 `390 × 844` 的会话、资产抽屉和项目设置关键页，确认无页面级横向溢出、裁切或不可达主操作。
- 记录核心交互：会话新建/搜索/删除确认，Agent 选择与依赖受阻，资产搜索/筛选/抽屉开合，Credential 多字段校验，MCP Credential submit/approve 失败时保持上下文。
- 对上述页面逐一检查浏览器 console；最终报告应明确错误数量和检查状态。
- 修复后为每个 P1/P2 生成同视口、同数据、同交互状态的第二轮对照，并写明“早期发现 → 修复 → 后置证据”。

### Comparison history

- 第 1 轮：在 `1672 × 941` 下完成新会话、Agent 选择器、Agent 抽屉和 MCP 页面同画面对照。发现 1 个 P1 和 2 个 P2；尚无修复后截图。
- 同轮确认的产品约束：不伪造接口未提供的字段、示例会话、资产数量、依赖名称或运行状态；不同真实数据状态不做像素级误判。

### Implementation checklist

- [ ] Agent 选择器依赖可用性与后端逻辑一致并 fail closed。
- [ ] 项目导航按真实路由分组，具备品牌与当前路由激活态。
- [ ] `Chats` 可见标签统一为“会话”。
- [ ] 补齐同状态桌面与 `390 × 844` 移动截图。
- [ ] 补齐正常执行、Human input、文件/Sidecar、Skill/MCP 抽屉证据。
- [ ] 记录核心交互和 console 检查。
- [ ] 对全部 P1/P2 完成修复后复核。

### Follow-up polish

- P3：如果后续 API 能批量解析固定版本 ID，可在不改变依赖合同的前提下同时展示资产名称与短 ID；本期不应在前端猜测名称。

final result: blocked

### 2026-07-22 最终验收续测（标准 `make dev`）

本轮先停止此前的临时启动方式，再从仓库根目录使用标准 `make dev` 启动 Gateway、Worker、Frontend 与 Nginx；统一入口为 `http://localhost:2026`。验收使用真实登录态、真实默认项目和真实 API 数据，没有伪造设计图中的会话、资产或运行结果。

#### 验收范围与证据

- 桌面视口：`1280 × 720`，覆盖会话入口、已有会话、Agent 选择器、Agent/Skill/MCP 列表和详情抽屉、删除确认。
- 移动视口：`390 × 844`，覆盖会话入口/详情、项目导航抽屉、会话列表抽屉、MCP 列表和全宽详情抽屉。
- 截图目录：`design-qa-artifacts/final-acceptance-2026-07-22/`，共 14 张现场截图。
- 浏览器 console：上述页面与交互完成后为 0 warning、0 error。
- 运行日志：本轮 MCP 列表和版本接口返回 200；日志中 2026-07-21 的旧 500 不是本轮请求。

#### 分步结果

1. 桌面会话入口：健康。项目主导航已按“工作 / 能力 / 项目管理”分组，当前路由有激活态，`Chats` 已统一为“会话”。
2. 桌面已有会话：健康。输入框、附件、输入润色、Pro、模型选择和快捷操作均可见；文件/Artifact 侧栏不是常驻区域。
3. Agent 选择器：健康。当前真实 Agent、Skill、MCP 都已绑定并启用版本 1，早期“可立即启用但依赖必然失败”的状态无法复现。
4. Agent、Skill、MCP 列表：健康。三类页面有各自的文案、筛选、真实资产和状态，不再是同一张“管理绑定”模板。
5. 三类资产详情抽屉：内容与响应式健康。抽屉展示接口实际返回的版本、说明、兼容性、依赖、传输方式、超时和 Credential 槽位等字段；桌面从右侧进入，390px 下为 `390px` 全宽且无横向溢出。
6. 移动项目导航与会话列表：布局健康。两类抽屉均为约 `320px`，搜索、新建、当前项和关闭入口可达，页面 `scrollWidth === clientWidth`。
7. 移动会话入口与详情：布局健康。欢迎区、Composer、模型与快捷操作在 `390px` 下没有页面级横向溢出。
8. 删除保护：部分健康。实际删除前会出现确认对话框，取消后会话保留；但确认文案遗漏空标题，见下方 P2。
9. 键盘与语义：部分健康。导航有 accessible name、分区和 `aria-current`，详情使用命名 dialog；但详情抽屉按 Escape 关闭后焦点落到 `BODY`，见下方 P1。

#### 当前阻断问题

- [P1] Agent/Skill/MCP 详情抽屉关闭后没有恢复触发资产行的焦点。
  - 现场复测：抽屉内初始焦点为“切换版本”；按 Escape 后 `dialogCount=0`，但 `document.activeElement === BODY`。
  - 原因：受控 Sheet 没有与资产行建立 `SheetTrigger` 或显式焦点恢复关系。
  - 位置：`frontend/src/components/projects/assets/project-asset-page-shell.tsx` 与 `frontend/src/components/projects/assets/project-asset-detail-sheet.tsx`。

- [P1] 最终确定性 E2E 门禁尚未跟随新抽屉流程与严格 Thread schema。
  - 隔离 Chromium 聚焦清单为 47 项：31 passed、16 failed。
  - 2 项仍按旧卡片内按钮操作资产，2 项仍断言旧文案，12 项 Chat/Sidecar fixture 缺少顶层 `created_at` / `updated_at` 并产生级联失败。
  - 这不说明现场 UI 的 16 个功能都坏了，但意味着当前自动化门禁没有证明重构后的真实路径，不能宣告最终通过。

- [P2] 删除确认标题缺少列表已经使用的回退值。
  - 现场文案为 `“”及其关联的侧边对话将被删除`；列表中同一会话显示“新对话”。
  - `project-conversation-rail.tsx` 列表使用 `projectConversationTitle()`，删除弹窗却直接使用 `titleOfThread()`，空字符串没有回退。

- [P2] 移动会话列表的删除按钮默认 `opacity-0`，只在 hover 或 focus-within 时显示，纯触屏用户难以发现删除入口。

- [P2] 通用 Sheet 关闭按钮的实测触控区域只有 `16 × 16px`，且中文界面的可访问名称固定为英文 `Close`。

#### 低优先级体验风险

- [P3] 移动端“项目导航”和“会话列表”是两个位置接近、外观几乎相同的汉堡按钮；虽然 accessible name 不同，首次使用时仍可能混淆。
- [P3] 移动会话 Sheet 的隐藏标题为“项目会话”，内容内部再出现 `h1“会话”`，标题层级可进一步整理。

#### 自动化结果

- 前端聚焦单元测试：22 个文件，119 passed，0 failed。
- 后端 MCP 500 精确回归：`test_project_assets_router.py` 17 passed，0 failed。
- 隔离 Chromium E2E：31 passed，16 failed；失败均需按本轮重构同步测试流程、文案或 Thread fixture。

#### 证据边界

本轮没有提交真实模型 Run，避免无授权消耗外部模型额度或创建额外业务数据。因此正常执行、Human input、生成文件预览/源码和 Sidecar 的动态运行态仍沿用已有实现证据，不能计入本轮现场通过项。本文不据此宣称完整 WCAG 合规。

最终结论：桌面、移动和三类详情抽屉的核心布局与内容已基本符合确认方向，MCP 500 现场及精确回归均通过；但焦点恢复 P1 与 16 项陈旧 E2E 门禁仍阻断“最终通过”，另有 3 个直接影响移动/删除操作的 P2。

final result: blocked
