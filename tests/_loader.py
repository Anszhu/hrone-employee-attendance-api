"""Load app/main.py for tests.

If the third-party packages are installed, the real module is imported (MODE == "full").
If they are not (e.g. an offline sandbox), only the stdlib-only section of main.py between
the PURE-HELPERS sentinels is executed (MODE == "pure-slice"). Tests that need fastapi /
pydantic / httpx skip themselves in that mode and are reported as skipped, never as passed.
"""
import ast
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"
THIRD_PARTY = {"fastapi", "pydantic", "pymongo", "bson", "dotenv", "starlette"}


def _pure_slice() -> types.ModuleType:
    src = MAIN.read_text()
    lines = src.splitlines()
    begin = next(i for i, l in enumerate(lines) if l.strip() == "# === PURE-HELPERS-BEGIN ===")
    end = next(i for i, l in enumerate(lines) if l.strip() == "# === PURE-HELPERS-END ===")
    header = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            root = (node.names[0].name if isinstance(node, ast.Import) else node.module).split(".")[0]
            if root in sys.stdlib_module_names:
                header.append(ast.get_source_segment(src, node))
    code = "\n".join(header) + "\nimport logging\nlogger = logging.getLogger('attendance')\n"
    code += "\n".join(lines[begin + 1 : end])
    mod = types.ModuleType("app_main_pure_slice")
    exec(compile(code, str(MAIN) + "#pure-slice", "exec"), mod.__dict__)
    return mod


def load_main():
    sys.path.insert(0, str(ROOT))
    try:
        import app.main as m
        return m, "full"
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] not in THIRD_PARTY:
            raise
        return _pure_slice(), "pure-slice"


def have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ModuleNotFoundError:
        return False
