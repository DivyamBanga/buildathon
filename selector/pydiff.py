"""Work out what a patch to a Python file really changes.

Two compiled versions of the file are walked side by side. A code object is
"changed" when its own bytecode, constants or names differ; line numbers and
nested code objects are not counted, so a comment or a reformatting changes
nothing, and an edit inside one method leaves the module and class bodies that
ran at import untouched.

For a changed `def`, the old and new source of just that function are parsed
and compared statement by statement, to name the old lines whose execution is
needed for a test to notice the change.
"""

from __future__ import annotations

import ast
import dis
import types

CODE = types.CodeType
CO_NEWLOCALS = 0x0002
_STORE_NAME = dis.opmap["STORE_NAME"]
_LOAD_CONST = dis.opmap["LOAD_CONST"]
_EXTENDED_ARG = dis.opmap["EXTENDED_ARG"]


def _canon(value):
    if isinstance(value, CODE):
        return ("<code>", value.co_name)
    if isinstance(value, tuple):
        return ("tuple",) + tuple(_canon(item) for item in value)
    if isinstance(value, frozenset):
        return ("frozenset", frozenset(_canon(item) for item in value))
    return (type(value).__name__, repr(value))


def _firstlineno_const(code: CODE) -> int:
    """Index of the constant a class body stores as `__firstlineno__`, or -1.

    Python 3.13 records each class's first line in its body, so moving a class
    down a line changes a constant without changing what the class does.
    """
    names = code.co_names
    if "__firstlineno__" not in names:
        return -1
    target = names.index("__firstlineno__")
    raw = code.co_code
    extended = 0
    previous = (-1, 0)
    for offset in range(0, min(len(raw), 96), 2):
        opcode = raw[offset]
        argument = raw[offset + 1] | extended
        if opcode == _EXTENDED_ARG:
            extended = argument << 8
            continue
        extended = 0
        if opcode == _STORE_NAME and argument == target:
            return previous[1] if previous[0] == _LOAD_CONST else -1
        previous = (opcode, argument)
    return -1


def own_key(code: CODE):
    """Everything that decides what this code object does, apart from its children."""
    masked = -1 if code.co_flags & CO_NEWLOCALS else _firstlineno_const(code)
    return (
        code.co_code,
        tuple(
            "<firstlineno>" if index == masked else _canon(const)
            for index, const in enumerate(code.co_consts)
        ),
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_flags,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_exceptiontable,
        code.co_name,
        code.co_qualname,
    )


def is_function_like(code: CODE) -> bool:
    """def, lambda, generator expression: code that runs only when called."""
    return bool(code.co_flags & CO_NEWLOCALS)


class CodeDiff:
    __slots__ = ("pairs", "changed", "isomorphic")

    def __init__(self) -> None:
        # (old, new) for every code object matched by position in the tree.
        self.pairs: list[tuple[CODE, CODE]] = []
        # (old, new or None) where the code object's own behaviour differs.
        self.changed: list[tuple[CODE, CODE | None]] = []
        self.isomorphic = True


def diff_code(old: CODE, new: CODE) -> CodeDiff:
    result = CodeDiff()
    stack = [(old, new)]
    while stack:
        old_code, new_code = stack.pop()
        result.pairs.append((old_code, new_code))
        old_children = [const for const in old_code.co_consts if isinstance(const, CODE)]
        new_children = [const for const in new_code.co_consts if isinstance(const, CODE)]
        same_shape = len(old_children) == len(new_children) and all(
            a.co_name == b.co_name and is_function_like(a) == is_function_like(b)
            for a, b in zip(old_children, new_children)
        )
        if own_key(old_code) != own_key(new_code):
            result.changed.append((old_code, new_code))
        elif not same_shape:
            result.changed.append((old_code, new_code))
        if same_shape:
            stack.extend(zip(old_children, new_children))
        else:
            # Children cannot be paired, so every old child counts as changed.
            result.isomorphic = False
            pending = list(old_children)
            while pending:
                child = pending.pop()
                result.changed.append((child, None))
                pending.extend(const for const in child.co_consts if isinstance(const, CODE))
    return result


_NO_LINE_EVENT = (ast.Pass, ast.Global, ast.Nonlocal)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _header_lines(node, first_body_line: int | None) -> range:
    start = node.lineno
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    stop = start + 1
    if first_body_line is not None and first_body_line > start:
        stop = first_body_line
    return range(start, stop)


class _Structural(Exception):
    """The two versions do not have the same statements in the same places."""


def _compare_blocks(old_body: list, new_body: list, lines: set[int]) -> None:
    if len(old_body) != len(new_body):
        raise _Structural
    for old, new in zip(old_body, new_body):
        _compare_statement(old, new, lines)


def _compare_statement(old, new, lines: set[int]) -> None:
    if type(old) is not type(new):
        raise _Structural
    if ast.dump(old) == ast.dump(new):
        return
    if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        # The body is another code object and is judged on its own. Here only the
        # statement that creates it matters: decorators, defaults, bases.
        old_head = [ast.dump(n) for n in old.decorator_list]
        new_head = [ast.dump(n) for n in new.decorator_list]
        if isinstance(old, ast.ClassDef):
            old_head += [ast.dump(n) for n in old.bases + old.keywords]
            new_head += [ast.dump(n) for n in new.bases + new.keywords]
        else:
            old_head += [ast.dump(old.args), ast.dump(old.returns) if old.returns else ""]
            new_head += [ast.dump(new.args), ast.dump(new.returns) if new.returns else ""]
        if old.name != new.name or old_head != new_head:
            lines.update(_header_lines(old, old.body[0].lineno if old.body else None))
        return
    block_fields = []
    header_differs = False
    for field in old._fields:
        old_value = getattr(old, field, None)
        new_value = getattr(new, field, None)
        if isinstance(old_value, list) and old_value and isinstance(old_value[0], ast.stmt):
            block_fields.append((old_value, new_value))
        elif isinstance(old_value, list) and old_value and isinstance(
            old_value[0], (ast.ExceptHandler, ast.match_case)
        ):
            if not isinstance(new_value, list) or len(old_value) != len(new_value):
                raise _Structural
            for old_item, new_item in zip(old_value, new_value):
                if isinstance(old_item, ast.ExceptHandler):
                    old_head = (ast.dump(old_item.type) if old_item.type else "", old_item.name)
                    new_head = (ast.dump(new_item.type) if new_item.type else "", new_item.name)
                    if old_head != new_head:
                        lines.update(_header_lines(old_item, old_item.body[0].lineno))
                else:
                    old_head = (ast.dump(old_item.pattern), ast.dump(old_item.guard) if old_item.guard else "")
                    new_head = (ast.dump(new_item.pattern), ast.dump(new_item.guard) if new_item.guard else "")
                    if old_head != new_head:
                        lines.update(range(old_item.pattern.lineno, old_item.body[0].lineno + 1))
                block_fields.append((old_item.body, new_item.body))
        else:
            if isinstance(new_value, list) and new_value and isinstance(new_value[0], ast.stmt):
                # An empty block became non-empty (an `else:` was added).
                raise _Structural
            old_dump = (
                [ast.dump(n) if isinstance(n, ast.AST) else repr(n) for n in old_value]
                if isinstance(old_value, list)
                else (ast.dump(old_value) if isinstance(old_value, ast.AST) else repr(old_value))
            )
            new_dump = (
                [ast.dump(n) if isinstance(n, ast.AST) else repr(n) for n in new_value]
                if isinstance(new_value, list)
                else (ast.dump(new_value) if isinstance(new_value, ast.AST) else repr(new_value))
            )
            if old_dump != new_dump:
                header_differs = True
    if not block_fields:
        # A simple statement: every line it spans.
        if isinstance(old, _NO_LINE_EVENT) or _is_docstring(old) or _is_docstring(new):
            # No bytecode of its own, so its execution cannot be observed.
            raise _Structural
        lines.update(range(old.lineno, (old.end_lineno or old.lineno) + 1))
        return
    if header_differs:
        first_body = None
        for old_block, _ in block_fields:
            if old_block:
                first_body = old_block[0].lineno
                break
        lines.update(_header_lines(old, first_body))
    for old_block, new_block in block_fields:
        if not isinstance(new_block, list):
            raise _Structural
        _compare_blocks(old_block, new_block, lines)


def _parse_function(source_lines: list[str], first: int, last: int):
    """Parse lines first..last (1-based, inclusive) holding one def.

    Returns (node, the fragment line number that `first` became), or None.
    """
    fragment = "".join(source_lines[first - 1:last])
    # A method is indented, so it is parsed as the body of a synthetic `if`.
    wrapped = fragment[:1] in (" ", "\t")
    try:
        tree = ast.parse("if 1:\n" + fragment if wrapped else fragment)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    body = tree.body[0].body if wrapped else tree.body  # type: ignore[attr-defined]
    if len(body) != 1 or not isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return body[0], (2 if wrapped else 1)


def changed_lines_in_function(
    old_lines: list[str],
    new_lines: list[str],
    old_span: tuple[int, int],
    new_span: tuple[int, int],
) -> set[int] | None:
    """Old line numbers whose execution a test needs in order to see the change.

    None means the function's statements were added, removed or moved, so any
    test that enters the function may see it.
    """
    old_parsed = _parse_function(old_lines, old_span[0], old_span[1])
    new_parsed = _parse_function(new_lines, new_span[0], new_span[1])
    if old_parsed is None or new_parsed is None:
        return None
    old_node, first_line = old_parsed
    new_node = new_parsed[0]
    if ast.dump(old_node.args) != ast.dump(new_node.args):
        return None
    lines: set[int] = set()
    try:
        _compare_blocks(old_node.body, new_node.body, lines)
    except (_Structural, RecursionError):
        return None
    if not lines:
        return None
    # Fragment line `first_line` is line old_span[0] of the file.
    shift = old_span[0] - first_line
    return {line + shift for line in lines}
