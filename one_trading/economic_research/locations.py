"""Resolve locators to typed raw positions; source bytes remain immutable."""
import json
import re


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def locate(raw, locator, numeric=False):
    if ("quote" in locator) == ("json_pointer" in locator):
        raise ValueError("one_locator_required")
    try:
        document = json.loads(raw, parse_float=str, object_pairs_hook=_object)
        is_json = True
    except (json.JSONDecodeError, UnicodeDecodeError):
        document, is_json = None, False
    if "quote" in locator:
        quote = locator["quote"]
        if not isinstance(quote, str) or not quote or quote.encode() not in raw:
            raise ValueError("locator_quote_missing")
        # JSON numeric facts have one supported addressing scheme. A substring
        # cannot establish which leaf it came from, even when values coincide.
        if numeric and is_json:
            raise ValueError("numeric_json_requires_pointer")
        trimmed = quote.strip()
        # HTML often repeats a quoted scalar in rendered and embedded content.
        # Keep ALL matching spans as shared provenance, never arbitrary first
        # occurrence or separate independent evidence. Whitespace aliases bind
        # to the same token spans. Partial numeric tokens are not measurements.
        token, spans, offset = trimmed.encode(), [], 0
        if not token:
            raise ValueError("locator_quote_missing")
        while (start := raw.find(token, offset)) >= 0:
            end = start + len(token)
            if not numeric or not ((start and raw[start-1:start] in b"0123456789.,+-")
                                   or raw[end:end+1] and raw[end:end+1] in b"0123456789.,eE"):
                spans.append([start, end])
            offset = start + 1
        if not spans:
            raise ValueError("partial_numeric_quote")
        return trimmed.replace(",", ""), ["text_spans", spans]
    pointer = locator["json_pointer"]
    if not is_json or not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("invalid_json_pointer")
    value, path = document, []
    for token in pointer[1:].split("/") if pointer else []:
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid_json_pointer_escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("noncanonical_array_index")
            index = int(token)
            if index >= len(value):
                raise ValueError("locator_pointer_missing")
            path.append(["index", index])
            value = value[index]
        elif isinstance(value, dict) and token in value:
            path.append(["key", token])
            value = value[token]
        else:
            raise ValueError("locator_pointer_missing")
    if "source_value" in locator and value != locator["source_value"]:
        raise ValueError("locator_value_mismatch")
    return value, ["json_path", path]
