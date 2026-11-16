"""Literal, optional hints for simple controls; never generate model arguments.

Contract: recall_matches -> bool | None; assess -> Assessment | None (None means
no hints). A constraint is a scalar target or a list. Lists with a missing-field
question are ambiguous alternatives; lists without one are explicit "all" sets
that the caller must completely cover with multiple calls. Neither enables the
single-control fast path. Targets are NEVER clamped, including invalid
numbers. ``explicit`` means all required fields are unambiguously supported and
valid; it is not permission to execute. Relative requests always have explicit
False and no numeric constraints: a known delta with ``relative_tool`` leaves the
numeric field out of missing so callers can query first; vague deltas (or no
query hint) require clarification. The query id remains in original metadata.
``excluded`` tells callers to reject a tool even if its schema accepts the args.
``single_call_complete`` is False for relative, ambiguous and multi-target goals.

Callers exclude complex/mixed-object requests before assessment, and may combine
the goal and user answers with newlines. This module does not interpret history
corrections or resolve conflicting answers. ``all`` phrases select enum "all"
if present, otherwise expose the complete field enum for multi-call coverage.
Choice aliases prefer the longest containing match (副驾驶位 over 驾驶位).
Negated corrections such as 不用全部主驾就好 conservatively return no constraints
and a clarification question; they never assert an all-target scope.
Call validate_hints at catalog import; runtime functions assume validated hints.
"""
import math
import re
import unicodedata
from typing import TypedDict

from jsonschema import Draft202012Validator


class Missing(TypedDict):
    field: str
    question: str


class Assessment(TypedDict):
    missing: list[Missing]
    constraints: dict[str, str | int | float | list]
    relative: bool
    explicit: bool
    excluded: bool
    single_call_complete: bool


MAX_TEXT = 32768
_NEGATIVE = re.compile(r"不|别|勿|莫|无需|并非|禁止|取消|\b(?:not|never|don't|dont|without)\b")
_RELATIVE = re.compile(r"增加|减少|降低|升高|调高|调低|提高|增大|减小|加大|开大|关小|调大|调小|调强|调弱|升|降|加|减|\b(?:increase|decrease|raise|lower)\b")
_VAGUE = re.compile(r"一点|一些|大点|小点|强点|弱点|多点|少点|快点|慢点|更大|更小|再|原来|当前|比现在|往上|往下|\b(?:more|less|higher|relative)\b")
_VAGUE_AMOUNT = re.compile(r"一点|一些|大点|小点|强点|弱点|多点|少点|快点|慢点|\b(?:a little|a bit|some)\b")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)|[负正]?[零〇一二两三四五六七八九十百千万点]+")
_DIGITS = dict(zip("零一二三四五六七八九", range(10))) | {"〇": 0, "两": 2}


def _norm(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _hints(tool):
    return tool.get("input_schema", {}).get("x-agent")


def _configured_value(prop, value):
    return prop.get(value) if isinstance(value, str) and value in ("minimum", "maximum") else value


def validate_hints(tool) -> None:
    """Reject malformed/oversized metadata, unknown fields and invalid constants.

    Limits: 32 mentions, 64 items per collection, 512 literals overall, 128
    characters per literal. No user-supplied expressions are compiled as regex.
    A relative_tool's catalog existence/kind must be checked by the catalog.
    """
    schema = tool.get("input_schema", {})
    if "x-agent" not in schema:
        return
    hints = schema["x-agent"]
    count = 0

    def fail(message):
        raise ValueError(f"x-agent: {message}")

    def obj(value, allowed, required=()):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            fail("invalid object keys")

    def literal(value):
        nonlocal count
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            fail("literals must contain 1..128 characters")
        count += 1
        if count > 512:
            fail("too many literals")

    def sequence(value, maximum=64):
        if not isinstance(value, list) or len(value) > maximum:
            fail("invalid or oversized list")

    def phrases(value):
        sequence(value)
        for phrase in value:
            literal(phrase)

    obj(hints, {"recall", "required_mentions", "relative_tool"})
    if "relative_tool" in hints:
        literal(hints["relative_tool"])
    if "recall" in hints:
        obj(hints["recall"], {"objects", "actions", "exclude"})
        for value in hints["recall"].values():
            phrases(value)
    mentions = hints.get("required_mentions", [])
    sequence(mentions, 32)
    seen = set()
    for mention in mentions:
        obj(mention, {"field", "kind", "choices", "all", "question", "units", "values"}, {"field", "kind", "question"})
        field = mention["field"]
        literal(field)
        literal(mention["question"])
        if field in seen or field not in schema.get("properties", {}):
            fail("duplicate or unknown field")
        seen.add(field)
        prop = schema["properties"][field]
        if not isinstance(prop, dict):
            fail("field must have a concrete schema")
        validator = Draft202012Validator(prop)
        if mention["kind"] == "choice":
            obj(mention, {"field", "kind", "choices", "all", "question"}, {"choices"})
            choices = mention["choices"]
            if not isinstance(choices, dict) or not 1 <= len(choices) <= 64:
                fail("choices must be a nonempty bounded mapping")
            for value, names in choices.items():
                literal(value)
                if value not in prop.get("enum", []) or not validator.is_valid(value):
                    fail("choice is not a field enum value")
                phrases(names)
                if not names:
                    fail("choice phrases cannot be empty")
            phrases(mention.get("all", []))
        elif mention["kind"] == "number":
            obj(mention, {"field", "kind", "units", "values", "question"})
            if prop.get("type") not in ("integer", "number"):
                fail("number hint requires a numeric field")
            phrases(mention.get("units", []))
            values = mention.get("values", [])
            sequence(values)
            for entry in values:
                obj(entry, {"phrases", "value"}, {"phrases", "value"})
                phrases(entry["phrases"])
                value = _configured_value(prop, entry["value"])
                if (not entry["phrases"] or type(value) not in (int, float)
                        or (isinstance(value, float) and not math.isfinite(value))
                        or not validator.is_valid(value)):
                    fail("invalid or out-of-range numeric constant")
        else:
            fail("unknown mention kind")


def _spans(text, phrase):
    """Literal matches, with ASCII word boundaries (on must not match ventilation)."""
    phrase = _norm(phrase)
    start = 0
    while phrase and (start := text.find(phrase, start)) >= 0:
        end = start + len(phrase)
        left = start and text[start - 1].isascii() and (text[start - 1].isalnum() or text[start - 1] == "_")
        right = end < len(text) and text[end].isascii() and (text[end].isalnum() or text[end] == "_")
        if not ((phrase[0].isascii() and phrase[0].isalnum() and left)
                or (phrase[-1].isascii() and phrase[-1].isalnum() and right)):
            yield start, end
        start = end


def _has(text, phrases):
    return any(next(_spans(text, phrase), None) is not None for phrase in phrases)


def recall_matches(tool, goal: str) -> bool | None:
    """Require an object AND action when configured; exclusions veto recall."""
    hints = _hints(tool)
    if not hints or "recall" not in hints:
        return None
    if len(goal) > MAX_TEXT:
        return False
    text, recall = _norm(goal), hints["recall"]
    return (not _has(text, recall.get("exclude", []))
            and not _NEGATIVE.search(text)
            and all(not recall.get(key) or _has(text, recall[key]) for key in ("objects", "actions")))


def object_matches(tool, goal: str) -> bool:
    """Whether a simple goal names this hinted object's vocabulary, even negated.

    Used to scope enforcement; a recall miss alone is not evidence that a tool
    is unrelated. Unrecognized goals retain the caller's ordinary agent flow.
    """
    hints = _hints(tool)
    return bool(hints and len(goal) <= MAX_TEXT and _has(_norm(goal), hints.get('recall', {}).get('objects', [])))


def _relative(text):
    if _VAGUE.search(text):
        return True
    # "调高到三档" names an absolute target; "调高一档" names a delta.
    return any(not re.match(r"\s*(?:到|至|为|成|to\b)", text[m.end():]) for m in _RELATIVE.finditer(text))


def _number(token):
    if len(token) > 32:
        return None
    if re.fullmatch(r"[+-]?[0-9]+", token):
        return int(token)  # Preserve integers exactly, even beyond float precision.
    try:
        value = float(token)
        return int(value) if math.isfinite(value) and value.is_integer() else value
    except ValueError:
        pass
    sign = -1 if token.startswith("负") else 1
    token = token.lstrip("负正")
    integer, dot, decimal = token.partition("点")
    if not integer or (dot and (not decimal or any(c not in _DIGITS for c in decimal))):
        return None
    total = section = digit = 0
    previous_unit = 10000
    previous_digit = False
    for char in integer:
        if char in _DIGITS:
            if previous_digit and digit != 0:
                return None  # Do not guess whether "二三" means 2 or 3 or 23.
            digit, previous_digit = _DIGITS[char], True
        elif char == "万":
            total += (section + digit) * 10000
            section = digit = 0
            previous_unit, previous_digit = 10000, False
        elif char in "十百千":
            unit = {"十": 10, "百": 100, "千": 1000}[char]
            if unit >= previous_unit:
                return None
            section += (digit or 1) * unit
            digit, previous_unit, previous_digit = 0, unit, False
        else:
            return None
    fraction = float("0." + "".join(str(_DIGITS[c]) for c in decimal)) if dot else 0
    return sign * (total + section + digit + fraction)


def _literal_values(text, entries, ordered=False):
    matches = [(start, end, value) for value, phrases in entries for phrase in phrases
               for start, end in _spans(text, phrase)]
    if not ordered:
        matches.sort(key=lambda m: m[1] - m[0], reverse=True)
    accepted = []
    for match in matches:
        start, end, value = match
        # Ordered numeric phrases resolve overlaps, not independent conflicts.
        if not any(a <= start and end <= b and (a, b) != (start, end) for a, b, _ in accepted):
            accepted.append(match)
    return accepted


def _numeric_values(text, mention, prop, allow_bare):
    matches = _literal_values(text, [(_configured_value(prop, e["value"]), e["phrases"])
                                    for e in mention.get("values", [])], ordered=True)
    units = [_norm(u) for u in mention.get("units", [])]
    for match in _NUMBER.finditer(text):
        start, end = match.span()
        if any(a <= start and end <= b for a, b, _ in matches):
            continue
        before, after = text[:start], text[end:]
        # Never turn the trailing part of 1e3, 2.3.4, or an identifier into a
        # different target. Arabic tokens are matched in full, including signs.
        if before and before[-1].isascii() and (before[-1].isalnum() or before[-1] in "._+-"):
            continue
        has_unit = any(next(_spans(after.lstrip(), unit), None) == (0, len(unit)) for unit in units)
        percent = "%" in units and before.endswith("百分之")
        tenths = "%" in units and after.lstrip().startswith("成")
        bare = allow_bare and (not before.strip() or re.search(r"(?:设为|设到|设成|调到|调成|调整到|调整为|设置为|开到|to)\s*$", before)) and not after.strip(" 。.!！吧")
        if not (has_unit or percent or tenths or bare):
            continue
        value = _number(match.group())
        if value is not None and math.isfinite(value):
            if tenths:
                value *= 10
                if after.lstrip().startswith('成半'):
                    value += 5
            matches.append((start, end, value))
    return [value for _, _, value in matches]


def assess(tool, goal: str) -> Assessment | None:
    """Assess literal evidence only; callers retain normal schema validation."""
    hints = _hints(tool)
    if hints is None:
        return None
    mentions = hints.get("required_mentions", [])
    text = _norm(goal) if len(goal) <= MAX_TEXT else ""
    relative = bool(_relative(text))
    excluded = _has(text, hints.get("recall", {}).get("exclude", []))
    result: Assessment = {"missing": [], "constraints": {}, "relative": relative, "explicit": False,
                          "excluded": excluded, "single_call_complete": False}
    blocked = not text or bool(_NEGATIVE.search(text)) or excluded
    properties = tool["input_schema"].get("properties", {})
    numeric_count = sum(m["kind"] == "number" for m in mentions)
    multi_target = False
    numeric_phrases = [span for m in mentions if m["kind"] == "number"
                       for span in _literal_values(text, [(e["value"], e["phrases"])
                                                        for e in m.get("values", [])], ordered=True)]
    for mention in mentions:
        field = mention["field"]
        values = []
        all_targets = False
        known_delta = False
        if not blocked:
            if mention["kind"] == "choice":
                values = [v for _, _, v in _literal_values(text, mention["choices"].items())]
                all_spans = [span for phrase in mention.get("all", []) for span in _spans(text, phrase)]
                # "左前车窗全部打开": 全部 belongs to the numeric phrase, not
                # the window selector. Do not silently replace 左前 with all.
                if any(not any(a <= start and end <= b for a, b, _ in numeric_phrases)
                       for start, end in all_spans):
                    if "all" in properties[field].get("enum", []):
                        values += ["all"]
                    else:
                        values = list(properties[field]["enum"])
                        all_targets = True
                        multi_target = multi_target or len(values) > 1
            elif relative:
                deltas = _numeric_values(text, mention, properties[field], False)
                known_delta = bool(hints.get("relative_tool") and len(set(deltas)) == 1
                                   and not _VAGUE_AMOUNT.search(text))
            else:
                values = _numeric_values(text, mention, properties[field], numeric_count == 1)
                # A bare numeric user answer may follow the original goal.
                if numeric_count == 1:
                    for line in goal.splitlines()[1:]:
                        if _NUMBER.fullmatch(_norm(line)):
                            value = _number(_norm(line))
                            if value is not None:
                                values.append(value)
        values = list(dict.fromkeys(values))
        if values:
            result["constraints"][field] = values[0] if len(values) == 1 else values
        validator = Draft202012Validator(properties[field])
        if not known_delta and (not values or (len(values) != 1 and not all_targets)
                                or not all(validator.is_valid(v) for v in values)):
            result["missing"].append({"field": field, "question": mention["question"]})
    if blocked:
        question = "请明确希望执行的操作和目标，当前表述包含否定、排除条件或无法确认的内容。"
        if result["missing"]:
            result["missing"][0]["question"] = question
        else:
            result["missing"].append({"field": "intent", "question": question})
    covered = {m["field"] for m in mentions}
    required = set(tool["input_schema"].get("required", []))
    result["explicit"] = bool(not blocked and not relative and not multi_target and not result["missing"]
                              and required <= covered and recall_matches(tool, goal) is not False
                              and (mentions or hints.get("recall")))
    result["single_call_complete"] = result["explicit"]
    return result
