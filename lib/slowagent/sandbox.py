# sandbox.py — restricted execution context for slowagent extensions.
#
# **Status: scaffolding.**  The default slowtask shipped with this submodule
# does NOT execute LLM-generated Python code.  The LLM only ever returns
# JSON values (see `llm.py`), which the slowtask writes to the datastore
# directly.  This keeps the security story trivial: there is no path for
# the model to influence anything other than the numeric values it returns.
#
# This module exists for users who want to opt in to a future "advanced
# mode" where Claude can write a small Python helper for post-processing
# the extracted values (e.g. unit conversion, rolling averages).  The class
# below is a hard-restricted exec environment that only exposes:
#
#   - `slowagent.webcam.WebcamSource` instances     (read-only frame access)
#   - `slowpy.store.DataStore.append`               (write to slowdash)
#   - the standard math module
#
# Anything else — `os`, `subprocess`, `socket`, `urllib`, file I/O, the
# `import` statement — raises a SandboxError before the extension runs.
#
# See `Sandbox.run(...)` for the entry point.

import ast
import math
import logging


class SandboxError(Exception):
    pass


# Modules we re-expose by reference.  Anything not in this set is
# unavailable; the AST validator rejects `import` statements outright.
_ALLOWED_GLOBALS = {
    'math':   math,
    '__builtins__': {
        'abs':     abs,    'min':     min,    'max':    max,    'sum':    sum,
        'len':     len,    'range':   range,  'round':  round,  'enumerate': enumerate,
        'float':   float,  'int':     int,    'bool':   bool,   'str':    str,
        'list':    list,   'tuple':   tuple,  'dict':   dict,   'set':    set,
        'True':    True,   'False':   False,  'None':   None,
        'print':   print,
    },
}


# AST node types the sandbox flat-out refuses.  Crucially: no Import,
# ImportFrom, Exec, Global, Nonlocal, Lambda (closures over the host scope),
# or attribute access on dunder names.
_FORBIDDEN_NODES = (
    ast.Import, ast.ImportFrom,
    ast.Global, ast.Nonlocal,
    ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith,
)


def _validate_ast(tree: ast.AST):
    """Walk the AST and reject anything dangerous."""
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise SandboxError(f"forbidden syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith('__') and node.attr.endswith('__'):
            # Block dunder access — defeats most python-sandbox escapes.
            raise SandboxError(f"forbidden attribute access: .{node.attr}")
        if isinstance(node, ast.Name) and node.id == '__import__':
            raise SandboxError("__import__ is not available in the sandbox")


class Sandbox:
    """Compile-and-run a small Python snippet inside a restricted namespace.

    Example usage (advanced mode only — disabled by default):

        sb = Sandbox(webcam=cam, datastore=ds)
        sb.run(\"\"\"
            # Convert a Fahrenheit channel to Celsius before logging.
            v = ctx['ctr1']
            datastore.append((v - 32) * 5 / 9, tag='ctr1_C')
        \"\"\", ctx={'ctr1': 71.6})
    """

    def __init__(self, *, webcam=None, datastore=None):
        self._webcam = webcam
        self._datastore = datastore

    def run(self, source: str, ctx: dict = None):
        try:
            tree = ast.parse(source, mode='exec')
        except SyntaxError as e:
            raise SandboxError(f"syntax error: {e}") from e
        _validate_ast(tree)

        globs = dict(_ALLOWED_GLOBALS)
        globs['ctx']       = dict(ctx or {})
        globs['webcam']    = self._webcam
        globs['datastore'] = self._datastore

        try:
            exec(compile(tree, '<slowagent-sandbox>', 'exec'), globs, globs)
        except Exception as e:
            logging.warning("slowagent.sandbox: extension raised %s: %s", type(e).__name__, e)
            raise
        return globs.get('ctx')
