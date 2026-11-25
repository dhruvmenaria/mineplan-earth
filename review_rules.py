"""Persistent, drawing-specific semantic review decisions."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


SCHEMA_VERSION = 1
RULE_DIRECTORY = Path(__file__).resolve().parent / "review_rules"


def source_digest(path):
    source = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rule_path(path, directory=None):
    source = Path(path).expanduser().resolve()
    identity = hashlib.sha256(str(source).casefold().encode("utf-8", "replace")).hexdigest()[:16]
    safe_stem = "".join(character if character.isalnum() or character in "-_" else "_"
                        for character in source.stem)[:60] or "drawing"
    return Path(directory or RULE_DIRECTORY) / f"{safe_stem}-{identity}.json"


def load_document(path, directory=None):
    source = Path(path).expanduser().resolve()
    target = rule_path(source, directory)
    if not target.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "source_path": str(source),
            "source_sha256": source_digest(source),
            "rules": [],
        }
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {
            "schema_version": SCHEMA_VERSION,
            "source_path": str(source),
            "source_sha256": source_digest(source),
            "rules": [],
            "warning": "The saved review-rule file could not be read and was ignored.",
        }
    current_digest = source_digest(source)
    if document.get("schema_version") != SCHEMA_VERSION or \
            document.get("source_sha256") != current_digest:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_path": str(source),
            "source_sha256": current_digest,
            "rules": [],
            "warning": "The drawing changed after review; old rules were not applied.",
        }
    document["rules"] = [rule for rule in document.get("rules", []) if isinstance(rule, dict)]
    return document


def load_rules(path, directory=None):
    return load_document(path, directory).get("rules", [])


def save_rules(path, rules, directory=None, source_sha256=None):
    source = Path(path).expanduser().resolve()
    target = rule_path(source, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": SCHEMA_VERSION,
        "source_path": str(source),
        "source_sha256": source_sha256 or source_digest(source),
        "rules": list(rules),
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=".mineplan-review-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def add_rule(path, rule, directory=None):
    document = load_document(path, directory)
    rules = document.get("rules", [])
    identity_fields = ("scope", "plan_id", "entity_handle", "signature", "original_layer")
    identity = tuple(json.dumps(rule.get(field), sort_keys=True) for field in identity_fields)
    kept = []
    for existing in rules:
        existing_identity = tuple(
            json.dumps(existing.get(field), sort_keys=True) for field in identity_fields)
        if existing_identity != identity:
            kept.append(existing)
    kept.append(dict(rule))
    return save_rules(
        path, kept, directory,
        source_sha256=document.get("source_sha256"))


def clear_rules(path, directory=None):
    document = load_document(path, directory)
    return save_rules(
        path, [], directory,
        source_sha256=document.get("source_sha256"))
