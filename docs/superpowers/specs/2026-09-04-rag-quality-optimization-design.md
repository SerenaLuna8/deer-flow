# RAG 知识库优化规格：确定性修复与质量增强分期

> 状态：设计草案，待用户确认；不是实施计划，不代表优化已实现。
> 日期：2026-09-04。
> 代码基线：`680b0fe45d006460cfd67e814fc84ae4f0de9b26` 加当前工作区未提交修改，包括 `PrefixTokenCounter` 和 Markdown parser 缓存优化。
> 本轮仅新增本规格，不修改实现、数据库、运行配置，不提交或部署。
> 使用 Superpowers brainstorming 形成设计；用户审阅后，再通过 writing-plans 按独立工作包制定实施计划。

## 1. 目标与证据边界

目标是修复已经证明的分段参数失效和文字解释错误，补齐低成本的 Word 标题识别，并明确模型容量边界；不以未经测量的召回收益推动整体重构。

### 1.1 已确认事实

以下路径均相对仓库根目录；`K/` 表示 `backend/packages/knowledge/actweave_knowledge/`。

| 编号 | 已确认行为 | 当前证据 |
| --- | --- | --- |
| F01 | 普通长段落被拆为 `text_fragment` 后，现有 overlap 保留逻辑不适用；PDF 普通正文保留 `page` 类型，也不在允许列表中 | `K/ingestion/splitter.py:515、573、636`；`K/ingestion/structure.py:116`；`K/extraction/builtin/pdf_extractor.py:78` |
| F02 | 部分非 Markdown 原文被解释为 Markdown 结构，导致索引文字丢失或改变。例如 `- item` 丢掉标记、`---` 变为空、`&amp;` 被再次解码 | `K/extraction/builtin/{word,pdf,html,text}_extractor.py`；`K/extraction/unstructured_local/elements.py`；内存探针 |
| F03 | Word 仅精确识别 `Heading 1–6`，未根据自定义样式的大纲级别和继承关系识别标题 | `K/extraction/builtin/word_extractor.py:227`；python-docx 内存对象验证 |
| F04 | Embedding 注册信息和 `KnowledgeEmbeddingMaterial` 没有模型输入容量、实际 tokenizer 契约；客户端直接发送文本 | `backend/app/model_registry/gateway.py:206`；`K/contracts.py:133`；`K/models/client.py:102` |
| F05 | CSV/Excel 逐行生成独立分段；Markdown 表格可合并多行，小代码块也可与正文同段 | `K/extraction/tabular.py:81`；`K/ingestion/structure.py:174、201、220` |
| F06 | PDF 逐页抽取文字层和图片，没有额外的版面分析、段落重建、页眉页脚识别或断词修复 | `K/extraction/builtin/pdf_extractor.py:49` |
| F07 | 冻结配置不等于保留历史执行器；当前配置不匹配就拒绝重试。手工父子分段派生还存在直接调用 splitter 的入口 | `K/ingestion/profiles.py:199`；`K/segments/service.py:144、222` |

本轮重新执行原分段、索引、Tokenizer 测试及冻结版本测试，共 **81 项通过，5.92 秒**。该结果只证明这些现有断言通过，不代表 F01/F02 不存在，也不是生产验收。

前一轮只读合成样例还确认：同等 30 条数据加表头，CSV 得到 31 个分段，Markdown 表格得到 1 个分段；普通段落双换行可产生重叠，真实 `page` 类型同样输入不产生重叠。样例不是实际业务语料。

### 1.2 合理推测与未验证假设

- 修复正文边界和文字解释错误可能改善检索，但改善幅度未知。
- 多行表格打包可能减少短分段和向量数量，也可能降低精确行定位的召回质量。
- 重复标题可能增加标题词匹配的候选；当前父块词法召回取最大子分数，不按子块数量求和。
- 实际使用的 Embedding 模型、部署服务、输入模板及超限行为尚未确认；不能认定正在静默截断。
- PDF 是否是当前业务最大的瓶颈、500 万字符耗时多少、是否优于其他 RAG 系统，均无本轮实测依据。

## 2. 方案比较与推荐

| 方案 | 内容 | 成本与风险 | 结论 |
| --- | --- | --- | --- |
| A：仅确定性修复 | 修复 overlap、字面量解释、自定义 Word 标题，补齐限制说明 | 无数据库变更和新依赖；仍需版本与资源锁发布验收 | 首期推荐 |
| B：修复后做定向增强 | 在 A 基础上，分别评测表格行打包、建立目标模型容量契约、调查 PDF 版面问题 | 每项独立验证、审批，避免同时改变多个召回变量 | 作为后续路线 |
| C：重建解析/分段框架 | 引入通用 AST、通用 tokenizer 框架、OCR/远程解析或多版本执行器 | 接口、依赖、部署和维护成本显著增加，缺少当前必要性证据 | 不采用 |

推荐 **A 先交付，B 分项准入**。本规格不把“提示限制”包装成“已经解决模型超限”，也不把旧规格明确要求的行边界当成程序错误。

## 3. 交付范围

| 工作包 | 内容 | 本规格状态 |
| --- | --- | --- |
| A1 正文 overlap | 同一合法分组内普通正文的安全后缀重叠，包括 `page` 和长段落碎片 | 首期目标行为已定义 |
| A2 原文字面量保真 | 非 Markdown 原文不能凭符号外观变成 Markdown 指令或结构 | 首期目标行为已定义 |
| A3 Word 自定义标题 | 大纲级别与样式继承回退，限 Markdown 可表达的一级至六级 | 首期目标行为已定义 |
| A4 用户预期与发布约束 | Token 口径、预览能力、历史版本、手工派生和资源锁约束 | 首期共同必需项 |
| B1 表格行打包 | CSV/Excel 同表相邻短行的分段策略改变 | 候选增强；未批准启用 |
| B2 模型真实容量保护 | 目标模型的计数、上限、模板和共享发送前检查 | 独立接口规格的准入要求；不在首期实现 |
| B3 PDF 版面增强 | 依据真实失败文件选择页内修复策略 | 先评测，不预先指定新解析器 |
| B4 性能优化与清理 | 依据测量决定剩余 parser 缓存、热点或死代码清理 | 无瓶颈证据不改代码 |

首期不实现：跨页合并、跨标题小块回填、Word 自动编号还原、`Title/Subtitle` 自动章节化、Word 七至九级标题映射、标题前缀降为叶子标题、无子块时复制父块兜底、提高配额、通用历史算法分派、OCR、远程解析、运行时下载。

## 4. 不得改变的共同契约

1. 预览、摄取和显式重新解析继续共用现有 extraction 与 `split_documents`，不增加第二条默认处理路径。
2. `content` 是显示 Markdown；Embedding、词法和 Reranker 消费 `index_text`。不得通过向量侧偷偷删字或截断绕过预算。
3. `SourceSpan` 和附件出现位置必须准确对应最终文本；原文属于 `source`，重复标题或字段标签等上下文属于 `context_prefix`。来源归属不是访问授权。
4. 保留服务器身份、Project 权限、任务租约、版本比较和原子发布；任何失败不得混用新旧分段、图片或向量。
5. token profile 父段默认 1000、overlap 默认 100；父子模式子块默认 500。父段范围 200..4000、overlap 0..500 且小于父段、子块范围 100..2000 且小于父段，不变；历史 character 值不套用这一 Token 口径。
6. token profile 的父段和 Child 分别满足显示 Markdown Token、`index_text` Token 及 16000 字符上限；标题、表头、分隔符和保护字符均参与相关预算。overlap 不是额外赠送的预算。
7. 每文档父分段最多 5000，父子模式的累计 Child 向量条目另限 5000；两者不是合计 5000。沿用当前其余配额检查，不增加上限。
8. Child 在各自父段内零重叠。父段重叠后，不同父段的 Children 可能覆盖相同原文；不新增跨父段全局去重。
9. 冻结 character 算法不改写为 token 算法；重新向量化不读取原文件、不重新切分、不丢失人工编辑。
10. 解析继续本地、离线、受 OS 沙箱约束；复用现有依赖、来源映射和错误机制，不新建解析服务、数据库表或通用框架。

## 5. A1：正文 overlap

### 5.1 适用范围

- 仅 token 父段生效，覆盖同一合法打包组中的普通正文，包括 PDF 页内正文和 fallback 后的长段落碎片。
- 页面、标题组、表格及已有明确分组边界不跨越；不得把“补重叠”顺带变成跨页合并。
- 普通段落之间可以重叠，即使来自不同 Word 段落或不同 `block_id`；只要它们本来属于同一允许打包组，来源映射可以同时包含多个原始位置。
- 原有列表整块重叠能力保留；不新增列表内部截断重叠。代码块、字段行、表格行和带附件单元不参与正文后缀复制。
- 来源容器类型与 Markdown 块类型不能混为一谈：PDF 的 `page` 不能成为禁止普通正文重叠的理由，但页码仍保留为硬边界。

### 5.2 后缀选择与预算

1. 先尝试保留末尾完整正文单元，维持既有完整单元优先行为。
2. 最后一个可拆正文单元超过 overlap 时，按用户分隔符和已有 fallback 顺序，在不切断 Markdown 原子的合法边界上选择连续后缀；不得只因整块太长就固定退化为零重叠。
3. 重复正文的显示 Token 和索引 Token 都不超过 overlap；上下文前缀不计入重复正文的 overlap 值，但计入最终段的总预算。
4. 下一段按“上下文前缀＋重复后缀＋新正文”测量。若预先生成的正文片段接近满预算，应在预留后缀的条件下继续拆分该片段，而不是例行清空后缀。
5. 只有不能同时容纳保留后缀和最小合法新正文单元时，才缩减重叠；原子不可拆或硬边界允许得到小于设置值、甚至为零的实际重叠。
6. 每次发段必须消费此前未输出的新正文；禁止纯重叠段、无限循环和只重复标题的段。

### 5.3 来源与图片

使用现有 `slice_unit`、`join_units` 与 span 裁剪重定位。重复正文保留原始 `source` 身份和物理位置；不得把它改标为上下文而隐藏来源。

图片 ref、行内代码、链接是不可拆原子。不得复制 AttachmentOccurrence 来填 overlap，也不改变现有临界图片与真实正文绑定策略。若尾部为不支持重叠的结构，允许减小 overlap，不新增逐段警告噪音。

## 6. A2：非 Markdown 原文字面量保真

### 6.1 处理边界

保护在格式 Adapter 的**原文文本叶节点序列化阶段**完成，不在公共 normalizer 中对整段 Markdown 全局转义。

| 来源 | 需要保护 | 必须保留 |
| --- | --- | --- |
| TXT、PDF | 原始文本及行首上下文 | 现有文字顺序、换行语义、物理来源和附件 |
| Word | 普通 run、链接可见文字中的原文字面量；跨 run 的行首结构上下文 | Adapter 生成的标题、表格、链接和图片标记；已有缩进保护 |
| HTML | `NavigableString` 等解码后的普通文本叶节点 | 原生标题、列表、表格、引用、安全链接、`code/pre` 的既有转换 |
| Unstructured | 普通元素及缺少结构的表格 fallback 文本 | 可靠的元素标题 metadata；有 `text_as_html` 的表格继续走 HTML 转换 |
| 原生 Markdown/MDX | 不进入普通文本保护路径 | 现有 Markdown 结构、代码和安全图片规范化；不执行 MDX |

可复用一个小型纯函数处理共同的字面量序列化，但必须放在 extraction 所有权内；上下文不同的 HTML/Word 结构生成逻辑不强行统一。不得引入通用 AST、额外依赖或第二套全局来源映射。

### 6.2 必须达到的行为

- 源文字面量 `# text`、`- item`、`+ item`、`1. item`、`1) item`、`> text`、`---`、`text\n===`、反引号、管道符、链接外观、图片外观、反斜杠和 `&amp;` 不得凭外观生成结构或再次被实体解码。
- 真实结构标记、安全链接目标、代码块正文和受权图片 ref 不被重复转义。
- 四空格、Tab、标记跨 Word run、紧邻图片的换行必须一起处理；不能把已添加的保护反斜杠送入意外代码块，污染 `index_text`。
- 不借此增加 PDF/TXT/Word 的空白压缩、断词修复或内容清洗。HTML 原有布局空白折叠不变。
- 验收“可见文字和结构语义保真”，不是要求经过既有索引空白规范化后逐字节复原原文件。
- 优先先渲染再分配 offset；必须修改已分配 offset 的场景，复用现有精确 remap。不能把转义后的字符位置伪称为原文件新的页码、行号。
- 额外 Markdown 保护字符可能增加显示 Token，导致段数量变化；必须重新走现有双预算，不能扣除保护字符绕过上限。

## 7. A3：Word 自定义标题

1. 保持已识别内置 `Heading 1–6` 的现有行为。
2. 对其他样式，先读取段落直接声明的 `w:outlineLvl`；未声明时，依次读取本样式及 `base_style` 链，采用最近的显式声明。
3. `0..5` 映射为 Markdown 一级至六级。遇到显式非支持级别，保持普通正文并停止向上继承；不按字号、加粗、名称含“标题”或正文编号猜测层级，不默默截成第六级。
4. 样式链使用已访问集合保证终止；不修复输入文档、不引入样式分析框架。
5. `heading_path` 保存可读标题原文，不保存 Markdown 保护反斜杠。显示时再生成结构标记和安全文字。
6. 后续普通段落正确继承已识别的标题路径；未识别的新标题不代表清空所有上级路径。

`Title/Subtitle`、自动编号、七至九级标题不作为本包完成项；这些需要独立定义产品语义，不能用自定义标题回退顺带猜测。

## 8. A4：用户提示与能力说明

复用现有 `wizard.knowledgeTokenUnit`，不新增模型容量字段或配置面板。

建议中文文案：

> “知识库 Token”是固定的本地分段单位，不等于所选 Embedding 模型的输入 Token。分段预览不校验模型输入上限；请结合所选模型和服务的限制配置分段。

英文文案表达相同事实。新建知识库上传、已有知识库上传和重新解析表单均显示该说明；上传向导已有展示位置，重新解析复用同一翻译 key。

overlap 的用户说明须明确：“最多保留设定数量的正文 Token；不跨页面、标题或表格边界，实际重叠可能更少。”不把“每两个分段恰好重复 N Token”作为承诺。

不恢复每个文档的内部 parser/tokenizer/version 元数据行，不新增大量重复 warning，不改变默认参数、请求身份或现有预览交互。历史版本错误使用明确的重新解析提示，重新解析继续警告将替换人工编辑、启停状态和附件绑定。

这一包只消除错误预期；**模型超限风险仍然存在**，其真正保护见 B2。

## 9. 版本、缓存和旧文档处理

### 9.1 首期版本身份

| 行为变化 | 版本要求 | 提取缓存 | 预览身份 |
| --- | --- | --- | --- |
| A1 token splitter 行为 | 目标 token splitter 版本为 `splitter-v3`；实施时核对是否已被其他工作占用 | 仅改 splitter 时仍可复用同 ParseProfile 的提取结果 | 完整 profile 与 capability revision 改变，旧预览失效 |
| A2/A3 Adapter 输出 | 共用 `ADAPTER_REVISION` 从 `adapter-v1` 升至 `adapter-v2`，同步资源锁 | 新 ParseProfile，不使用旧解析输出冒充新结果 | 失效 |
| 公共 normalizer 本身改变 | 本方案不要求；若实现确实改变其行为，必须单独升级 normalization version | 失效 | 失效 |
| A4 说明文字 | 不改变处理版本 | 不变 | 不因纯文案变化失效 |

当前所有格式共用 Adapter revision 和资源摘要，因此 Adapter 升级会影响所有格式的冻结解析身份，不只是 Word/PDF。使用现有 `backend/scripts/build_extraction_resources.py` 和实际支持平台核验资源锁；不得伪造未验证平台条目。

固定 tokenizer、词表和清洗规则没有变化时，其版本与摘要不变。character 算法保持现状，不因 token 行为变化虚称获得了新算法；它的完整历史解析配置是否还能执行，仍受当前冻结配置可用性检查约束。

### 9.2 读取、重试、重新处理和手工编辑

- 已发布 Segment、Child、Extraction 和索引不自动重写，不后台批量重新解析。
- 旧冻结配置不可执行时，继续以 `PROCESSING_PROFILE_UNAVAILABLE` 明确拒绝重试；不得替换成新配置再继续。
- 显式重新解析采用用户确认的新 profile，预览与发布使用同一身份，并在既有事务中替换全部派生结果。
- 重新向量化继续只读取已发布索引文本；不会借此获得 A1/A2/A3 的文本修复，也不会清除人工编辑。
- 必须覆盖绕过完整解析配置验证的直接 splitter 调用。`split_documents` 的 token 分支应拒绝不支持的 splitter 版本，不能在旧 `splitter-v2` 身份下执行新算法。
- 这意味着旧 token parent-child 文档若手工新增/修改需要重新派生 Children，应在模型发送和数据库修改前明确拒绝，提示显式重新解析；不得偷偷升级整个文档 profile。前端保留本次未保存编辑，不自动触发重新解析。
- 不需要重新切分的普通 Segment 编辑，以及现有 null/character 的手工派生路径，不因无关 parser 版本失配被额外阻断；仍执行各自现有预算和权限检查。
- 不复制父块作为缺失 Children 的兜底；保留失败保护，直到有明确复现和满足子预算、来源条件的根因修复。

首期不增加历史 token 算法注册表。若业务要求“升级后旧 token 文档仍可按旧算法编辑或重试”，须另行批准历史执行器维护范围；不能同时声称不保留旧实现又保证其继续执行。

### 9.3 发布约束

首期无 schema 变更、无自动数据迁移。Gateway 与 Worker 使用相同代码及资源版本，一起发布；部署前处理在途解析任务，避免不同能力版本混跑。部署与任务处置是后续显式操作，不是本规格授权。

单纯回退代码不保证新 profile 可继续重试或编辑。恢复旧服务版本前应停止新的处理准入，并核对已发布新 profile 的兼容性；不回写、伪造版本、不删除新数据。目标环境回退流程必须在发布计划中验收。

## 10. 首期验收规格

每个用例既验证文本，也验证 `index_text`、Token、source spans 和附件，不只比较分段数量。新增测试先证明当前缺陷，再验证修复；以下是未来验收标准，不是本轮已通过清单。

| ID | 用例与通过条件 |
| --- | --- |
| A01 | 英文单长段、单换行长段、无空格长中文、中文标点 fallback：可安全继续拆分时，正 overlap 产生真实尾部正文重复；零 overlap 不新增重复 |
| A02 | 使用真实 `Document(kind="page")`：同页普通正文可重叠，不同页不合并、不重叠；不能仅用默认 paragraph helper 冒充 PDF |
| A03 | 正文恰好接近满预算、overlap 接近允许上限：后续正文重新分配预算，循环持续消费新内容，没有纯重叠段 |
| A04 | 图片、链接、行内代码、代码块、字段行处于边界：原子完整，附件出现次数不增加，不把不支持结构复制进 overlap |
| A05 | 新 token profile 的所有父子结果通过显示 Token、索引 Token、字符限制；父段与累计 Children 的 5000 上限分别触发，超限不部分发布；历史 character 按原契约回归 |
| A06 | `source` 覆盖原文不丢失；新增重复仅来自允许的 overlap 或既有上下文；原文中本来重复的内容不能被去重 |
| A07 | TXT/PDF/Word/HTML/Unstructured 的字面标记矩阵保留可见文字，不生成伪标题、列表、链接、图片或分隔线；纯 `---` 原文仍可索引 |
| A08 | 四空格/Tab、跨 Word run 的标记、图片前后文字、字面 `&amp;`：没有保护字符泄漏、重复实体解码或 offset 漂移 |
| A09 | 原生 Markdown 的真实标题、列表、代码与图片，以及 HTML 的真实 h/ol/table/code/pre/a，结构行为不退化 |
| A10 | Word 自定义直接大纲级别、多级样式继承、最近显式值、普通正文及样式环：符合 A3；内置 Heading 1–6 回归通过 |
| A11 | 同一文件与同一新 profile 的预览、冷解析、可复用缓存和正式摄取，正文/索引/来源/警告一致；图片 ID 与绑定闭包正确 |
| A12 | 只改变 splitter 版本不强制重新提取；改变 Adapter 版本不读取旧缓存冒充新结果；旧预览 fingerprint 不被接受 |
| A13 | 旧 token profile 的重试和直接二次切分明确拒绝；无模型请求、无部分写入；已发布内容仍按现有治理状态可读 |
| A14 | 新 token profile 的手工父子派生保持用户输入为一个完整父段，父 overlap=0；null/character 原路径回归，不虚构原文件位置 |
| A15 | 重新向量化零原文件读取、零重新切分；重新解析失败不混用新旧发布代次；摘要保持现有批次发布语义 |
| A16 | 三个分段配置入口正确显示单位/预览限制和 overlap 语义；中英文一致；参数、payload、权限、预览过期逻辑不因提示改变 |
| A17 | 来源或预算异常、授权撤销、失租约、版本竞争仍明确失败；不可用 parser/tokenizer 不静默替换，不增加联网解析 |
| A18 | 当前工作区的 PrefixTokenCounter 与缓存优化保持其数值等价测试；本次不顺手重写或删除用户未提交优化 |

## 11. 后续增强的具体准入条件

### B1：CSV/Excel 相邻行打包

旧规格明确要求逐行独立。因此这是一项产品行为调整，不是 A1 的附带修复。

候选设计：提取阶段继续保留逐行字段和物理来源；只在 splitter 内打包已确认同一来源表、同一 sheet、同一标题上下文中的相邻短行。字段名和值继续逐行成组显示，行间有明确分隔；不把多个值错误归给同一行。

可单独放入预算的一行不得为了填满前段而拆开。超长行继续走现有字段拆分，并与其他记录分开打包；表头、说明、图片、非字段内容、不同表或 sheet 都结束当前组。原始表头完整保留一次，重复字段标签仍是上下文。Word 表格与 Markdown 表格规则不顺带修改。

不得凭相同列名、空白行或 `(None, None, None)` 猜测共同表身份；当前解析器会省略空行。实施前必须确认 CSV/Excel 行身份可被下游无歧义识别，再确定最小契约调整。

设计与试验准入：提供有授权的实际 CSV/Excel 查询样本，并确认可调整旧版逐行独立语义，再单独批准试验范围。默认启用准入：分别评测精确行查询和跨行查询，证明向量/分段数减少且关键精确行查询不退化后，才发布新的 token splitter 策略。默认不预建“是否合并”持久化开关；若逐行模式必须保留为用户选项，另行确认其产品需求和冻结参数契约。

### B2：实际 Embedding 输入容量保护

必须先确认目标模型及服务版本、实际 tokenizer/资源版本、query/passage 输入模板、特殊 Token 开销、逐项输入上限、服务是否截断及如何拒绝超限。短文本连接测试不能替代这项确认。

这些信息没有给定时，不决定 tokenizer 依赖、不登记猜测的容量、不用 cl100k 数值冒充实际模型计数。B2 尚未解决，首期文案只是风险说明。

独立规格必须满足：

1. 保护放在共享 `KnowledgeModelClient.embed`，在拼接实际前缀后、任何本次 HTTP 发送前检查全部输入；覆盖摄取/重新解析、重新向量化、手工编辑、查询、摘要索引。
2. 使用已确认的计数与输入模板；本地资源安装时固定，运行时不下载。资源缺失不能偷偷降级为不等价估算。
3. 超限明确失败、零 HTTP 发送，不截断已发布 `index_text`，不为了适配容量自动重新解析用户文档。
4. 模型能力、输入模板、缓存身份、既存向量兼容性、修改权限与历史行处置一起定义；不能只增加 `max_input_tokens` 字段。
5. 若需要 schema 改动，同步 ORM、Schema V1 SQL、生成注释、catalog digest、结构测试及正式升级链路；无运行时建库、stamp 或手工修表。
6. 使用权威边界样本验证上限内/上限外、中英混合、长标识符和模板开销；离线计数与真实目标服务分别验收。

### B3：PDF 版面与 Word 其他结构

先收集真实失败文件，标注预期阅读顺序、段落、标题或编号，再决定单一改进。PDF 优先调查页内行连接及断词；不先删除页眉页脚，因为误删会损失正文。不默认跨页合并，不以空 `heading_path` 推断所有标题都不可用。

Word 自动编号需要同时处理编号定义、层级和继续/重启语义；`Title/Subtitle` 和七至九级标题需要明确产品映射。它们不能通过样式名猜测顺带交付。

每次仅改变一类规则，验证文字覆盖、物理来源、前后检索结果及平台解析一致性；收益不足时保持现状，不预装新解析框架。

### B4：性能与清理

先测解析、清洗、结构解析、分词、打包、父子派生的分项耗时和峰值内存，记录文本长度、段/子块数量和参数。比较相同输入与相同配置，区分增加 overlap 带来的合理输出增长和算法退化。

性能改动必须证明分段边界、正文、`index_text`、spans、warnings 和预算逐项等价。剩余 MarkdownIt 缓存只在测量证明必要后实施，保持显示解析与来源解析的规则实例隔离，不能共享后再修改规则。

`attach_children` 仍用于 character 路径，不删除。其他旧接口先核实全仓调用与导出契约，单独清理，不和行为修复混在一个交付中。不把微型样例外推为 500 万字符的耗时结论。

## 12. 文件所有权与验证落点

本表是后续计划的定位依据，不授权修改其中所有文件，也不是逐步实施计划。

| 工作包 | 主要实现位置 | 现有测试落点 |
| --- | --- | --- |
| A1 | `K/ingestion/splitter.py`、`structure.py` | `backend/tests/knowledge/test_markdown_chunking.py` |
| A2 | `K/extraction/builtin/text_extractor.py`、`pdf_extractor.py`、`word_extractor.py`、`html_extractor.py`；`K/extraction/unstructured_local/elements.py`；必要的小型共享 literal helper | `test_builtin_text_extractors.py`、`test_builtin_office_pdf.py`、`test_local_unstructured.py`、`test_index_text.py` |
| A3 | `K/extraction/builtin/word_extractor.py` | `test_builtin_office_pdf.py`、`test_markdown_chunking.py` |
| A4 版本 | `K/ingestion/profiles.py`、`splitter.py`；`K/extraction/runtime_resources.py`、`resources.lock.json`；核对 `K/segments/service.py` | `test_parsing_profiles.py`、`test_extraction_resources.py`、`test_extraction_cache.py`、`test_parsing_governance.py`、`test_parsing_pipeline.py` |
| A4 用户说明 | `frontend/src/core/i18n/locales/zh-CN.ts`、`en-US.ts`；`frontend/src/components/projects/knowledge/knowledge-documents-view.tsx`；复用 `knowledge-create-wizard.tsx` 已有位置 | `frontend/tests/e2e/project-knowledge.spec.ts`；相关组件单测 |
| 文档 | `README.md`、`backend/AGENTS.md`、`frontend/AGENTS.md` | 行为说明、版本与边界检查；不把指南扩成变更日志 |

上表未带完整前缀的后端测试均位于 `backend/tests/knowledge/`。确需新增 helper 或测试文件时，由已批准的实施计划明确唯一位置，不预先拆分大型模块。

### 12.1 后续实施验证

基础测试示例：

```bash
cd backend
uv run pytest tests/knowledge/test_markdown_chunking.py tests/knowledge/test_index_text.py tests/knowledge/test_knowledge_tokenizer.py tests/knowledge/test_parsing_profiles.py -q
uv run pytest tests/knowledge/test_builtin_text_extractors.py tests/knowledge/test_builtin_office_pdf.py tests/knowledge/test_local_unstructured.py tests/knowledge/test_extraction_resources.py -q
```

随后执行受影响的 PostgreSQL/MinIO 解析发布、手工治理、重解析/重嵌入及权限测试，使用测试框架创建的隔离目标；不得把开发或生产数据库当测试重置目标。缺少真实服务时明确记为未验证，不能把 skip 算通过。

按仓库要求完成后端格式和相关本地门禁；前端运行 `pnpm check`、相关单测及 `pnpm test:e2e tests/e2e/project-knowledge.spec.ts`。真实后端浏览器测试使用现有 `playwright.real-backend.config.ts`，不得把 mock 测试当成端到端生产证明。解析资源与沙箱在实际目标平台另行验证。

### 12.2 检索质量门

首期可用确定性断言证明缺陷修复，但“召回变好”必须另有证据。B1/B3 的默认行为调整必须先过质量门：

- 使用有授权且冻结的文件和查询，标注预期文档、物理来源和关键原文，不把重新解析后会变化的 Segment UUID 当标准答案。
- 查询至少覆盖长段落边界、精确表格行、标题上下文、字面标识符四类；每类至少 5 条，总计至少 20 条。这是最低冒烟集，不是统计显著性声明。
- 前后固定 Embedding/Reranker、检索路线、top_k、阈值、摘要开关和语料；不同时调参。
- 记录 Hit@5、MRR@5、关键定位成功情况、总段/向量数和耗时。关键精确来源查询不得退化，整体 Hit@5/MRR@5 不低于基线；没有达到标准则不批准该增强作为默认策略。
- 结果按“离线内容验证 / PostgreSQL 检索 / 实际模型 / 目标平台”分别报告，禁止用 81 项现有单测代替质量门。

## 13. 风险、实施顺序与评审关口

### 13.1 维护成本与影响

- A1 修改复杂打包逻辑，主要风险是预算预留、循环进展、图片和来源边界；应复用既有裁剪及重打包工具，不新增独立 splitter。
- A2 可能增加转义长度和分段数量；所有格式共用 Adapter revision，会扩大历史配置不可执行的范围。这是必须提前说明的发布成本。
- A3 的范围刻意限于已有依赖可以确定的大纲级别，不维护 Word 全部排版语义。
- 新 token splitter 的版本检查会使旧 token parent-child 文档的手工二次派生需要先重新解析；重新解析可能覆盖人工编辑，必须明确提示并由用户决定。
- 首期不改变数据库、模型选择和检索融合；模型超限、PDF 高级版面能力和表格行粒度的剩余风险不会自动消失。

### 13.2 推荐顺序

1. 用户确认本规格首期范围、历史版本处理及后续增强准入条件。
2. writing-plans 拆成分段契约、格式保真/Word 标题、前端说明三个独立实施计划；版本身份和发布验收作为共同约束随各包落实，不能最后补票。
3. 各包按 TDD 独立验证；A2/A3 同改 Word 文件，不并行写该文件。公共 splitter、profiles 和资源锁由明确的单一集成人员协调。
4. 汇总跨路径、平台和发布证据，确认用户现有未提交改动没有被覆盖，再决定是否部署。
5. B1/B2/B3/B4 分别满足准入条件后重新形成独立规格，不自动接续开发。

本规格确认不等于数据库操作、提交、部署或外部模型调用授权。本轮到设计评审为止。
