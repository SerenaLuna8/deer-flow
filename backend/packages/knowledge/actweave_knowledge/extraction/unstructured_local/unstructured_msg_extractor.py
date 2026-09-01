"""Local branch adapted from Dify 9c16c865977e9d89a9ec7ae0536e893f4385a758.

See ../UPSTREAM.md and ../patches.md. API credentials, runtime downloads,
second-pass decoding, and host configuration are deliberately absent.
"""

from __future__ import annotations

from typing import override

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractSetting
from ..runtime_resources import prepare_local_parser
from .elements import elements_to_documents


class UnstructuredMsgExtractor(BaseExtractor):
    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        context.check_cancelled()
        prepare_local_parser("unstructured.msg")
        from unstructured.partition.msg import partition_msg

        elements = partition_msg(filename=str(setting.source_path), languages=[""], process_attachments=False)
        context.check_cancelled()
        return elements_to_documents(elements, kind="mail")
