"""Canonical, bounded parsing for complete ``SKILL.md`` documents.

This module is the only owner of YAML semantics for Skill frontmatter.  It is
pure harness code so archive admission, authoring, review, and runtime loading
can share the same fail-closed projection without importing application code.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import yaml
from yaml.events import AliasEvent

from .types import SecretRequirement

MAX_SKILL_FRONTMATTER_BYTES = 256 * 1024
MAX_SKILL_FRONTMATTER_NODES = 2048
MAX_SKILL_FRONTMATTER_DEPTH = 32
MAX_SKILL_SECRET_REQUIREMENTS = 256

_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*(?P<opening_newline>\r\n|\n)"
    r"(?P<yaml>.*?)"
    r"(?P<closing_separator>\r\n|\n)---[ \t]*"
    r"(?P<closing_newline>\r\n|\n|\Z)",
    re.DOTALL,
)

type DiagnosticSeverity = Literal["error", "warning"]
type DiagnosticPathPart = str | int
type ManagedSkillFrontmatterField = Literal[
    "required-secrets",
    "secrets-autonomous",
]


@dataclass(frozen=True)
class SkillFrontmatterDiagnostic:
    """One safe author-facing frontmatter diagnostic.

    Positions are 1-based positions in the complete ``SKILL.md`` document.
    Messages are fixed vocabulary and intentionally never include input text,
    paths, or raw parser exceptions.
    """

    code: str
    severity: DiagnosticSeverity
    field_path: tuple[DiagnosticPathPart, ...]
    line: int | None
    column: int | None
    public_message: str


@dataclass(frozen=True)
class SkillSecretProjection:
    """Structured form projection of the two managed frontmatter fields."""

    required_secrets: tuple[SecretRequirement, ...]
    secrets_autonomous: bool
    secrets_autonomous_explicit: bool
    shorthand_count: int


@dataclass(frozen=True)
class SkillFrontmatterParseResult:
    """Canonical parse result for one complete ``SKILL.md`` string."""

    source_sha256: str
    valid: bool
    patchable: bool
    projection: SkillSecretProjection | None
    frontmatter: dict[str, object] | None
    body: str | None
    diagnostics: tuple[SkillFrontmatterDiagnostic, ...]


@dataclass(frozen=True)
class SkillFrontmatterPatchResult:
    """Byte-preserving managed-field patch result."""

    source_sha256: str
    result_sha256: str
    content: str
    changed: bool
    changed_fields: tuple[ManagedSkillFrontmatterField, ...]
    projection: SkillSecretProjection
    diagnostics: tuple[SkillFrontmatterDiagnostic, ...]


class SkillFrontmatterSourceMismatch(ValueError):
    """The caller attempted to patch a stale editor buffer."""

    def __init__(
        self,
        expected_source_sha256: str,
        actual_source_sha256: str,
    ) -> None:
        self.expected_source_sha256 = expected_source_sha256
        self.actual_source_sha256 = actual_source_sha256
        super().__init__("Skill frontmatter source checksum does not match")


class SkillFrontmatterPatchRejected(ValueError):
    """The document or requested projection cannot be patched safely."""

    def __init__(
        self,
        source_sha256: str,
        diagnostics: Sequence[SkillFrontmatterDiagnostic],
    ) -> None:
        self.source_sha256 = source_sha256
        self.diagnostics = tuple(diagnostics)
        super().__init__("Skill frontmatter patch was rejected")


@dataclass(frozen=True)
class _FrontmatterEnvelope:
    yaml_text: str
    yaml_start: int
    yaml_end: int
    markdown_body: str
    newline: Literal["\n", "\r\n"]


@dataclass(frozen=True)
class _ManagedFieldLocation:
    name: ManagedSkillFrontmatterField
    start: int
    end: int
    line: int
    column: int


@dataclass(frozen=True)
class _ParsedSkillFrontmatter:
    result: SkillFrontmatterParseResult
    envelope: _FrontmatterEnvelope | None
    root_node: yaml.MappingNode | None
    managed_fields: dict[ManagedSkillFrontmatterField, _ManagedFieldLocation]


class _CanonicalYamlError(yaml.YAMLError):
    def __init__(
        self,
        code: str,
        mark: yaml.error.Mark | None,
    ) -> None:
        self.code = code
        self.problem_mark = mark
        super().__init__(code)


class _CanonicalSafeLoader(yaml.SafeLoader):
    """SafeLoader with duplicate, alias, node-count, and depth guards."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._skill_node_count = 0
        self._skill_depth = 0

    def compose_node(self, parent: yaml.Node | None, index: int | None) -> yaml.Node:
        if self.check_event(AliasEvent):
            raise _CanonicalYamlError(
                "yaml_alias_not_allowed",
                self.peek_event().start_mark,
            )
        self._skill_node_count += 1
        if self._skill_node_count > MAX_SKILL_FRONTMATTER_NODES:
            raise _CanonicalYamlError(
                "yaml_node_limit_exceeded",
                self.peek_event().start_mark,
            )
        self._skill_depth += 1
        if self._skill_depth > MAX_SKILL_FRONTMATTER_DEPTH:
            raise _CanonicalYamlError(
                "yaml_depth_limit_exceeded",
                self.peek_event().start_mark,
            )
        try:
            return super().compose_node(parent, index)
        finally:
            self._skill_depth -= 1


def _construct_unique_mapping(
    loader: _CanonicalSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise _CanonicalYamlError(
                "frontmatter_key_not_string",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise _CanonicalYamlError(
                "duplicate_mapping_key",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.constructor.BaseConstructor.construct_mapping(
        loader,
        node,
        deep=deep,
    )


_CanonicalSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


_PUBLIC_MESSAGES: dict[str, str] = {
    "frontmatter_missing": "SKILL.md must start with YAML frontmatter",
    "frontmatter_too_large": "Skill frontmatter exceeds the size limit",
    "yaml_invalid": "Skill frontmatter contains invalid YAML",
    "yaml_alias_not_allowed": "YAML aliases are not allowed in Skill frontmatter",
    "yaml_node_limit_exceeded": "Skill frontmatter exceeds the YAML node limit",
    "yaml_depth_limit_exceeded": "Skill frontmatter exceeds the YAML depth limit",
    "duplicate_mapping_key": "Skill frontmatter mapping keys must be unique",
    "frontmatter_not_mapping": "Skill frontmatter must be a YAML mapping",
    "frontmatter_key_not_string": "Skill frontmatter keys must be strings",
    "required_secrets_not_list": "required-secrets must be a list",
    "required_secrets_limit_exceeded": "required-secrets exceeds the item limit",
    "required_secret_invalid_item": "Each required-secrets item must be a name or mapping",
    "required_secret_unknown_field": "A required-secrets item contains an unsupported field",
    "required_secret_name_required": "Each required-secrets item must contain a string name",
    "required_secret_target_env_required": "required-secrets target_env must be a string",
    "invalid_env_name": "Environment variable names must use POSIX syntax",
    "required_secret_optional_not_boolean": "required-secrets optional must be a boolean",
    "duplicate_env_name": "Environment variable names must be unique",
    "duplicate_target_env": "Sandbox environment targets must be unique within one Skill",
    "secrets_autonomous_not_boolean": "secrets-autonomous must be a boolean",
    "managed_comments_unsupported": "Managed frontmatter comments must be edited in source mode",
    "patch_verification_failed": "The requested frontmatter change could not be verified",
}


def skill_frontmatter_source_sha256(content: str) -> str:
    """Return the lowercase SHA-256 of the document's exact UTF-8 bytes."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _diagnostic(
    code: str,
    *,
    severity: DiagnosticSeverity = "error",
    field_path: tuple[DiagnosticPathPart, ...] = (),
    line: int | None = None,
    column: int | None = None,
) -> SkillFrontmatterDiagnostic:
    return SkillFrontmatterDiagnostic(
        code=code,
        severity=severity,
        field_path=field_path,
        line=line,
        column=column,
        public_message=_PUBLIC_MESSAGES[code],
    )


def _document_position(
    mark: yaml.error.Mark | None,
) -> tuple[int | None, int | None]:
    if mark is None:
        return None, None
    # The YAML stream excludes the first ``---`` line, so add one to the
    # stream's zero-based line and one more to make it editor-facing 1-based.
    return mark.line + 2, mark.column + 1


def _node_position(node: yaml.Node | None) -> tuple[int | None, int | None]:
    return _document_position(None if node is None else node.start_mark)


def _extract_envelope(content: str) -> _FrontmatterEnvelope | None:
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return None
    newline = match.group("opening_newline")
    assert newline in {"\n", "\r\n"}
    return _FrontmatterEnvelope(
        yaml_text=match.group("yaml"),
        yaml_start=match.start("yaml"),
        yaml_end=match.end("yaml"),
        markdown_body=content[match.end() :],
        newline=newline,
    )


def _load_yaml(
    yaml_text: str,
) -> tuple[object, yaml.Node | None]:
    loader = _CanonicalSafeLoader(yaml_text)
    try:
        root_node = loader.get_single_node()
        if root_node is None:
            return None, None
        value = loader.construct_document(root_node)
        return value, root_node
    finally:
        loader.dispose()


def _top_level_nodes(
    root_node: yaml.MappingNode,
) -> dict[str, tuple[yaml.Node, yaml.Node]]:
    nodes: dict[str, tuple[yaml.Node, yaml.Node]] = {}
    for key_node, value_node in root_node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.tag == "tag:yaml.org,2002:str":
            nodes[key_node.value] = (key_node, value_node)
    return nodes


def _mapping_value_nodes(node: yaml.Node) -> dict[str, tuple[yaml.Node, yaml.Node]]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    values: dict[str, tuple[yaml.Node, yaml.Node]] = {}
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.tag == "tag:yaml.org,2002:str":
            values[key_node.value] = (key_node, value_node)
    return values


def _parse_required_secrets(
    raw: object,
    node: yaml.Node | None,
) -> tuple[
    tuple[SecretRequirement, ...],
    int,
    tuple[SkillFrontmatterDiagnostic, ...],
]:
    if not isinstance(raw, list):
        line, column = _node_position(node)
        return (
            (),
            0,
            (
                _diagnostic(
                    "required_secrets_not_list",
                    field_path=("required-secrets",),
                    line=line,
                    column=column,
                ),
            ),
        )

    if len(raw) > MAX_SKILL_SECRET_REQUIREMENTS:
        line, column = _node_position(node)
        return (
            (),
            0,
            (
                _diagnostic(
                    "required_secrets_limit_exceeded",
                    field_path=("required-secrets",),
                    line=line,
                    column=column,
                ),
            ),
        )

    item_nodes = node.value if isinstance(node, yaml.SequenceNode) else []
    requirements: list[SecretRequirement] = []
    diagnostics: list[SkillFrontmatterDiagnostic] = []
    seen: set[str] = set()
    seen_targets: set[str] = set()
    shorthand_count = 0

    for index, item in enumerate(raw):
        item_node = item_nodes[index] if index < len(item_nodes) else None
        name_node = item_node
        optional_node = item_node
        if isinstance(item, str):
            shorthand_count += 1
            name = item.strip()
            target_env = name
            target_env_node = item_node
            optional = False
        elif isinstance(item, dict):
            item_mapping_nodes = _mapping_value_nodes(item_node) if item_node is not None else {}
            unknown = [key for key in item if not isinstance(key, str) or key not in {"name", "target_env", "optional"}]
            if unknown:
                unknown_key = unknown[0]
                key_node = item_mapping_nodes.get(str(unknown_key), (item_node, item_node))[0]
                line, column = _node_position(key_node)
                diagnostics.append(
                    _diagnostic(
                        "required_secret_unknown_field",
                        field_path=("required-secrets", index),
                        line=line,
                        column=column,
                    )
                )
                continue
            raw_name = item.get("name")
            name_node = item_mapping_nodes.get("name", (item_node, item_node))[1]
            if not isinstance(raw_name, str):
                line, column = _node_position(name_node)
                diagnostics.append(
                    _diagnostic(
                        "required_secret_name_required",
                        field_path=("required-secrets", index, "name"),
                        line=line,
                        column=column,
                    )
                )
                continue
            name = raw_name.strip()
            raw_target_env = item.get("target_env", name)
            target_env_node = item_mapping_nodes.get(
                "target_env",
                (name_node, name_node),
            )[1]
            if not isinstance(raw_target_env, str):
                line, column = _node_position(target_env_node)
                diagnostics.append(
                    _diagnostic(
                        "required_secret_target_env_required",
                        field_path=("required-secrets", index, "target_env"),
                        line=line,
                        column=column,
                    )
                )
                continue
            target_env = raw_target_env.strip()
            raw_optional = item.get("optional", False)
            optional_node = item_mapping_nodes.get("optional", (item_node, item_node))[1]
            if not isinstance(raw_optional, bool):
                line, column = _node_position(optional_node)
                diagnostics.append(
                    _diagnostic(
                        "required_secret_optional_not_boolean",
                        field_path=("required-secrets", index, "optional"),
                        line=line,
                        column=column,
                    )
                )
                continue
            optional = raw_optional
        else:
            line, column = _node_position(item_node)
            diagnostics.append(
                _diagnostic(
                    "required_secret_invalid_item",
                    field_path=("required-secrets", index),
                    line=line,
                    column=column,
                )
            )
            continue

        if _ENV_VAR_NAME_RE.fullmatch(name) is None:
            line, column = _node_position(name_node)
            diagnostics.append(
                _diagnostic(
                    "invalid_env_name",
                    field_path=("required-secrets", index, "name"),
                    line=line,
                    column=column,
                )
            )
            continue
        if name in seen:
            line, column = _node_position(name_node)
            diagnostics.append(
                _diagnostic(
                    "duplicate_env_name",
                    field_path=("required-secrets", index, "name"),
                    line=line,
                    column=column,
                )
            )
            continue
        if _ENV_VAR_NAME_RE.fullmatch(target_env) is None:
            line, column = _node_position(target_env_node)
            diagnostics.append(
                _diagnostic(
                    "invalid_env_name",
                    field_path=("required-secrets", index, "target_env"),
                    line=line,
                    column=column,
                )
            )
            continue
        if target_env in seen_targets:
            line, column = _node_position(target_env_node)
            diagnostics.append(
                _diagnostic(
                    "duplicate_target_env",
                    field_path=("required-secrets", index, "target_env"),
                    line=line,
                    column=column,
                )
            )
            continue
        seen.add(name)
        seen_targets.add(target_env)
        requirements.append(
            SecretRequirement(
                name=name,
                optional=optional,
                target_env=target_env,
            )
        )

    return tuple(requirements), shorthand_count, tuple(diagnostics)


def _validate_json_compatible(
    value: object,
    *,
    node: yaml.Node | None,
) -> tuple[SkillFrontmatterDiagnostic, ...]:
    """Reject SafeLoader values that cannot enter the persisted JSON contract."""

    if value is None or isinstance(value, (str, bool, int)):
        return ()
    if isinstance(value, float):
        if math.isfinite(value):
            return ()
        line, column = _node_position(node)
        return (_diagnostic("yaml_invalid", line=line, column=column),)
    if isinstance(value, list):
        value_nodes = node.value if isinstance(node, yaml.SequenceNode) else []
        diagnostics: list[SkillFrontmatterDiagnostic] = []
        for index, item in enumerate(value):
            item_node = value_nodes[index] if index < len(value_nodes) else None
            diagnostics.extend(_validate_json_compatible(item, node=item_node))
        return tuple(diagnostics)
    if isinstance(value, dict):
        mapping_nodes = node.value if isinstance(node, yaml.MappingNode) else []
        diagnostics = []
        for index, (key, item) in enumerate(value.items()):
            key_node, value_node = mapping_nodes[index] if index < len(mapping_nodes) else (None, None)
            if not isinstance(key, str):
                line, column = _node_position(key_node)
                diagnostics.append(
                    _diagnostic(
                        "frontmatter_key_not_string",
                        line=line,
                        column=column,
                    )
                )
                continue
            diagnostics.extend(_validate_json_compatible(item, node=value_node))
        return tuple(diagnostics)
    line, column = _node_position(node)
    return (_diagnostic("yaml_invalid", line=line, column=column),)


def _line_start(source: str, index: int) -> int:
    lf = source.rfind("\n", 0, index)
    return 0 if lf < 0 else lf + 1


def _line_end(source: str, index: int) -> int:
    if index > 0 and source[index - 1 : index] == "\n":
        return index
    lf = source.find("\n", index)
    return len(source) if lf < 0 else lf + 1


def _managed_field_locations(
    envelope: _FrontmatterEnvelope,
    root_node: yaml.MappingNode,
) -> dict[ManagedSkillFrontmatterField, _ManagedFieldLocation]:
    locations: dict[ManagedSkillFrontmatterField, _ManagedFieldLocation] = {}
    for key_node, value_node in root_node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        name = key_node.value
        if name not in {"required-secrets", "secrets-autonomous"}:
            continue
        start = _line_start(envelope.yaml_text, key_node.start_mark.index)
        end = _line_end(
            envelope.yaml_text,
            max(key_node.end_mark.index, value_node.end_mark.index),
        )
        line, column = _node_position(key_node)
        assert line is not None and column is not None
        locations[name] = _ManagedFieldLocation(
            name=name,
            start=start,
            end=end,
            line=line,
            column=column,
        )
    return locations


def _parse_skill_frontmatter_document(content: str) -> _ParsedSkillFrontmatter:
    source_sha256 = skill_frontmatter_source_sha256(content)
    envelope = _extract_envelope(content)
    if envelope is None:
        result = SkillFrontmatterParseResult(
            source_sha256=source_sha256,
            valid=False,
            patchable=False,
            projection=None,
            frontmatter=None,
            body=None,
            diagnostics=(
                _diagnostic(
                    "frontmatter_missing",
                    line=1,
                    column=1,
                ),
            ),
        )
        return _ParsedSkillFrontmatter(result, None, None, {})

    if len(envelope.yaml_text.encode("utf-8")) > MAX_SKILL_FRONTMATTER_BYTES:
        result = SkillFrontmatterParseResult(
            source_sha256=source_sha256,
            valid=False,
            patchable=False,
            projection=None,
            frontmatter=None,
            body=envelope.markdown_body,
            diagnostics=(
                _diagnostic(
                    "frontmatter_too_large",
                    line=2,
                    column=1,
                ),
            ),
        )
        return _ParsedSkillFrontmatter(result, envelope, None, {})

    try:
        loaded, root_node = _load_yaml(envelope.yaml_text)
    except _CanonicalYamlError as exc:
        line, column = _document_position(exc.problem_mark)
        result = SkillFrontmatterParseResult(
            source_sha256=source_sha256,
            valid=False,
            patchable=False,
            projection=None,
            frontmatter=None,
            body=envelope.markdown_body,
            diagnostics=(
                _diagnostic(
                    exc.code,
                    line=line,
                    column=column,
                ),
            ),
        )
        return _ParsedSkillFrontmatter(result, envelope, None, {})
    except yaml.YAMLError as exc:
        line, column = _document_position(getattr(exc, "problem_mark", None))
        result = SkillFrontmatterParseResult(
            source_sha256=source_sha256,
            valid=False,
            patchable=False,
            projection=None,
            frontmatter=None,
            body=envelope.markdown_body,
            diagnostics=(
                _diagnostic(
                    "yaml_invalid",
                    line=line,
                    column=column,
                ),
            ),
        )
        return _ParsedSkillFrontmatter(result, envelope, None, {})

    if not isinstance(loaded, dict) or not isinstance(root_node, yaml.MappingNode):
        line, column = _node_position(root_node)
        result = SkillFrontmatterParseResult(
            source_sha256=source_sha256,
            valid=False,
            patchable=False,
            projection=None,
            frontmatter=None,
            body=envelope.markdown_body,
            diagnostics=(
                _diagnostic(
                    "frontmatter_not_mapping",
                    line=line,
                    column=column,
                ),
            ),
        )
        return _ParsedSkillFrontmatter(result, envelope, None, {})

    top_nodes = _top_level_nodes(root_node)
    diagnostics: list[SkillFrontmatterDiagnostic] = list(_validate_json_compatible(loaded, node=root_node))
    if any(not isinstance(key, str) for key in loaded):
        # _validate_json_compatible already returns source-positioned entries.
        diagnostics = [item for item in diagnostics if item.code == "frontmatter_key_not_string"] or [_diagnostic("frontmatter_key_not_string", line=2, column=1)]

    requirements: tuple[SecretRequirement, ...] = ()
    shorthand_count = 0
    if "required-secrets" in loaded:
        requirements, shorthand_count, requirement_diagnostics = _parse_required_secrets(
            loaded["required-secrets"],
            top_nodes.get("required-secrets", (None, None))[1],
        )
        diagnostics.extend(requirement_diagnostics)

    secrets_autonomous_explicit = "secrets-autonomous" in loaded
    raw_autonomous = loaded.get("secrets-autonomous", True)
    if not isinstance(raw_autonomous, bool):
        autonomous_node = top_nodes.get("secrets-autonomous", (None, None))[1]
        line, column = _node_position(autonomous_node)
        diagnostics.append(
            _diagnostic(
                "secrets_autonomous_not_boolean",
                field_path=("secrets-autonomous",),
                line=line,
                column=column,
            )
        )

    errors = tuple(item for item in diagnostics if item.severity == "error")
    if errors:
        result = SkillFrontmatterParseResult(
            source_sha256=source_sha256,
            valid=False,
            patchable=False,
            projection=None,
            frontmatter=None,
            body=envelope.markdown_body,
            diagnostics=tuple(diagnostics),
        )
        return _ParsedSkillFrontmatter(result, envelope, root_node, {})

    managed_fields = _managed_field_locations(envelope, root_node)
    for name, location in managed_fields.items():
        managed_source = envelope.yaml_text[location.start : location.end]
        if "#" in managed_source:
            diagnostics.append(
                _diagnostic(
                    "managed_comments_unsupported",
                    severity="warning",
                    field_path=(name,),
                    line=location.line,
                    column=location.column,
                )
            )

    projection = SkillSecretProjection(
        required_secrets=requirements,
        secrets_autonomous=raw_autonomous,
        secrets_autonomous_explicit=secrets_autonomous_explicit,
        shorthand_count=shorthand_count,
    )
    patchable = not any(item.code == "managed_comments_unsupported" for item in diagnostics)
    result = SkillFrontmatterParseResult(
        source_sha256=source_sha256,
        valid=True,
        patchable=patchable,
        projection=projection,
        frontmatter=dict(loaded),
        body=envelope.markdown_body,
        diagnostics=tuple(diagnostics),
    )
    return _ParsedSkillFrontmatter(
        result,
        envelope,
        root_node,
        managed_fields,
    )


def parse_skill_frontmatter_document(content: str) -> SkillFrontmatterParseResult:
    """Parse a complete ``SKILL.md`` without raising for authoring errors."""

    return _parse_skill_frontmatter_document(content).result


def parse_required_secrets_value(raw: object) -> tuple[SecretRequirement, ...]:
    """Strict compatibility helper backed by the canonical value projector."""

    requirements, _, diagnostics = _parse_required_secrets(raw, None)
    if diagnostics:
        raise ValueError(diagnostics[0].public_message)
    return requirements


def parse_secrets_autonomous_value(raw: object) -> bool:
    """Strict compatibility helper for one explicit autonomous value."""

    if not isinstance(raw, bool):
        raise ValueError(_PUBLIC_MESSAGES["secrets_autonomous_not_boolean"])
    return raw


def _validate_requested_projection(
    required_secrets: Sequence[SecretRequirement],
    secrets_autonomous: bool,
) -> tuple[SkillSecretProjection | None, tuple[SkillFrontmatterDiagnostic, ...]]:
    diagnostics: list[SkillFrontmatterDiagnostic] = []
    normalized: list[SecretRequirement] = []
    seen: set[str] = set()
    seen_targets: set[str] = set()
    if len(required_secrets) > MAX_SKILL_SECRET_REQUIREMENTS:
        return None, (
            _diagnostic(
                "required_secrets_limit_exceeded",
                field_path=("required-secrets",),
            ),
        )
    for index, requirement in enumerate(required_secrets):
        if not isinstance(requirement, SecretRequirement):
            diagnostics.append(
                _diagnostic(
                    "required_secret_invalid_item",
                    field_path=("required-secrets", index),
                )
            )
            continue
        if not isinstance(requirement.name, str) or _ENV_VAR_NAME_RE.fullmatch(requirement.name) is None:
            diagnostics.append(
                _diagnostic(
                    "invalid_env_name",
                    field_path=("required-secrets", index, "name"),
                )
            )
            continue
        if not isinstance(requirement.optional, bool):
            diagnostics.append(
                _diagnostic(
                    "required_secret_optional_not_boolean",
                    field_path=("required-secrets", index, "optional"),
                )
            )
            continue
        if not isinstance(requirement.target_env, str) or _ENV_VAR_NAME_RE.fullmatch(requirement.target_env) is None:
            diagnostics.append(
                _diagnostic(
                    "invalid_env_name",
                    field_path=("required-secrets", index, "target_env"),
                )
            )
            continue
        if requirement.name in seen:
            diagnostics.append(
                _diagnostic(
                    "duplicate_env_name",
                    field_path=("required-secrets", index, "name"),
                )
            )
            continue
        if requirement.target_env in seen_targets:
            diagnostics.append(
                _diagnostic(
                    "duplicate_target_env",
                    field_path=("required-secrets", index, "target_env"),
                )
            )
            continue
        seen.add(requirement.name)
        seen_targets.add(requirement.target_env)
        normalized.append(requirement)

    if not isinstance(secrets_autonomous, bool):
        diagnostics.append(
            _diagnostic(
                "secrets_autonomous_not_boolean",
                field_path=("secrets-autonomous",),
            )
        )
    if diagnostics:
        return None, tuple(diagnostics)
    return (
        SkillSecretProjection(
            required_secrets=tuple(normalized),
            secrets_autonomous=secrets_autonomous,
            secrets_autonomous_explicit=False,
            shorthand_count=0,
        ),
        (),
    )


__all__ = [
    "MAX_SKILL_FRONTMATTER_BYTES",
    "MAX_SKILL_FRONTMATTER_DEPTH",
    "MAX_SKILL_FRONTMATTER_NODES",
    "MAX_SKILL_SECRET_REQUIREMENTS",
    "ManagedSkillFrontmatterField",
    "SkillFrontmatterDiagnostic",
    "SkillFrontmatterParseResult",
    "SkillFrontmatterPatchRejected",
    "SkillFrontmatterPatchResult",
    "SkillFrontmatterSourceMismatch",
    "SkillSecretProjection",
    "parse_required_secrets_value",
    "parse_secrets_autonomous_value",
    "parse_skill_frontmatter_document",
    "skill_frontmatter_source_sha256",
]
