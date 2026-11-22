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
import builtins
import functools
import re
import dis
import types
from collections import Counter

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


# Names through which a function can read its own local names (or a caller's).
_INTROSPECTION = frozenset(
    ("locals", "vars", "dir", "eval", "exec", "_getframe", "currentframe", "f_locals", "stack", "getframeinfo")
)


def _shape(code: CODE):
    """What must match exactly, whatever the instructions are."""
    flags = code.co_flags
    varnames = code.co_varnames
    if flags & CO_NEWLOCALS:
        # Parameters keep their names (callers may pass them by keyword); the other
        # locals are slots in the bytecode and their names are never looked at,
        # unless the function can ask for them (`locals()`, a frame): then which
        # names there are matters (rule N), their order still does not.
        parameters = (
            code.co_argcount + code.co_kwonlyargcount + bool(flags & CO_VARARGS) + bool(flags & CO_VARKEYWORDS)
        )
        others = frozenset(varnames[parameters:]) if _INTROSPECTION.intersection(code.co_names) else None
        varnames = (varnames[:parameters], others, len(varnames))
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


# Flags that say how a function is called and what calling it gives back: the
# parameter kinds, generator, coroutine and async generator.
_CALL_FLAGS = 0x0001 | 0x0002 | CO_VARARGS | CO_VARKEYWORDS | 0x0020 | 0x0080 | 0x0100 | 0x0200


def _parameters(code: CODE):
    count = code.co_argcount + code.co_kwonlyargcount
    count += bool(code.co_flags & CO_VARARGS) + bool(code.co_flags & CO_VARKEYWORDS)
    return code.co_varnames[:count]


def signature_differs(old: CODE, new: CODE) -> bool:
    """True when what is fixed about a function when `def` runs differs.

    A decorator, a registry or `inspect.signature` reads the parameters (their
    names, number, order and kind), the free variables, the name and what kind
    of function it is at the time the function is defined, and keeps what it
    found. Putting new bytecode in the old function object (`__code__`) does not
    redo any of that, so such a change cannot be checked by swapping code.
    """
    return (
        old.co_argcount != new.co_argcount
        or old.co_posonlyargcount != new.co_posonlyargcount
        or old.co_kwonlyargcount != new.co_kwonlyargcount
        or _parameters(old) != _parameters(new)
        or old.co_flags & _CALL_FLAGS != new.co_flags & _CALL_FLAGS
        or old.co_freevars != new.co_freevars
        or old.co_name != new.co_name
        or old.co_qualname != new.co_qualname
    )


def last_line(code) -> int:
    """The largest line number any instruction of a code object has, nested code objects included."""
    best = code.co_firstlineno
    for _start, _end, line in code.co_lines():
        if line is not None and line > best:
            best = line
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            best = max(best, last_line(const))
    return best


def bindings_differ(old: CODE, new: CODE) -> bool:
    """True when the function binds other names than before (rule N).

    Which names are local, cell or free decides what every other line of the
    function means: `exp` stored once makes `exp.Expr` on a line nobody edited
    raise UnboundLocalError, and `locals()` lists the names. The statements
    that differ are then not the only ones a test must run. Compared as sets:
    the order of the other locals is a slot number nobody reads.
    """
    return (
        set(old.co_varnames) != set(new.co_varnames)
        or set(old.co_cellvars) != set(new.co_cellvars)
        or set(old.co_freevars) != set(new.co_freevars)
    )


_PAIRED = {
    "LOAD_FAST_LOAD_FAST": ("LOAD_FAST", "LOAD_FAST"),
    "STORE_FAST_STORE_FAST": ("STORE_FAST", "STORE_FAST"),
    "STORE_FAST_LOAD_FAST": ("STORE_FAST", "LOAD_FAST"),
}
_PLAIN_OPNAME = {"LOAD_FAST_CHECK": "LOAD_FAST", "LOAD_FAST_BORROW": "LOAD_FAST"}
_SKIPPED = frozenset(("NOP", "EXTENDED_ARG", "CACHE"))


def _rows_by_line(code: CODE) -> dict[int, list]:
    """The instructions of one code object, by source line, resolved: constants
    by value, names and slots by name, jumps by opcode only. The compiler's
    choices that carry no meaning (a fused pair of loads, a load that checks
    for an unbound variable, a NOP that marks a line, a copied exit block) are undone."""
    rows: dict[int, list] = {}
    for instruction in dis.get_instructions(code):
        name = instruction.opname
        if name in _SKIPPED:
            continue
        positions = instruction.positions
        line = positions.lineno if positions is not None and positions.lineno is not None else instruction.line_number
        if line is None:
            continue
        opcode = instruction.opcode
        if name in _PAIRED:
            first, second = instruction.argval
            rows.setdefault(line, []).extend(
                [(_PAIRED[name][0], first), (_PAIRED[name][1], second)]
            )
            continue
        if opcode in _HASCONST:
            value = _canon(instruction.argval)
        elif opcode in _HASJUMP:
            value = None
        elif opcode in _HASNAME or opcode in dis.haslocal or opcode in dis.hasfree:
            value = (instruction.argval, instruction.argrepr)
        else:
            value = instruction.arg
        rows.setdefault(line, []).append((_PLAIN_OPNAME.get(name, name), value))
    return {line: _without_copies(found) for line, found in rows.items()}


def _without_copies(rows: list) -> list:
    """The compiler copies a small exit block (a `return`) to every path that
    ends in it: a row list that is one block repeated is that block."""
    size = len(rows)
    for period in range(1, size // 2 + 1):
        if size % period == 0 and rows == rows[:period] * (size // period):
            return rows[:period]
    return rows


def changed_instruction_lines(old: CODE, new: CODE, new_line) -> set[int]:
    """Old lines whose instructions differ from those of the same line in the new code.

    `new_line` maps an old line to its place in the new file (None when the
    patch edited it). Catches what the statement comparison cannot: a line
    whose text is unchanged but whose names now resolve to something else (a
    global that became a local, `global` or `nonlocal` added). A line the
    patch edited and that has no counterpart counts as changed.
    """
    old_rows = _rows_by_line(old)
    new_rows = _rows_by_line(new)
    lines = set()
    for line, rows in old_rows.items():
        target = new_line(line)
        if target is None or new_rows.get(target) != rows:
            lines.add(line)
    return lines


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


_ANONYMOUS = ("<lambda>", "<genexpr>", "<listcomp>", "<dictcomp>", "<setcomp>")


def _children(code: CODE) -> list:
    return [const for const in code.co_consts if isinstance(const, CODE)]


def _deep_key(code: CODE):
    """Everything that decides what a code object and all code inside it do, or
    None when that cannot be worked out. Two code objects with the same key can
    be exchanged for each other."""
    try:
        body = _normal_form(code)
    except Exception:
        return None
    children = _children(code)
    kids = []
    for child in children:
        key = _deep_key(child)
        if key is None:
            return None
        kids.append(key)
    # Which child each instruction that loads one refers to.
    slot = {id(child): index for index, child in enumerate(children)}
    loads = tuple(
        slot[id(code.co_consts[instruction.arg])]
        for instruction in dis.get_instructions(code)
        if instruction.opcode == _LOAD_CONST and isinstance(code.co_consts[instruction.arg], CODE)
    )
    return (_shape(code), body, tuple(kids), loads)


def _pair_children(old_children: list, new_children: list):
    """Pair the code objects of two scopes whose children do not line up by position.

    Returns (pairs, old children without a partner). A child pairs only within
    its own scope, by name and kind (`def` or class body): a function that moved
    to another scope or was renamed is a removal and an addition. A new child
    with no partner needs no entry, the scope that creates it is reported
    changed. Rules:

    - A name defined once on each side pairs with itself, wherever it stands.
    - A name defined several times (overloads, a property's getter and setter,
      conditional definitions) pairs the k-th with the k-th only when it is
      defined as often as before; otherwise every old definition is changed.
    - Anonymous code (lambdas, generator expressions) first pairs with an
      identical one anywhere in the scope (the same instructions, names and
      code inside), in order; what is left pairs by position when as many are
      left on each side, else the old ones are changed.
    """
    groups: dict = {}
    for side, children in ((0, old_children), (1, new_children)):
        for child in children:
            groups.setdefault((child.co_name, is_function_like(child)), ([], []))[side].append(child)
    pairs: list = []
    lost: list = []
    for (name, _), (olds, news) in groups.items():
        if name in _ANONYMOUS:
            waiting: dict = {}
            for child in news:
                key = _deep_key(child)
                if key is not None:
                    waiting.setdefault(key, []).append(child)
            taken = set()
            left_old = []
            for child in olds:
                key = _deep_key(child)
                partners = waiting.get(key) if key is not None else None
                if partners:
                    partner = partners.pop(0)
                    taken.add(id(partner))
                    pairs.append((child, partner))
                else:
                    left_old.append(child)
            left_new = [child for child in news if id(child) not in taken]
            if len(left_old) == len(left_new):
                pairs.extend(zip(left_old, left_new))
            else:
                lost.extend(left_old)
        elif len(olds) == len(news):
            pairs.extend(zip(olds, news))
        else:
            lost.extend(olds)
    return pairs, lost


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
            # Definitions were added, removed, moved or renamed here. Children
            # are paired by name (see _pair_children); an old child with no
            # partner is changed, together with everything inside it. When the
            # code itself is unchanged yet its children do not line up, nothing
            # is trusted.
            result.isomorphic = False
            if same_behaviour(old_code, new_code):
                matched, lost = [], list(old_children)
            else:
                matched, lost = _pair_children(old_children, new_children)
            stack.extend(matched)
            pending = lost
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


def lines_to_run(old, new, old_lines, new_lines, old_span, new_span, new_line) -> set[int] | None:
    """Old lines a test must execute to notice the change to a function; None
    when any test that runs the function may. The statements that differ, plus
    (rule N) the whole function when it binds other names, plus the lines whose
    instructions differ although their text does not."""
    lines = changed_lines_in_function(old_lines, new_lines, old_span, new_span)
    if lines is None:
        return None
    if bindings_differ(old, new):
        return None
    return lines | changed_instruction_lines(old, new, new_line)


_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")

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


def _plain_function(node, method: bool = False) -> bool:
    """A def that cannot fail or act when it is merely defined.

    `method` is true for a def directly in a class body: stores to `self` count as its own there.
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.decorator_list:
        return False
    arguments = node.args
    defaults = list(arguments.defaults) + [d for d in arguments.kw_defaults if d is not None]
    if any(not isinstance(default, ast.Constant) for default in defaults):
        return False
    annotated = [a.annotation for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
    return node.returns is None and not any(annotated) and not any(_global_effects(node, "", _own_self(node) if method else False))


# calls that change something other than the callee's own locals, or run arbitrary text
_MUTATORS = frozenset((
    "setattr", "delattr", "__setattr__", "__delattr__", "__setitem__", "__delitem__", "exec", "eval", "compile",
    "__import__", "import_module", "reload", "update", "pop", "popitem", "clear", "setdefault", "append", "extend",
    "insert", "remove", "add", "discard", "sort", "reverse", "start", "patch", "register", "setenv", "putenv",
    "unsetenv", "chdir", "chroot", "filterwarnings", "simplefilter", "setrecursionlimit", "settrace", "setprofile",
    "disable", "_exit", "exit", "signal", "set", "setLevel", "addHandler", "addFilter", "monkeypatch",
    "__init_subclass__", "setUpClass", "tearDownClass", "addCleanup", "enterContext",
    # process-wide settings that no test of the pinned suite calls (so the names cost nothing there)
    "setlocale", "setcontext", "setrlimit", "alarm", "setitimer", "set_start_method", "set_int_max_str_digits",
    "umask", "tzset", "basicConfig", "captureWarnings",
))
_ESCAPES = frozenset(("__dict__", "__class__", "__globals__", "__builtins__", "__code__", "__bases__", "__mro__", "__subclasses__"))


def _rebinds_self(func) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == "self" and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return True
    # a second parameter named self anywhere inside (a nested def or a lambda)
    return sum(1 for node in ast.walk(func) if isinstance(node, ast.arg) and node.arg == "self") > 1


def _own_self(node) -> bool:
    return bool(node.args.args) and node.args.args[0].arg == "self" and not _rebinds_self(node)


def _global_effects(tree, scope: str = "", method_ok: bool = False, statement=None):
    """Signatures of the statements under `tree` that can act outside the local scope.

    A function that only stores to its own locals and to attributes of `self`
    leaves every other test as it was. These are the ones that may not:
    `global` or `nonlocal`; an attribute or subscript store (or `del`) on
    anything but `self`, and on `self` unless it sits in a def directly in a
    class body whose first parameter is `self` and which never rebinds it, nor
    takes another parameter of that name; a call named like something that
    changes shared state or runs text (`setattr`, `update`, `clear`, `exec`,
    `reload`, `patch`, ...; a list of names, not a proof); and a read of
    `__dict__`, `__class__`, `__globals__` and the like.
    """
    found = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            own = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(tree, ast.ClassDef) and _own_self(node)
            found.extend(_global_effects(node, scope + "." + node.name, own))
            continue
        here = node if isinstance(node, ast.stmt) else statement
        flagged = False
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            flagged = True
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            flagged = not (
                method_ok and isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
            )
        elif isinstance(node, ast.Attribute) and node.attr in _ESCAPES:
            flagged = True
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
            flagged = name in _MUTATORS
        if flagged:
            # A store is identified with the statement it sits in: the same target
            # given another value (a test restoring a class attribute to a new
            # value) is another effect.
            stored = isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del))
            found.append((scope, ast.dump(here if stored and here is not None else node)))
        found.extend(_global_effects(node, scope, method_ok, here))
    return found


@functools.lru_cache(maxsize=2)
def _parse(text: str):
    """The tree of a source text. The rules for test files each need the trees of the same two
    texts and only read them; `analyze` drops them when it is done with a commit."""
    return ast.parse(text)


def forget_trees() -> None:
    _parse.cache_clear()


def new_global_effects(old_text: str, new_text: str) -> bool:
    """True when the new text has a statement that stores outside the local scope
    (see `_global_effects`) which the old text did not have in that place.

    It matters in any test file, new or edited: pytest runs a new test, and
    imports every test module, in the same process as every other test, so a
    store into a class, a module or a registry reaches tests of other files.
    """
    try:
        new_tree = _parse(new_text)
        old_tree = _parse(old_text) if old_text else None
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    old_found = Counter(_global_effects(old_tree)) if old_tree is not None else Counter()
    # A module-level `NAME = re.compile("...")` is named like the builtin that runs
    # text, but it only builds a pattern (see _harmless_constant).
    regex = _binds_re_module(new_tree)
    kept = [s for s in new_tree.body if not (isinstance(s, ast.Assign) and _harmless_constant(s, regex))]
    new_found = Counter(_global_effects(ast.Module(body=kept, type_ignores=[])))
    return any(count > old_found[signature] for signature, count in new_found.items())


# Bases that cannot put a class of a test file under the metaclass of Dialect: unittest's
# TestCase and the tests' own Validator (tests/dialects/test_dialect.py), which derives from it.
_PLAIN_BASES = frozenset(("TestCase", "IsolatedAsyncioTestCase", "Validator", "object"))


def filed_class(old_text: str | None, new_text: str, expressions: frozenset) -> str:
    """A class that the new text of a test file makes and the old one did not (a class statement
    in that place with those bases, or a call that makes a class) and that may get the metaclass
    of Dialect: "Name(base)" or the call, for the first one; "" when there is none.

    The metaclass files every subclass of Dialect in a registry that the whole process shares,
    under the value of the member of `Dialects` named like the class or else under the class
    name in lower case, when the class is made (a test body, setUp, a helper, the module).
    Whoever asks the registry for that name later gets the new class: every test of a built-in
    dialect that is no member of the enum (SingleStore), a test that expects a name to be
    unknown. A new class is therefore harmless only when its bases rule the metaclass out: no
    bases, unittest's TestCase, the tests' Validator, an expression class of the package
    (`expressions`, their names: none is the name of a dialect, tools/test_kinds.py), or a
    class of the same file of which that holds. Any other base (Dialect, a dialect, a parser
    class, a name bound some other way, a call) and any keyword (metaclass=) may file it.

    A class is also made by a call: `type(name, bases, namespace)` under any name, computed or
    not, `types.new_class`, the metaclass reached through a class (`type(Dialect)(...)`,
    `X.__class__(...)`, `__new__` or `__call__` with a name, bases and a namespace) or by its
    own name. Such a call that is new is harmless when its bases are written out as a tuple of
    harmless bases, and may file a class otherwise.
    """
    try:
        new_tree = _parse(new_text)
        old_tree = _parse(old_text) if old_text else None
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ""

    def making(call) -> bool:
        """True for a call that can make a class."""
        func = call.func
        spread = any(isinstance(arg, ast.Starred) for arg in call.args) or any(k.arg is None for k in call.keywords)
        if isinstance(func, ast.Name) and func.id == "type":
            return len(call.args) >= 3 or spread
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "type":
            return True  # type(X)(...): the metaclass of X
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in ("new_class", "_Dialect"):
            return True
        if name in ("__class__", "__call__"):
            return len(call.args) >= 3 or spread
        if name == "__new__":
            return len(call.args) >= 4 or spread
        return False

    def made(tree) -> tuple:
        classes, calls = [], []

        def walk(node, path: tuple) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    shape = tuple(ast.dump(base) for base in child.bases) + tuple(
                        (keyword.arg, ast.dump(keyword.value)) for keyword in child.keywords
                    )
                    classes.append(((path + (child.name,), shape), child))
                    walk(child, path + (child.name,))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, path + (child.name,))
                else:
                    if isinstance(child, ast.Call) and making(child):
                        calls.append(((path, ast.dump(child)), child))
                    walk(child, path)

        walk(tree, ())
        return classes, calls

    old_classes, old_calls = made(old_tree) if old_tree is not None else ([], [])
    now, now_calls = made(new_tree)
    had = Counter(key for key, _ in old_classes)
    had_calls = Counter(key for key, _ in old_calls)
    new_classes, new_calls = [], []
    for key, node in now:
        if had[key] > 0:
            had[key] -= 1
        else:
            new_classes.append(node)
    for key, call in now_calls:
        if had_calls[key] > 0:
            had_calls[key] -= 1
        else:
            new_calls.append(call)
    if not new_classes and not new_calls:
        return ""
    aliases = {}
    for node in ast.walk(new_tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name.rsplit(".", 1)[-1]
    local: dict = {}
    for _, node in now:
        local.setdefault(node.name, []).append(node)
    # Every name the file binds anywhere (None: a star import may bind any). A base that nothing binds
    # is a NameError where the class statement runs: no class is made, and what that does to the
    # file's own tests is another rule's business (`_importable_bases`).
    bound: set | None = set(dir(builtins))
    for node in ast.walk(new_tree):
        if bound is None:
            break
        if isinstance(node, ast.alias):
            if node.name == "*":
                bound = None
            else:
                bound.add((node.asname or node.name).split(".", 1)[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, _DEFS):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)

    def filing(base, seen: frozenset) -> str:
        """What a base may be that gives a class the metaclass, "" when it cannot."""
        if isinstance(base, ast.Name):
            if base.id in local and base.id not in aliases:
                # A class of this file: as harmless as its own bases.
                if base.id in seen or any(filing_base(other, seen | {base.id}) for other in local[base.id]):
                    return base.id
                return ""
            name = aliases.get(base.id, base.id)
            if name in _PLAIN_BASES or name in expressions:
                return ""
            if bound is not None and base.id not in bound:
                return ""
            return name
        if isinstance(base, ast.Attribute) and (base.attr in _PLAIN_BASES or base.attr in expressions):
            return ""
        return ast.unparse(base)[:40]

    def filing_base(node, seen: frozenset) -> str:
        """The base (or keyword) that may give the class the metaclass, "" when none can."""
        if node.keywords:
            return f"{node.keywords[0].arg or '**'}="
        for base in node.bases:
            found = filing(base, seen)
            if found:
                return found
        return ""

    def plain_call(call) -> bool:
        """type(name, (bases...), namespace) or new_class(name, (bases...)) with harmless bases written out."""
        func = call.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name not in ("type", "new_class") or call.keywords or any(isinstance(arg, ast.Starred) for arg in call.args):
            return False
        if len(call.args) != (3 if name == "type" else 2):
            return False
        bases = call.args[1]
        return isinstance(bases, ast.Tuple) and not any(filing(base, frozenset()) for base in bases.elts)

    for node in new_classes:
        base = filing_base(node, frozenset((node.name,)))
        if base:
            return f"{node.name}({base})"
    for call in new_calls:
        if not plain_call(call):
            return ast.unparse(call)[:60]
    return ""


# ---- an edited test that leaves something behind ---------------------------------------
#
# `new_global_effects` looks at the statements the new text has and the old one had not. It
# sees neither a restore that was removed nor a changed line of a cleanup that stores nothing
# itself. These work from the changed lines instead, on both sides of the edit.

_CLEANUP_DEFS = frozenset(("teardown", "teardownclass", "teardownmodule", "docleanups", "doclasscleanups", "domodulecleanups"))
_CLEANUP_CALLS = frozenset(("addCleanup", "addClassCleanup", "addModuleCleanup", "enterContext", "enterClassContext", "callback", "push"))
# What a name is bound to when it holds something made on the spot. A name, an attribute, a
# subscript, a conditional or an `or` hands on a thing that exists elsewhere.
_FRESH_VALUES = (
    ast.Constant, ast.JoinedStr, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp,
    ast.GeneratorExp, ast.Call, ast.BinOp, ast.Compare, ast.UnaryOp,
)
_TRY = (ast.Try, getattr(ast, "TryStar", ast.Try))
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _source_lines(text: str) -> list:
    """The lines of a source text as the compiler numbers them (a form feed does not end one)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _changed_lines(old_text: str, new_text: str) -> tuple:
    """(1-based lines of the old text that were removed or changed, the same for the new text)."""
    import difflib

    old_lines = _source_lines(old_text)
    new_lines = _source_lines(new_text)
    # What the two share at both ends needs no matching: an edit is a few lines of a long file.
    limit = min(len(old_lines), len(new_lines))
    head = 0
    while head < limit and old_lines[head] == new_lines[head]:
        head += 1
    tail = 0
    while tail < limit - head and old_lines[-1 - tail] == new_lines[-1 - tail]:
        tail += 1
    removed: set = set()
    added: set = set()
    matcher = difflib.SequenceMatcher(None, old_lines[head:len(old_lines) - tail], new_lines[head:len(new_lines) - tail])
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            removed.update(range(head + i1 + 1, head + i2 + 1))
            added.update(range(head + j1 + 1, head + j2 + 1))
    return removed, added


def _aliasing_names(func) -> set:
    """Names bound in the def other than by giving them a value made on the spot: parameters, loop
    and with targets, imports, unpacked values, and `x = y.z` style names for something that exists elsewhere."""
    names: set = set()
    for node in ast.walk(func):
        if isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            names |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names |= {n.id for n in ast.walk(item.optional_vars) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(node.value, _FRESH_VALUES):
                    # also the name stored into: `d[k] = shared` makes `d` hold something shared
                    names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
                elif not isinstance(target, ast.Name):
                    # `a, b = shared, {}`: a tuple made on the spot hands on what it was made of
                    names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if node.value is not None and not isinstance(node.value, _FRESH_VALUES) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.NamedExpr):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
        elif isinstance(node, _DEFS):
            if node is not func:
                names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _def_paths(tree) -> set:
    """The dotted path of every def and class in the tree, nested ones included."""
    found: set = set()

    def walk(node, path: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFS):
                found.add(path + "." + child.name)
                walk(child, path + "." + child.name)
            elif isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                walk(child, path)

    walk(tree, "")
    return found


def _stores_state_in(tree, lines: set, other_tree, added: bool) -> bool:
    """True when one of `lines` of this text touches what outlasts the test (see `edit_leaves_state`).

    `other_tree` is the other side of the edit and `added` says that this is the new side. A def or
    class that the other side does not have, and every line of which is among `lines`, is new (or
    gone) as a whole and not an edit: on the new side rule K judges it, on the old side only the
    stores it made still count.
    """
    if not lines:
        return False
    first, last = min(lines), max(lines)
    other_paths: list = []

    def touched(node) -> bool:
        start = getattr(node, "lineno", None)
        if start is None:
            return True  # no position of its own (arguments, a comprehension clause): look inside
        end = getattr(node, "end_lineno", None) or start
        if end < first or start > last:
            return False
        return any(line in lines for line in range(start, end + 1))

    def whole(node, path: str) -> bool:
        if not all(line in lines for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)):
            return False
        if not other_paths:
            other_paths.append(_def_paths(other_tree))
        return path not in other_paths[0]

    def visit(node, fresh, self_ok, cleanup: bool, path: str, entire: bool) -> bool:
        """fresh: the fresh locals of the enclosing def (None outside any def); self_ok: "class"
        directly in a class body, else whether `self` is the enclosing method's own instance;
        entire: an enclosing def or class is new or gone as a whole."""
        if not touched(node):
            return False
        if isinstance(node, _DEFS + (ast.Lambda,)):
            name = getattr(node, "name", "<lambda>")
            here = path + "." + name
            is_class = isinstance(node, ast.ClassDef)
            inside_cleanup = cleanup or (not is_class and name.lower() in _CLEANUP_DEFS)
            if inside_cleanup and not is_class and not entire:
                # tearDown and its like, in a class (or module) that was there before: also when
                # the def itself is new or gone, since it then replaces or uncovers an inherited one.
                return True
            entire_here = entire or (not isinstance(node, ast.Lambda) and whole(node, here))
            if entire_here and added:
                return False
            if is_class:
                return any(visit(child, None, "class", cleanup, here, entire_here) for child in ast.iter_child_nodes(node))
            if isinstance(node, ast.Lambda):
                local_names: set = set()
            else:
                local_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                local_names -= _aliasing_names(node)
            own = not isinstance(node, ast.Lambda) and self_ok == "class" and _own_self(node)
            for child in ast.iter_child_nodes(node):
                if visit(child, local_names, own, inside_cleanup, here, entire_here):
                    return True
            if not entire_here:
                # a def handed to addCleanup and the like runs after the test, wherever it stands
                callbacks = {
                    call.args[0].id
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call) and getattr(call.func, "attr", getattr(call.func, "id", "")) in _CLEANUP_CALLS
                    and call.args and isinstance(call.args[0], ast.Name)
                }
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner.name in callbacks and touched(inner):
                        return True
            return False
        if not entire:
            if isinstance(node, _TRY) and any(touched(statement) for statement in node.finalbody):
                return True
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in _CLEANUP_CALLS:
                return True
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if not isinstance(root, ast.Name):
                return True
            if root.id == "self":
                if not (self_ok is True and isinstance(node, ast.Attribute) and node.value is root):
                    return True
            elif fresh is None or root.id not in fresh or node.value is not root:
                # not a fresh local, or a store one level further down, into what it holds
                return True
        return any(visit(child, fresh, self_ok, cleanup, path, entire) for child in ast.iter_child_nodes(node))

    return visit(tree, None, None, False, "", False)


def edit_leaves_state(old_text: str, new_text: str) -> bool:
    """True when an edit to a test file touches what outlasts the test it is in.

    pytest runs every test in one process, so a test that no longer puts back what it changed
    fails tests of other files, and a child that runs one test sees nothing of it. Looked for
    on the lines the edit removed or changed (old side) and on those it added (new side):

    - a store to (or `del` of) an attribute or subscript of anything but `self` in its own
      method and the fresh locals of its def (a parameter, a loop or `with` target, an
      unpacked value and `x = y.z` count as the thing they name);
    - a call that registers a cleanup (`addCleanup`, `enterContext`, `callback`, ...), and a
      def handed to one;
    - any line of `tearDown`, `tearDownClass`, `tearDownModule` and the like;
    - any statement of a `finally` block.

    A test, helper or class that is new as a whole is not an edit: rule K
    (`new_global_effects`, `only_new_definitions`) judges it, and its own `finally` or
    `tearDown` only undoes what it did itself. Of one that is gone as a whole, only the
    stores still count (a later test may have relied on what they left).
    """
    try:
        old_tree = _parse(old_text)
        new_tree = _parse(new_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    removed, added = _changed_lines(old_text, new_text)
    return _stores_state_in(old_tree, removed, new_tree, False) or _stores_state_in(new_tree, added, old_tree, True)


_TEST_PACKAGES = ("unittest", "tests")


def _plain_base(base, kinds: dict) -> bool:
    """A base that exists when the class statement runs and does nothing when it is subclassed:
    `object`, `unittest.TestCase`, a plain class the module defined further up, or a name
    imported from unittest or the tests that reads as a class (`TestCase`, `Validator`; not
    `mock`, not `load`). `kinds` says what each name is bound to at that point of the module."""
    if isinstance(base, ast.Name):
        kind = kinds.get(base.id)
        if kind is None:
            return base.id == "object"
        return kind == "class" or (kind == "imported" and base.id[:1].isupper() and not base.id.isupper())
    return (
        isinstance(base, ast.Attribute)
        and base.attr == "TestCase"
        and isinstance(base.value, ast.Name)
        and base.value.id == "unittest"
        and kinds.get("unittest") == "unittest"
    )


def _importable_bases(module_body: list, statement) -> bool:
    """Can this new class statement run where it stands in the module?

    A name nothing binds is a NameError and a module or a function as a base a TypeError,
    for the whole file, when the module is imported; two bases may have no consistent
    method resolution order. So: at most one base, and one that `_plain_base` accepts with
    what the statements above it bind (a base defined further down does not exist yet).
    """
    if len(statement.bases) > 1:
        return False
    if not statement.bases:
        return True
    kinds: dict = {}
    for node in module_body:
        if node is statement:
            return _plain_base(statement.bases[0], kinds)
        if isinstance(node, ast.ClassDef):
            plain = (
                not node.keywords
                and not node.decorator_list
                and len(node.bases) <= 1
                and all(_plain_base(base, kinds) for base in node.bases)
                and not any(getattr(member, "name", "") == "__init_subclass__" for member in node.body)
            )
            kinds[node.name] = "class" if plain else "other"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kinds[node.name] = "other"
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return False  # it may rebind any name
            first_party = not node.level and (node.module or "").split(".", 1)[0] in _TEST_PACKAGES
            for alias in node.names:
                kinds[alias.asname or alias.name] = "imported" if first_party else "other"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                plain = alias.asname is None and bound == "unittest"
                kinds[bound] = "unittest" if plain else "other"
        else:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, (ast.Store, ast.Del)):
                    kinds[inner.id] = "other"
                elif isinstance(inner, ast.alias):
                    kinds[(inner.asname or inner.name).split(".", 1)[0]] = "other"
                elif isinstance(inner, _DEFS):
                    kinds[inner.name] = "other"
    return False  # not a statement of the module itself


def _hashable_literal(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = getattr(node.operand, "value", None)
        return isinstance(node.operand, ast.Constant) and isinstance(value, (int, float, complex)) and not isinstance(value, bool)
    if isinstance(node, ast.Tuple):
        return all(_hashable_literal(item) for item in node.elts)
    return False


def _literal(node) -> bool:
    """A value that is built without calling anything: a constant, or a tuple,
    list, set or dict of such (set members and dict keys hashable, no unpacking)."""
    if _hashable_literal(node):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_literal(item) for item in node.elts)
    if isinstance(node, ast.Set):
        return all(_hashable_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _hashable_literal(key) for key in node.keys) and all(
            _literal(value) for value in node.values
        )
    return False


def _regex_flags(node) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "re" and node.attr.isupper()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _regex_flags(node.left) and _regex_flags(node.right)
    return False


def _regex_literal(node) -> bool:
    """`re.compile` of a constant pattern, with constant flags at most."""
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    if not (
        isinstance(callee, ast.Attribute)
        and callee.attr == "compile"
        and isinstance(callee.value, ast.Name)
        and callee.value.id == "re"
    ):
        return False
    if not 1 <= len(node.args) <= 2 or any(keyword.arg != "flags" for keyword in node.keywords):
        return False
    pattern = node.args[0]
    if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, (str, bytes)):
        return False
    flags = list(node.args[1:]) + [keyword.value for keyword in node.keywords]
    return len(flags) <= 1 and all(_regex_flags(flag) for flag in flags)


def _binds_re_module(tree) -> bool:
    """True when the name `re` is bound in the module by `import re` and by nothing else."""
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re" and alias.asname in (None, "re"):
                    imported = True
                elif (alias.asname or alias.name.split(".")[0]) == "re":
                    return False
        elif isinstance(node, ast.ImportFrom):
            if any((alias.asname or alias.name) in ("re", "*") for alias in node.names):
                return False
        elif isinstance(node, ast.Name) and node.id == "re" and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        elif isinstance(node, ast.arg) and node.arg == "re":
            return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "re":
            return False
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and "re" in node.names:
            return False
        elif isinstance(node, ast.ExceptHandler) and node.name == "re":
            return False
    return imported


# Names a test module must not gain as plain constants: pytest and unittest read them.
_MODULE_READ_BY_RUNNERS = _MODULE_HOOKS + ("pytest", "collect", "load_tests")


def _harmless_constant(statement, regex: bool) -> bool:
    """`NAME = value` at the top of a test module, where building the value can
    do nothing but give a value: a constant, a tuple, list, set or dict of
    constants, or (when `regex`: the module's `re` is the standard one)
    `re.compile` of a constant pattern. At worst it raises, and then only this
    file cannot be imported. Not under a name pytest or unittest reads from a
    module, nor a dunder."""
    if not isinstance(statement, ast.Assign):
        return False
    for target in statement.targets:
        if not isinstance(target, ast.Name):
            return False
        name = target.id
        if name.startswith("__") or name.lower().startswith(_MODULE_READ_BY_RUNNERS):
            return False
    return _literal(statement.value) or (regex and _regex_literal(statement.value))


def _only_new_definitions(old_body: list, new_body: list, known: set, prefix: str, taken: tuple, module=None) -> bool:
    """`module`: the whole body of the new module when `new_body` leaves part of it out."""
    import unittest

    old_names = _bound_names(old_body)
    module_taken, method_taken = taken
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
            if not _only_new_definitions(old.body, statement.body, known, prefix + statement.name + "::", taken):
                return False
            position += 1
            continue
        name = getattr(statement, "name", "")
        if not name or name in old_names or name.startswith("__") or prefix + name in known:
            return False
        if prefix:
            # A method: only a new test, never something existing tests could call
            # or read through self: not a name unittest.TestCase has, not one the
            # old code of the module reads as an attribute or defines elsewhere.
            if (
                not name.startswith("test")
                or hasattr(unittest.TestCase, name)
                or name in method_taken
                or not _plain_function(statement, True)
            ):
                return False
        elif isinstance(statement, ast.ClassDef):
            if statement.decorator_list or statement.keywords or name in module_taken:
                return False
            if not _importable_bases(new_body if module is None else module, statement):
                return False
            for member in statement.body:
                simple = isinstance(member, ast.Pass) or _is_docstring(member) or _plain_function(member, True)
                if isinstance(member, ast.Assign) and isinstance(member.value, ast.Constant):
                    simple = all(isinstance(target, ast.Name) for target in member.targets)
                if not simple or getattr(member, "name", "x").startswith("__"):
                    return False
        else:
            # A new global must not be a builtin, nor a name the old module loads
            # as a global anywhere: existing code would find the new one instead.
            if not _plain_function(statement) or name.lower().startswith(_MODULE_HOOKS) or name in module_taken:
                return False
    return position == len(old_body)


_KNOWN_PACKAGES = ("sqlglot", "tests", "pytest", "unittest", "duckdb", "pandas", "numpy")


def harmless_new_test_module(text: str, import_ok=None) -> bool:
    """True when a brand-new test file can only add tests of its own.

    pytest imports every test file it finds, so a new one runs at collection.
    This accepts a file made of imports of the standard library, of sqlglot,
    of the existing tests or of the installed test tools, plus plain classes
    and functions, and module-level constants (`NAME = value` with a value
    built without calling anything, or `re.compile` of a constant pattern:
    see _harmless_constant). Importing those modules does nothing a test file
    of the pinned commit does not already do, and defining classes, functions
    and constants does nothing to anything else. Anything else at module level
    could change what the existing tests see. `import_ok(module, names)` (names
    None for `import module`) is asked about every import and says False for one
    that is certain to fail: a file that cannot be imported is not one that only
    adds tests.
    """
    import sys

    try:
        tree = _parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    regex = _binds_re_module(tree)
    body = []
    for statement in tree.body:
        if _is_docstring(statement) or isinstance(statement, ast.Pass):
            continue
        if _harmless_constant(statement, regex):
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if isinstance(statement, ast.ImportFrom):
                if statement.level or not statement.module:
                    return False
                modules = [statement.module]
                names = [alias.name for alias in statement.names]
            else:
                modules = [alias.name for alias in statement.names]
                names = None
            for module in modules:
                top = module.split(".", 1)[0]
                if top not in _KNOWN_PACKAGES and top not in sys.stdlib_module_names:
                    return False
                if import_ok is not None and not import_ok(module, names):
                    return False
            continue
        body.append(statement)
    return _only_new_definitions([], body, set(), "", (frozenset(), frozenset()), tree.body)


def coding_cookie(text: str) -> str:
    """The encoding a source file declares in its first two lines, "" for none."""
    for line in text.split("\n", 2)[:2]:
        match = _COOKIE.match(line)
        if match:
            name = match.group(1).lower().replace("_", "-")
            return "" if name in ("utf-8", "utf8") else name
        if line.strip() and not line.lstrip().startswith("#"):
            break
    return ""


def _taken_names(tree, reserved) -> tuple:
    """(names a new module-level definition must not have, names a new method must not have)."""
    loaded = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return frozenset(loaded | set(dir(builtins))), frozenset(attributes | defined | set(reserved))


def only_new_definitions(old_text: str, new_text: str, known: set, reserved=()) -> bool:
    """True when a test file only gained whole new tests, test classes or helper functions.

    Everything that was there is unchanged, and every addition has a name that
    was not bound before and that nothing existing can reach: a new `test...`
    method (not one that would replace an inherited test, `known` holds those),
    a new class of plain methods, or a new module-level function that is not a
    pytest or unittest hook. Defining them cannot fail and no existing test
    behaves differently, so no test needs to run.
    """
    try:
        old_tree = _parse(old_text)
        new_tree = _parse(new_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return _only_new_definitions(old_tree.body, new_tree.body, known, "", _taken_names(old_tree, reserved))
