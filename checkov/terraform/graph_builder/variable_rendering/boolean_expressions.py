"""Bounded Terraform logical expression handling that preserves unresolved inputs."""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Callable

_UNKNOWN = object()
_REFERENCE = re.compile(r"\b(?:var|local|module|data|each|count)\.")


def _positions(text: str) -> list[tuple[int, str]] | None:
    stack: list[str] = []
    quote = ""
    escaped = False
    positions = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                return None
        elif not stack:
            positions.append((index, char))
    return None if quote or stack else positions


def _unwrap(text: str) -> str:
    while text.startswith("(") and text.endswith(")"):
        inner = text[1:-1]
        if _positions(inner) is None:
            break
        text = inner.strip()
    return text


def _operand(text: str, evaluate: Callable[[Any], Any], depth: int = 0) -> Any:
    if depth > 64:
        return _UNKNOWN
    text = _unwrap(text.strip())
    if not text:
        return _UNKNOWN
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text[0] in "\"'" and text[-1:] == text[:1]:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            pass
    handled, value = evaluate_boolean_expression(text, evaluate, depth + 1)
    if handled:
        return _UNKNOWN if isinstance(value, str) and value == text else value
    if _REFERENCE.search(text):
        return _UNKNOWN
    call = re.fullmatch(r"(jsondecode|alltrue)\((.*)\)", text, flags=re.S)
    if call:
        try:
            if call[1] == 'jsondecode':
                return json.loads(ast.literal_eval(call[2]))
            try:
                items = json.loads(call[2])
            except ValueError:
                items = ast.literal_eval(call[2])
            if isinstance(items, list) and all(type(item) is bool or item is None for item in items):
                return all(item is True for item in items)
        except (ValueError, TypeError, SyntaxError):
            return _UNKNOWN
    value = evaluate(text)
    return _UNKNOWN if value is None or value == text else value


def evaluate_boolean_expression(text: str, evaluate: Callable[[Any], Any], depth: int = 0) -> tuple[bool, Any]:
    """Return handled/value; unresolved expressions retain their original text."""
    if not isinstance(text, str) or len(text) > 65536:
        return False, text
    if depth > 64:
        return True, text
    original = text
    text = _unwrap(text.strip())
    positions = _positions(text)
    if positions is None:
        return False, original
    available = dict(positions)
    question = next((i for i, c in positions if c == "?"), None)
    if question is not None:
        # Leave legacy standalone comparison/coercion behavior unchanged;
        # this patch targets Boolean operators and unresolved references.
        condition_text = text[:question]
        if re.search(r"==|!=|>=|<=|>|<", condition_text) and not _REFERENCE.search(condition_text) and not any(
            operator in condition_text for operator in ('||', '&&')
        ) and not condition_text.lstrip().startswith('!'):
            return False, original
        nested = 0
        colon = None
        for index, char in positions:
            if index <= question:
                continue
            if char == "?":
                nested += 1
            elif char == ":":
                if nested:
                    nested -= 1
                else:
                    colon = index
                    break
        if colon is None:
            return True, original
        condition = _operand(text[:question], evaluate, depth + 1)
        if type(condition) is bool:
            branch = text[question + 1:colon] if condition else text[colon + 1:]
            value = _operand(branch, evaluate, depth + 1)
            if value is _UNKNOWN and re.fullmatch(r"[A-Za-z_][\w-]*", branch.strip()):
                value = branch.strip()
            return True, original if value is _UNKNOWN else value
        return True, original
    for operator in ("||", "&&"):
        splits = [i for i, c in positions if c == operator[0] and available.get(i + 1) == operator[1]
                  and available.get(i - 1) != operator[0]]
        if splits:
            parts = []
            start = 0
            for index in splits:
                parts.append(text[start:index])
                start = index + 2
            parts.append(text[start:])
            values = [_operand(part, evaluate, depth + 1) for part in parts]
            if any(value is not _UNKNOWN and type(value) is not bool for value in values):
                return True, original
            if operator == "||" and any(value is True for value in values):
                return True, True
            if operator == "&&" and any(value is False for value in values):
                return True, False
            if any(value is _UNKNOWN for value in values):
                return True, original
            return True, any(values) if operator == "||" else all(values)
    if text.startswith("!") and not text.startswith("!="):
        value = _operand(text[1:], evaluate, depth + 1)
        return True, not value if type(value) is bool else original
    return False, original


def reference_words(text: str) -> list[str]:
    """Separate operator punctuation from references, leaving string text intact."""
    result: list[str] = []
    quote = ""
    escaped = False
    for char in text:
        if quote:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            result.append(char)
        elif char in "()!&|?=<>+*":
            result.append(" ")
        else:
            result.append(char)
    return "".join(result).split()


def evaluate_boolean_container(value: Any, evaluate: Callable[[Any], Any]) -> tuple[bool, Any]:
    """Evaluate logical expressions in containers without coercing literal strings.

    The handled flag also covers unresolved logical expressions: serializing those
    containers into a quoted string would let the legacy comparison parser truncate
    the expression before its remaining references have been resolved.
    """
    if isinstance(value, (list, dict)):
        items = value.items() if isinstance(value, dict) else enumerate(value)
        result = {} if isinstance(value, dict) else []
        handled = False
        for key, item in items:
            item_handled, item_value = evaluate_boolean_container(item, evaluate)
            handled = handled or item_handled
            if isinstance(result, dict):
                result[key] = item_value
            else:
                result.append(item_value)
        return handled, result
    if isinstance(value, str):
        expression = value[2:-1] if value.startswith('${') and value.endswith('}') else value
        handled, result = evaluate_boolean_expression(expression, evaluate)
        return handled, result if handled and result != expression else value
    return False, value
