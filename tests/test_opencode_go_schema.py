from cautious_crypto_bro.domain import (
    IntentExtraction,
)
from cautious_crypto_bro.opencode_go import (
    _strict_response_schema,
)


def _walk(value):
    yield value

    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)

    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_intent_schema_is_normalized_for_strict_responses() -> None:
    original = IntentExtraction.model_json_schema()

    normalized = _strict_response_schema(original)

    objects = [
        item
        for item in _walk(normalized)
        if isinstance(item, dict)
        and isinstance(
            item.get("properties"),
            dict,
        )
    ]

    assert objects

    for item in objects:
        properties = item["properties"]

        assert set(item["required"]) == set(properties)

        assert item["additionalProperties"] is False

    for item in _walk(normalized):
        if isinstance(item, dict):
            assert "default" not in item


def test_normalization_does_not_mutate_pydantic_schema() -> None:
    original = IntentExtraction.model_json_schema()

    assert any(isinstance(item, dict) and "default" in item for item in _walk(original))

    _strict_response_schema(original)

    assert any(isinstance(item, dict) and "default" in item for item in _walk(original))
