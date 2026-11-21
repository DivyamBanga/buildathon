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
CO_FUTURE_ANNOTATIONS = 0x1000000
# Names a module or class body stores that nothing in sqlglot or its tests reads.
_INERT_NAMES = ("__firstlineno__", "__doc__", "__static_attributes__")
_CACHE = dis.opmap.get("CACHE", 0)
_STORE_NAME = dis.opmap["STORE_NAME"]
_LOAD_CONST = dis.opmap["LOAD_CONST"]
_EXTENDED_ARG = dis.opmap["EXTENDED_ARG"]
_MAKE_FUNCTION = dis.opmap["MAKE_FUNCTION"]
_SET_FUNCTION_ATTRIBUTE = dis.opmap.get("SET_FUNCTION_ATTRIBUTE", -1)
_ANNOTATIONS_ATTRIBUTE = 4


def _canon(value):
    if isinstance(value, CODE):
        return ("<code>", value.co_name)
    if isinstance(value, tuple):
        return ("tuple",) + tuple(_canon(item) for item in value)
    if isinstance(value, frozenset):
        return ("frozenset", frozenset(_canon(item) for item in value))
    return (type(value).__name__, repr(value))


CO_VARARGS = 0x0004
CO_VARKEYWORDS = 0x0008
_HASCONST = frozenset(dis.hasconst)
_HASNAME = frozenset(dis.hasname)
_HASJUMP = frozenset(getattr(dis, "hasjump", ())) | frozenset(dis.hasjrel) | frozenset(dis.hasjabs)


def _shape(code: CODE):
    """What must match exactly, whatever the instructions are."""
    flags = code.co_flags
    varnames = code.co_varnames
    if flags & CO_NEWLOCALS:
        # Parameters keep their names (callers may pass them by keyword); the other
        # locals are slots in the bytecode and their names are never looked at.
        parameters = (
            code.co_argcount + code.co_kwonlyargcount + bool(flags & CO_VARARGS) + bool(flags & CO_VARKEYWORDS)
        )
        varnames = (varnames[:parameters], len(varnames))
    return (
        varnames,
        code.co_freevars,
        code.co_cellvars,
        flags,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_name,
        code.co_qualname,
    )


def _raw_key(code: CODE):
    return (
        code.co_code,
        tuple(_canon(const) for const in code.co_consts),
        code.co_names,
        code.co_varnames,
        code.co_exceptiontable,
    ) + _shape(code)


def _normal_form(code: CODE):
    """The code object as its instructions, with every table index resolved and
    the constants no test can observe masked.

    Two compilations of the same statements can number their constants and
    names differently (the tables are deduplicated, so one edited constant
    renumbers the rest) and can differ in instruction offsets. Here constants
    appear by value, names by name, jumps and exception ranges by instruction
    position. Masked, because nothing in sqlglot or its tests reads them:

    - what a module or class body stores as `__doc__`, `__firstlineno__` (the
      line a class starts on, new in Python 3.13) or `__static_attributes__`
      (the `self.x` names its methods assign, also new in 3.13);
    - the annotations handed to a function being defined, in a module under
      `from __future__ import annotations` (strings that are never evaluated);
    - the docstring of a function, which no instruction loads.
    """
    consts = code.co_consts
    rows = []
    position = {}
    waiting = []
    for instruction in dis.get_instructions(code):
        if instruction.opname == "EXTENDED_ARG":
            waiting.append(instruction.offset)
            continue
        for offset in waiting:
            position[offset] = len(rows)
        waiting = []
        position[instruction.offset] = len(rows)
        rows.append(instruction)
    inert = set()
    module_like = not code.co_flags & CO_NEWLOCALS
    future = bool(code.co_flags & CO_FUTURE_ANNOTATIONS)
    for index, instruction in enumerate(rows):
        if instruction.opcode != _LOAD_CONST:
            continue
        following = rows[index + 1] if index + 1 < len(rows) else None
        if module_like and following is not None and following.opcode == _STORE_NAME and following.argval in _INERT_NAMES:
            inert.add(index)
        elif (
            future
            and index + 3 < len(rows)
            and following.opcode == _LOAD_CONST
            and isinstance(consts[following.arg], CODE)
            and rows[index + 2].opcode == _MAKE_FUNCTION
            and rows[index + 3].opcode == _SET_FUNCTION_ATTRIBUTE
            and rows[index + 3].arg == _ANNOTATIONS_ATTRIBUTE
            and isinstance(consts[instruction.arg], tuple)
            and all(isinstance(item, str) for item in consts[instruction.arg])
        ):
            inert.add(index)
    resolved = []
    for index, instruction in enumerate(rows):
        opcode = instruction.opcode
        if opcode in _HASCONST:
            value = "<inert>" if index in inert else _canon(consts[instruction.arg])
        elif opcode in _HASJUMP:
            value = ("to", position.get(instruction.argval, -1))
        elif opcode in _HASNAME:
            value = (instruction.argval, instruction.argrepr)
        else:
            value = instruction.arg
        resolved.append((instruction.opname, value))
    handlers = tuple(
        (position.get(entry.start, -1), position.get(entry.end, len(rows)), position.get(entry.target, -1),
         entry.depth, entry.lasti)
        for entry in dis._parse_exception_table(code)
    )
    return tuple(resolved), handlers


def same_behaviour(old: CODE, new: CODE) -> bool:
    """True when the two code objects differ at most in line numbers, in their
    children and in what no test can observe."""
    if _raw_key(old) == _raw_key(new):
        return True
    if _shape(old) != _shape(new):
        return False
    try:
        return _normal_form(old) == _normal_form(new)
    except Exception:
        return False


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
        if not same_behaviour(old_code, new_code):
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
        if _is_docstring(old) and _is_docstring(new):
            # An edited docstring (or a bare string that does nothing).
            return
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


# Module-level names pytest or unittest treat as fixtures for every test of the file.
_MODULE_HOOKS = ("setup", "teardown", "pytest_", "pytestmark")


def _bound_names(body: list) -> set[str]:
    names: set[str] = set()
    for statement in body:
        for node in ast.walk(statement) if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [statement]:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.alias):
                names.add((node.asname or node.name).split(".")[0])
    return names


def _plain_function(node) -> bool:
    """A def that cannot fail or act when it is merely defined."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.decorator_list:
        return False
    arguments = node.args
    defaults = list(arguments.defaults) + [d for d in arguments.kw_defaults if d is not None]
    if any(not isinstance(default, ast.Constant) for default in defaults):
        return False
    annotated = [a.annotation for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
    return node.returns is None and not any(annotated)


def _only_new_definitions(old_body: list, new_body: list, known: set, prefix: str) -> bool:
    import unittest

    old_names = _bound_names(old_body)
    position = 0
    for statement in new_body:
        old = old_body[position] if position < len(old_body) else None
        if old is not None and ast.dump(old) == ast.dump(statement):
            position += 1
            continue
        if (
            isinstance(statement, ast.ClassDef)
            and isinstance(old, ast.ClassDef)
            and old.name == statement.name
            and [ast.dump(n) for n in old.bases + old.keywords + old.decorator_list]
            == [ast.dump(n) for n in statement.bases + statement.keywords + statement.decorator_list]
        ):
            if not _only_new_definitions(old.body, statement.body, known, prefix + statement.name + "::"):
                return False
            position += 1
            continue
        name = getattr(statement, "name", "")
        if not name or name in old_names or name.startswith("__") or prefix + name in known:
            return False
        if prefix:
            # A method: only a new test, never something existing tests could call.
            if not name.startswith("test") or hasattr(unittest.TestCase, name) or not _plain_function(statement):
                return False
        elif isinstance(statement, ast.ClassDef):
            if statement.decorator_list or statement.keywords:
                return False
            for member in statement.body:
                simple = isinstance(member, ast.Pass) or _is_docstring(member) or _plain_function(member)
                if isinstance(member, ast.Assign) and isinstance(member.value, ast.Constant):
                    simple = all(isinstance(target, ast.Name) for target in member.targets)
                if not simple or getattr(member, "name", "x").startswith("__"):
                    return False
        else:
            if not _plain_function(statement) or name.lower().startswith(_MODULE_HOOKS):
                return False
    return position == len(old_body)


def only_new_definitions(old_text: str, new_text: str, known: set) -> bool:
    """True when a test file only gained whole new tests, test classes or helper functions.

    Everything that was there is unchanged, and every addition has a name that
    was not bound before and that nothing existing can reach: a new `test...`
    method (not one that would replace an inherited test, `known` holds those),
    a new class of plain methods, or a new module-level function that is not a
    pytest or unittest hook. Defining them cannot fail and no existing test
    behaves differently, so no test needs to run.
    """
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return _only_new_definitions(old_tree.body, new_tree.body, known, "")
