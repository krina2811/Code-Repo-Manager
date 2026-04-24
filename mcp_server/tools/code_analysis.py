"""
MCP Server — Code Analysis Tools

Static analysis tools called by LangGraph agents.
Uses stdlib ast + radon for metrics; no external API calls.
"""

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import radon.complexity as radon_cc
    from radon.raw import analyze as radon_analyze
    _RADON = True
except ImportError:
    _RADON = False

try:
    # bandit is optional — we fall back to regex patterns only
    import bandit.core.manager as bandit_manager
    from bandit.core import config as bandit_config
    _BANDIT = True
except ImportError:
    _BANDIT = False


class CodeAnalysisTools:
    """Static analysis tools exposed to agents."""

    # ──────────────────────────────────────────────────────────────────
    # Imports
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def analyze_imports(file_path: str) -> Dict[str, Any]:
        """
        Detect unused imports in a Python file.

        Returns dict with 'unused_imports' list, each entry:
          {name, module, line, type}
        """
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(content)

            imports: Dict[str, Dict] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imports[name] = {
                            "type": "import",
                            "module": alias.name,
                            "line": node.lineno,
                        }
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imports[name] = {
                            "type": "from_import",
                            "module": node.module,
                            "name": alias.name,
                            "line": node.lineno,
                        }

            used: set = set()
            unused: List[Dict] = []
            for imp_name, info in imports.items():
                if content.count(imp_name) > 1:   # >1 means used beyond the import line
                    used.add(imp_name)
                else:
                    unused.append({
                        "name":   imp_name,
                        "module": info.get("module", ""),
                        "line":   info["line"],
                        "type":   info["type"],
                    })

            return {
                "file_path":      file_path,
                "total_imports":  len(imports),
                "used_imports":   list(used),
                "unused_imports": unused,
            }
        except Exception as exc:
            return {"error": str(exc), "file_path": file_path}

    # ──────────────────────────────────────────────────────────────────
    # Functions
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def analyze_functions(file_path: str) -> Dict[str, Any]:
        """
        Detect unused functions and high-complexity functions.
        """
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(content)

            functions = []
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    functions.append({
                        "name":          node.name,
                        "line":          node.lineno,
                        "is_private":    node.name.startswith("_") and not node.name.startswith("__"),
                        "is_special":    node.name.startswith("__") and node.name.endswith("__"),
                        "args_count":    len(node.args.args),
                        "has_docstring": ast.get_docstring(node) is not None,
                    })

            unused: List[Dict] = []
            for func in functions:
                if func["is_special"]:
                    continue
                pattern = rf"\b{func['name']}\s*\("
                if len(re.findall(pattern, content)) <= 1:
                    unused.append(func)

            complex_funcs: List[Dict] = []
            if _RADON:
                for item in radon_cc.cc_visit(content):
                    if item.complexity <= 5:
                        continue

                    item_type = type(item).__name__   # "Function", "Method", "Class"

                    # Skip Class-level entries — radon reports aggregate complexity
                    # for the whole class body. We want the individual methods.
                    if item_type == "Class":
                        continue

                    # Determine if this is a method (belongs to a class) or a
                    # standalone function, and record the parent class name if any.
                    parent_class = getattr(item, "classname", None)

                    complex_funcs.append({
                        "name":           item.name,
                        "complexity":     item.complexity,
                        "line":           item.lineno,
                        "classification": item.letter,
                        "kind":           "method" if parent_class else "function",
                        "parent_class":   parent_class,
                    })

            return {
                "file_path":               file_path,
                "total_functions":         len(functions),
                "unused_functions":        unused,
                "complex_functions":       complex_funcs,
                "functions_without_docstring": [
                    f for f in functions
                    if not f["has_docstring"] and not f["is_private"]
                ],
            }
        except Exception as exc:
            return {"error": str(exc), "file_path": file_path}

    # ──────────────────────────────────────────────────────────────────
    # Security
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def analyze_security(file_path: str) -> Dict[str, Any]:
        """
        Detect hardcoded secrets and insecure patterns using regex.
        Each finding includes the source line as 'code' for use as a
        content fingerprint in the executor.
        """
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            lines   = content.splitlines()
            findings: List[Dict] = []

            # ── Hardcoded secrets ─────────────────────────────────────
            secret_patterns = [
                (r'(?i)(api[_-]?key|apikey)\s*[=:]\s*["\']([^"\']{10,})["\']',   "API Key"),
                (r'(?i)(secret|password|passwd|pwd)\s*[=:]\s*["\']([^"\']{5,})["\']', "Secret/Password"),
                (r'(?i)(token|auth[_-]?token)\s*[=:]\s*["\']([^"\']{10,})["\']', "Auth Token"),
                (r'sk-[a-zA-Z0-9]{32,}',                                          "OpenAI Key"),
                (r'(?i)aws[_-]?access[_-]?key[_-]?id\s*[=:]\s*["\']([A-Z0-9]{20})["\']', "AWS Key"),
                (r'(?i)(database_url|db_url)\s*[=:]\s*["\']([^"\']{10,})["\']',  "Database URL"),
            ]
            for pattern, label in secret_patterns:
                for m in re.finditer(pattern, content):
                    ln = content[: m.start()].count("\n") + 1
                    findings.append({
                        "type":     "hardcoded_secret",
                        "severity": "high",
                        "message":  f"Potential hardcoded {label}",
                        "line":     ln,
                        "code":     lines[ln - 1] if 0 < ln <= len(lines) else "",
                    })

            # ── Insecure patterns ─────────────────────────────────────
            insecure_patterns = [
                (r"exec\s*\(",         "Use of exec()"),
                (r"eval\s*\(",         "Use of eval()"),
                (r"pickle\.loads?\s*\(", "Pickle deserialisation (code execution risk)"),
                (r"shell\s*=\s*True",  "shell=True in subprocess (injection risk)"),
            ]
            for pattern, desc in insecure_patterns:
                for m in re.finditer(pattern, content):
                    ln = content[: m.start()].count("\n") + 1
                    findings.append({
                        "type":     "insecure_code",
                        "severity": "medium",
                        "message":  desc,
                        "line":     ln,
                        "code":     lines[ln - 1] if 0 < ln <= len(lines) else "",
                    })

            return {
                "file_path":       file_path,
                "total_issues":    len(findings),
                "findings":        findings,
                "high_severity":   sum(1 for f in findings if f["severity"] == "high"),
                "medium_severity": sum(1 for f in findings if f["severity"] == "medium"),
            }
        except Exception as exc:
            return {"error": str(exc), "file_path": file_path}

    # ──────────────────────────────────────────────────────────────────
    # Documentation
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def analyze_documentation(file_path: str) -> Dict[str, Any]:
        """
        Detect missing or very short docstrings in modules, functions, classes.
        """
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(content)
            issues: List[Dict] = []

            if not ast.get_docstring(tree):
                issues.append({
                    "type": "missing_module_docstring",
                    "line": 1,
                    "message": "Module is missing a docstring",
                })

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    if node.name.startswith("_") and not node.name.startswith("__"):
                        continue   # skip private

                    doc = ast.get_docstring(node)
                    kind = "function" if isinstance(node, ast.FunctionDef) else "class"

                    if not doc:
                        issues.append({
                            "type":    f"missing_{kind}_docstring",
                            "name":    node.name,
                            "line":    node.lineno,
                            "message": f'{kind.capitalize()} "{node.name}" is missing a docstring',
                        })
                    elif len(doc) < 10:
                        issues.append({
                            "type":    "short_docstring",
                            "name":    node.name,
                            "line":    node.lineno,
                            "message": f'Docstring for "{node.name}" is very short ({len(doc)} chars)',
                        })

            return {
                "file_path":          file_path,
                "total_issues":       len(issues),
                "issues":             issues,
                "has_module_docstring": ast.get_docstring(tree) is not None,
            }
        except Exception as exc:
            return {"error": str(exc), "file_path": file_path}

    # ──────────────────────────────────────────────────────────────────
    # File metrics
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def get_file_metrics(file_path: str) -> Dict[str, Any]:
        """Return raw line-count metrics via radon (or fallback counts)."""
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            if _RADON:
                m = radon_analyze(content)
                return {
                    "file_path":       file_path,
                    "loc":             m.loc,
                    "lloc":            m.lloc,
                    "sloc":            m.sloc,
                    "comments":        m.comments,
                    "multi":           m.multi,
                    "blank":           m.blank,
                    "single_comments": m.single_comments,
                }
            # Fallback without radon
            loc = len(content.splitlines())
            return {"file_path": file_path, "loc": loc, "lloc": loc, "sloc": loc}
        except Exception as exc:
            return {"error": str(exc), "file_path": file_path}

    # ──────────────────────────────────────────────────────────────────
    # File discovery
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def get_python_files(
        repo_path: str,
        exclude_patterns: Optional[List[str]] = None,
    ) -> List[str]:
        """Return all .py files in repo_path, respecting exclude patterns."""
        if exclude_patterns is None:
            exclude_patterns = [
                "venv", ".venv", "__pycache__", ".git", ".tox",
                "node_modules", "dist", "build", ".eggs",
            ]

        result: List[str] = []
        for py_file in Path(repo_path).rglob("*.py"):
            if not any(pat in str(py_file) for pat in exclude_patterns):
                result.append(str(py_file))

        return result


# ── Tool registry (used by MCP server) ───────────────────────────────────────

TOOLS: Dict[str, Any] = {
    "analyze_imports":      CodeAnalysisTools.analyze_imports,
    "analyze_functions":    CodeAnalysisTools.analyze_functions,
    "analyze_security":     CodeAnalysisTools.analyze_security,
    "analyze_documentation": CodeAnalysisTools.analyze_documentation,
    "get_file_metrics":     CodeAnalysisTools.get_file_metrics,
    "get_python_files":     CodeAnalysisTools.get_python_files,
}