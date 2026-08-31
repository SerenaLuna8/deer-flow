# Dify 知识库文件解析机制分析

> 分析对象:Dify 开源仓库(本地路径 `/Users/jiangfeng/dify`,后端代码在 `api/`)
> 分析日期:2026-08-31
> 用途:为本项目 RAG 知识库的文档摄取(ingestion)设计提供参考

## TL;DR

Dify 的文件解析位于 `api/core/rag/extractor/`,核心是一个策略模式分发器 `ExtractProcessor`,按 **数据源类型 → ETL 模式(`ETL_TYPE` 配置)→ 文件扩展名** 三级路由到 14 个具体解析器。高频格式(PDF/Word/Excel)使用 Dify 自研解析器,深度定制了图片持久化、表格转 Markdown、超链接保留;长尾格式(ppt/eml/msg/epub/xml 等)委托给 Unstructured 库(本地或 API)。所有解析器统一输出 `list[Document]`,且解析阶段就完成了一次"结构化预分段"(Excel 每行、PDF 每页、Markdown 每标题一个 Document)。

## 一、从上传到解析的完整链路

```
前端上传文件
  → POST /console/api/files/upload?source=datasets
    controllers/console/files.py → FileService.upload_file()
    校验扩展名(DOCUMENT_EXTENSIONS)与大小,写对象存储 + upload_files 表
创建知识库文档
  → POST /console/api/datasets/<id>/documents
    DocumentService.save_document_with_dataset_id()   (services/dataset_service.py)
  → DocumentIndexingTaskProxy(...).delay()
    按计费套餐分发 Celery 队列:
      Sandbox 套餐 → normal_document_indexing_task(dataset 队列)
      付费/自部署 → priority_document_indexing_task(priority_dataset 队列)
    (旧的 document_indexing_task 已标记待废弃)
Celery Worker 异步执行
  → tasks/document_indexing_task.py → 文档状态置 PARSING
  → IndexingRunner().run()                            (core/indexing_runner.py)
      → IndexProcessorFactory(doc_form).init_index_processor()
      → IndexingRunner._extract()   按 data_source_type 构造 ExtractSetting
      → index_processor.extract() → ExtractProcessor.extract()   ← 解析发生在这里
      → 状态置 SPLITTING
      → index_processor.transform()  清洗 + 分段(QA 模式还有 LLM 生成)
      → _load_segments() + _load()   写 document_segments 表 + 向量/关键词索引
```

- `IndexingRunner._extract()` 是数据源分叉点:`UPLOAD_FILE` 从数据库取回 `UploadFile` 记录构造 `ExtractSetting(datasource_type=FILE)`;`NOTION_IMPORT`、`WEBSITE_CRAWL` 分别构造对应的 setting。
- 三种索引处理器(paragraph / qa / parent_child)的 `extract()` 完全一致,均委托 `ExtractProcessor.extract()`,只是透传 `is_automatic`(处理规则为 automatic/hierarchical 时为 true)。

`ExtractProcessor` 的非测试调用点共 5 处:

| 调用点 | 用途 |
| --- | --- |
| `core/rag/index_processor/processor/paragraph_index_processor.py` | 通用分段索引 |
| `core/rag/index_processor/processor/qa_index_processor.py` | QA 模式索引 |
| `core/rag/index_processor/processor/parent_child_index_processor.py` | 父子分段索引 |
| `services/file_service.py`(`load_from_upload_file`) | 控制台文件文本预览,截前 3000 字 |
| `core/tools/utils/web_reader_tool.py`(`load_from_url`) | Agent webscraper 工具抓网页转文本 |

另有两个场景绕过分发器直接实例化 `NotionExtractor`:Notion 页面预览接口(`controllers/console/datasets/data_source.py`),以及 Notion 增量同步任务(`tasks/document_indexing_sync_task.py`,先比对 `last_edited_time`,有变更才清旧索引重跑)。

## 二、分发机制:ExtractProcessor 三级路由

文件:`api/core/rag/extractor/extract_processor.py`

对文件类数据源,先把文件从对象存储下载到临时目录,然后按扩展名分发:

```python
input_file = Path(file_path)
file_extension = input_file.suffix.lower()
etl_type = dify_config.ETL_TYPE          # "dify"(默认)或 "Unstructured"
if etl_type == "Unstructured":
    unstructured_api_url = dify_config.UNSTRUCTURED_API_URL or ""
    unstructured_api_key = dify_config.UNSTRUCTURED_API_KEY or ""
    # ... 按扩展名逐一分支
```

### 配置(api/configs/feature/__init__.py,RagEtlConfig)

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `ETL_TYPE` | `dify` | `dify` 或 `Unstructured` |
| `UNSTRUCTURED_API_URL` | `None` | Unstructured 服务地址(SaaS 或自部署) |
| `UNSTRUCTURED_API_KEY` | `""` | Unstructured API 密钥 |

### 格式 → 解析器对照表

| 扩展名 | dify 模式(默认) | Unstructured 模式 |
| --- | --- | --- |
| `.xlsx` `.xls` | ExcelExtractor(自研) | 同左 |
| `.pdf` | PdfExtractor(自研) | 同左 |
| `.docx` | WordExtractor(自研) | 同左 |
| `.htm` `.html` | HtmlExtractor(自研) | 同左 |
| `.csv` | CSVExtractor(自研) | 同左 |
| `.md` `.markdown` `.mdx` | MarkdownExtractor(自研) | `is_automatic` 时用 UnstructuredMarkdownExtractor,否则同左 |
| `.epub` | UnstructuredEpubExtractor(本地) | UnstructuredEpubExtractor(可走 API) |
| `.doc` | 不支持 | UnstructuredWordExtractor |
| `.ppt` | 不支持 | UnstructuredPPTExtractor(必须配 API) |
| `.pptx` | 不支持 | UnstructuredPPTXExtractor |
| `.msg` / `.eml` | 不支持 | UnstructuredMsg/EmailExtractor |
| `.xml` | 不支持 | UnstructuredXmlExtractor |
| 其他(`.txt` `.vtt` `.properties` `.odt` 等) | TextExtractor 兜底 | 同左 |

两个关键设计判断:

1. **即使切到 Unstructured 模式,Excel/PDF/Word/HTML/CSV 依然走自研解析器**——自研版本支持图片抽取与 Markdown 化,Unstructured 给不了。
2. Markdown 只有在自动分段模式(`is_automatic`)下才走 Unstructured,手动规则下保持自研的按标题切分。

### 扩展名白名单(api/constants/__init__.py)

`DOCUMENT_EXTENSIONS` 按 `ETL_TYPE` 在进程启动时生成:

- dify 模式:`txt / markdown / md / mdx / pdf / html / htm / xlsx / xls / docx / csv / vtt / properties / odt`
- Unstructured 模式:上述基础上再加 `doc / eml / msg / pptx / xml / epub`;`ppt` 仅在配置了 `UNSTRUCTURED_API_URL` 时加入

白名单同时用于上传校验(`FileService.upload_file`,`source=datasets` 时)与预览校验,并通过 `GET /console/api/files/support-type` 暴露给前端,知识库上传组件据此渲染可选文件类型(前端不写死)。

细节:白名单里的 `odt` 在分发器中没有专属分支,实际落到 TextExtractor 兜底。

### 统一接口与输出

所有解析器实现 `BaseExtractor.extract() -> list[Document]`(`extractor_base.py`)。`Document`(`core/rag/models/document.py`)为 Pydantic 模型:

```python
class Document(BaseModel):
    page_content: str
    vector: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = "dify"
    children: list[ChildDocument] | None = None      # 父子分段用
    attachments: list[AttachmentDocument] | None = None  # 多模态附件用
```

## 三、自研解析器实现细节

### PDF — `pdf_extractor.py`

- 底层 **pypdfium2**(Chromium 的 PDF 引擎),逐页 `get_textpage().get_text_range()` 提取文本,**每页一个 Document**(metadata 带页码)。
- **图片抽取**:`page.get_objects(filter=FPDF_PAGEOBJ_IMAGE)` 枚举页内图片对象,提取字节后用 magic bytes 表(JPEG/PNG/JP2/GIF/BMP/TIFF)识别真实格式,存对象存储并写 `UploadFile` 表,再以 `![image](FILES_URL/files/{id}/file-preview)` 的 Markdown 链接内联回该页文本——这是 Dify 多模态知识库的基础:图片链接随文本一起分段、索引,检索时可还原展示。
- 明文缓存:`file_cache_key` 命中时直接从 storage 读缓存文本,跳过重复解析。
- 图片解析失败仅 warning 不中断,保证文本主流程健壮。
- 唯一使用 `blob/blob.py` 的解析器(Blob 是借鉴 LangChain 的惰性数据容器,按引用或按值持有数据,提供 `as_bytes_io()` 等物化接口;不是新一代解析器体系)。

### Word (.docx) — `word_extractor.py`(最复杂,约 470 行)

用 **python-docx** 但几乎重写了内容遍历逻辑,`doc.iter_inner_content()` 保持段落与表格的原始顺序,整个文档产出单个 Document:

- **表格 → Markdown 表格**:处理 `grid_span` 合并单元格(colspan 展开)、单元格内多段落去重。
- **图片双通道**:遍历 `doc.part.rels` 的 image 关系——内嵌图片取 `target_part.blob`,外链图片经统一的 `remote_fetcher` 下载;同样持久化为 `UploadFile` + Markdown 链接。同时处理现代 `w:drawing`(`a:blip`)与古老 VML `w:pict`(`binData`/`imagedata`)两种嵌入方式。
- **超链接三种形态**:`w:hyperlink` 元素(r:id 关系)、表格单元格内超链接、遗留 HYPERLINK 域(`fldChar begin/separate/end` 状态机 + `instrText` 正则提取 URL),统一转 `[text](url)`。
- 支持直接传入远程 URL(先下载到临时文件)。

### Excel — `excel_extractor.py`

`.xlsx` 与 `.xls` 走不同路径:

- **.xlsx** 用 openpyxl(非 read-only 模式,只读模式拿不到嵌入图片):
  - **表头启发式识别**:扫描前 10 行,取第一个"非空列 ≥ 2"的行作为表头(否则取非空列最多的行),容忍表格上方有标题行/空行。
  - 每个数据行输出 `"列名":"值";"列名":"值"` 键值对格式,**每行一个 Document**——每个分段自带表头语义,对表格问答很关键。
  - 单元格超链接转 Markdown;**嵌入图片按锚点单元格归位**(`sheet._images` 的 anchor 行列),用 `sha256(内容) + sheet + 锚点` 生成确定性存储 key,重试索引时查库复用已有 `UploadFile`,不重复落图(幂等)。
- **.xls** 用 pandas + xlrd,`dropna` 全空行后同样每行一个 Document(无图片/超链接支持)。

### Markdown / HTML / CSV / TXT

- **Markdown**(`markdown_extractor.py`):按 `#+` 标题切成 `(标题, 正文)` 元组,**每个标题块一个 Document**;有代码块保护(``` 内的 `#` 不当标题),最后正则剥掉内联 HTML 标签;可选去链接/图片。
- **HTML**(`html_extractor.py`):BeautifulSoup `html.parser` 直接 `get_text()`,全文一个 Document,最简单。
- **CSV**(`csv_extractor.py`):pandas `read_csv(on_bad_lines="skip")`,每行输出 `列名: 值;列名: 值`,每行一个 Document。
- **TXT(兜底)**(`text_extractor.py`):直接读文本;`UnicodeDecodeError` 时用 **charset_normalizer** 自动探测编码(采样 1MB、线程池 5 秒超时,`helpers.py`),按置信度逐个编码重试。Markdown/CSV 复用同一套编码探测。

## 四、Unstructured 系列解析器

目录:`api/core/rag/extractor/unstructured/`,8 个解析器共享同一模板:

> 配置了 `UNSTRUCTURED_API_URL` 就调 `partition_via_api()`(远程服务),否则调本地 `partition_xxx()`,然后聚合 elements。

差异点:

- **`.ppt` 与 `.doc` 强制走 API**:unstructured 本地版没有旧版二进制 Office 格式的解析能力(`.ppt` 无 API URL 时直接抛 `NotImplementedError`;`.doc` 用 libmagic 检测真实文件类型后走 API)。
- **聚合策略两派**:
  - PPT/PPTX 按 `element.metadata.page_number` 逐页合并,每页一个 Document;
  - doc/eml/msg/epub/xml/md 用 unstructured 的 `chunk_by_title()` 按标题语义聚合,`max_characters` 取 `INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH` 配置——相当于解析阶段就完成语义分段。
- **`.eml` 邮件特化**:尝试对元素文本做 base64 解码 + BeautifulSoup 去 HTML(邮件正文常见 base64 编码的 HTML),失败则静默保留原文。
- **`.epub` 本地解析**依赖 pandoc,运行时 `pypandoc.download_pandoc()` 自动下载。

## 五、非文件数据源

`ExtractProcessor` 同时统一另外两类数据源,输出同样的 `Document`:

- **Notion**(`notion_extractor.py`):直接调 Notion REST API 递归拉取 block 树——`heading_1/2/3` 映射为 Markdown `#/##/###`,table block 重组为 Markdown 表格,database 逐行拼属性键值对。凭证优先从租户 datasource plugin 凭证取,回退 `NOTION_INTEGRATION_TOKEN` 环境变量;每次抽取回写页面 `last_edited_time` 供增量同步比对。
- **网页爬取**(`firecrawl/`、`watercrawl/`、`jina_reader_extractor.py`):按 `website_info.provider` 三选一(firecrawl / watercrawl / jinareader)。爬取动作(crawl job)由数据源服务提前完成,extractor 的 `extract()` 主要是取回 job 对应的抓取结果(Markdown)转成 Document,解析压力在外部爬虫服务侧。

`ExtractProcessor` 还有两个复用入口:`load_from_url()`(下载后按 Content-Type 推断后缀,走同一套文件分发,供 webscraper 工具用)、`load_from_upload_file()`(文件预览等场景)。

## 六、解析之后:与下游的衔接

以段落模式(`paragraph_index_processor.py`)为例,`transform()` 流程:

1. `CleanProcessor.clean()` 按处理规则清洗(多余空白、URL/邮箱移除等,`core/rag/cleaner/`);
2. 基于 embedding 模型 token 计数的 splitter 二次分段(`max_tokens` / `chunk_overlap` / `separator` 来自处理规则,automatic 模式用 `DatasetProcessRule.AUTOMATIC_RULES`);
3. 每段生成 `doc_id`(uuid)与内容 hash;
4. 把正文里的 Markdown 图片链接解析回 `File` 对象挂到分段的多模态附件上——解析器保留的图片链接在这里闭环。

其他两种模式的差异:

- **父子分段**:先按父规则切大块再切子块(或 `full_doc` 模式全文为单个父块),只有**子块**写入向量库,父块存库供召回时回溯上下文;
- **QA 模式**:清洗切分后按每批 10 段并发调 LLM 生成问答对,问题作为索引内容、答案存 metadata。

最后 `_load_segments()` 写 `document_segments` 表,`_load()` 按索引技术写向量库(high_quality)或关键词索引(economy)。

## 七、设计要点小结(对本项目的借鉴)

1. **策略模式 + 三级路由**:数据源类型 → ETL 模式 → 扩展名。新格式只需加一个 `BaseExtractor` 实现和一个分发分支;统一输出 `list[Document]` 使下游对格式无感。
2. **自研与外包清晰分工**:高频、需要富内容(图片/表格/链接)的格式自研深度定制;长尾格式交给 Unstructured,并可通过 API 模式把重解析卸载到独立服务。
3. **"万物皆 Markdown"**:表格、超链接、图片、Notion 块统统归一为 Markdown 表示,下游分段、索引、前端渲染只需理解一种格式。
4. **解析即预分段**:Document 粒度(PDF 页、Excel 行、Markdown 标题节、unstructured 语义块)本身是第一次结构化切分,后续 splitter 只做 token 级细分——两级分段比"整篇文本无脑滑窗"保留了更多结构语义。
5. **图片作为一等公民**:解析阶段即持久化图片(对象存储 + UploadFile 记录)并以 Markdown 链接内联,为多模态检索/展示铺路;Excel 图片用内容哈希做确定性 key 保证重试幂等。
6. **工程健壮性**:编码自动探测(charset_normalizer + 超时保护)、图片失败降级不中断、PDF 明文缓存、扩展名白名单随 ETL 能力动态生成并经 API 下发给前端。
