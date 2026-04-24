"""
File Watcher Service

Watches registered project repo paths for Python file changes.
Debounces rapid saves (waits 30s after last change before triggering analysis).
Runs as a background thread alongside FastAPI.
"""

import threading
from pathlib import Path
from typing import Callable, Dict, Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from core.logger import get_logger

logger = get_logger(__name__)


# Debounce delay in seconds — waits this long after last change before analyzing
DEBOUNCE_SECONDS = 30


class _DebounceHandler(FileSystemEventHandler):
    """
    Watchdog handler that debounces rapid file saves.
    Only fires the callback after DEBOUNCE_SECONDS of inactivity.
    """

    def __init__(self, project_id: str, repo_path: str, on_change: Callable):
        super().__init__()
        self.project_id = project_id
        self.repo_path  = repo_path
        self.on_change  = on_change
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _schedule(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            # Create timer under lock so self._timer is always consistent
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        logger.info("Change detected in '%s' — triggering analysis", self.project_id)
        self.on_change(self.project_id, self.repo_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".py"):
            self._schedule()

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".py"):
            self._schedule()


class ProjectWatcher:
    """
    Manages file watchers for all registered projects.
    One Observer per project repo_path.
    """

    def __init__(self):
        self._observers: Dict[str, Observer] = {}   # project_id → Observer
        self._lock = threading.Lock()

    def start_watching(
        self,
        project_id: str,
        repo_path: str,
        on_change: Callable,
    ) -> bool:
        """Start watching a project. Returns False if already watching."""
        with self._lock:
            if project_id in self._observers:
                return False

            path = Path(repo_path)
            if not path.exists():
                logger.warning("Watch path does not exist: %s", repo_path)
                return False

            handler  = _DebounceHandler(project_id, repo_path, on_change)
            observer = Observer()
            observer.schedule(handler, str(path), recursive=True)
            observer.daemon = True
            observer.start()

            self._observers[project_id] = observer
            logger.info("Started watching '%s' → %s", project_id, repo_path)
            return True

    def stop_watching(self, project_id: str) -> bool:
        """Stop watching a project."""
        with self._lock:
            observer = self._observers.pop(project_id, None)
            if observer:
                observer.stop()
                observer.join(timeout=3)
                logger.info("Stopped watching '%s'", project_id)
                return True
            return False

    def is_watching(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._observers

    def stop_all(self):
        with self._lock:
            for project_id, observer in list(self._observers.items()):
                observer.stop()
                observer.join(timeout=3)
                logger.info("Stopped watching '%s'", project_id)
            self._observers.clear()


# Singleton
project_watcher = ProjectWatcher()
