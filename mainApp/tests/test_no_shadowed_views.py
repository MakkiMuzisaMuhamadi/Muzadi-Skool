"""
Guard: a view must not be silently replaced by another function with the same name.

That happened with ajax_teacher_streams: ajax_views.py defined it twice, the second
(competency-only) copy won, and Enter Marks showed every stream instead of the
teacher's own. These checks fail as soon as such a duplicate comes back.
"""
import ast
import re
from collections import defaultdict
from pathlib import Path

from django.test import SimpleTestCase

APP = Path(__file__).resolve().parent.parent


def top_level_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


class NoShadowedViewsTests(SimpleTestCase):
    def test_no_function_defined_twice_in_one_module(self):
        problems = []
        for path in sorted(APP.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            seen = {}
            for node in top_level_functions(path):
                if node.name in seen:
                    problems.append(f"{path.relative_to(APP)}: '{node.name}' at lines "
                                    f"{seen[node.name]} and {node.lineno}")
                seen.setdefault(node.name, node.lineno)
        self.assertEqual(problems, [], "Duplicate definitions (the later one silently wins):\n" + "\n".join(problems))

    def test_url_views_not_defined_in_two_star_imported_modules(self):
        urls = (APP / "urls.py").read_text(encoding="utf-8")
        modules = dict.fromkeys(re.findall(r"^from (mainApp\.[\w.]+) import \*", urls, re.M))
        used = set(re.findall(r"path\([^,]+,\s*(\w+)\s*,", urls))
        where = defaultdict(list)
        for mod in modules:
            path = APP.parent / (mod.replace(".", "/") + ".py")
            for node in top_level_functions(path):
                if node.name in used:
                    where[node.name].append(mod)
        clashes = {name: mods for name, mods in where.items() if len(mods) > 1}
        self.assertEqual(clashes, {}, "A URL view name exists in several 'import *' modules; "
                                      "the last import wins silently: %s" % clashes)
