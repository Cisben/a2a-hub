"""Validate publisher-declared artifact references. Never fetch their URLs."""
import re
from urllib.parse import urlsplit

from task_trust import Problem

MAX_ARTIFACTS = 8
SCHEMA = {
    "type": "array", "maxItems": MAX_ARTIFACTS,
    "description": "Publisher declarations only; the hub does not fetch or verify files.",
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["url", "media_type", "size_bytes"],
        "properties": {
            "url": {"type": "string", "format": "uri", "maxLength": 2000},
            "media_type": {"type": "string", "maxLength": 128},
            "size_bytes": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
        },
    },
}


def validate(value, required=False):
    """Return a normalized copy, or fail the whole request before any write."""
    if not isinstance(value, list) or len(value) > MAX_ARTIFACTS or (required and not value):
        raise Problem(400, "invalid_artifacts", "artifacts must be a list of up to 8 references")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - set(SCHEMA["items"]["properties"]):
            raise Problem(400, "invalid_artifacts", "unknown artifact fields")
        url = entry.get("url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid_url = (parsed and parsed.scheme == "https" and parsed.hostname
                         and parsed.username is None and parsed.password is None
                         and parsed.port in (None, 443) and len(url) <= 2000
                         and not any(char.isspace() or ord(char) < 32 for char in url))
        except ValueError:
            valid_url = False
        if not valid_url:
            raise Problem(400, "invalid_artifacts", "artifact url must be HTTPS without credentials, on port 443")
        media_type = entry.get("media_type")
        if not isinstance(media_type, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", media_type, re.ASCII) or len(media_type) > 128:
            raise Problem(400, "invalid_artifacts", "media_type must be a MIME type without parameters")
        size = entry.get("size_bytes")
        if type(size) is not int or not 0 <= size <= 2**53 - 1:
            raise Problem(400, "invalid_artifacts", "size_bytes must be a nonnegative safe integer")
        if "sha256" in entry and (not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise Problem(400, "invalid_artifacts", "sha256 must contain 64 lowercase hexadecimal characters")
        if "name" in entry and (not isinstance(entry["name"], str) or not entry["name"].strip() or len(entry["name"]) > 200):
            raise Problem(400, "invalid_artifacts", "name must contain 1-200 characters")
        result.append(dict(entry))
    return result
