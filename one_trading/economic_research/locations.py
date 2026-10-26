"""Resolve locators to typed raw positions; source bytes remain immutable."""
import json
import re


# Read a complete numeric-looking token before accepting its value. Unicode
# digits and unsupported separators stay in the token, so an ASCII suffix of
# an unsupported spelling cannot become a separate measurement.
_NUMERIC_CANDIDATE = re.compile(
    r"(?<![\d.,_+\-\u2212\uFF0B\uFF0D\uFF0E\uFF0C])"
    r"[+\-]?(?:\d|\.\d)[\d.,_eE+\-\u2212\uFF0B\uFF0D\uFF0E\uFF0C]*"
)
_NUMERIC_LITERAL = re.compile(
    r"[+\-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
    r"(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+\-]?[0-9]+)?"
)


def _numeric_quote_spans(raw, quote):
    if _NUMERIC_LITERAL.fullmatch(quote) is None:
        raise ValueError("invalid_numeric_quote")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("numeric_quote_requires_utf8") from exc
    spans, char_offset, byte_offset = [], 0, 0
    for match in _NUMERIC_CANDIDATE.finditer(text):
        # Advance over disjoint slices, preserving exact UTF-8 byte positions
        # without repeatedly encoding the whole document prefix.
        start = byte_offset + len(text[char_offset:match.start()].encode("utf-8"))
        end = start + len(match.group().encode("utf-8"))
        if match.group() == quote:
            spans.append([start, end])
        char_offset, byte_offset = match.end(), end
    if not spans:
        raise ValueError("partial_numeric_quote")
    return spans


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
        if not trimmed:
            raise ValueError("locator_quote_missing")
        if numeric:
            spans = _numeric_quote_spans(raw, trimmed)
        else:
            token, spans, offset = trimmed.encode(), [], 0
            while (start := raw.find(token, offset)) >= 0:
                end = start + len(token)
                spans.append([start, end])
                offset = start + 1
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
