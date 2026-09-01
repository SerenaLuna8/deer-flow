"""upstream's fixed local XML branch, with entity rejection before partitioning."""

from __future__ import annotations

import tempfile
from typing import override

from lxml import etree

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractionError, ExtractSetting
from ..runtime_resources import prepare_local_parser
from .elements import elements_to_documents


class UnstructuredXmlExtractor(BaseExtractor):
    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        context.check_cancelled()
        # lxml decodes the XML declaration/BOM before checking the document
        # type, so UTF-16 cannot hide DTDs from a UTF-8 substring scan.
        parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
        try:
            tree = etree.parse(str(setting.source_path), parser)
            if tree.docinfo.doctype or any(isinstance(node, etree._Entity) for node in tree.iter()):
                raise ExtractionError("FORMAT_SIGNATURE_MISMATCH")
        except etree.XMLSyntaxError:
            raise ExtractionError("FORMAT_SIGNATURE_MISMATCH") from None
        prepare_local_parser("unstructured.xml")
        from unstructured.partition.xml import partition_xml

        with tempfile.NamedTemporaryFile(suffix=".xml", dir=context.work_dir) as safe:
            safe.write(etree.tostring(tree, encoding="utf-8", xml_declaration=True))
            safe.flush()
            elements = partition_xml(filename=safe.name, languages=[""])
        context.check_cancelled()
        return elements_to_documents(elements, kind="xml")
