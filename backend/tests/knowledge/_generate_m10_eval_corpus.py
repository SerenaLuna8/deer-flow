"""One-shot generator for the frozen M10 T14 retrieval corpus.

Run from ``backend/``:

    uv run python tests/knowledge/_generate_m10_eval_corpus.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "m10_retrieval_cases.json"


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _seg(position: int, content: str, children: list[str] | None = None) -> dict:
    item: dict = {"position": position, "content": content}
    if children is not None:
        item["children"] = [{"position": index, "content": text} for index, text in enumerate(children, start=1)]
    return item


def _doc(
    source_id: str,
    base: str,
    segments: list[dict],
    *,
    chunking_mode: str = "general",
    metadata: dict | None = None,
) -> dict:
    return {
        "source_id": source_id,
        "base": base,
        "chunking_mode": chunking_mode,
        "metadata": metadata or {},
        "segments": segments,
    }


def _j(source_id: str, position: int, content: str, grade: int) -> dict:
    return {
        "source_id": source_id,
        "position": position,
        "content_sha256": _sha(content),
        "grade": grade,
    }


def _q(
    query_id: str,
    split: str,
    category: str,
    query: str,
    judgments: list[dict],
    *,
    identifier_token: str | None = None,
    answer_marker: str | None = None,
    base_scope: str = "same_domain",
    metadata_filters: list[dict] | None = None,
) -> dict:
    item = {
        "id": query_id,
        "split": split,
        "category": category,
        "query": query,
        "base_scope": base_scope,
        "metadata_filters": metadata_filters,
        "judgments": judgments,
    }
    if identifier_token is not None:
        item["identifier_token"] = identifier_token
    if answer_marker is not None:
        item["answer_marker"] = answer_marker
    return item


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

EMG3088 = "值班手册把 EMG-3088 定义为嵌入批次在第三次尝试后仍返回 5xx 的终态。出现该码时文档保持 failed，进度行必须显示 Failed during Embedding 与 Attempt 3/3，不得把部分成功批次显示成 ready。"
ERRAUTH = "认证网关把 ERR.AUTH_4012 映射为项目成员能力不足。只读成员调用批量 metadata 必须得到 403，且整批回滚，不留下半写入。"
ETIMEOUT = "模型客户端把连续两次传输超时记为 E_TIMEOUT_88。该错误必须上浮为 KNOWLEDGE_MODEL_UNAVAILABLE，搜索不得改写成空成功。"
IDXLEAK = "巡检把 IDX-LEAK-7701 留给词法索引与正文不一致的行。hybrid 检索若发现 lexical_version 不是 1，必须冲突失败，禁止静默跳过。"
SMTP554 = "通知通道把 SMTP-554-7.1.1 解释为收件人被对端永久拒绝。知识库任务不得把该码当成可重试的 429。"
PROCLOCK = "Worker 领取任务时使用 PROC.LOCK_9F 作为文档行锁标签。同一文档同一版本只允许一个开放索引任务，ingest 与 reembed 共享该互斥。"
CASVER = "重新解析提交必须携带 CAS-EXPECTED-VERSION。版本不一致时保留未保存参数表单，预览作废，由用户重新确认。"
ORPHAN = "调度器把 SKILL_TREE_ORPHAN 留给超过宽限期仍无活 owner 的技能树节点。该标识只出现在运行时回收文档，与知识库检索单元无关但必须能被精确搜到。"
PGADV = "安装脚本使用 PG-ADVISORY-0x0DEE 作为模型注册表 bootstrap 的会话锁。评测不得把它写进日志或诊断正文。"
IP_A = "回环演练把检索探针固定到 192.0.2.17，这是 TEST-NET-1 地址，不指向真实主机。"
IP_B = "备用探针落在 198.51.100.77。文档要求防火墙只放行该地址的 8001 端口。"
IP_C = "观测旁路使用 203.0.113.9 作为伪造源，用于确认诊断不回显对端地址。"
IP6_A = "IPv6 演练记录规范地址 2001:db8:85a3::8a2e:370:7334，词法索引必须保留完整项。"
IP6_B = "第二组 IPv6 探针是 2001:db8::cafe，用于确认大小写折叠后仍能命中。"
QWEMBED = "主嵌入绑定名称写作 QW-EMBED-8B，对应评测冻结的主向量模型别名，不是第二套配置行。"
ACTWR = "硬件资产表把机架控制器称为 ACTW-R42，固件升级必须先停摄入任务。"
HWFAN = "机柜风扇策略 HW-FAN-B7 要求温度超过 71 摄氏度才提升转速，与检索分数无关。"
METAFIELDS = "Agent 只读工具 knowledge_metadata_fields 只返回字段定义，不扫描值，也不提供写能力。"
REPARSE = "文档动作 Reparse from original 对应 HTTP reparse-preview 与 reparse。预览按权威原文件切分，确认后才替换段行。"
LEXICAL = "词法派生版本固定为 lexical_v1。中文用重叠二元词元，标识符保留完整项并补部件，PostgreSQL 只提供 simple 配置，不声明 BM25。"
MINIO = "评测对象前缀使用 MINIO-BUCKET-actweave-eval。该名字只出现在本语料，生产桶名不得写入评测文档。"
OMITTED = "工具装包字段 TOOL-OMITTED-COUNT 表示因 64KiB 预算被整段跳过的命中数。它不是第二套 LLM 计数器。"
EMG1099 = "EMG-1099 表示嵌入探测成功但维度与库绑定不一致，必须拒绝写入。"
IP_DEV = "开发集探针地址 192.0.2.88 仅用于确认词法 IP 规则，不得出现在验收集题面。"
QUOTA413 = "上传配额耗尽返回 ERR.QUOTA_413，前端必须按文件保留失败原因。"
CHUNK500 = "重新解析示例把 CHUNK-SIZE-500 写成覆盖后的切分宽度，旧行不得用新参数解释。"
RRFK = "秩融合常量写作 RRF-K60，公式为 61/2 乘各路 1/(60+rank)。"
BUDGETC = "分库预算记号 BASE-BUDGET-C 等于 min(B, floor(400/N))，C<1 时显式拒绝。"
IP6_DEV = "开发集 IPv6 写作 2001:db8::1，用于确认压缩形式保留。"
HIT409 = "详情钉住失败返回 HIT-DETAIL-409，提示重新检索而不是静默换正文。"
OPENDOC = "定位按钮文案键 OPEN-IN-DOCUMENTS 必须把 doc 与 segment 写入 URL。"
OVERBUDGET = "首段超过预算时稳定错误码是 KNOWLEDGE_PASSAGE_OVER_BUDGET，错误 ToolMessage 不带引用。"

NEAR_MISS_ERROR = "值班手册还列出若干相近但不相同的嵌入失败描述，例如批次重试耗尽、向量维度漂移。这些段落能帮助理解运维，但没有给出任何一个精确错误码，不能单独用来回答具体码值。"

REEMBED_NL = "重嵌入读取当前已发布段和子块，包括禁用行，只重算向量并翻转代次。UUID、正文、位置、启停和人工编辑必须保持。从未发布的失败文档被跳过，不能偷偷从原文件解析。"
HYBRID_NL = "混合召回在向量路之外增加词法路，两路按父段 RRF 合并后再截 C。词法新增项仍要回填真实 cosine 并先过原生阈值。纯语义搜索不建词法查询。"
METADATA_NL = "批量元数据一次共同 patch 最多 100 篇文档和 20 个字段。未传字段保留，null 清空，任一冲突整批回滚。内建字段不可写。"
PROGRESS_NL = "任务进度来自当前 attempt 的成功批次。新领取会清零计数。失败保留失败阶段，未知总数不显示百分比，旧 attempt 不得倒灌。"
WIZARD_NL = "创建向导的 File picker 只对当前选中文件自动预览一次。参数编辑只标过期，迟到的较慢响应不能覆盖已选文件的预览。"
RANKING_NL = "最终排序有三支：统一重排器走原生重排分，同域无重排器走原生 cosine，异构域做秩融合。融合分是排序证据，不是正确性概率。"
RETENTION_NL = "查询日志按 owner 保留。项目删除会清掉该项目全部 owner 历史，恢复不能把已 purge 的检索日志变回来。"

_TAIL_PAD = (
    "下面这些句子只用来把可支持答案推到第 320 个字符之后，本身不包含结论。"
    "值班人员会先核对项目仍有效、库仍是 active、文档仍是 ready，然后才看任务行。"
    "队列里的租约、尝试次数和阶段字段必须来自当前领取，不能把上一轮成功批次累加进来。"
    "若只看到短摘要就下结论，很容易把界面 snippet 误当成模型读到的全部证据。"
    "手册因此要求先读完铺垫，再看段尾那一句可执行规则。"
    "这一段继续说明权限复核、版本对齐和诊断不得回传正文，仍然没有给出题目要的那句结论。"
)
TAIL_REPARSE = (
    "许多团队会把换模型和换切分混成一次重建。有人以为点一次重建就会同时改 embedding、"
    "改 chunk 大小、再从对象存储拉原文件。手册先解释队列、租约、发布代次和失败重试。" + _TAIL_PAD + "直到段尾才给出可执行区分：重嵌入只重算当前正文的向量并保留段身份；"
    "若必须从原文件重新切分，唯一入口是显式重新解析，确认文案必须写明覆盖人工编辑。"
    "结论标记 TAIL-REPARSE-ONLY：重新解析才会替换段行，重嵌入不会。"
)
TAIL_BUDGET = (
    "检索测试面板默认展示短摘要，容易让人以为模型也只看到三百二十个字符。"
    "下面连续解释 snippet、引用投影、历史缺字段和诊断安全边界。" + _TAIL_PAD + "真正可引用的规则出现在段尾：Agent 工具按 UTF-8 JSON 64KiB 整段装包，"
    "装不下的整段跳过并报告 omitted_count。"
    "结论标记 TAIL-BUDGET-64KIB：模型正文是完整父段，界面摘要保持短引用。"
)
TAIL_FUSION = (
    "跨库检索时两个库可能绑定不同的 embedding。前面先解释分组、query vector 复用、"
    "阈值只作用于原生分，以及诊断里的 heterogeneous 标记。" + _TAIL_PAD + "段尾给出判定：异构且没有词法证据时只做域内名次折中，不得宣称比单域更准。"
    "结论标记 TAIL-FUSION-LIMIT：无词法证据的异构域只保证公平，不保证更准。"
)
TAIL_FILTER = (
    "维护页可以改文档名、启停和自定义字段。前半段强调改名不会触发嵌入，"
    "列表筛选和检索过滤也不是同一条路径。" + _TAIL_PAD + "段尾才写清：metadata 条件在全部召回路的 limit 前以及最终复核都要重放。"
    "结论标记 TAIL-FILTER-AND：过滤是召回约束，不是排序后的装饰。"
)

PC_PARENT = "父子切分手册把一章拆成父段摘要加若干子块。父段只保留章节意图，具体操作步骤写在子块里，检索应回卷到父段。"
PC_CHILD_1 = "子块一只讲如何打开库设置，不包含回卷规则的判定句。"
PC_CHILD_2 = "子块二写明 PC-ROLLUP-SECOND：parent_child 命中非首个子块时，引用仍然返回父段全文，matched_children 必须包含该真实子块。"
PC_CHILD_3 = "子块三写明 PC-CHILD-COSINE-MAX：词法只命中某个子块时，父段 cosine 取当前全部子块的最大 cosine，不能用空父向量比较。"
PC_PARENT_B = "另一章说明禁用父段会同时撤出其全部子块，不单独禁用子块。"
PC_CHILD_B1 = "该章子块一只重复启停入口位置。"
PC_CHILD_B2 = "该章子块二写明 PC-DISABLE-PARENT：禁用父段后任何子块都不得进入召回。"

SMALL_ALPHA = "小库独有事实 SMALL-ALPHA-WIDGET：只有轻量库记载小部件校准周期为 14 天。大库里的相近段落只讨论通用维护，不包含这个周期。"
SMALL_BETA = "小库独有事实 SMALL-BETA-OFFSET：时区偏移校准值是 37 分钟。该数字不出现在大库或异构库。"
LARGE_WIDGET = "大库维护手册讨论小部件外观和清洁，不给出校准周期，也不能回答 14 天这个问题。"
HETERO_GAMMA = "异构库独有事实 HETERO-GAMMA-SLOPE：第二套 embedding 空间里把斜率记为 0.13。该事实不进入主向量库。"
HETERO_DELTA = "异构库独有事实 HETERO-DELTA-BIAS：偏置常数是 -0.42，只写在第二套模型库。"

META_RD_1 = "研发备忘把接口冻结窗口放在每周三，字段 dept=研发。"
META_RD_2 = "研发备忘补充联调禁止改绑定，字段仍是研发。"
META_MKT_1 = "市场备忘把对外演示稿锁在绿色主题，字段 dept=市场。"
META_MKT_2 = "市场备忘禁止在演示中展示原始分数当置信度，字段仍是市场。"

WEATHER_1 = "华北冬小麦区本旬平均降水 12 毫米，与知识库运维无关。"
WEATHER_2 = "观测站记录能见度 18 公里，没有错误码或接口名。"


def documents() -> list[dict]:
    return [
        _doc(
            "ops-error-codes",
            "large_hybrid",
            [
                _seg(1, EMG3088),
                _seg(2, ERRAUTH),
                _seg(3, ETIMEOUT),
                _seg(4, IDXLEAK),
                _seg(5, SMTP554),
                _seg(6, PROCLOCK),
                _seg(7, CASVER),
                _seg(8, ORPHAN),
                _seg(9, PGADV),
                _seg(10, EMG1099),
                _seg(11, QUOTA413),
                _seg(12, OVERBUDGET),
            ],
        ),
        _doc(
            "ops-network",
            "large_hybrid",
            [
                _seg(1, IP_A),
                _seg(2, IP_B),
                _seg(3, IP_C),
                _seg(4, IP6_A),
                _seg(5, IP6_B),
                _seg(6, IP_DEV),
                _seg(7, IP6_DEV),
            ],
        ),
        _doc(
            "ops-hardware",
            "large_hybrid",
            [
                _seg(1, QWEMBED),
                _seg(2, ACTWR),
                _seg(3, HWFAN),
                _seg(4, MINIO),
            ],
        ),
        _doc(
            "ops-interfaces",
            "large_hybrid",
            [
                _seg(1, METAFIELDS),
                _seg(2, REPARSE),
                _seg(3, LEXICAL),
                _seg(4, OMITTED),
                _seg(5, CHUNK500),
                _seg(6, RRFK),
                _seg(7, BUDGETC),
                _seg(8, HIT409),
                _seg(9, OPENDOC),
            ],
        ),
        _doc("product-reembed", "large_hybrid", [_seg(1, REEMBED_NL)]),
        _doc("product-hybrid", "large_hybrid", [_seg(1, HYBRID_NL)]),
        _doc("product-metadata", "large_hybrid", [_seg(1, METADATA_NL)]),
        _doc("product-progress", "large_hybrid", [_seg(1, PROGRESS_NL)]),
        _doc("product-wizard", "large_hybrid", [_seg(1, WIZARD_NL)]),
        _doc("product-ranking", "large_hybrid", [_seg(1, RANKING_NL)]),
        _doc("product-retention", "large_hybrid", [_seg(1, RETENTION_NL)]),
        _doc(
            "tail-answers",
            "large_hybrid",
            [
                _seg(1, TAIL_REPARSE),
                _seg(2, TAIL_BUDGET),
                _seg(3, TAIL_FUSION),
                _seg(4, TAIL_FILTER),
            ],
        ),
        _doc("near-miss-ops", "large_hybrid", [_seg(1, NEAR_MISS_ERROR)]),
        _doc(
            "large-similar-widget",
            "large_hybrid",
            [_seg(1, LARGE_WIDGET)],
        ),
        _doc(
            "small-unique",
            "small_hybrid",
            [_seg(1, SMALL_ALPHA), _seg(2, SMALL_BETA)],
        ),
        _doc(
            "pc-manual",
            "parent_child_hybrid",
            [
                _seg(1, PC_PARENT, [PC_CHILD_1, PC_CHILD_2, PC_CHILD_3]),
                _seg(2, PC_PARENT_B, [PC_CHILD_B1, PC_CHILD_B2]),
            ],
            chunking_mode="parent_child",
        ),
        _doc("meta-rd", "large_hybrid", [_seg(1, META_RD_1), _seg(2, META_RD_2)], metadata={"dept": "研发"}),
        _doc("meta-mkt", "large_hybrid", [_seg(1, META_MKT_1), _seg(2, META_MKT_2)], metadata={"dept": "市场"}),
        _doc(
            "hetero-special",
            "hetero_semantic",
            [_seg(1, HETERO_GAMMA), _seg(2, HETERO_DELTA)],
        ),
        _doc("weather-distractor", "large_hybrid", [_seg(1, WEATHER_1), _seg(2, WEATHER_2)]),
    ]


def queries() -> list[dict]:
    items: list[dict] = []

    def ident(qid: str, split: str, token: str, source_id: str, position: int, content: str, query: str) -> None:
        items.append(
            _q(
                qid,
                split,
                "identifier",
                query,
                [_j(source_id, position, content, 2), _j("near-miss-ops", 1, NEAR_MISS_ERROR, 1)],
                identifier_token=token,
            )
        )

    ident("h-id-01", "holdout", "EMG-3088", "ops-error-codes", 1, EMG3088, "EMG-3088 是什么错误")
    ident("h-id-02", "holdout", "ERR.AUTH_4012", "ops-error-codes", 2, ERRAUTH, "ERR.AUTH_4012 如何处理")
    ident("h-id-03", "holdout", "E_TIMEOUT_88", "ops-error-codes", 3, ETIMEOUT, "E_TIMEOUT_88 应该映射成哪个错误")
    ident("h-id-04", "holdout", "IDX-LEAK-7701", "ops-error-codes", 4, IDXLEAK, "IDX-LEAK-7701 出现时检索怎么做")
    ident("h-id-05", "holdout", "SMTP-554-7.1.1", "ops-error-codes", 5, SMTP554, "SMTP-554-7.1.1 能不能当 429 重试")
    ident("h-id-06", "holdout", "PROC.LOCK_9F", "ops-error-codes", 6, PROCLOCK, "PROC.LOCK_9F 保护什么互斥")
    ident("h-id-07", "holdout", "CAS-EXPECTED-VERSION", "ops-error-codes", 7, CASVER, "CAS-EXPECTED-VERSION 冲突后表单怎么处理")
    ident("h-id-08", "holdout", "SKILL_TREE_ORPHAN", "ops-error-codes", 8, ORPHAN, "SKILL_TREE_ORPHAN 指什么节点")
    ident("h-id-09", "holdout", "PG-ADVISORY-0x0DEE", "ops-error-codes", 9, PGADV, "PG-ADVISORY-0x0DEE 是哪把锁")
    ident("h-id-10", "holdout", "192.0.2.17", "ops-network", 1, IP_A, "192.0.2.17 用在哪次演练")
    ident("h-id-11", "holdout", "198.51.100.77", "ops-network", 2, IP_B, "198.51.100.77 要放行哪个端口")
    ident("h-id-12", "holdout", "203.0.113.9", "ops-network", 3, IP_C, "203.0.113.9 是什么角色")
    ident("h-id-13", "holdout", "2001:db8:85a3::8a2e:370:7334", "ops-network", 4, IP6_A, "2001:db8:85a3::8a2e:370:7334 要不要保留完整项")
    ident("h-id-14", "holdout", "2001:db8::cafe", "ops-network", 5, IP6_B, "2001:db8::cafe 能否在大小写折叠后命中")
    ident("h-id-15", "holdout", "QW-EMBED-8B", "ops-hardware", 1, QWEMBED, "QW-EMBED-8B 是什么别名")
    ident("h-id-16", "holdout", "ACTW-R42", "ops-hardware", 2, ACTWR, "ACTW-R42 升级前要停什么")
    ident("h-id-17", "holdout", "HW-FAN-B7", "ops-hardware", 3, HWFAN, "HW-FAN-B7 的温度阈值是多少")
    ident("h-id-18", "holdout", "knowledge_metadata_fields", "ops-interfaces", 1, METAFIELDS, "knowledge_metadata_fields 工具会不会写值")
    ident("h-id-19", "holdout", "reparse-preview", "ops-interfaces", 2, REPARSE, "reparse-preview 读的是什么文件")
    ident("h-id-20", "holdout", "lexical_v1", "ops-interfaces", 3, LEXICAL, "lexical_v1 用什么数据库配置")
    ident("h-id-21", "holdout", "MINIO-BUCKET-actweave-eval", "ops-hardware", 4, MINIO, "MINIO-BUCKET-actweave-eval 是什么前缀")
    ident("h-id-22", "holdout", "TOOL-OMITTED-COUNT", "ops-interfaces", 4, OMITTED, "TOOL-OMITTED-COUNT 表示什么")

    ident("d-id-01", "dev", "EMG-1099", "ops-error-codes", 10, EMG1099, "EMG-1099 在什么情况下出现")
    ident("d-id-02", "dev", "192.0.2.88", "ops-network", 6, IP_DEV, "192.0.2.88 属于哪一集")
    ident("d-id-03", "dev", "ERR.QUOTA_413", "ops-error-codes", 11, QUOTA413, "ERR.QUOTA_413 前端怎么展示")
    ident("d-id-04", "dev", "CHUNK-SIZE-500", "ops-interfaces", 5, CHUNK500, "CHUNK-SIZE-500 何时生效")
    ident("d-id-05", "dev", "RRF-K60", "ops-interfaces", 6, RRFK, "RRF-K60 的公式是什么")
    ident("d-id-06", "dev", "BASE-BUDGET-C", "ops-interfaces", 7, BUDGETC, "BASE-BUDGET-C 怎么计算")
    ident("d-id-07", "dev", "2001:db8::1", "ops-network", 7, IP6_DEV, "2001:db8::1 用于什么")
    ident("d-id-08", "dev", "HIT-DETAIL-409", "ops-interfaces", 8, HIT409, "HIT-DETAIL-409 应该提示什么")
    ident("d-id-09", "dev", "OPEN-IN-DOCUMENTS", "ops-interfaces", 9, OPENDOC, "OPEN-IN-DOCUMENTS 往 URL 写哪些参数")
    ident("d-id-10", "dev", "KNOWLEDGE_PASSAGE_OVER_BUDGET", "ops-error-codes", 12, OVERBUDGET, "KNOWLEDGE_PASSAGE_OVER_BUDGET 带不带引用")

    items.extend(
        [
            _q("h-nl-01", "holdout", "natural_language", "重嵌入会不会丢掉人工改过的段落", [_j("product-reembed", 1, REEMBED_NL, 2)]),
            _q("h-nl-02", "holdout", "natural_language", "混合召回怎样和向量路合并", [_j("product-hybrid", 1, HYBRID_NL, 2)]),
            _q("h-nl-03", "holdout", "natural_language", "批量改元数据时未编辑的字段会怎样", [_j("product-metadata", 1, METADATA_NL, 2)]),
            _q("h-nl-04", "holdout", "natural_language", "任务失败时进度为什么还显示原来的阶段", [_j("product-progress", 1, PROGRESS_NL, 2)]),
            _q("h-nl-05", "holdout", "natural_language", "创建向导里后选的文件预览会不会被先发出的请求盖掉", [_j("product-wizard", 1, WIZARD_NL, 2)]),
            _q("h-nl-06", "holdout", "natural_language", "跨库融合分能当成正确性概率吗", [_j("product-ranking", 1, RANKING_NL, 2)]),
            _q("d-nl-01", "dev", "natural_language", "项目删除后旧的检索日志还能恢复吗", [_j("product-retention", 1, RETENTION_NL, 2)]),
            _q("d-nl-02", "dev", "natural_language", "从未成功发布的失败文档重嵌入时怎么办", [_j("product-reembed", 1, REEMBED_NL, 2)]),
            _q("d-nl-03", "dev", "natural_language", "纯语义搜索会不会走词法路", [_j("product-hybrid", 1, HYBRID_NL, 2)]),
            _q("d-nl-04", "dev", "natural_language", "批量元数据冲突是不是只回滚出错的那一篇", [_j("product-metadata", 1, METADATA_NL, 2)]),
            _q("d-nl-05", "dev", "natural_language", "领取新尝试时旧的完成计数还在吗", [_j("product-progress", 1, PROGRESS_NL, 2)]),
            _q(
                "h-tail-01",
                "holdout",
                "tail",
                "想从原文件重新切分应该用哪个入口",
                [_j("tail-answers", 1, TAIL_REPARSE, 2)],
                answer_marker="TAIL-REPARSE-ONLY",
            ),
            _q(
                "h-tail-02",
                "holdout",
                "tail",
                "模型实际读到的是短摘要还是完整父段",
                [_j("tail-answers", 2, TAIL_BUDGET, 2)],
                answer_marker="TAIL-BUDGET-64KIB",
            ),
            _q(
                "d-tail-01",
                "dev",
                "tail",
                "两个库 embedding 不同又没有词法证据时能不能说比单库更准",
                [_j("tail-answers", 3, TAIL_FUSION, 2)],
                answer_marker="TAIL-FUSION-LIMIT",
            ),
            _q(
                "d-tail-02",
                "dev",
                "tail",
                "元数据过滤是排序之后再裁一次吗",
                [_j("tail-answers", 4, TAIL_FILTER, 2)],
                answer_marker="TAIL-FILTER-AND",
            ),
            _q(
                "h-pc-01",
                "holdout",
                "parent_child",
                "parent_child 命中第二个子块时引用返回什么",
                [_j("pc-manual", 1, PC_PARENT, 2)],
                answer_marker="PC-ROLLUP-SECOND",
                base_scope="parent_child",
            ),
            _q(
                "h-pc-02",
                "holdout",
                "parent_child",
                "词法只打中某一个子块时父段 cosine 怎么取",
                [_j("pc-manual", 1, PC_PARENT, 2)],
                answer_marker="PC-CHILD-COSINE-MAX",
                base_scope="parent_child",
            ),
            _q(
                "d-pc-01",
                "dev",
                "parent_child",
                "禁用父段之后子块还能被搜到吗",
                [_j("pc-manual", 2, PC_PARENT_B, 2)],
                answer_marker="PC-DISABLE-PARENT",
                base_scope="parent_child",
            ),
            _q(
                "d-pc-02",
                "dev",
                "parent_child",
                "父子回卷时 matched children 要不要包含真实命中的非首子块",
                [_j("pc-manual", 1, PC_PARENT, 2)],
                answer_marker="PC-ROLLUP-SECOND",
                base_scope="parent_child",
            ),
            _q(
                "h-xb-01",
                "holdout",
                "cross_base",
                "小部件校准周期是多少天",
                [_j("small-unique", 1, SMALL_ALPHA, 2), _j("large-similar-widget", 1, LARGE_WIDGET, 1)],
                answer_marker="SMALL-ALPHA-WIDGET",
                base_scope="same_domain",
            ),
            _q(
                "h-xb-02",
                "holdout",
                "cross_base",
                "时区偏移校准值是多少分钟",
                [_j("small-unique", 2, SMALL_BETA, 2)],
                answer_marker="SMALL-BETA-OFFSET",
                base_scope="same_domain",
            ),
            _q(
                "d-xb-01",
                "dev",
                "cross_base",
                "轻量库里的小部件校准周期写的是多久",
                [_j("small-unique", 1, SMALL_ALPHA, 2), _j("large-similar-widget", 1, LARGE_WIDGET, 1)],
                answer_marker="SMALL-ALPHA-WIDGET",
                base_scope="same_domain",
            ),
            _q(
                "d-xb-02",
                "dev",
                "cross_base",
                "只有小库记载的偏移校准是多少",
                [_j("small-unique", 2, SMALL_BETA, 2)],
                answer_marker="SMALL-BETA-OFFSET",
                base_scope="same_domain",
            ),
            _q(
                "h-meta-01",
                "holdout",
                "metadata",
                "接口冻结窗口在周几",
                [_j("meta-rd", 1, META_RD_1, 2)],
                metadata_filters=[{"name": "dept", "operator": "eq", "value": "研发", "field_kind": "custom"}],
            ),
            _q(
                "h-meta-02",
                "holdout",
                "metadata",
                "对外演示稿锁在什么主题",
                [_j("meta-mkt", 1, META_MKT_1, 2)],
                metadata_filters=[{"name": "dept", "operator": "eq", "value": "市场", "field_kind": "custom"}],
            ),
            _q(
                "d-meta-01",
                "dev",
                "metadata",
                "研发联调期间能不能改模型绑定",
                [_j("meta-rd", 2, META_RD_2, 2)],
                metadata_filters=[{"name": "dept", "operator": "eq", "value": "研发", "field_kind": "custom"}],
            ),
            _q(
                "d-meta-02",
                "dev",
                "metadata",
                "市场演示能不能把原始分数说成置信度",
                [_j("meta-mkt", 2, META_MKT_2, 2)],
                metadata_filters=[{"name": "dept", "operator": "eq", "value": "市场", "field_kind": "custom"}],
            ),
            _q("h-na-01", "holdout", "no_answer", "火星基地供水协议的循环周期是多少小时", []),
            _q("h-na-02", "holdout", "no_answer", "中世纪羊皮纸修复要用哪种虫胶配比", []),
            _q("d-na-01", "dev", "no_answer", "南极冰核氚浓度的放行阈值是多少", []),
            _q("d-na-02", "dev", "no_answer", "宋代官窑釉面气泡密度如何分级", []),
            _q(
                "h-he-01",
                "holdout",
                "cross_base",
                "第二套 embedding 空间把斜率记成多少",
                [_j("hetero-special", 1, HETERO_GAMMA, 2)],
                answer_marker="HETERO-GAMMA-SLOPE",
                base_scope="hetero",
            ),
            _q(
                "d-he-01",
                "dev",
                "cross_base",
                "异构库里的偏置常数是多少",
                [_j("hetero-special", 2, HETERO_DELTA, 2)],
                answer_marker="HETERO-DELTA-BIAS",
                base_scope="hetero",
            ),
        ]
    )
    return items


def main() -> None:
    payload = {
        "schema_version": 1,
        "annotation_unit": "parent_segment",
        "source": {
            "kind": "synthetic_desensitized",
            "pii": False,
            "description": "ActWeave Knowledge M10 评测语料：合成运维/产品手册，不含真实用户、主机或密钥。",
            "method": ("由实现者根据 M10 设计第11节类别手写父段与三级标注；身份键为 source_id+position+SHA-256(content)；开发集与验收集按类别分层一次冻结；调参不得改验收集。"),
        },
        "models": {
            "primary_embedding": {
                "provider": "siliconflow",
                "name": "Qwen/Qwen3-Embedding-8B",
                "dimension": 1024,
                "note": "生产默认名为 Qwen/Qwen3-VL-Embedding-8B；评测改用同系列文本嵌入并固定 1024 维以控制费用与 SiliconFlow 512 token 上限。",
            },
            "secondary_embedding": {
                "provider": "siliconflow",
                "name": "Qwen/Qwen3-Embedding-0.6B",
                "dimension": 1024,
                "note": "生产客户端固定发送 dimensions；bge-m3 拒绝该字段，故异构域改用同系列 0.6B。",
            },
            "reranker": {
                "provider": "siliconflow",
                "name": "Qwen/Qwen3-Reranker-8B",
            },
        },
        "parameters": {
            "top_k": 10,
            "score_threshold": 0.2,
            "scale_retrieval_units": 10000,
            "filler_topic": "north_china_winter_wheat_weather",
        },
        "gates": {
            "identifier_recall_candidate_min": 0.95,
            "identifier_recall_at_10_min": 0.95,
            "natural_language_recall_regression_max": 0.02,
            "natural_language_ndcg_regression_max": 0.02,
            "p95_regression_review_ratio": 1.5,
        },
        "documents": documents(),
        "queries": queries(),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} queries={len(payload['queries'])} documents={len(payload['documents'])}")


if __name__ == "__main__":
    main()
