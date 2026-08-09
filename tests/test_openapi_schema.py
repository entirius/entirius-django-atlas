# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stage 6: defensive OpenAPI schema generation tests.

drf-spectacular schema generation MUST pass without warnings/errors and produce
a clean operationId/tag set. Mirrors what `python manage.py spectacular --validate`
checks, but runs inline in pytest.
"""

import pytest

EXPECTED_TAGS = {
    "Sources",
    "Source Feeds",
    "Source Mappings",
    "Source Products",
    "Product Source Links",
    "Source Import Logs",
    "Source Events",
    "Source Push",
    "Source Settings",
    "Source Connectors",
}


def _generate_schema():
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


@pytest.mark.django_db
def test_schema_generates_without_exception():
    schema = _generate_schema()
    assert "paths" in schema
    assert len(schema["paths"]) > 0


@pytest.mark.django_db
def test_schema_contains_all_expected_tags():
    schema = _generate_schema()
    tags_seen: set[str] = set()
    for path_item in schema["paths"].values():
        for method_def in path_item.values():
            if isinstance(method_def, dict):
                tags_seen.update(method_def.get("tags", []))
    missing = EXPECTED_TAGS - tags_seen
    assert not missing, f"Missing OpenAPI tags: {missing}"


@pytest.mark.django_db
def test_schema_has_no_underscore_2_operation_ids():
    schema = _generate_schema()
    duplicates: list[str] = []
    for path_item in schema["paths"].values():
        for method_def in path_item.values():
            if isinstance(method_def, dict):
                op_id = method_def.get("operationId", "")
                if op_id.endswith("_2"):
                    duplicates.append(op_id)
    assert not duplicates, f"drf-spectacular collisions detected (operationId ending '_2'): {duplicates}"
