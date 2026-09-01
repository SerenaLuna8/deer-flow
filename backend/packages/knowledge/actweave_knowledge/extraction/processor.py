"""One admission sequence for already-authorized local extraction inputs."""

from __future__ import annotations

from pathlib import Path

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from .contracts import Document, ExtractionContext, ExtractionError, ExtractSetting
from .registry import ExtractorRegistry, default_registry
from .signatures import validate_file_signature


class ExtractProcessor:
    def __init__(self, registry: ExtractorRegistry | None = None):
        self.registry = registry if registry is not None else default_registry()

    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        extension = Path(setting.original_name).suffix.lower()
        item = self.registry.resolve(datasource_type=setting.datasource_type, etl_type=setting.profile.etl_type, extension=extension)
        if (setting.profile.extractor_id, setting.profile.extractor_version) != (item.extractor_id, item.extractor_version):
            raise ExtractionError("PARSER_PROFILE_UNAVAILABLE")
        if item.dependency_probe() is not None:
            raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
        context.check_cancelled()
        try:
            size = setting.source_path.stat().st_size
        except OSError:
            raise ExtractionError("FORMAT_SIGNATURE_MISMATCH") from None
        if size > context.limits.max_source_bytes:
            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "原文件大小超过限制")
        validate_file_signature(setting.source_path, extension, context.limits)
        context.check_cancelled()
        documents = item.factory().extract(setting, context)
        total = 0
        for document in documents:
            context.check_cancelled()
            total += len(document.page_content)
            if total > context.limits.max_text_chars:
                raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "提取正文长度超过限制")
        return documents
