-- ActWeave Knowledge Package MVP · Schema V1 fragment
-- PostgreSQL + pgvector
--
-- 实施 M1 时把本片段合并进现有 full_schema.sql，并同步 Package ORM、
-- catalog digest、required relations、setup/check/reset 和 Schema 测试。
-- Runtime 不执行本文件。
-- 初始 SiliconFlow Embedding + Reranker 配置由 setup-db 的代码 bootstrap
-- 在 DDL 完成后写入；本文件不 INSERT 模型、密文或明文 API Key。
--
-- 现状：本文件为 MVP 五张表设计存档，未随 M8 更新。M8 后 knowledge_* 表
-- 共八张（新增 knowledge_segment_children、knowledge_queries、
-- knowledge_metadata_fields，并扩展 documents/segments/bases 列）。
-- 权威 DDL 见 backend/packages/harness/deerflow/persistence/full_schema.sql。

DO $$
BEGIN
    IF to_regtype('public.vector') IS NULL THEN
        RAISE EXCEPTION 'SCHEMA_RECREATE_REQUIRED: public.vector extension type is missing';
    END IF;
END
$$;

-- 1. 检索模型配置。一行同时绑定 Embedding 与 Reranker，并持有二者共用的当前加密 API Key。
CREATE TABLE knowledge_model_configurations (
    id UUID NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    base_url VARCHAR(1024) NOT NULL,
    embedding_model VARCHAR(255) NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    embedding_max_batch INTEGER DEFAULT 64 NOT NULL,
    reranker_model VARCHAR(255) NOT NULL,
    reranker_max_batch INTEGER DEFAULT 32 NOT NULL,
    request_timeout_seconds INTEGER DEFAULT 30 NOT NULL,
    api_key_nonce BYTEA NOT NULL,
    api_key_ciphertext BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_model_configurations PRIMARY KEY (id),
    CONSTRAINT ck_knowledge_model_configurations_name CHECK (btrim(display_name) <> ''),
    CONSTRAINT ck_knowledge_model_configurations_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_knowledge_model_configurations_base_url CHECK (btrim(base_url) <> ''),
    CONSTRAINT ck_knowledge_model_configurations_embedding_model CHECK (btrim(embedding_model) <> ''),
    CONSTRAINT ck_knowledge_model_configurations_dimension CHECK (embedding_dimension BETWEEN 1 AND 16000),
    CONSTRAINT ck_knowledge_model_configurations_batch CHECK (embedding_max_batch BETWEEN 1 AND 2048),
    CONSTRAINT ck_knowledge_model_configurations_reranker_model CHECK (btrim(reranker_model) <> ''),
    CONSTRAINT ck_knowledge_model_configurations_reranker_batch CHECK (reranker_max_batch BETWEEN 1 AND 256),
    CONSTRAINT ck_knowledge_model_configurations_timeout CHECK (request_timeout_seconds BETWEEN 1 AND 300),
    CONSTRAINT ck_knowledge_model_configurations_secret CHECK (
        octet_length(api_key_nonce) = 12 AND octet_length(api_key_ciphertext) >= 16
    )
);

CREATE UNIQUE INDEX uq_knowledge_model_configurations_name
    ON knowledge_model_configurations (lower(display_name));

-- 2. Project 内的 Knowledge Base。
CREATE TABLE knowledge_bases (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    name VARCHAR(120) NOT NULL,
    description VARCHAR(500) DEFAULT '' NOT NULL,
    model_configuration_id UUID NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_bases PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_bases_project_id_id UNIQUE (project_id, id),
    CONSTRAINT ck_knowledge_bases_name CHECK (btrim(name) <> ''),
    CONSTRAINT ck_knowledge_bases_status CHECK (status IN ('active', 'disabled', 'deleting')),
    CONSTRAINT fk_knowledge_bases_project FOREIGN KEY (project_id)
        REFERENCES public.projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_bases_model FOREIGN KEY (model_configuration_id)
        REFERENCES knowledge_model_configurations (id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_knowledge_bases_project_name
    ON knowledge_bases (project_id, lower(name));

CREATE INDEX ix_knowledge_bases_project_status
    ON knowledge_bases (project_id, status, updated_at DESC, id);

-- 3. 上传文件及其处理状态。每个 Document 自己持有 MinIO object key 和切分参数。
CREATE TABLE knowledge_documents (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    storage_key VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255),
    size_bytes BIGINT NOT NULL,
    status VARCHAR(16) DEFAULT 'uploading' NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    chunk_size INTEGER DEFAULT 1000 NOT NULL,
    chunk_overlap INTEGER DEFAULT 100 NOT NULL,
    segment_count INTEGER DEFAULT 0 NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_documents PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_documents_project_base_id UNIQUE (project_id, knowledge_base_id, id),
    CONSTRAINT ck_knowledge_documents_name CHECK (btrim(name) <> '' AND btrim(original_name) <> ''),
    CONSTRAINT ck_knowledge_documents_storage_key CHECK (btrim(storage_key) <> ''),
    CONSTRAINT ck_knowledge_documents_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_knowledge_documents_status CHECK (
        status IN ('uploading', 'queued', 'processing', 'ready', 'failed', 'deleting')
    ),
    CONSTRAINT ck_knowledge_documents_version CHECK (version >= 1),
    CONSTRAINT ck_knowledge_documents_chunk_size CHECK (chunk_size BETWEEN 200 AND 4000),
    CONSTRAINT ck_knowledge_documents_chunk_overlap CHECK (
        chunk_overlap BETWEEN 0 AND 500 AND chunk_overlap < chunk_size
    ),
    CONSTRAINT ck_knowledge_documents_segment_count CHECK (segment_count >= 0),
    CONSTRAINT ck_knowledge_documents_error CHECK (
        (status = 'failed' AND error_message IS NOT NULL)
        OR (status <> 'failed' AND error_message IS NULL)
    ),
    CONSTRAINT fk_knowledge_documents_base FOREIGN KEY (project_id, knowledge_base_id)
        REFERENCES knowledge_bases (project_id, id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_knowledge_documents_storage_key
    ON knowledge_documents (storage_key);

CREATE INDEX ix_knowledge_documents_base_status
    ON knowledge_documents (project_id, knowledge_base_id, status, updated_at DESC, id);

-- 4. 文本块与向量。MVP 直接把 embedding 保存在 Segment 行。
CREATE TABLE knowledge_segments (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    source_position JSONB DEFAULT '{}'::jsonb NOT NULL,
    embedding public.vector NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_segments PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_segments_document_version_position UNIQUE (
        knowledge_document_id,
        document_version,
        position
    ),
    CONSTRAINT ck_knowledge_segments_version CHECK (document_version >= 1),
    CONSTRAINT ck_knowledge_segments_position CHECK (position >= 1),
    CONSTRAINT ck_knowledge_segments_content CHECK (content <> ''),
    CONSTRAINT ck_knowledge_segments_source_position CHECK (jsonb_typeof(source_position) = 'object'),
    CONSTRAINT ck_knowledge_segments_embedding CHECK (
        public.vector_dims(embedding) BETWEEN 1 AND 16000
    ),
    CONSTRAINT fk_knowledge_segments_document FOREIGN KEY (
        project_id,
        knowledge_base_id,
        knowledge_document_id
    ) REFERENCES knowledge_documents (
        project_id,
        knowledge_base_id,
        id
    ) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_segments_document
    ON knowledge_segments (
        project_id,
        knowledge_base_id,
        knowledge_document_id,
        document_version,
        position
    );

-- 5. 摄取与删除后台任务。claim 和重试直接保存在单行，不建立 Attempt 历史表。
CREATE TABLE knowledge_tasks (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    resource_id UUID NOT NULL,
    kind VARCHAR(32) NOT NULL,
    target_version INTEGER,
    status VARCHAR(16) DEFAULT 'queued' NOT NULL,
    attempt_count SMALLINT DEFAULT 0 NOT NULL,
    max_attempts SMALLINT DEFAULT 3 NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    claim_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_knowledge_tasks PRIMARY KEY (id),
    CONSTRAINT ck_knowledge_tasks_kind CHECK (
        kind IN ('ingest_document', 'delete_document', 'delete_knowledge_base')
    ),
    CONSTRAINT ck_knowledge_tasks_target_version CHECK (
        (kind = 'ingest_document' AND target_version IS NOT NULL AND target_version >= 1)
        OR (kind <> 'ingest_document' AND target_version IS NULL)
    ),
    CONSTRAINT ck_knowledge_tasks_status CHECK (
        status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')
    ),
    CONSTRAINT ck_knowledge_tasks_attempts CHECK (
        attempt_count BETWEEN 0 AND max_attempts AND max_attempts = 3
    ),
    CONSTRAINT ck_knowledge_tasks_claim CHECK (
        (status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)
    ),
    CONSTRAINT ck_knowledge_tasks_finished CHECK (
        (status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)
    ),
    CONSTRAINT fk_knowledge_tasks_project FOREIGN KEY (project_id)
        REFERENCES public.projects (id) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_tasks_claim
    ON knowledge_tasks (available_at, created_at, id)
    WHERE status IN ('queued', 'retry_wait');

CREATE INDEX ix_knowledge_tasks_expired
    ON knowledge_tasks (lease_until, id)
    WHERE status = 'running';

CREATE UNIQUE INDEX uq_knowledge_tasks_open_ingest
    ON knowledge_tasks (resource_id, target_version)
    WHERE kind = 'ingest_document' AND status IN ('queued', 'running', 'retry_wait');

CREATE UNIQUE INDEX uq_knowledge_tasks_open_document_delete
    ON knowledge_tasks (resource_id)
    WHERE kind = 'delete_document' AND status IN ('queued', 'running', 'retry_wait');

CREATE UNIQUE INDEX uq_knowledge_tasks_open_base_delete
    ON knowledge_tasks (resource_id)
    WHERE kind = 'delete_knowledge_base' AND status IN ('queued', 'running', 'retry_wait');

COMMENT ON TABLE knowledge_model_configurations IS 'Knowledge Package 使用的一组 Embedding 与 Reranker 模型配置及二者共用的当前加密 API Key。';
COMMENT ON TABLE knowledge_bases IS 'Project 内使用同一 Knowledge Model Configuration 的 Knowledge Document 集合。';
COMMENT ON TABLE knowledge_documents IS '上传文件、MinIO object key、切分参数、处理版本和当前处理状态。';
COMMENT ON TABLE knowledge_segments IS 'Knowledge Document 当前已发布 version 的有序文本块及其 embedding。';
COMMENT ON TABLE knowledge_tasks IS '摄取和删除后台任务；claim、lease 与尝试次数直接保存在任务行。';

COMMENT ON COLUMN knowledge_model_configurations.id IS 'Knowledge 模型配置标识。';
COMMENT ON COLUMN knowledge_model_configurations.display_name IS '管理页面显示名称。';
COMMENT ON COLUMN knowledge_model_configurations.status IS '配置状态：active 或 disabled。';
COMMENT ON COLUMN knowledge_model_configurations.base_url IS 'OpenAI-compatible API base URL。';
COMMENT ON COLUMN knowledge_model_configurations.embedding_model IS 'Provider 接受的 Embedding model 名称。';
COMMENT ON COLUMN knowledge_model_configurations.embedding_dimension IS '该配置返回的固定向量维度。';
COMMENT ON COLUMN knowledge_model_configurations.embedding_max_batch IS '单次 Embedding 请求最多包含的文本数量。';
COMMENT ON COLUMN knowledge_model_configurations.reranker_model IS 'Provider 接受的 Reranker model 名称。';
COMMENT ON COLUMN knowledge_model_configurations.reranker_max_batch IS '单次 Rerank 请求最多包含的候选数量。';
COMMENT ON COLUMN knowledge_model_configurations.request_timeout_seconds IS '单次 Provider 请求超时秒数。';
COMMENT ON COLUMN knowledge_model_configurations.api_key_nonce IS '当前 API Key Secret Envelope 的 12-byte nonce。';
COMMENT ON COLUMN knowledge_model_configurations.api_key_ciphertext IS '当前 API Key Secret Envelope 的 ciphertext。';
COMMENT ON COLUMN knowledge_model_configurations.created_at IS '配置创建时间。';
COMMENT ON COLUMN knowledge_model_configurations.updated_at IS '配置最后更新时间。';

COMMENT ON COLUMN knowledge_bases.id IS 'Knowledge Base 标识。';
COMMENT ON COLUMN knowledge_bases.project_id IS '所属 Project 标识。';
COMMENT ON COLUMN knowledge_bases.name IS 'Project 内唯一的 Knowledge Base 名称。';
COMMENT ON COLUMN knowledge_bases.description IS 'Knowledge Base 描述。';
COMMENT ON COLUMN knowledge_bases.model_configuration_id IS '该 Knowledge Base 固定使用的 Embedding 与 Reranker 配置。';
COMMENT ON COLUMN knowledge_bases.status IS 'Knowledge Base 状态：active、disabled 或 deleting。';
COMMENT ON COLUMN knowledge_bases.created_at IS 'Knowledge Base 创建时间。';
COMMENT ON COLUMN knowledge_bases.updated_at IS 'Knowledge Base 最后更新时间。';

COMMENT ON COLUMN knowledge_documents.id IS 'Knowledge Document 标识。';
COMMENT ON COLUMN knowledge_documents.project_id IS '所属 Project 标识。';
COMMENT ON COLUMN knowledge_documents.knowledge_base_id IS '所属 Knowledge Base 标识。';
COMMENT ON COLUMN knowledge_documents.name IS '用户看到的 Document 名称。';
COMMENT ON COLUMN knowledge_documents.original_name IS '上传文件的原始文件名。';
COMMENT ON COLUMN knowledge_documents.storage_key IS '由 Package 生成、位于配置 MinIO bucket 内的 object key。';
COMMENT ON COLUMN knowledge_documents.media_type IS '上传时记录的媒体类型。';
COMMENT ON COLUMN knowledge_documents.size_bytes IS '上传文件字节数。';
COMMENT ON COLUMN knowledge_documents.status IS '处理状态：uploading、queued、processing、ready、failed 或 deleting。';
COMMENT ON COLUMN knowledge_documents.version IS '每次用户重试或删除前递增，用于拒绝旧摄取结果。';
COMMENT ON COLUMN knowledge_documents.chunk_size IS '按字符切分的目标最大长度。';
COMMENT ON COLUMN knowledge_documents.chunk_overlap IS '相邻 Segment 重叠字符数。';
COMMENT ON COLUMN knowledge_documents.segment_count IS '当前已发布 version 的 Segment 数量。';
COMMENT ON COLUMN knowledge_documents.error_message IS '摄取最终失败时向用户展示的错误。';
COMMENT ON COLUMN knowledge_documents.created_at IS 'Knowledge Document 创建时间。';
COMMENT ON COLUMN knowledge_documents.updated_at IS 'Knowledge Document 最后更新时间。';

COMMENT ON COLUMN knowledge_segments.id IS 'Knowledge Segment 标识。';
COMMENT ON COLUMN knowledge_segments.project_id IS '所属 Project 标识。';
COMMENT ON COLUMN knowledge_segments.knowledge_base_id IS '所属 Knowledge Base 标识。';
COMMENT ON COLUMN knowledge_segments.knowledge_document_id IS '所属 Knowledge Document 标识。';
COMMENT ON COLUMN knowledge_segments.document_version IS '生成本 Segment 时的 Knowledge Document version。';
COMMENT ON COLUMN knowledge_segments.position IS 'Segment 在 Document version 内从 1 开始的位置。';
COMMENT ON COLUMN knowledge_segments.content IS '用于检索和引用展示的文本内容。';
COMMENT ON COLUMN knowledge_segments.source_position IS '页码、sheet 或行号等来源位置。';
COMMENT ON COLUMN knowledge_segments.embedding IS '与 Base 模型配置维度一致的 pgvector 向量。';
COMMENT ON COLUMN knowledge_segments.created_at IS 'Knowledge Segment 创建时间。';

COMMENT ON COLUMN knowledge_tasks.id IS 'Knowledge Task 标识。';
COMMENT ON COLUMN knowledge_tasks.project_id IS '所属 Project 标识。';
COMMENT ON COLUMN knowledge_tasks.resource_id IS '按 kind 指向 Knowledge Document 或 Knowledge Base 的业务 id。';
COMMENT ON COLUMN knowledge_tasks.kind IS '任务类型：摄取 Document、删除 Document 或删除 Base。';
COMMENT ON COLUMN knowledge_tasks.target_version IS 'ingest_document 任务允许发布的 Document version。';
COMMENT ON COLUMN knowledge_tasks.status IS '任务状态：queued、running、retry_wait、succeeded 或 failed。';
COMMENT ON COLUMN knowledge_tasks.attempt_count IS '已经开始执行的次数。';
COMMENT ON COLUMN knowledge_tasks.max_attempts IS '允许执行的最大次数，MVP 固定为 3。';
COMMENT ON COLUMN knowledge_tasks.available_at IS 'queued 或 retry_wait 任务最早可 claim 的时间。';
COMMENT ON COLUMN knowledge_tasks.claim_token IS '当前 Worker claim 的随机 token；非 running 时为空。';
COMMENT ON COLUMN knowledge_tasks.lease_until IS '当前 claim 到期时间；过期后任务可重新 claim。';
COMMENT ON COLUMN knowledge_tasks.error_message IS '最近一次执行失败的可展示错误。';
COMMENT ON COLUMN knowledge_tasks.created_at IS 'Knowledge Task 创建时间。';
COMMENT ON COLUMN knowledge_tasks.updated_at IS 'Knowledge Task 最后更新时间。';
COMMENT ON COLUMN knowledge_tasks.finished_at IS '任务最终成功或失败的时间。';
