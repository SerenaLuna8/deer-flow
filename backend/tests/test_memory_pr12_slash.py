from deerflow.skills.slash import parse_slash_skill_reference


def test_dream_is_reserved_from_skill_activation() -> None:
    assert parse_slash_skill_reference("/dream") is None
    assert parse_slash_skill_reference("/dream now") is None
    assert parse_slash_skill_reference("/Dream") is None
