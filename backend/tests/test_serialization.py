"""Tests for deerflow.runtime.serialization."""

from __future__ import annotations


class _FakePydanticV2:
    """Object with model_dump (Pydantic v2)."""

    def model_dump(self):
        return {"key": "v2"}


class _FakePydanticV1:
    """Object with dict (Pydantic v1)."""

    def dict(self):
        return {"key": "v1"}


class _Unprintable:
    """Object whose str() raises."""

    def __str__(self):
        raise RuntimeError("no str")

    def __repr__(self):
        return "<Unprintable>"


def test_serialize_none():
    from deerflow.runtime.serialization import serialize_lc_object

    assert serialize_lc_object(None) is None


def test_serialize_primitives():
    from deerflow.runtime.serialization import serialize_lc_object

    assert serialize_lc_object("hello") == "hello"
    assert serialize_lc_object(42) == 42
    assert serialize_lc_object(3.14) == 3.14
    assert serialize_lc_object(True) is True


def test_serialize_dict():
    from deerflow.runtime.serialization import serialize_lc_object

    obj = {"a": _FakePydanticV2(), "b": [1, "two"]}
    result = serialize_lc_object(obj)
    assert result == {"a": {"key": "v2"}, "b": [1, "two"]}


def test_serialize_list():
    from deerflow.runtime.serialization import serialize_lc_object

    result = serialize_lc_object([_FakePydanticV1(), 1])
    assert result == [{"key": "v1"}, 1]


def test_serialize_tuple():
    from deerflow.runtime.serialization import serialize_lc_object

    result = serialize_lc_object((_FakePydanticV2(),))
    assert result == [{"key": "v2"}]


def test_serialize_pydantic_v2():
    from deerflow.runtime.serialization import serialize_lc_object

    assert serialize_lc_object(_FakePydanticV2()) == {"key": "v2"}


def test_serialize_pydantic_v1():
    from deerflow.runtime.serialization import serialize_lc_object

    assert serialize_lc_object(_FakePydanticV1()) == {"key": "v1"}


def test_serialize_fallback_str():
    from deerflow.runtime.serialization import serialize_lc_object

    result = serialize_lc_object(object())
    assert isinstance(result, str)


def test_serialize_fallback_repr():
    from deerflow.runtime.serialization import serialize_lc_object

    assert serialize_lc_object(_Unprintable()) == "<Unprintable>"


def test_serialize_channel_values_strips_pregel_keys():
    from deerflow.runtime.serialization import serialize_channel_values

    raw = {
        "messages": ["hello"],
        "__pregel_tasks": "internal",
        "__pregel_resuming": True,
        "__interrupt__": [{"value": "ask_human", "resumable": True}],
        "title": "Test",
    }
    result = serialize_channel_values(raw)
    assert "messages" in result
    assert "title" in result
    assert "__pregel_tasks" not in result
    assert "__pregel_resuming" not in result
    assert "__interrupt__" in result
    assert isinstance(result["__interrupt__"], list)
    assert len(result["__interrupt__"]) == 1
    assert result["__interrupt__"][0]["value"] == "ask_human"


def test_serialize_channel_values_serializes_objects():
    from deerflow.runtime.serialization import serialize_channel_values

    result = serialize_channel_values({"obj": _FakePydanticV2()})
    assert result == {"obj": {"key": "v2"}}


def test_serialize_messages_tuple():
    from deerflow.runtime.serialization import serialize_messages_tuple

    chunk = _FakePydanticV2()
    metadata = {"langgraph_node": "agent"}
    result = serialize_messages_tuple((chunk, metadata))
    assert result == [{"key": "v2"}, {"langgraph_node": "agent"}]


def test_serialize_messages_tuple_non_dict_metadata():
    from deerflow.runtime.serialization import serialize_messages_tuple

    result = serialize_messages_tuple((_FakePydanticV2(), "not-a-dict"))
    assert result == [{"key": "v2"}, {}]


def test_serialize_messages_tuple_fallback():
    from deerflow.runtime.serialization import serialize_messages_tuple

    result = serialize_messages_tuple("not-a-tuple")
    assert result == "not-a-tuple"


def test_serialize_dispatcher_messages_mode():
    from deerflow.runtime.serialization import serialize

    chunk = _FakePydanticV2()
    result = serialize((chunk, {"node": "x"}), mode="messages")
    assert result == [{"key": "v2"}, {"node": "x"}]


def test_serialize_dispatcher_values_mode():
    from deerflow.runtime.serialization import serialize

    result = serialize({"msg": "hi", "__pregel_tasks": "x"}, mode="values")
    assert result == {"msg": "hi"}


def test_serialize_dispatcher_default_mode():
    from deerflow.runtime.serialization import serialize

    result = serialize(_FakePydanticV1())
    assert result == {"key": "v1"}


# ── strip_data_url_image_blocks ──────────────────────────────────────────────


def _make_msg(
    content,
    *,
    hide_from_ui=False,
    msg_type="human",
):
    """Build a serialised-style message dict."""
    msg = {"type": msg_type, "content": content}
    if hide_from_ui:
        msg["additional_kwargs"] = {"hide_from_ui": True}
    return msg


def test_strip_data_url_removes_base64_from_hidden_messages():
    from deerflow.runtime.serialization import strip_data_url_image_blocks

    messages = [
        _make_msg(
            [
                {"type": "text", "text": "Here are the images:"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBOR..."},
                },
                {"type": "text", "text": "- file.jpg (image/jpeg)"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,/9j/..."},
                },
            ],
            hide_from_ui=True,
        ),
    ]
    result = strip_data_url_image_blocks(messages)
    assert len(result) == 1
    content = result[0]["content"]
    # Only text blocks remain
    assert content == [
        {"type": "text", "text": "Here are the images:"},
        {"type": "text", "text": "- file.jpg (image/jpeg)"},
    ]


def test_strip_data_url_preserves_non_hidden_messages():
    from deerflow.runtime.serialization import strip_data_url_image_blocks

    messages = [
        _make_msg(
            [
                {"type": "text", "text": "Check this out"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBOR..."},
                },
            ],
            hide_from_ui=False,
        ),
    ]
    result = strip_data_url_image_blocks(messages)
    assert result == messages


def test_strip_data_url_preserves_https_image_urls():
    from deerflow.runtime.serialization import strip_data_url_image_blocks

    messages = [
        _make_msg(
            [
                {"type": "text", "text": "See image"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/img.png"},
                },
            ],
            hide_from_ui=True,
        ),
    ]
    result = strip_data_url_image_blocks(messages)
    assert result == messages


def test_strip_data_url_handles_string_content():
    from deerflow.runtime.serialization import strip_data_url_image_blocks

    messages = [
        _make_msg("plain text content", hide_from_ui=True),
    ]
    result = strip_data_url_image_blocks(messages)
    assert result == messages


def test_strip_data_url_handles_non_dict_messages():
    from deerflow.runtime.serialization import strip_data_url_image_blocks

    result = strip_data_url_image_blocks(["a_string", None, 42])
    assert result == ["a_string", None, 42]


def test_strip_data_url_mixed_messages():
    """A realistic mix: normal user message + hidden image injection + AI reply."""
    from deerflow.runtime.serialization import strip_data_url_image_blocks

    messages = [
        _make_msg("Please analyze this image", hide_from_ui=False),
        _make_msg(
            [
                {"type": "text", "text": "Here are the images:"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AABBCCDD"},
                },
            ],
            hide_from_ui=True,
        ),
        _make_msg("I can see a landscape", msg_type="ai"),
    ]
    result = strip_data_url_image_blocks(messages)
    assert len(result) == 3
    # First message untouched
    assert result[0]["content"] == "Please analyze this image"
    # Hidden message: image_url stripped, text kept
    assert result[1]["content"] == [{"type": "text", "text": "Here are the images:"}]
    # AI message untouched
    assert result[2]["content"] == "I can see a landscape"


def test_serialize_channel_values_for_api_strips_base64():
    from deerflow.runtime.serialization import serialize_channel_values_for_api

    channel_values = {
        "messages": [
            {
                "type": "human",
                "content": "hello",
            },
            {
                "type": "human",
                "content": [
                    {"type": "text", "text": "images:"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,BIGDATA"},
                    },
                ],
                "additional_kwargs": {"hide_from_ui": True},
            },
        ],
        "title": "My thread",
    }
    result = serialize_channel_values_for_api(channel_values)
    assert result["title"] == "My thread"
    assert len(result["messages"]) == 2
    assert result["messages"][0]["content"] == "hello"
    # base64 block stripped, text block kept
    assert result["messages"][1]["content"] == [{"type": "text", "text": "images:"}]


def test_serialize_channel_values_for_api_no_messages():
    """When channel_values has no messages key, returns without error."""
    from deerflow.runtime.serialization import serialize_channel_values_for_api

    result = serialize_channel_values_for_api({"title": "empty"})
    assert result == {"title": "empty"}


def test_serialize_channel_values_for_api_drops_legacy_viewed_image_base64():
    from deerflow.runtime.serialization import serialize_channel_values_for_api

    result = serialize_channel_values_for_api(
        {
            "viewed_images": {
                "/mnt/user-data/uploads/legacy.png": {
                    "mime_type": "image/png",
                    "base64": "LEGACY_PERSISTED_IMAGE_BYTES",
                },
                "/mnt/user-data/uploads/current.png": {
                    "mime_type": "image/png",
                    "size": 42,
                    "sha256": "a" * 64,
                    "file_ref": {
                        "path": "/mnt/user-data/uploads/current.png",
                        "sandbox_id": "sandbox-1",
                        "run_id": "run-1",
                        "project_id": "project-1",
                        "owner_user_id": "owner-1",
                    },
                },
                "/Users/private/secret.svg": {
                    "mime_type": "image/svg+xml",
                    "size": 42,
                    "sha256": "b" * 64,
                    "file_ref": {
                        "path": "/Users/private/secret.svg",
                        "sandbox_id": "sandbox-secret",
                        "run_id": "run-1",
                    },
                },
            }
        }
    )

    assert result["viewed_images"] == {
        "/mnt/user-data/uploads/current.png": {
            "mime_type": "image/png",
            "size": 42,
            "sha256": "a" * 64,
        }
    }
    assert "LEGACY_PERSISTED_IMAGE_BYTES" not in str(result)
    assert "sandbox-1" not in str(result)
    assert "sandbox-secret" not in str(result)
    assert "/Users/private" not in str(result)


def test_serialize_values_mode_strips_base64_from_hidden_messages():
    """The SSE stream emits ``values`` snapshots of the full state, so it must
    strip base64 image data from hide_from_ui messages just like the REST
    endpoints do — otherwise the same payload leaks over the stream."""
    import json

    from deerflow.runtime.serialization import serialize

    state = {
        "messages": [
            _make_msg(
                [
                    {"type": "text", "text": "context"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBOR..."},
                    },
                ],
                hide_from_ui=True,
            ),
        ],
    }
    result = serialize(state, mode="values")
    # the hidden message survives (count/order preserved) but the data: block is gone
    assert len(result["messages"]) == 1
    assert "data:image/png;base64" not in json.dumps(result)
    assert result["messages"][0]["content"] == [{"type": "text", "text": "context"}]


def test_serialize_all_public_state_stream_modes_project_nested_viewed_images():
    import json

    from deerflow.runtime.serialization import serialize

    image_path = "/mnt/user-data/uploads/current.png"
    legacy_host_path = "/Users/private/project/thread/current.png"
    chunk = {
        "public_event": {
            "run_id": "public-run-id",
            "status": "running",
        },
        "nested": {
            "viewed_images": {
                image_path: {
                    "mime_type": "image/png",
                    "size": 42,
                    "sha256": "a" * 64,
                    "file_ref": {
                        "path": image_path,
                        "sandbox_id": "private-sandbox-locator",
                        "run_id": "private-run-locator",
                        "project_id": "private-project-locator",
                        "owner_user_id": "private-owner-locator",
                    },
                },
                legacy_host_path: {
                    "mime_type": "image/png",
                    "base64": "LEGACY_PERSISTED_IMAGE_BYTES",
                    "actual_path": legacy_host_path,
                },
            }
        },
    }

    for mode in ("updates", "debug", "tasks", "checkpoints", "custom"):
        result = serialize(chunk, mode=mode)
        rendered = json.dumps(result)

        assert result["public_event"] == {
            "run_id": "public-run-id",
            "status": "running",
        }
        assert result["nested"]["viewed_images"] == {
            image_path: {
                "mime_type": "image/png",
                "size": 42,
                "sha256": "a" * 64,
            }
        }
        assert "private-sandbox-locator" not in rendered
        assert "private-run-locator" not in rendered
        assert "private-project-locator" not in rendered
        assert "private-owner-locator" not in rendered
        assert "LEGACY_PERSISTED_IMAGE_BYTES" not in rendered
        assert legacy_host_path not in rendered


def test_serialize_messages_mode_strips_hidden_legacy_image_bytes():
    import json

    from deerflow.runtime.serialization import serialize

    chunk = _make_msg(
        [
            {"type": "text", "text": "safe context"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,LEGACY_PERSISTED_IMAGE_BYTES"},
            },
        ],
        hide_from_ui=True,
    )

    result = serialize(
        (chunk, {"langgraph_node": "model"}),
        mode="messages",
    )

    assert result == [
        {
            "type": "human",
            "content": [{"type": "text", "text": "safe context"}],
            "additional_kwargs": {"hide_from_ui": True},
        },
        {"langgraph_node": "model"},
    ]
    assert "LEGACY_PERSISTED_IMAGE_BYTES" not in json.dumps(result)


def test_serialize_preserves_unrelated_public_event_fields():
    from deerflow.runtime.serialization import serialize

    event = {
        "event": "subagent.progress",
        "run_id": "public-run-id",
        "project_id": "public-project-id",
        "payload": {
            "path": "/docs/public-report.md",
            "items": [1, 2, 3],
        },
    }

    assert serialize(event, mode="custom") == event


def test_serialize_preserves_already_projected_viewed_image_metadata():
    from deerflow.runtime.serialization import serialize

    image_path = "/mnt/user-data/uploads/current.png"
    event = {
        "viewed_images": {
            image_path: {
                "mime_type": "image/png",
                "size": 42,
                "sha256": "a" * 64,
            }
        }
    }

    assert serialize(event, mode="custom") == event


def test_serialize_projects_viewed_images_nested_in_model_dump_tuples():
    import json

    from deerflow.runtime.serialization import serialize

    image_path = "/mnt/user-data/uploads/current.png"

    class TuplePayload:
        def model_dump(self):
            return {
                "items": (
                    {
                        "viewed_images": {
                            image_path: {
                                "mime_type": "image/png",
                                "size": 42,
                                "sha256": "a" * 64,
                                "file_ref": {
                                    "path": image_path,
                                    "sandbox_id": "private-sandbox-locator",
                                    "run_id": "private-run-locator",
                                },
                            }
                        }
                    },
                )
            }

    result = serialize(TuplePayload(), mode="custom")

    assert result == {
        "items": [
            {
                "viewed_images": {
                    image_path: {
                        "mime_type": "image/png",
                        "size": 42,
                        "sha256": "a" * 64,
                    }
                }
            }
        ]
    }
    assert "private-" not in json.dumps(result)


def test_serialize_fails_closed_for_cycles_and_excessive_depth():
    import json

    from deerflow.runtime.serialization import serialize

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    assert serialize(cyclic, mode="custom") == {"self": None}

    root: dict[str, object] = {}
    cursor = root
    for _index in range(1_100):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    cursor["viewed_images"] = {
        "/Users/private/secret.png": {
            "mime_type": "image/png",
            "base64": "DEEP_PRIVATE_IMAGE_BYTES",
            "actual_path": "/Users/private/secret.png",
        }
    }

    result = serialize(root, mode="custom")
    rendered = json.dumps(result)

    assert "DEEP_PRIVATE_IMAGE_BYTES" not in rendered
    assert "/Users/private" not in rendered
    serialized_depth = 0
    cursor = result
    while isinstance(cursor, dict) and "child" in cursor:
        serialized_depth += 1
        cursor = cursor["child"]
    assert serialized_depth <= 128


def test_serialize_hidden_messages_strips_all_data_url_shapes_case_insensitively():
    from deerflow.runtime.serialization import serialize

    chunk = _make_msg(
        [
            {
                "type": "image_url",
                "image_url": {"url": "  DATA:image/png;base64,PRIVATE_DICT_BYTES"},
            },
            {
                "type": "image_url",
                "image_url": "DaTa:image/png;base64,PRIVATE_STRING_BYTES",
            },
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/public.png"},
            },
        ],
        hide_from_ui=True,
    )

    result = serialize((chunk, {}), mode="messages")

    assert result[0]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/public.png"},
        }
    ]


def test_serialize_preserves_projected_gif_metadata():
    from deerflow.runtime.serialization import serialize

    image_path = "/mnt/user-data/uploads/current.gif"
    event = {
        "viewed_images": {
            image_path: {
                "mime_type": "image/gif",
                "size": 42,
                "sha256": "a" * 64,
            }
        }
    }

    assert serialize(event, mode="custom") == event


def test_serialize_enforces_width_and_string_budgets_without_tail_leakage():
    import json

    from deerflow.runtime.serialization import serialize

    wide_primitives = list(range(20_000))
    primitive_result = serialize(wide_primitives, mode="custom")
    assert len(primitive_result) <= 10_000

    wide_containers = [{"index": index} for index in range(20_000)]
    container_result = serialize(wide_containers, mode="custom")
    assert len(container_result) <= 10_000
    assert container_result[-1] is not None

    oversized_string = "x" * 1_100_000 + "PRIVATE_STRING_TAIL"
    string_result = serialize(
        {"content": oversized_string},
        mode="custom",
    )
    rendered = json.dumps(string_result)
    assert len(string_result["content"]) <= 1_000_000
    assert "PRIVATE_STRING_TAIL" not in rendered


def test_serialize_messages_metadata_tuple_and_shared_dag_are_safe():
    import json

    from deerflow.runtime.serialization import serialize

    image_path = "/mnt/user-data/uploads/current.png"
    private_image = {
        "viewed_images": {
            image_path: {
                "mime_type": "image/png",
                "size": 42,
                "sha256": "a" * 64,
                "file_ref": {
                    "path": image_path,
                    "sandbox_id": "private-sandbox-locator",
                    "run_id": "private-run-locator",
                },
            }
        }
    }
    message_result = serialize(
        (
            _make_msg("safe"),
            {"nested": (private_image,)},
        ),
        mode="messages",
    )
    rendered = json.dumps(message_result)
    assert "private-" not in rendered
    assert message_result[1]["nested"][0]["viewed_images"][image_path] == {
        "mime_type": "image/png",
        "size": 42,
        "sha256": "a" * 64,
    }

    shared = {"safe": [1, 2, 3]}
    assert serialize(
        {"left": shared, "right": shared},
        mode="custom",
    ) == {
        "left": shared,
        "right": shared,
    }


def test_serialize_hidden_data_url_cannot_hide_marker_behind_width_budget():
    import json

    from deerflow.runtime.serialization import serialize

    hidden_message = {
        "type": "human",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,PRIVATE_WIDTH_SENTINEL"},
            }
        ],
        # Put enough material between the content and its privacy marker that
        # an output-width cap can omit the marker after retaining the bytes.
        "filler": list(range(20_000)),
        "additional_kwargs": {"hide_from_ui": True},
    }

    cases = (
        (serialize((hidden_message, {}), mode="messages"), "messages"),
        (serialize({"messages": [hidden_message]}, mode="values"), "values"),
        (serialize({"nested": hidden_message}, mode="updates"), "updates"),
        (serialize({"nested": hidden_message}, mode="debug"), "debug"),
        (serialize({"nested": hidden_message}, mode="tasks"), "tasks"),
        (serialize({"nested": hidden_message}, mode="checkpoints"), "checkpoints"),
        (serialize({"nested": hidden_message}, mode="custom"), "custom"),
        (serialize({"nested": hidden_message}), "default"),
    )

    for result, mode in cases:
        assert "PRIVATE_WIDTH_SENTINEL" not in json.dumps(result), mode

    messages_result = cases[0][0]
    assert len(messages_result) == 2
    assert isinstance(messages_result[1], dict)


def test_serialize_rejects_non_string_keys_and_preserves_first_bounded_key():
    import json

    from deerflow.runtime.serialization import serialize

    shared_prefix = "k" * 1_024
    result = serialize(
        {
            ("PRIVATE_TUPLE_KEY",): "must-not-break-json",
            shared_prefix + "first": "first-value",
            shared_prefix + "second": "second-value",
        },
        mode="custom",
    )

    # Public frames remain JSON serialisable. Unsupported key types fail
    # closed, and truncation collisions cannot let a later value replace an
    # earlier one.
    json.dumps(result)
    assert ("PRIVATE_TUPLE_KEY",) not in result
    assert result == {shared_prefix: "first-value"}


def test_serialize_rejects_noncanonical_viewed_image_paths():
    import json

    from deerflow.runtime.serialization import serialize

    traversal_path = "/mnt/user-data/uploads/../../Users/private/PRIVATE_PATH_SENTINEL.png"
    result = serialize(
        {
            "viewed_images": {
                traversal_path: {
                    "mime_type": "image/png",
                    "size": 42,
                    "sha256": "a" * 64,
                }
            }
        },
        mode="custom",
    )

    assert result == {"viewed_images": {}}
    assert "PRIVATE_PATH_SENTINEL" not in json.dumps(result)


def test_serialize_total_string_budget_is_shared_across_message_and_metadata():
    from deerflow.runtime.serialization import serialize

    result = serialize(
        (
            {
                "type": "ai",
                "content": [
                    "a" * 1_000_000,
                    "b" * 1_000_000,
                    "c" * 1_000_000,
                ],
            },
            {
                "first": "d" * 1_000_000,
                "second": "PRIVATE_METADATA_TAIL",
            },
        ),
        mode="messages",
    )

    strings: list[str] = []

    def collect(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            strings.extend(value)
            for item in value.values():
                collect(item)

    collect(result)
    assert sum(map(len, strings)) <= 4_000_000
    assert "PRIVATE_METADATA_TAIL" not in "".join(strings)


def test_serialize_all_public_modes_enforce_final_projection_budgets():
    from deerflow.runtime.serialization import serialize

    def metrics(value, *, depth=0):
        nodes = 1
        items = 0
        string_chars = len(value) if isinstance(value, str) else 0
        max_depth = depth
        if isinstance(value, list):
            items += len(value)
            for item in value:
                child_nodes, child_items, child_chars, child_depth = metrics(
                    item,
                    depth=depth + 1,
                )
                nodes += child_nodes
                items += child_items
                string_chars += child_chars
                max_depth = max(max_depth, child_depth)
        elif isinstance(value, dict):
            items += len(value)
            string_chars += sum(len(key) for key in value)
            for item in value.values():
                child_nodes, child_items, child_chars, child_depth = metrics(
                    item,
                    depth=depth + 1,
                )
                nodes += child_nodes
                items += child_items
                string_chars += child_chars
                max_depth = max(max_depth, child_depth)
        return nodes, items, string_chars, max_depth

    wide_payload = {"wide": list(range(20_000))}
    width_cases = (
        serialize((wide_payload, {}), mode="messages"),
        serialize(wide_payload, mode="values"),
        serialize(wide_payload, mode="updates"),
        serialize(wide_payload, mode="debug"),
        serialize(wide_payload, mode="tasks"),
        serialize(wide_payload, mode="checkpoints"),
        serialize(wide_payload, mode="custom"),
        serialize(wide_payload),
    )
    for result in width_cases:
        nodes, items, _string_chars, max_depth = metrics(result)
        assert nodes <= 10_000
        assert items <= 10_000
        assert max_depth <= 128

    oversized = "x" * 1_000_100
    string_payload = {"strings": [oversized] * 5}
    string_cases = (
        serialize((string_payload, {}), mode="messages"),
        serialize(string_payload, mode="values"),
        serialize(string_payload, mode="updates"),
        serialize(string_payload, mode="debug"),
        serialize(string_payload, mode="tasks"),
        serialize(string_payload, mode="checkpoints"),
        serialize(string_payload, mode="custom"),
        serialize(string_payload),
    )
    for result in string_cases:
        _nodes, _items, string_chars, _max_depth = metrics(result)
        assert string_chars <= 4_000_000

        def assert_per_string_budget(value):
            if isinstance(value, str):
                assert len(value) <= 1_000_000
            elif isinstance(value, list):
                for item in value:
                    assert_per_string_budget(item)
            elif isinstance(value, dict):
                assert all(len(key) <= 1_024 for key in value)
                for item in value.values():
                    assert_per_string_budget(item)

        assert_per_string_budget(result)


def test_serialize_all_public_modes_emit_strict_json_for_nonfinite_floats():
    import json

    from deerflow.runtime.serialization import serialize

    payload = {
        "nan": float("nan"),
        "positive_infinity": float("inf"),
        "negative_infinity": float("-inf"),
    }
    cases = (
        serialize((payload, {}), mode="messages"),
        serialize(payload, mode="values"),
        serialize(payload, mode="updates"),
        serialize(payload, mode="debug"),
        serialize(payload, mode="tasks"),
        serialize(payload, mode="checkpoints"),
        serialize(payload, mode="custom"),
        serialize(payload),
    )

    for result in cases:
        json.dumps(result, allow_nan=False)
