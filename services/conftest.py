# pytest collection rules for this directory.
#
# Many `test_*.py` files at this level are NOT pytest cases: they are standalone scripts whose body runs
# at import time and ends in `sys.exit()` / `raise SystemExit`. pytest imports them during collection,
# the SystemExit escapes into `wrap_session`, and the run dies with INTERNALERROR having executed
# nothing. A third party following the README's own sequence saw that instead of a test run.
# `dendra_tests_all.sh` -- the project's gate battery, which lives in the development repository and is
# not published -- launches every `test_*.py` of this level as a script and grades its exit code; they
# also run one by one as `python3 <file>`, which is what a published clone has. pytest simply has to
# leave the scripts alone.
#
# THE RULE IS DERIVED, NOT LISTED. This file used to carry a nominal `collect_ignore` list. It was right
# the day it was written and rotted without a sound: scripts added to this directory afterwards were
# never added to it, and `pytest` run here died with INTERNALERROR again. A list that has to be kept in
# step with a directory by hand is a count copied into a file.
# So the hook below asks the question pytest itself asks -- "does this module define anything I would
# collect?" -- and asks it of the file's SYNTAX TREE, without importing it (importing is what runs the
# script). A module that defines no collectable test yields no case when collected: leaving it alone
# loses nothing. A module that defines one is collected as usual.
#
# The obvious shortcut is still wrong: `collect_ignore_glob = ["test_*.py"]` also swallows the files of
# this directory that DO define real `def test_` functions, and would delete their cases from every run
# in silence to repair a loud error -- the exact silent green it claims to fix, in the other direction.
#
# THREE OUTCOMES, NEVER TWO:
#   · the tree binds a name pytest collects     -> collected (pytest's default);
#   · the tree binds none                       -> ignored here: it is a script, run as one;
#   · the file cannot be read or parsed         -> collected, so pytest reports the error itself
#                                                  instead of this hook hiding it.
# A module that defines tests AND exits at import is collected and fails loudly: the fix belongs in that
# file (an `if __name__ == "__main__":` guard), never in a rule here that would drop its cases quietly.
#
# What counts as "a name pytest collects" is read from pytest's own settings (`python_functions`,
# `python_classes`), matched the way pytest matches them (a prefix, or a glob), over every name the
# module binds at its top level: a def, a class, an assignment, a `from x import y` -- pytest collects an
# imported test function too. A name whose value the tree cannot tell (an assignment, an import) counts
# when it matches: in doubt the module is collected, and a doubt costs a loud error, never a lost case.
# A class deriving from a `TestCase` is collected whatever its name, as the unittest plugin does. A
# `from x import *` binds names nobody can list from the tree: such a module is collected.
import ast
import fnmatch
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _matches(name, patterns):
    """pytest's own rule (PyCollector._matches_prefix_or_glob_option): a prefix, or a glob."""
    for p in patterns:
        if name.startswith(p):
            return True
        if any(c in p for c in "*?[") and fnmatch.fnmatch(name, p):
            return True
    return False


def _bound_names(body):
    """(name, node) for every name bound at module level, descending into the blocks that do not open a
    scope (if / try / with / for / while). None for a star import: its names cannot be listed."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node
        elif isinstance(node, ast.ImportFrom):
            # `import x` binds a module, which pytest never collects; `from x import y` may bind a function.
            for a in node.names:
                if a.name == "*":
                    yield None, node
                else:
                    yield (a.asname or a.name), node
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        yield n.id, node
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            yield from _bound_names(node.body)
            yield from _bound_names(getattr(node, "orelse", []))
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            yield from _bound_names(node.body)
            for h in node.handlers:
                yield from _bound_names(h.body)
            yield from _bound_names(node.orelse)
            yield from _bound_names(node.finalbody)


def _derives_from_testcase(node):
    for b in node.bases:
        n = b.attr if isinstance(b, ast.Attribute) else (b.id if isinstance(b, ast.Name) else "")
        if n.endswith("TestCase"):
            return True
    return False


def _defines_a_test(path, functions, classes):
    """True / False from the syntax tree; None when the file cannot be read or parsed."""
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None
    for name, node in _bound_names(tree.body):
        if name is None:
            return True
        if isinstance(node, ast.ClassDef):
            if _matches(name, classes) or _derives_from_testcase(node):
                return True
        elif _matches(name, functions) or _matches(name, classes):
            return True
    return False


def pytest_ignore_collect(collection_path, config):
    """Ignore a module of THIS directory (not of tests/) that pytest would collect as a test file and
    that defines no test. Returns None, never False, for everything else: pytest's own rules decide."""
    p = Path(collection_path)
    if p.suffix != ".py" or p.parent.resolve() != HERE or p.name == "conftest.py":
        return None
    if not any(fnmatch.fnmatch(p.name, pat) for pat in config.getini("python_files")):
        return None
    found = _defines_a_test(p, config.getini("python_functions"), config.getini("python_classes"))
    if found is False:
        return True
    return None
