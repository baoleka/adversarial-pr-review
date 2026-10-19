"""A small JSON Schema validator for the subset of draft 2020-12 that this skill's schemas use.

It fails closed: a schema keyword it doesn't implement raises SchemaError instead of being
ignored, so a schema can't silently weaken validation. Supported keywords: type, enum, const,
properties, required, additionalProperties, items, minItems, maxItems, minLength, maxLength,
minimum, maximum, pattern, anyOf, oneOf, $ref (local), $defs, plus the annotations $schema,
$id, title, description, default and examples.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples", "$comment"}
SUPPORTED = ANNOTATIONS | {
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
    "anyOf",
    "oneOf",
    "$ref",
    "$defs",
}
MAX_DEPTH = 64


class SchemaError(ValueError):
    """The schema itself is invalid or uses an unsupported keyword."""


def _type_ok(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type {name!r}")


def _resolve(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaError(f"only local $ref is supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            raise SchemaError(f"unresolvable $ref {ref!r}")
        node = node[part]
    if not isinstance(node, Mapping):
        raise SchemaError(f"$ref {ref!r} does not point to a schema")
    return node


def _path(parent: str, key: str | int) -> str:
    return f"{parent}[{key}]" if isinstance(key, int) else f"{parent}.{key}"


def _validate(value: Any, schema: Any, root: Mapping[str, Any], where: str, errors: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        errors.append(f"{where}: nesting deeper than {MAX_DEPTH}")
        return
    if schema is True:
        return
    if schema is False:
        errors.append(f"{where}: no value is allowed here")
        return
    if not isinstance(schema, Mapping):
        raise SchemaError(f"schema at {where} is not an object")
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"unsupported keywords at {where}: {sorted(unknown)}")

    if "$ref" in schema:
        _validate(value, _resolve(root, schema["$ref"]), root, where, errors, depth + 1)

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            errors.append(f"{where}: expected {' or '.join(types)}, got {type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: expected {schema['const']!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{where}: does not match {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above maximum {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], root, _path(where, index), errors, depth + 1)

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required property {key!r}")
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append(f"{where}: non-string key {key!r}")
                continue
            if key in properties:
                _validate(item, properties[key], root, _path(where, key), errors, depth + 1)
            elif "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    errors.append(f"{where}: unexpected property {key!r}")
                elif extra is not True:
                    _validate(item, extra, root, _path(where, key), errors, depth + 1)

    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = 0
            branch_errors: list[list[str]] = []
            for branch in schema[keyword]:
                attempt: list[str] = []
                _validate(value, branch, root, where, attempt, depth + 1)
                branch_errors.append(attempt)
                if not attempt:
                    matches += 1
            if matches == 0:
                best = min(branch_errors, key=len)
                errors.append(f"{where}: matches none of the {keyword} branches ({'; '.join(best[:3])})")
            elif keyword == "oneOf" and matches > 1:
                errors.append(f"{where}: matches {matches} oneOf branches, expected exactly one")


def validate(value: Any, schema: Mapping[str, Any]) -> list[str]:
    """Return a list of validation errors (empty when ``value`` is valid)."""
    errors: list[str] = []
    _validate(value, schema, schema, "$", errors, 0)
    return errors


def check_schema(schema: Mapping[str, Any]) -> None:
    """Raise SchemaError if ``schema`` uses an unsupported keyword anywhere."""

    def walk(node: Any, where: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, Mapping):
            raise SchemaError(f"schema at {where} is not an object")
        unknown = set(node) - SUPPORTED
        if unknown:
            raise SchemaError(f"unsupported keywords at {where}: {sorted(unknown)}")
        for key in ("properties", "$defs"):
            for name, sub in node.get(key, {}).items():
                walk(sub, f"{where}.{key}.{name}")
        for key in ("items", "additionalProperties"):
            if key in node and not isinstance(node[key], bool):
                walk(node[key], f"{where}.{key}")
        for key in ("anyOf", "oneOf"):
            for index, sub in enumerate(node.get(key, [])):
                walk(sub, f"{where}.{key}[{index}]")
        if "$ref" in node:
            _resolve(schema, node["$ref"])

    walk(schema, "$")
