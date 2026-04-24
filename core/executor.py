"""
Registry-Driven Action Executor

Every action reads and writes exclusively through FileRegistry.
Zero raw line numbers are stored — symbols are resolved just-in-time from
the live SymbolTable which is rebuilt after each edit.

Action pipeline per execution
──────────────────────────────
  1. Parse target  → (file_path, entity_type, entity_name)
  2. Load snapshot from FileRegistry  (always current in-memory state)
  3. Resolve symbol via SymbolTable  (current line numbers after prior edits)
  4. Backup the file
  5. Call FileEditor mutation  (rebuilds AST + SymbolTable automatically)
  6. Flush snapshot to disk

Local LLM (Ollama) is used for:
  • add_docstring  — deepseek-coder or codellama
  • refactor_code  — same models, longer context
  • restructure    — structural analysis & split suggestions

Security fix flow:
  • Extract variable name + value with regex
  • Write placeholder to .env.example (create if absent)
  • Replace original line with os.getenv("VAR_NAME")
  • Inject `import os` at top if missing
"""

import ast
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.models import Action, ActionType
from core.file_registry import FileEditor, FileRegistry, FileSnapshot
from core.logger import get_logger

registry = FileRegistry.get_instance()
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# BackupManager
# ─────────────────────────────────────────────────────────────────────────────

class BackupManager:
    """
    Creates timestamped file backups before every modification.
    Maintains a JSON log for restore capability.

    Disabled when ENABLE_BACKUP=false (default on production/EC2).
    In production, backups should be handled by object storage (S3/MinIO).
    Set ENABLE_BACKUP=true in local .env to enable.
    """

    def __init__(self, backup_dir: Optional[str] = None):
        self.enabled = os.getenv("ENABLE_BACKUP", "false").lower() == "true"

        if not self.enabled:
            logger.debug("BackupManager disabled (ENABLE_BACKUP != true)")
            self.backup_dir = None
            self.log = {"backups": []}
            return

        if backup_dir is None:
            backup_dir = os.getenv(
                "BACKUP_DIR",
                str("/mnt/e/My_work/code_repo_backup/.code_analysis_backups")
            )
        self.backup_dir = Path(backup_dir)
        logger.debug("Backup directory: %s", self.backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.backup_dir / "backup_log.json"
        self._load_log()

    def _load_log(self):
        if self._log_path.exists():
            with open(self._log_path) as f:
                self.log = json.load(f)
        else:
            self.log = {"backups": []}

    def _save_log(self):
        with open(self._log_path, "w") as f:
            json.dump(self.log, f, indent=2, default=str)

    def create_backup(self, file_path: str) -> str:
        if not self.enabled:
            return ""

        source = Path(file_path)
        if not source.exists():
            raise FileNotFoundError(f"Cannot backup missing file: {file_path}")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_name = f"{source.stem}_{stamp}{source.suffix}"
        backup_path = self.backup_dir / backup_name
        shutil.copy2(source, backup_path)

        entry = {
            "original":  str(source.resolve()),
            "backup":    str(backup_path),
            "timestamp": stamp,
        }
        self.log["backups"].append(entry)
        self._save_log()
        logger.debug("Backup created: %s", backup_name)
        return str(backup_path)

    def restore_latest(self, file_path: str) -> bool:
        if not self.enabled:
            logger.warning("BackupManager disabled — restore not available")
            return False

        abs_path = str(Path(file_path).resolve())
        candidates = [b for b in self.log["backups"] if b["original"] == abs_path]
        if not candidates:
            return False
        latest = max(candidates, key=lambda b: b["timestamp"])
        shutil.copy2(latest["backup"], file_path)
        registry.invalidate(file_path)
        logger.info("Restored from backup: %s", Path(latest['backup']).name)
        return True

    def list_backups(self, file_path: Optional[str] = None) -> List[Dict]:
        if not self.enabled:
            return []
        if file_path:
            abs_path = str(Path(file_path).resolve())
            return [b for b in self.log["backups"] if b["original"] == abs_path]
        return self.log["backups"]


# ─────────────────────────────────────────────────────────────────────────────
# LocalLLM  (Ollama wrapper)
# ─────────────────────────────────────────────────────────────────────────────

class LocalLLM:
    """
    Calls a locally-running Ollama instance.

    Model preference order (first available wins):
      deepseek-coder:6.7b  →  codellama:7b  →  deepseek-coder  →
      codellama  →  mistral  →  llama3

    Falls back to template strings when Ollama is not running.
    """

    _PREFERRED = [
        "deepseek-coder:6.7b",
        "codellama:7b",
        "deepseek-coder",
        "codellama",
        "mistral",
        "llama3",
    ]
    BASE_URL = "http://localhost:11434"

    def __init__(self):
        self.model: Optional[str] = self._detect()

    def _detect(self) -> Optional[str]:
        try:
            resp = requests.get(f"{self.BASE_URL}/api/tags", timeout=5)
        except requests.exceptions.ConnectionError:
            logger.warning("Ollama not running — start it with: ollama serve")
            return None
        except requests.exceptions.Timeout:
            logger.warning("Ollama timed out — is it still starting up?")
            return None
        except Exception as e:
            logger.warning("Ollama connection error: %s: %s", type(e).__name__, e)
            return None

        if resp.status_code != 200:
            logger.warning("Ollama returned HTTP %d", resp.status_code)
            return None

        try:
            data = resp.json()
        except Exception as e:
            logger.warning("Ollama response not valid JSON: %s", e)
            return None

        # Extract all available model names
        available = [m["name"] for m in data.get("models", [])]

        if not available:
            logger.warning("Ollama is running but no models are downloaded. Run: ollama pull deepseek-coder:6.7b")
            return None

        logger.info("Ollama models available: %s", available)

        # Pass 1 — exact name match
        for preferred in self._PREFERRED:
            if preferred in available:
                logger.info("Ollama model selected: %s", preferred)
                return preferred

        # Pass 2 — prefix match (e.g. "deepseek-coder" matches "deepseek-coder:6.7b")
        for preferred in self._PREFERRED:
            base = preferred.split(":")[0]
            for avail in available:
                if avail.startswith(base):
                    logger.info("Ollama model selected: %s", avail)
                    return avail

        # Pass 3 — none of preferred found, use whatever is installed
        fallback = available[0]
        logger.warning("No preferred coding model found — using: %s. For best results run: ollama pull deepseek-coder:6.7b", fallback)
        return fallback

    @property
    def available(self) -> bool:
        return self.model is not None

    def generate(
        self,
        prompt: str,
        system: str = "",
        timeout: int = 300,
    ) -> Optional[str]:
        """
        Call Ollama synchronously.

        When running inside a background thread (async job from main.py),
        this can safely block for as long as needed — the HTTP server is
        not waiting on this thread.

        timeout is the requests read timeout in seconds. Defaults to 300s
        (5 min) which covers even large refactor tasks on CPU-only setups.
        Callers can override for shorter tasks (docstrings) or longer ones
        (complex refactors).
        """
        if not self.model:
            return None
        try:
            payload = {
                "model":   self.model,
                "prompt":  prompt,
                "system":  system,
                "stream":  False,
                "options": {
                    "temperature": 0.15,
                    "num_predict": 2048,   # raised from 1024 for refactor tasks
                },
            }
            resp = requests.post(
                f"{self.BASE_URL}/api/generate",
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json().get("response", "").strip()
            else:
                logger.error("Ollama HTTP %d: %s", resp.status_code, resp.text[:200])
        except requests.exceptions.Timeout:
            logger.error("Ollama timed out after %ds — increase timeout or use a faster model", timeout)
        except requests.exceptions.ConnectionError:
            logger.error("Ollama connection refused — is it still running?")
        except Exception as e:
            logger.error("Ollama error: %s: %s", type(e).__name__, e)
        return None

    # ── Task-specific helpers ─────────────────────────────────────────

    def generate_docstring(self, function_code: str, function_name: str) -> str:
        """Return a formatted triple-quoted docstring string."""
        system = (
            "You are an expert Python developer. "
            "Write concise, accurate Google-style docstrings. "
            "Return ONLY the docstring content — no triple quotes, no code fences."
        )
        prompt = (
            f"Write a Google-style docstring for this Python function.\n"
            f"Return ONLY the docstring text (no triple quotes, no code fences).\n\n"
            f"```python\n{function_code}\n```\n\n"
            f"Format:\nBrief one-line description.\n\nArgs:\n"
            f"    param: Description.\n\nReturns:\n    Description."
        )

        result = self.generate(prompt, system, timeout=300)   # 5 min — runs in background thread
        if result:
            result = result.strip()
            # Strip accidental code fences
            result = re.sub(r"^```[a-zA-Z]*\n?", "", result)
            result = re.sub(r"\n?```$", "", result)
            # Strip accidental triple-quotes
            result = result.strip('"""').strip("'''").strip()
            return f'"""{result}"""'

        # Template fallback
        return (
            f'"""Add description for {function_name}.\n\n'
            f"    TODO: Ollama unavailable — fill in manually.\n"
            f'    """'
        )

    def refactor_function(
        self,
        function_code: str,
        function_name: str,
        complexity: int,
    ) -> Optional[str]:
        """
        Return refactored Python source for the function (may include helpers).

        LLMs frequently wrap output in markdown fences, add explanation text,
        or split helper functions into multiple code blocks. This method uses
        a multi-stage extraction pipeline to handle all those cases robustly
        before returning to the caller for ast.parse() validation.
        """
        system = (
            "You are an expert Python developer. Your only job is to return "
            "valid, executable Python code. "
            "CRITICAL: A function signature `def name(params):` contains ONLY "
            "parameter names — never a docstring, never any text. "
            "A docstring is ALWAYS the first statement inside the function body, "
            "never inside the parentheses. "
            "Return ONLY raw Python code — no markdown, no backticks, no prose."
        )
        prompt = (
            f"Refactor this Python function to reduce cyclomatic complexity "
            f"(currently {complexity}).\n\n"
            f"RULES:\n"
            f"1. The `def {function_name}(...)` line must contain ONLY the "
            f"parameter list — copy it exactly from the original.\n"
            f"2. A docstring goes as the FIRST statement INSIDE the body — "
            f"never inside the parentheses.\n"
            f"3. Extract helper functions BEFORE the main function if needed.\n"
            f"4. Keep the exact same function name and parameter names.\n"
            f"5. Return ONLY valid Python — no backticks, no explanation text.\n\n"
            f"WRONG — never do this:\n"
            f"def {function_name}(\n"
            f'    """docstring here is wrong"""\n'
            f"    param1, param2\n"
            f"):\n"
            f"    ...\n\n"
            f"CORRECT:\n"
            f"def {function_name}(param1, param2):\n"
            f'    """Docstring goes here, inside the body."""\n'
            f"    ...\n\n"
            f"Function to refactor:\n\n"
            f"{function_code}"
        )

        raw = self.generate(prompt, system, timeout=600)
        if not raw:
            return None

        return self._extract_python_from_llm_output(raw, function_name)

    @staticmethod
    def _extract_python_from_llm_output(raw: str, function_name: str) -> Optional[str]:
        """
        Robustly extract valid Python code from LLM output.

        Handles these common LLM output patterns:
          1. Pure code — returned correctly with no fences
          2. Single code fence — ```python ... ```
          3. Multiple code fences — LLM split helpers into separate blocks
          4. Explanation + code fence — "Here is the refactored code:\n```python..."
          5. Code fence with wrong language tag — ```py, ```Python, ``` (no tag)
          6. Trailing explanation after closing fence
          7. Docstring-wrapped output — LLM wrapped entire response in triple quotes

        Returns clean Python string or None if extraction fails.
        """
        import ast as _ast

        text = raw.strip()

        # ── Stage 1: Try raw output directly ─────────────────────────
        # If the LLM followed instructions perfectly, this works immediately
        cleaned = _LocalLLM_clean(text)
        if _is_valid_python(cleaned):
            return cleaned

        # ── Stage 2: Extract all ```...``` code blocks ────────────────
        # Handles single block, multiple blocks, wrong language tags
        import re as _re
        code_blocks = _re.findall(
            r"```(?:python|py|Python)?[^\n]*\n(.*?)```",
            text,
            flags=_re.DOTALL,
        )

        if code_blocks:
            # Join all blocks — LLM may have put helpers in separate fences
            joined = "\n\n".join(block.strip() for block in code_blocks)
            cleaned = _LocalLLM_clean(joined)
            if _is_valid_python(cleaned):
                return cleaned

            # Try each block individually — sometimes only one is valid
            for block in code_blocks:
                cleaned = _LocalLLM_clean(block.strip())
                if _is_valid_python(cleaned):
                    return cleaned

        # ── Stage 3: Find the first 'def' and extract from there ──────
        # Handles "Here is the code:\ndef my_func..." pattern
        match = _re.search(r"^(def |async def )", text, flags=_re.MULTILINE)
        if match:
            candidate = text[match.start():].strip()
            cleaned = _LocalLLM_clean(candidate)
            if _is_valid_python(cleaned):
                return cleaned

        # ── Stage 4: Strip leading/trailing triple quotes ─────────────
        # Handles LLM wrapping entire output in """..."""
        if text.startswith('"""') or text.startswith("'''"):
            inner = text[3:]
            end_q = inner.find('"""') if text.startswith('"""') else inner.find("'''")
            if end_q != -1:
                candidate = inner[:end_q].strip()
                if _is_valid_python(candidate):
                    return candidate

        # All stages failed
        logger.warning("Could not extract valid Python from LLM output. Preview: %r", text[:200])
        return None


def _LocalLLM_clean(text: str) -> str:
    """
    Strip ALL non-Python content from LLM output.

    Handles every known LLM output pattern:
      • "Here's the refactored code with complexity (21):\n```python\n..."
      • "```python\n...\n```"
      • "`python\n...\n`"   (single backtick)
      • Trailing sentences like "I simplified by extracting..."
      • Leading sentences before the first def/class
      • Mixed text + multiple code fences
    """
    import re as _re

    text = text.strip()

    # ── Step 1: Strip leading prose before first code fence or Python keyword ──
    # Walk lines from the top; discard every line that is pure prose until we
    # hit a code fence opener OR a line that starts a Python construct.
    # NOTE: do NOT use DOTALL here — we must stop at line boundaries.
    PYTHON_LEAD = (
        "def ", "async def ", "class ", "import ", "from ",
        "@",           # decorator
        "    ", "\t",  # indented body (e.g. helper already indented)
    )
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Code fence — keep from here
        if stripped.startswith("```") or stripped.startswith("`"):
            start = i
            break
        # Recognisable Python start keyword — keep from here
        if any(line.startswith(kw) for kw in PYTHON_LEAD):
            start = i
            break
        # Variable assignment at module level: NAME = ...
        if _re.match(r'^[A-Za-z_]\w*\s*=', line):
            start = i
            break
        # Otherwise it's prose — skip it
    text = "\n".join(lines[start:]).strip()

    # ── Step 2: Strip ALL code fence variants ─────────────────────────
    # Remove opening fences: ```python, ```py, ```Python, ```, `python
    text = _re.sub(r'^`{1,3}(?:python|py|Python)?\s*\n?', '', text, flags=_re.MULTILINE)
    # Remove closing fences: ``` or ` at end
    text = _re.sub(r'\n?`{1,3}\s*$', '', text, flags=_re.MULTILINE)
    text = text.strip()

    # ── Step 3: Strip trailing prose after last Python line ───────────
    # Walk backward, drop lines that aren't valid Python constructs
    PYTHON_STARTS = (
        "def ", "async def ", "class ", "return", "if ", "elif ",
        "else:", "for ", "while ", "try:", "except", "finally:",
        "with ", "import ", "from ", "yield", "raise", "pass",
        "break", "continue", "@", "    ", "\t",   # indented = body line
    )
    lines = text.splitlines()
    last = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if s[0].isdigit() or s[0] in '([{"\'"':
            last = i + 1
            break
        if any(s.startswith(kw) for kw in PYTHON_STARTS):
            last = i + 1
            break
        # Line is prose — keep searching upward
    text = "\n".join(lines[:last]).strip()

    return text


def _is_valid_python(code: str) -> bool:
    """Return True if code is non-empty and parses as valid Python."""
    import ast as _ast
    if not code or not code.strip():
        return False
    try:
        _ast.parse(code)
        return True
    except SyntaxError:
        return False

    def suggest_restructure(
        self,
        file_name: str,
        source_excerpt: str,
        functions: List[str],
        classes: List[str],
        loc: int,
    ) -> Dict:
        """Ask LLM how to split a large file. Returns a structured suggestion dict."""
        prompt = (
            f"Analyse this Python file and suggest how to split it into smaller modules.\n"
            f"File: {file_name}  |  Lines: {loc}\n"
            f"Functions: {functions}\nClasses: {classes}\n\n"
            f"Source (first 3000 chars):\n```python\n{source_excerpt[:3000]}\n```\n\n"
            f"Return a JSON object ONLY:\n"
            f'{{"recommendation":"...", "modules":[{{"name":"x.py","symbols":[],"reason":"..."}}]}}'
        )
        result = self.generate(prompt, timeout=300)   # 5 min — runs in background thread
        if result:
            try:
                return json.loads(result)
            except Exception:
                return {"recommendation": result, "modules": []}
        return {"recommendation": "Ollama unavailable", "modules": []}


# ── Module-level singleton (lazy) ────────────────────────────────────────────
_llm: Optional[LocalLLM] = None

def get_llm() -> LocalLLM:
    global _llm
    if _llm is None:
        _llm = LocalLLM()
    return _llm


# ─────────────────────────────────────────────────────────────────────────────
# RegistryActionExecutor
# ─────────────────────────────────────────────────────────────────────────────

class RegistryActionExecutor:
    """
    Executes all action types exclusively through FileRegistry.

    Contract
    ─────────
    • Never reads a line number from Action.target (those may be stale).
    • Always resolves the current position via FileSnapshot.symbols or
      FileSnapshot.find_line_by_content() just before the edit.
    • Every mutation goes through FileEditor which triggers _reindex(),
      keeping the registry consistent for subsequent actions in the same batch.
    • flush() is called after each successful action so disk matches memory.
    """

    def __init__(self, dry_run: bool = False, backup_dir: Optional[str] = None):
        self.dry_run = dry_run
        self.backup  = BackupManager(backup_dir)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute(self, action: Action) -> Dict[str, Any]:
        handlers = {
            ActionType.DELETE_IMPORT:      self._delete_import,
            ActionType.DELETE_FUNCTION:    self._delete_function,
            ActionType.ADD_DOCSTRING:      self._add_docstring,
            ActionType.FIX_SECURITY:       self._fix_security,
            ActionType.REFACTOR_CODE:      self._refactor_code,
            ActionType.RESTRUCTURE:        self._restructure,
            ActionType.MOVE_FILE:          self._move_file,
            ActionType.UPDATE_DEPENDENCY:  self._update_dependency,
        }
        handler = handlers.get(action.action_type)
        if not handler:
            return {"success": False, "error": f"No handler: {action.action_type}"}

        try:
            return handler(action)
        except Exception as exc:
            import traceback
            return {
                "success":   False,
                "error":     str(exc),
                "traceback": traceback.format_exc(),
            }

    # ------------------------------------------------------------------
    # Target parsing
    # ------------------------------------------------------------------

    def _parse_target(self, target: str) -> Tuple[str, str, str]:
        """
        Decode "file/path@entity_type:entity_name"
        or legacy "file/path:line_number".
        Returns (resolved_abs_path, entity_type, entity_name).
        """
        if "@" in target:
            file_part, rest = target.split("@", 1)
            entity_type, entity_name = rest.split(":", 1)
            return str(Path(file_part).resolve()), entity_type, entity_name

        # Legacy format
        parts = target.split(":")
        return str(Path(parts[0]).resolve()), "line", parts[1] if len(parts) > 1 else ""

    def _load(self, file_path: str) -> Optional[FileSnapshot]:
        snap = registry.get(file_path)
        if snap and snap.parse_error:
            logger.warning("Parse warning in %s: %s", Path(file_path).name, snap.parse_error)
        return snap

    # ══════════════════════════════════════════════════════════════════
    # 1. DELETE IMPORT
    # ══════════════════════════════════════════════════════════════════

    def _delete_import(self, action: Action) -> Dict[str, Any]:
        file_path, _, entity_name = self._parse_target(action.target)
        logger.info("Delete import '%s' ← %s", entity_name, Path(file_path).name)

        snap = self._load(file_path)
        if not snap:
            return {"success": False, "error": f"Cannot load {file_path}"}

        sym = snap.symbols.imports.get(entity_name)
        if not sym:
            return {"success": False, "error": f"Import '{entity_name}' not found (already removed?)"}

        line_num     = sym.start_line
        line_content = snap.get_line(line_num) or ""

        # ── Determine whether to delete the whole line or just one name ──
        # If the import line contains multiple names, only remove the unused
        # one and rewrite the line — preserving the other still-used names.
        # If it is the only import on that line, delete the whole line.
        trimmed, whole_line = self._remove_name_from_import_line(
            line_content, entity_name, sym
        )

        if self.dry_run:
            if whole_line:
                msg = f"Would delete entire line {line_num}: {line_content.strip()}"
            else:
                msg = f"Would rewrite line {line_num}: {line_content.strip()!r} → {trimmed.strip()!r}"
            return {"success": True, "dry_run": True, "message": msg}

        self.backup.create_backup(file_path)

        if whole_line:
            # Only import on this line — delete the entire line
            ok = FileEditor.delete_lines(snap, line_num, line_num)
            action_desc = "deleted_line"
            result_detail = line_content.strip()
        else:
            # Multiple imports — rewrite line without the unused name
            ok = FileEditor.replace_lines(snap, line_num, line_num, [trimmed])
            action_desc = "rewritten_line"
            result_detail = trimmed.strip()

        if ok:
            registry.flush(file_path)
            return {
                "success":      True,
                "action":       "delete_import",
                "file":         file_path,
                "import_name":  entity_name,
                action_desc:    result_detail,
                "whole_line":   whole_line,
                "lines_now":    snap.total_lines(),
            }
        return {"success": False, "error": "FileEditor operation failed"}

    @staticmethod
    def _remove_name_from_import_line(
        line: str,
        name: str,
        sym,
    ):
        """
        Remove a single import name from a potentially multi-name import line.

        Returns (new_line: str, whole_line: bool).
          whole_line=True  → caller should delete the entire line
          whole_line=False → caller should replace the line with new_line

        Handles all import forms:
          import os, sys, json                  → import os, sys
          from typing import List, Dict, Any    → from typing import List, Dict
          import numpy as np, pandas as pd      → import pandas as pd
          from os import path as p, getcwd      → from os import getcwd
        """
        stripped = line.rstrip("\n").rstrip()
        indent   = line[: len(line) - len(line.lstrip())]

        # ── Case 1: "import a, b, c" ──────────────────────────────────
        plain = re.match(r"^(\s*import\s+)(.+)$", stripped)
        if plain and "from " not in stripped:
            prefix  = plain.group(1)
            names   = plain.group(2)
            parts   = [p.strip() for p in names.split(",")]

            # Build canonical name for each part (handle aliases)
            def canonical(part):
                m = re.match(r"(\w+)(?:\s+as\s+(\w+))?", part.strip())
                if m:
                    return m.group(2) or m.group(1)   # alias if present, else name
                return part.strip()

            remaining = [p for p in parts if canonical(p) != name]
            if not remaining:
                return (stripped, True)   # only import → delete whole line
            new_line = prefix + ", ".join(remaining) + "\n"
            return (new_line, False)

        # ── Case 2: "from module import a, b, c" ─────────────────────
        frm = re.match(r"^(\s*from\s+[\w.]+\s+import\s+)(.+)$", stripped)
        if frm:
            prefix = frm.group(1)
            names  = frm.group(2)
            parts  = [p.strip() for p in names.split(",")]

            def canonical(part):
                m = re.match(r"(\w+)(?:\s+as\s+(\w+))?", part.strip())
                if m:
                    return m.group(2) or m.group(1)
                return part.strip()

            remaining = [p for p in parts if canonical(p) != name]
            if not remaining:
                return (stripped, True)   # only import → delete whole line
            new_line = prefix + ", ".join(remaining) + "\n"
            return (new_line, False)

        # ── Fallback: can't parse → delete whole line ─────────────────
        return (stripped, True)

    # ══════════════════════════════════════════════════════════════════
    # 2. DELETE FUNCTION
    # ══════════════════════════════════════════════════════════════════

    def _delete_function(self, action: Action) -> Dict[str, Any]:
        file_path, _, entity_name = self._parse_target(action.target)
        logger.info("Delete function '%s' ← %s", entity_name, Path(file_path).name)

        snap = self._load(file_path)
        if not snap:
            return {"success": False, "error": f"Cannot load {file_path}"}

        sym = snap.symbols.functions.get(entity_name)
        if not sym:
            return {"success": False, "error": f"Function '{entity_name}' not found (already removed?)"}

        start, end = sym.start_line, sym.end_line

        # Include preceding blank separator line so we don't leave gaps
        actual_start = start
        if start > 1:
            prev = snap.get_line(start - 1) or ""
            if prev.strip() == "":
                actual_start = start - 1

        if self.dry_run:
            return {
                "success": True, "dry_run": True,
                "message": f"Would delete '{entity_name}' lines {actual_start}–{end}",
            }

        self.backup.create_backup(file_path)
        ok = FileEditor.delete_lines(snap, actual_start, end)
        if ok:
            registry.flush(file_path)
            return {
                "success":      True,
                "action":       "delete_function",
                "file":         file_path,
                "function":     entity_name,
                "lines_removed": end - actual_start + 1,
                "lines_now":    snap.total_lines(),
            }
        return {"success": False, "error": "FileEditor.delete_lines failed"}

    # ══════════════════════════════════════════════════════════════════
    # 3. ADD DOCSTRING  (via local LLM)
    # ══════════════════════════════════════════════════════════════════

    def _add_docstring(self, action: Action) -> Dict[str, Any]:
        file_path, entity_type, entity_name = self._parse_target(action.target)
        logger.info("Add docstring → '%s' (%s) ← %s", entity_name, entity_type, Path(file_path).name)

        snap = self._load(file_path)
        if not snap:
            return {"success": False, "error": f"Cannot load {file_path}"}

        # ── Resolve symbol — check BOTH functions and classes ─────────
        # entity_type from the coordinator may say "function" even when
        # the DocumentationAgent flagged a class (coordinator defaults
        # unknown kinds to "function"). Check both buckets so the executor
        # never fails purely due to a wrong entity_type in the target.
        sym = snap.symbols.functions.get(entity_name)
        resolved_kind = "function"

        if sym is None:
            sym = snap.symbols.classes.get(entity_name)
            resolved_kind = "class"

        if sym is None:
            return {
                "success": False,
                "error": (
                    f"'{entity_name}' not found as function or class in "
                    f"{Path(file_path).name}. It may have already been removed."
                ),
            }

        # ── Skip __init__ when parent class already has a docstring ───
        if entity_name == "__init__" and resolved_kind == "function":
            parent_class = self._find_parent_class(snap, sym.start_line)
            if parent_class:
                cls_sym = snap.symbols.classes.get(parent_class)
                if cls_sym and cls_sym.has_docstring:
                    return {
                        "success": False,
                        "skipped": True,
                        "reason":  (
                            f"__init__ skipped — parent class '{parent_class}' "
                            f"already has a docstring"
                        ),
                    }

        if sym.has_docstring:
            # Not a failure — refactor step may have added a docstring already.
            # Return skipped=True so the sub-action loop treats it as success
            # and the UI shows "already documented" rather than "failed".
            return {
                "success": True,
                "skipped": True,
                "reason":  (
                    f"'{entity_name}' already has a docstring "
                    f"(likely added by the refactor step)"
                ),
            }

        start, end = sym.start_line, sym.end_line
        code_block = "".join(snap.lines[start - 1 : end])

        if self.dry_run:
            return {
                "success": True, "dry_run": True,
                "message": (
                    f"Would add AI docstring to {resolved_kind} "
                    f"'{entity_name}' at line {start}"
                ),
            }

        # ── Generate via Ollama ───────────────────────────────────────
        logger.info("Generating docstring for %s '%s'", resolved_kind, entity_name)
        raw_docstring = get_llm().generate_docstring(code_block, entity_name)

        # ── Determine indentation ─────────────────────────────────────
        def_line = snap.get_line(start) or ""
        base_indent = len(def_line) - len(def_line.lstrip())
        body_indent = " " * (base_indent + 4)

        # ── Format docstring lines ────────────────────────────────────
        inner = raw_docstring[3:-3] if raw_docstring.startswith('"""') else raw_docstring
        inner_lines = inner.split("\n")

        if len(inner_lines) == 1:
            formatted = [f'{body_indent}"""{inner_lines[0].strip()}"""\n']
        else:
            formatted = [f'{body_indent}"""{inner_lines[0]}\n']
            for ln in inner_lines[1:]:
                stripped = ln.strip()
                formatted.append(f"{body_indent}{stripped}\n" if stripped else "\n")
            formatted.append(f'{body_indent}"""\n')

        self.backup.create_backup(file_path)

        # Insert after the def/class line
        ok = FileEditor.insert_lines(snap, start, formatted)
        if ok:
            registry.flush(file_path)
            return {
                "success":           True,
                "action":            "add_docstring",
                "file":              file_path,
                "entity":            entity_name,
                "kind":              resolved_kind,
                "docstring_preview": inner_lines[0][:80].strip(),
                "lines_inserted":    len(formatted),
            }
        return {"success": False, "error": "FileEditor.insert_lines failed"}

    # ── Refactor helper methods ──────────────────────────────────────

    @staticmethod
    def _split_refactored_output(
        refactored: str,
        target_name: str,
    ):
        """
        Split LLM refactored output into (helpers_code, main_func_code).

        The LLM often returns multiple function definitions when it extracts
        helpers. We parse the AST to identify which definition is the target
        function and which are helpers that need to be inserted before it.

        Returns:
            helpers_code  — string of all non-target definitions (may be empty)
            main_func_code — string of just the target function definition
        """
        try:
            tree = ast.parse(refactored)
        except SyntaxError:
            # Can't split — return entire output as main function
            return ("", refactored)

        lines = refactored.splitlines(keepends=True)
        top_level = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]

        if len(top_level) <= 1:
            # Only one definition — no splitting needed
            return ("", refactored)

        # Find the target function node
        target_node = None
        for node in top_level:
            if node.name == target_name:
                target_node = node
                break

        if target_node is None:
            # Target not found by name — treat last definition as main
            target_node = top_level[-1]

        # Extract target function lines (1-indexed in the refactored string)
        t_start = target_node.lineno - 1        # 0-indexed
        t_end   = target_node.end_lineno        # exclusive

        main_func_lines   = lines[t_start:t_end]
        helper_lines_list = lines[:t_start]

        main_func_code = "".join(main_func_lines).strip()
        helpers_code   = "".join(helper_lines_list).strip()

        return (helpers_code, main_func_code)

    @staticmethod
    def _detect_indent(snap, start_line: int) -> int:
        """
        Return the column indentation of a function at start_line.
        Module-level functions have indent=0, class methods have indent=4.
        """
        line = snap.get_line(start_line) or ""
        return len(line) - len(line.lstrip())

    @staticmethod
    def _indent_code_block(code: str, indent: int) -> list:
        """
        Re-indent a code block string to the given column offset.
        Returns a list of lines with newlines, ready for FileEditor.

        If the code is already at indent=0 (most LLM output is),
        adds the required prefix to each line.
        If the code already has the correct indent, returns as-is.
        """
        if not code.strip():
            return []

        lines = code.splitlines(keepends=False)
        if not lines:
            return []

        # Detect current indentation of the block (first non-empty line)
        current_indent = 0
        for ln in lines:
            if ln.strip():
                current_indent = len(ln) - len(ln.lstrip())
                break

        delta = indent - current_indent
        result = []
        for ln in lines:
            if ln.strip():
                # Re-indent non-empty lines
                stripped = ln.lstrip()
                current_line_indent = len(ln) - len(stripped)
                new_indent = max(0, current_line_indent + delta)
                result.append(" " * new_indent + stripped + "\n")
            else:
                result.append("\n")
        return result

    @staticmethod
    def _find_parent_class(snap, method_line: int) -> Optional[str]:
        """Return the class name containing method_line, or None."""
        for cls_name, cls_sym in snap.symbols.classes.items():
            if cls_sym.start_line < method_line <= cls_sym.end_line:
                return cls_name
        return None

    # ══════════════════════════════════════════════════════════════════
    # 4. FIX SECURITY  — extract to .env.example + replace with os.getenv
    # ══════════════════════════════════════════════════════════════════

    def _fix_security(self, action: Action) -> Dict[str, Any]:
        file_path, entity_type, entity_name = self._parse_target(action.target)

        snap = self._load(file_path)
        if not snap:
            return {"success": False, "error": f"Cannot load {file_path}"}

        credential_hint = action.impact_analysis.get("credential_line", "").strip()

        # ══ Resolve current line number ═══════════════════════════════════════
        #
        # Path A  @variable:VAR_NAME  — AST-based, always current.
        #   The SymbolTable is rebuilt after every FileEditor mutation, so
        #   snap.symbols.assignments[VAR_NAME].start_line is always the live
        #   line — immune to shifts from delete_function / delete_import above.
        #
        # Path B  @line:N  — legacy format produced by old reviews.
        #   Uses a three-stage fallback:
        #     B1. stored line + hint_var match  (fast, no search)
        #     B2. find_line_by_content from line 1  (handles large shifts)
        #     B3. already-fixed check  (variable was already moved to os.getenv)

        actual_line_num: Optional[int] = None
        line_content:    Optional[str] = None
        hint_var: str = ""

        if entity_type == "variable":
            # ── Path A: AST lookup ────────────────────────────────────────────
            hint_var = entity_name
            asgn = snap.symbols.assignments.get(entity_name)

            if asgn is None:
                # Not in assignments — either inside a function or already fixed.
                # os.getenv() is an ast.Call, not a Constant, so it's no longer
                # tracked as a plain assignment after the fix is applied.
                already = (
                    snap.find_line_by_content(f"{entity_name} = os.getenv", search_from=1)
                    or snap.find_line_by_content(f"{entity_name} = os.environ", search_from=1)
                )
                if already:
                    return {
                        "success": True,
                        "action":  "fix_security_skipped",
                        "file":    file_path,
                        "line":    already,
                        "note":    f"'{entity_name}' already uses os.getenv/os.environ — already fixed",
                    }
                return {
                    "success": False,
                    "error":   f"Variable '{entity_name}' not found in assignments table",
                }

            actual_line_num = asgn.start_line
            line_content    = snap.get_line(actual_line_num)
            logger.info(
                "Fix security ← %s:%d (AST variable '%s')",
                Path(file_path).name, actual_line_num, entity_name,
            )

            # Quick already-fixed check before touching anything
            if line_content and (
                "os.getenv" in line_content or "os.environ" in line_content
            ):
                return {
                    "success": True,
                    "action":  "fix_security_skipped",
                    "file":    file_path,
                    "line":    actual_line_num,
                    "note":    f"'{entity_name}' already uses os.getenv/os.environ — already fixed",
                }

        else:
            # ── Path B: legacy @line:N ────────────────────────────────────────
            try:
                stored_line_num = int(entity_name)
            except (ValueError, TypeError):
                return {"success": False, "error": f"Invalid line reference: {entity_name}"}

            logger.info("Fix security ← %s:%d (legacy line target)", Path(file_path).name, stored_line_num)

            hint_var = (
                credential_hint.split()[0].rstrip("=:")
                if credential_hint and credential_hint.split()
                else ""
            )

            # B1 — stored line, both gates must pass
            direct_line = snap.get_line(stored_line_num)
            direct_matches_hint = (not hint_var) or (hint_var in (direct_line or ""))
            if direct_line and self._parse_credential_line(direct_line) and direct_matches_hint:
                actual_line_num = stored_line_num
                line_content    = direct_line
                logger.debug("Credential at stored line %d: %s", stored_line_num, direct_line.strip()[:60])
            else:
                actual_line_num = stored_line_num
                line_content    = direct_line or ""

                if credential_hint:
                    # B2 — full-file content search (handles large line shifts)
                    found = snap.find_line_by_content(credential_hint, search_from=1)

                    if found:
                        found_line = snap.get_line(found) or ""
                        found_is_valid = (
                            self._parse_credential_line(found_line) is not None
                            or (hint_var and hint_var in found_line)
                        )
                        if found_is_valid:
                            if found != stored_line_num:
                                logger.debug("Line shifted: %d → %d", stored_line_num, found)
                            actual_line_num = found
                            line_content    = found_line
                        else:
                            logger.warning(
                                "find_line_by_content returned line %d but invalid — "
                                "falling back to stored line %d", found, stored_line_num,
                            )
                            actual_line_num = stored_line_num
                            line_content    = direct_line or ""
                    else:
                        # B3 — hint text gone; check if variable was already fixed
                        if hint_var:
                            already_fixed_line = (
                                snap.find_line_by_content(f"{hint_var} = os.getenv", search_from=1)
                                or snap.find_line_by_content(f"{hint_var} = os.environ", search_from=1)
                            )
                            if already_fixed_line:
                                logger.info(
                                    "'%s' already uses os.getenv/os.environ at line %d — skipping",
                                    hint_var, already_fixed_line,
                                )
                                if not self.dry_run:
                                    self.backup.create_backup(file_path)
                                return {
                                    "success": True,
                                    "action":  "fix_security_skipped",
                                    "file":    file_path,
                                    "line":    already_fixed_line,
                                    "note": (
                                        f"'{hint_var}' already uses os.getenv / os.environ "
                                        f"(found at line {already_fixed_line}) — already fixed"
                                    ),
                                }

                        logger.warning(
                            "Could not relocate credential anywhere in file "
                            "(stored line %d) — using stored line as last resort",
                            stored_line_num,
                        )

        if not line_content:
            return {
                "success": False,
                "error": (
                    f"Line {actual_line_num} not found in registry "
                    f"(file has {snap.total_lines()} lines)"
                ),
            }

        logger.debug("Target line %d: %s", actual_line_num, line_content.strip()[:80])

        # ── Parse credential ──────────────────────────────────────────
        parsed = self._parse_credential_line(line_content)

        if self.dry_run:
            if parsed:
                var_name, _, _ = parsed
                return {
                    "success": True, "dry_run": True,
                    "message": (
                        f"Would extract '{var_name}' to .env.example "
                        f"and replace with os.getenv()"
                    ),
                }
            return {
                "success": True, "dry_run": True,
                "message": f"Would comment out security issue at line {actual_line_num}",
            }

        self.backup.create_backup(file_path)

        if not parsed:
            # Already fixed in a prior run — don't touch it again
            if "os.getenv" in line_content or "os.environ" in line_content:
                return {
                    "success": True,
                    "action":  "fix_security_skipped",
                    "file":    file_path,
                    "line":    actual_line_num,
                    "note":    "Line already uses os.getenv / os.environ — no change needed",
                }

            # insecure_code pattern (exec/eval/shell=True/pickle) — comment is the right fix
            _INSECURE_PATS = (
                re.compile(r"\bexec\s*\("),
                re.compile(r"\beval\s*\("),
                re.compile(r"\bpickle\.loads?\s*\("),
                re.compile(r"shell\s*=\s*True"),
            )
            _INSECURE_MSGS = {
                r"\bexec\s*\(":           "exec() allows arbitrary code execution — validate or replace with safe alternative",
                r"\beval\s*\(":           "eval() allows arbitrary code execution — validate or replace with safe alternative",
                r"\bpickle\.loads?\s*\(": "pickle.load/loads can execute arbitrary code — use json or safer format",
                r"shell\s*=\s*True":      "shell=True in subprocess is vulnerable to injection — pass args as a list",
            }
            for pat, msg in _INSECURE_MSGS.items():
                if re.search(pat, line_content):
                    return self._comment_out_security(snap, file_path, actual_line_num, line_content, reason=msg)

            # Generic credential line that didn't parse (e.g. complex expression)
            return self._comment_out_security(snap, file_path, actual_line_num, line_content)

        var_name, secret_value, _ = parsed
        env_var_name = re.sub(r"[^A-Z0-9_]", "_", var_name.upper())

        # Step 1 — write .env.example
        env_result = self._write_env_example(file_path, env_var_name, secret_value, var_name)

        # Step 2 — ensure `import os` (this may shift lines by +1)
        os_added = self._ensure_os_import(snap)

        # Step 3 — re-locate our target line after possible os-import shift
        if os_added:
            # Search near (not from top) to avoid false early matches
            search_from = max(1, actual_line_num - 3)
            found = snap.find_line_by_content(line_content.strip(), search_from=search_from)
            if found:
                actual_line_num = found

        # Step 4 — replace credential line with os.getenv()
        indent = len(line_content) - len(line_content.lstrip())
        indent_str = " " * indent
        replacement = f"{indent_str}{var_name} = os.getenv(\"{env_var_name}\")\n"

        ok = FileEditor.replace_lines(snap, actual_line_num, actual_line_num, [replacement])
        if ok:
            registry.flush(file_path)

        return {
            "success":          ok,
            "action":           "fix_security",
            "file":             file_path,
            "variable":         var_name,
            "env_var":          env_var_name,
            "replacement":      replacement.strip(),
            "env_file":         env_result.get("env_file"),
            "env_file_created": env_result.get("created", False),
            "import_os_added":  os_added,
        }

    def _comment_out_security(
        self,
        snap: FileSnapshot,
        file_path: str,
        line_num: int,
        original_line: str,
        reason: str = "Hardcoded secret detected — move to environment variable",
    ) -> Dict[str, Any]:
        """Fallback when we can't parse the credential — just comment it out."""
        indent = len(original_line) - len(original_line.lstrip())
        pad = " " * indent
        replacement = [
            f"{pad}# SECURITY WARNING: {reason}\n",
            f"{pad}# {original_line.lstrip()}",
        ]
        ok = FileEditor.replace_lines(snap, line_num, line_num, replacement)
        if ok:
            registry.flush(file_path)
        return {
            "success": ok,
            "action":  "fix_security_comment",
            "file":    file_path,
            "line":    line_num,
            "note":    "Could not parse credential — commented out with warning",
        }

    @staticmethod
    def _parse_credential_line(line: str) -> Optional[Tuple[str, str, str]]:
        """
        Parse a line and return (var_name, value, quote_char) if it looks
        like a hardcoded credential assignment. Returns None otherwise.

        Two-gate approach:
          Gate 1 — Variable name gate:
            The variable name must contain a credential-related keyword
            (case-insensitive). This filters out false positives like
            STATUS = "active", VERSION = "1.0.0", ENV = "development".

          Gate 2 — Value gate (applied when name gate fails):
            If the name doesn't look credential-like, the value itself
            must be long (> 12 chars) AND contain complexity markers
            (mixed case + digits, or special chars like -/_/.).
            This catches things like db_url = "postgresql://user:pass@host".

        Combined, these two gates eliminate:
          - VERSION = "1.0.0"         (name not credential-like, value too short/simple)
          - STATUS = "active"         (name not credential-like, value too short)
          - ENV = "development"       (name not credential-like, value too simple)
          - CHUNK_SIZE = 8192         (not a string — skipped by outer pattern)
          - MAX_RETRIES = 3           (not a string — skipped by outer pattern)

        While correctly matching:
          - API_KEY = "sk-proj-abc"   (name gate: contains "key")
          - smtp_password = "Pass@1"  (name gate: contains "password")
          - DATABASE_URL = "postgres://..."  (name gate: contains "url")
          - jwt_secret = "abcXYZ123"  (name gate: contains "secret")
        """
        # Outer pattern: must be a string assignment (quoted value).
        # (?:\s*#.*)? allows an optional trailing inline comment like  # noqa
        pattern = r"""^\s*([\w_]+)\s*(?::\s*[\w\[\], ]+)?\s*=\s*(['"])(.*?)\2\s*(?:#.*)?$"""
        m = re.match(pattern, line.strip())
        if not m:
            return None

        var_name    = m.group(1)
        quote_char  = m.group(2)
        value       = m.group(3)
        var_lower   = var_name.lower()

        # ── Gate 1: Variable name contains a credential keyword ────────
        CREDENTIAL_KEYWORDS = {
            "key", "secret", "password", "passwd", "pwd", "token",
            "auth", "api", "jwt", "dsn", "url", "host", "endpoint",
            "credential", "private", "signing", "webhook", "stripe",
            "sendgrid", "twilio", "aws", "gcp", "azure", "github",
            "slack", "discord", "smtp", "ftp", "ssh", "ssl", "cert",
            "access", "refresh", "client", "server", "database", "db",
            "redis", "mongo", "postgres", "mysql", "sentry", "rollbar",
        }
        name_is_credential = any(kw in var_lower for kw in CREDENTIAL_KEYWORDS)

        if name_is_credential:
            return (var_name, value, quote_char)

        # ── Gate 2: Value complexity check (catches generic-named vars) ─
        # Value must be long enough and look like a real credential,
        # not a simple word, version number, or environment name.
        if len(value) < 12:
            return None   # too short to be a real secret

        # Must contain complexity: not just lowercase letters/dots/digits
        # A real credential typically has: uppercase + lowercase + digits,
        # or special characters like - / _ @ : that appear in keys/urls
        has_upper   = any(c.isupper() for c in value)
        has_lower   = any(c.islower() for c in value)
        has_digit   = any(c.isdigit() for c in value)
        has_special = any(c in "-_/@:!#$%^&*" for c in value)

        is_complex = (
            (has_upper and has_lower and has_digit) or   # mixed case + digits
            (has_special and len(value) > 16) or          # special chars + long
            (has_upper and has_digit and len(value) > 20) # all-caps + digits + very long
        )

        # Exclude obvious non-secrets even if long
        is_simple_word = re.match(r"^[a-z_]+$", value)      # all lowercase letters
        is_version     = re.match(r"^[\d]+\.[\d.]+$", value)  # "1.2.3" pattern
        is_env_name    = re.match(r"^(development|production|staging|test|debug|local)$",
                                  value, re.I)

        if is_simple_word or is_version or is_env_name:
            return None

        if is_complex:
            return (var_name, value, quote_char)

        return None

    def _write_env_example(
        self,
        source_file: str,
        env_var: str,
        real_value: str,
        original_var: str,
    ) -> Dict[str, Any]:
        """
        Write env files for an extracted credential:
          • .env.example  — placeholder value (safe to commit to git)
          • .env           — real secret value  (gitignored, stays local)

        Both files are idempotent: if the variable already exists the file
        is left unchanged.  .env is also added to .gitignore automatically.
        """
        # Walk up from source file to find the repo root.
        # Accept the first directory that contains any project-root marker.
        # If nothing found within 6 levels, use the source file's own directory
        # so we never write env files to an unrelated parent directory.
        _ROOT_MARKERS = {
            ".git", "pyproject.toml", "setup.py", "setup.cfg",
            "requirements.txt", "Pipfile", "poetry.lock",
        }
        src_dir = Path(source_file).parent
        root = src_dir
        for candidate in [src_dir, *src_dir.parents]:
            if any((candidate / m).exists() for m in _ROOT_MARKERS):
                root = candidate
                break
            if src_dir != candidate and len(candidate.parts) <= len(src_dir.parts) - 6:
                break

        source_name = Path(source_file).name
        result: Dict[str, Any] = {}

        # ── .env.example — placeholder only ──────────────────────────────
        # example_path = root / ".env.example"
        # example_created = not example_path.exists()
        # existing_example = example_path.read_text(encoding="utf-8") if not example_created else ""

        # if env_var not in existing_example:
        #     placeholder = self._make_placeholder(env_var, real_value)
        #     with open(example_path, "a", encoding="utf-8") as f:
        #         if existing_example and not existing_example.endswith("\n"):
        #             f.write("\n")
        #         f.write(f"\n# Extracted from {source_name} by code-repo-manager\n")
        #         f.write(f"{env_var}={placeholder}\n")
        #     logger.info(
        #         "%s .env.example: %s=%s",
        #         "Created" if example_created else "Updated", env_var, placeholder,
        #     )
        #     result["env_example_updated"] = True
        # else:
        #     result["env_example_updated"] = False

        # result["env_file"] = str(example_path)
        # result["created"]  = example_created

        # ── .env — real secret value ──────────────────────────────────────
        env_path = root / ".env"
        env_created = not env_path.exists()
        existing_env = env_path.read_text(encoding="utf-8") if not env_created else ""

        if env_var not in existing_env:
            with open(env_path, "a", encoding="utf-8") as f:
                if existing_env and not existing_env.endswith("\n"):
                    f.write("\n")
                f.write(f"\n# Extracted from {source_name} by code-repo-manager\n")
                f.write(f"{env_var}={real_value}\n")
            logger.info(
                "%s .env: %s=<secret>",
                "Created" if env_created else "Updated", env_var,
            )
            result["env_real_updated"] = True
        else:
            result["env_real_updated"] = False

        result["env_real_file"] = str(env_path)

        # ── .gitignore — ensure .env is ignored ──────────────────────────
        gitignore_path = root / ".gitignore"
        self._ensure_gitignore_entry(gitignore_path, ".env")

        return result

    @staticmethod
    def _ensure_gitignore_entry(gitignore_path: Path, entry: str) -> None:
        """Add `entry` to .gitignore if not already present."""
        existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
        # Match as a whole line to avoid partial matches
        if re.search(rf"^{re.escape(entry)}\s*$", existing, re.MULTILINE):
            return
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")
        logger.info(".gitignore: added %s", entry)

    @staticmethod
    def _make_placeholder(var_name: str, real_value: str) -> str:
        n = var_name.lower()
        if "key" in n:     return "your_api_key_here"
        if "secret" in n:  return "your_secret_here"
        if "password" in n or "pwd" in n: return "your_password_here"
        if "token" in n:   return "your_token_here"
        if "url" in n or "host" in n:     return "your_url_here"
        return f"your_{n}_here"

    def _ensure_os_import(self, snap: FileSnapshot) -> bool:
        """
        Insert 'import os' at the top of the file if not already present.
        Returns True if actually inserted.
        """
        if snap.has_import("os"):
            return False

        # Find insert point: after any module docstring / existing imports
        insert_after = 0
        for i, line in enumerate(snap.lines[:15]):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                insert_after = i   # insert before first import block
                break
            if stripped and not stripped.startswith("#") \
               and not stripped.startswith('"""') \
               and not stripped.startswith("'''"):
                insert_after = i
                break

        FileEditor.insert_lines(snap, insert_after, ["import os\n"])
        logger.debug("Injected 'import os' at line %d", insert_after + 1)
        return True

    # ══════════════════════════════════════════════════════════════════
    # 5. REFACTOR CODE  (via local LLM)
    # ══════════════════════════════════════════════════════════════════

    def _refactor_code(self, action: Action) -> Dict[str, Any]:
        """
        Refactor a complex function or method via local LLM.

        Both standalone functions and class methods are stored identically
        in SymbolTable.functions (ast.walk captures all FunctionDef nodes
        regardless of nesting depth). There is no distinction between
        "function" and "method" at the executor level — both are looked up
        by name in snap.symbols.functions and replaced the same way.

        parent_class is carried purely for display context and LLM prompt
        enrichment — it does not change the lookup or edit path.
        """
        file_path, _, entity_name = self._parse_target(action.target)
        parent_class = action.impact_analysis.get("parent_class")

        # Display label — purely cosmetic, no logic difference
        display = f"'{entity_name}'"
        if parent_class:
            display = f"'{entity_name}' (in class '{parent_class}')"
        logger.info("Refactor %s ← %s", display, Path(file_path).name)

        snap = self._load(file_path)
        if not snap:
            return {"success": False, "error": f"Cannot load {file_path}"}

        # ── Symbol lookup ─────────────────────────────────────────────
        # SymbolTable.functions covers ALL FunctionDef nodes:
        #   standalone functions, class methods, static methods, etc.
        # Single lookup path — no method/function distinction needed.
        sym = snap.symbols.functions.get(entity_name)

        # Line-number fallback when entity_name is "line_N" or was not
        # found by name (e.g. coordinator encoded it as a line reference)
        if not sym:
            stored_line = action.impact_analysis.get("line_number")
            if stored_line:
                for fn_name, fn_sym in snap.symbols.functions.items():
                    if fn_sym.start_line == stored_line:
                        sym = fn_sym
                        entity_name = fn_name
                        logger.debug("Resolved by line %d → '%s'", stored_line, entity_name)
                        break

        if not sym:
            return {
                "success": False,
                "error": (
                    f"'{entity_name}' not found in {Path(file_path).name}. "
                    f"Available: {list(snap.symbols.functions.keys())[:10]}"
                ),
            }

        start, end    = sym.start_line, sym.end_line
        complexity    = sym.complexity
        function_code = "".join(snap.lines[start - 1 : end])

        if self.dry_run:
            return {
                "success": True, "dry_run": True,
                "message": (
                    f"Would refactor {display} "
                    f"(complexity={complexity}, lines {start}–{end})"
                ),
            }

        logger.info("Refactoring complexity: %s  |  lines: %d–%d", complexity, start, end)

        llm = get_llm()
        if not llm.available:
            return {
                "success":    False,
                "error":      "Local LLM (Ollama) not available for refactoring",
                "suggestion": "Run: ollama pull deepseek-coder:6.7b",
                "manual":     f"Refactor {display} at lines {start}–{end}, complexity={complexity}",
            }

        logger.info("Sending to LLM model: %s", llm.model)
        refactored = llm.refactor_function(function_code, entity_name, complexity)

        if not refactored:
            return {"success": False, "error": "LLM returned empty response"}

        # Guard: detect docstring-in-signature pattern before ast.parse
        # e.g. def func(\n    """..."""\n    params\n):
        import re as _re
        if _re.search(rf'def\s+{re.escape(entity_name)}\s*\([^)]*"""', refactored, _re.DOTALL):
            logger.warning(
                "LLM mixed docstring into function signature for '%s' — rejecting output",
                entity_name,
            )
            return {
                "success": False,
                "error":   (
                    f"LLM placed a docstring inside the function signature of '{entity_name}'. "
                    "The function signature must contain only parameters. "
                    "Retry or refactor manually."
                ),
                "raw_output": refactored[:300],
            }

        # Validate refactored output is parseable Python before writing
        try:
            ast.parse(refactored)
        except SyntaxError as e:
            return {
                "success":    False,
                "error":      f"LLM returned invalid Python: {e}",
                "raw_output": refactored[:300],
            }

        self.backup.create_backup(file_path)

        # ── Split LLM output into helper functions + main function ────────
        # The LLM may return helper functions alongside the refactored method.
        # helpers_code = standalone helpers (always go at module level)
        # main_func    = the refactored method/function itself
        helpers_code, main_func = self._split_refactored_output(
            refactored, entity_name
        )

        # ── Detect original indentation from the function's first line ──────
        # This handles both standalone functions (indent=0) and class methods
        # (indent=4 or more) without relying on parent_class being set.
        original_first_line = snap.lines[start - 1] if start <= len(snap.lines) else ""
        original_indent = len(original_first_line) - len(original_first_line.lstrip())

        # If parent_class not in impact_analysis, look it up from the symbol
        # table using the function's start line — more reliable than scanning.
        if not parent_class and original_indent > 0:
            parent_class = self._find_parent_class(snap, start)
            if parent_class:
                logger.debug("Detected enclosing class '%s' via symbol table", parent_class)

        # Step 1 — delete the original function/method
        delete_start = start
        if start > 1:
            prev = snap.get_line(start - 1) or ""
            if prev.strip() == "":
                delete_start = start - 1   # include preceding blank separator

        ok = FileEditor.delete_lines(snap, delete_start, end)
        if not ok:
            return {"success": False, "error": "delete_lines failed for original function"}

        logger.debug("Deleted original '%s' (lines %d–%d)", entity_name, delete_start, end)

        # Step 2 — insert refactored code at the original position
        # Apply the same indentation as the original so class methods stay
        # inside their class and standalone functions stay at module level.
        if original_indent > 0:
            main_lines = self._indent_code_block(main_func, original_indent)
        else:
            main_lines = [
                (ln if ln.endswith("\n") else ln + "\n")
                for ln in main_func.splitlines()
            ]

        insert_lines = ["\n"] + main_lines + ["\n"]
        ok2 = FileEditor.insert_lines(snap, delete_start, insert_lines)
        if not ok2:
            return {"success": False, "error": "insert_lines at original position failed"}

        insert_desc = f"line {delete_start} (original position)"
        logger.info(
            "Inserted refactored '%s' at original position (line %d, indent=%d)",
            entity_name, delete_start, original_indent,
        )

        # Step 3 — insert any LLM-generated helper functions
        # Helpers go at module level: if inside a class, before the class;
        # if standalone, just before the inserted function.
        if helpers_code.strip():
            if parent_class:
                cls_sym_after = snap.symbols.classes.get(parent_class)
                helper_insert = max(1, (cls_sym_after.start_line - 1) if cls_sym_after else delete_start - 1)
            else:
                helper_insert = max(1, delete_start - 1)

            helper_lines = (
                ["\n", "\n"]
                + self._indent_code_block(helpers_code, 0)
                + ["\n", "\n"]
            )
            FileEditor.insert_lines(snap, helper_insert, helper_lines)
            logger.info("Inserted %d helper lines at module level (line %d)", len(helper_lines), helper_insert)

        registry.flush(file_path)

        new_sym = snap.symbols.functions.get(entity_name)

        return {
            "success":             True,
            "action":              "refactor_code",
            "file":                file_path,
            "function":            entity_name,
            "parent_class":        parent_class,
            "is_method":           parent_class is not None,
            "inserted_at":         insert_desc,
            "original_complexity": complexity,
            "new_complexity":      new_sym.complexity if new_sym else "?",
            "original_lines":      end - delete_start + 1,
            "refactored_lines":    len(refactored.splitlines()),
        }

    # ══════════════════════════════════════════════════════════════════
    # 6. RESTRUCTURE  (LLM-powered split suggestion)
    # ══════════════════════════════════════════════════════════════════

    def _restructure(self, action: Action) -> Dict[str, Any]:
        file_path, _, _ = self._parse_target(action.target)
        logger.info("Restructure ← %s", Path(file_path).name)

        snap = self._load(file_path)
        if not snap:
            return {"success": False, "error": f"Cannot load {file_path}"}

        loc = snap.total_lines()

        if self.dry_run:
            return {
                "success": True, "dry_run": True,
                "message": f"Would analyse {Path(file_path).name} ({loc} lines) for restructuring",
            }

        llm = get_llm()
        suggestion = llm.suggest_restructure(
            file_name=Path(file_path).name,
            source_excerpt=snap.source,
            functions=list(snap.symbols.functions.keys()),
            classes=list(snap.symbols.classes.keys()),
            loc=loc,
        )

        return {
            "success":    True,
            "action":     "restructure",
            "file":       file_path,
            "loc":        loc,
            "suggestion": suggestion,
            "note":       (
                "Review the suggestion above. "
                "Implement the split manually or re-run with auto-split enabled."
            ),
        }

    # ══════════════════════════════════════════════════════════════════
    # 7. MOVE FILE
    # ══════════════════════════════════════════════════════════════════

    def _move_file(self, action: Action) -> Dict[str, Any]:
        parts = action.target.split("->")
        if len(parts) != 2:
            return {"success": False, "error": "Target must be 'source->destination'"}

        source = Path(parts[0].strip())
        dest   = Path(parts[1].strip())

        if self.dry_run:
            return {
                "success": True, "dry_run": True,
                "message": f"Would move {source} → {dest}",
            }

        if not source.exists():
            return {"success": False, "error": f"Source not found: {source}"}

        self.backup.create_backup(str(source))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))

        # Update registry: invalidate old path, load new path
        registry.invalidate(str(source.resolve()))
        registry.load(str(dest.resolve()))

        return {
            "success": True,
            "action":  "move_file",
            "from":    str(source),
            "to":      str(dest),
        }

    # ══════════════════════════════════════════════════════════════════
    # 8. UPDATE DEPENDENCY
    # ══════════════════════════════════════════════════════════════════

    def _update_dependency(self, action: Action) -> Dict[str, Any]:
        file_path, _, entity_name = self._parse_target(action.target)

        # Locate requirements.txt
        candidates = [
            Path(file_path).parent / "requirements.txt",
            Path(file_path).parent.parent / "requirements.txt",
        ]
        req_file = next((r for r in candidates if r.exists()), None)

        return {
            "success":           False,
            "error":             "Dependency updates require manual testing before applying",
            "suggestion":        action.suggested_changes or "Update in requirements.txt and run tests",
            "requirements_file": str(req_file) if req_file else "Not found",
            "dependency":        entity_name,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def execute_action(action: Action, dry_run: bool = False) -> Dict[str, Any]:
    """Execute a single action through the registry-driven executor."""
    executor = RegistryActionExecutor(dry_run=dry_run)
    return executor.execute(action)


def create_backup(file_path: str) -> str:
    return BackupManager().create_backup(file_path)


def restore_backup(file_path: str) -> bool:
    return BackupManager().restore_latest(file_path)


def list_backups(file_path: Optional[str] = None) -> List[Dict]:
    return BackupManager().list_backups(file_path)