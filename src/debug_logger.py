import json
import logging
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import defaultdict
import threading
import pickle
import gzip
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError


class _FlushingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that flushes after every record so lines survive a crash."""

    def emit(self, record):
        try:
            super().emit(record)
            self.flush()
        except Exception:
            pass


class DebugLogger:
    """Centralized debug logging system for the MCP server."""

    def __init__(self):
        """
        Initializes the DebugLogger.

        Variables:
            self._errors (List[Dict[str, Any]]): Stores error logs.
            self._warnings (List[Dict[str, Any]]): Stores warning logs.
            self._info (List[Dict[str, Any]]): Stores info logs.
            self._stats (Dict[str, int]): Stores statistics for errors, warnings, and calls.
            self._lock (threading.Lock): Ensures thread safety for logging.
            self._enabled (bool): Indicates if logging is enabled.
            self._seen_errors (set): Track error signatures to prevent duplicates.
        """
        self._errors: List[Dict[str, Any]] = []
        self._warnings: List[Dict[str, Any]] = []
        self._info: List[Dict[str, Any]] = []
        self._stats: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
        self._enabled = False
        self._lock_owner = "none"
        import time
        self._lock_acquired_time = 0
        self._seen_errors: set = set()
        self._file_logger: Optional[logging.Logger] = self._init_file_logger()

    def _init_file_logger(self) -> Optional[logging.Logger]:
        """Set up an always-on rotating file logger so logs survive crashes."""
        try:
            log_dir_env = os.getenv("STEALTH_BROWSER_LOG_DIR")
            if log_dir_env:
                log_dir = Path(log_dir_env)
            else:
                log_dir = Path(__file__).resolve().parent.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            logger = logging.getLogger("stealth_browser_mcp")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False

            have_file_handler = any(
                isinstance(h, RotatingFileHandler) for h in logger.handlers
            )
            if not have_file_handler:
                handler = _FlushingRotatingFileHandler(
                    log_dir / "server.log",
                    maxBytes=5 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s.%(msecs)03d %(levelname)s pid=%(process)d %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S",
                    )
                )
                logger.addHandler(handler)

            logger.info(
                "logger_init pid=%d python=%s cwd=%s",
                os.getpid(),
                sys.version.split()[0],
                os.getcwd(),
            )
            return logger
        except Exception as exc:
            try:
                print(f"[DEBUG] Failed to init file logger: {exc}", file=sys.stderr)
            except Exception:
                pass
            return None

    def _file_log(self, level: int, component: str, method: str, message: str, extra: Any = None):
        """Write to the persistent file log. Safe if the file logger is unavailable."""
        if self._file_logger is None:
            return
        try:
            if extra is None:
                self._file_logger.log(level, "%s.%s: %s", component, method, message)
            else:
                try:
                    extra_str = json.dumps(extra, default=str)
                except Exception:
                    extra_str = repr(extra)
                self._file_logger.log(level, "%s.%s: %s | %s", component, method, message, extra_str)
        except Exception:
            pass

    def _emit_stderr(self, message: str, force: bool = False):
        """
        Emit a debug line to stderr only when logging is enabled unless forced.

        Args:
            message (str): Message to emit.
            force (bool): Whether to emit regardless of current debug state.
        """
        if not force and not self._enabled:
            return
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass

    def log_error(self, component: str, method: str, error: Exception, context: Optional[Dict[str, Any]] = None):
        """
        Log an error with full context.

        Args:
            component (str): Name of the component where the error occurred.
            method (str): Name of the method where the error occurred.
            error (Exception): The exception instance.
            context (Optional[Dict[str, Any]]): Additional context for the error.
        """
        tb = traceback.format_exc()
        self._file_log(
            logging.ERROR,
            component,
            method,
            f"{type(error).__name__}: {error}",
            extra={"traceback": tb, "context": context or {}},
        )

        if not self._enabled:
            return

        with self._lock:
            error_signature = f"{component}.{method}.{type(error).__name__}.{str(error)}"
            
            if error_signature in self._seen_errors:
                self._stats[f'{component}.{method}.errors'] += 1
                return
            
            self._seen_errors.add(error_signature)
            
            error_entry = {
                'timestamp': datetime.now().isoformat(),
                'component': component,
                'method': method,
                'error_type': type(error).__name__,
                'error_message': str(error),
                'traceback': traceback.format_exc(),
                'context': context or {}
            }
            self._errors.append(error_entry)
            self._stats[f'{component}.{method}.errors'] += 1
            self._emit_stderr(f"[DEBUG ERROR] {component}.{method}: {error}")

    def log_warning(self, component: str, method: str, message: str, context: Optional[Dict[str, Any]] = None):
        """
        Log a warning.

        Args:
            component (str): Name of the component where the warning occurred.
            method (str): Name of the method where the warning occurred.
            message (str): Warning message.
            context (Optional[Dict[str, Any]]): Additional context for the warning.
        """
        self._file_log(logging.WARNING, component, method, message, extra=context)

        if not self._enabled:
            return

        with self._lock:
            warning_entry = {
                'timestamp': datetime.now().isoformat(),
                'component': component,
                'method': method,
                'message': message,
                'context': context or {}
            }
            self._warnings.append(warning_entry)
            self._stats[f'{component}.{method}.warnings'] += 1
            self._emit_stderr(f"[DEBUG WARN] {component}.{method}: {message}")

    def log_info(self, component: str, method: str, message: str, data: Optional[Any] = None):
        """
        Log information for debugging.

        Args:
            component (str): Name of the component where the info is logged.
            method (str): Name of the method where the info is logged.
            message (str): Info message.
            data (Optional[Any]): Additional data for the info log.
        """
        self._file_log(logging.INFO, component, method, message, extra=data)

        if not self._enabled:
            return

        with self._lock:
            info_entry = {
                'timestamp': datetime.now().isoformat(),
                'component': component,
                'method': method,
                'message': message,
                'data': data
            }
            self._info.append(info_entry)
            self._stats[f'{component}.{method}.calls'] += 1
            self._emit_stderr(f"[DEBUG INFO] {component}.{method}: {message}")
            if data:
                self._emit_stderr(f"  Data: {data}")

    def get_debug_view(self) -> Dict[str, Any]:
        """
        Get comprehensive debug view of all logged data.

        Returns:
            Dict[str, Any]: Dictionary containing summary, recent errors/warnings, all errors/warnings, and component breakdown.
        """
        return self.get_debug_view_paginated()
    
    def get_debug_view_paginated(
        self,
        max_errors: Optional[int] = None,
        max_warnings: Optional[int] = None,
        max_info: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get paginated debug view of logged data with size limits.

        Args:
            max_errors (Optional[int]): Maximum number of errors to include. None for all.
            max_warnings (Optional[int]): Maximum number of warnings to include. None for all.
            max_info (Optional[int]): Maximum number of info logs to include. None for all.

        Returns:
            Dict[str, Any]: Dictionary containing summary, recent errors/warnings, limited errors/warnings, and component breakdown.
        """
        with self._lock:
            if max_errors is not None:
                limited_errors = self._errors[-max_errors:] if self._errors else []
                all_errors = limited_errors
            else:
                limited_errors = self._errors[-10:] if self._errors else []
                all_errors = self._errors
            
            if max_warnings is not None:
                limited_warnings = self._warnings[-max_warnings:] if self._warnings else []
                all_warnings = limited_warnings
            else:
                limited_warnings = self._warnings[-10:] if self._warnings else []
                all_warnings = self._warnings
            
            if max_info is not None:
                limited_info = self._info[-max_info:] if self._info else []
                all_info = limited_info
            else:
                limited_info = self._info[-10:] if self._info else []
                all_info = self._info

            return {
                'summary': {
                    'total_errors': len(self._errors),
                    'total_warnings': len(self._warnings),
                    'total_info': len(self._info),
                    'returned_errors': len(all_errors),
                    'returned_warnings': len(all_warnings),
                    'returned_info': len(all_info),
                    'error_types': self._get_error_summary(),
                    'stats': dict(self._stats)
                },
                'recent_errors': limited_errors,
                'recent_warnings': limited_warnings,
                'recent_info': limited_info,
                'all_errors': all_errors,
                'all_warnings': all_warnings,
                'all_info': all_info,
                'component_breakdown': self._get_component_breakdown()
            }

    def _get_error_summary(self) -> Dict[str, int]:
        """
        Get summary of error types.

        Returns:
            Dict[str, int]: Dictionary mapping error type names to their counts.
        """
        error_types = defaultdict(int)
        for error in self._errors:
            error_types[error['error_type']] += 1
        return dict(error_types)

    def _get_component_breakdown(self) -> Dict[str, Dict[str, int]]:
        """
        Get breakdown by component.

        Returns:
            Dict[str, Dict[str, int]]: Dictionary mapping component names to their error, warning, and call counts.
        """
        breakdown = defaultdict(lambda: {'errors': 0, 'warnings': 0, 'calls': 0})

        for error in self._errors:
            breakdown[error['component']]['errors'] += 1

        for warning in self._warnings:
            breakdown[warning['component']]['warnings'] += 1

        for info in self._info:
            breakdown[info['component']]['calls'] += 1

        return dict(breakdown)

    def clear_debug_view(self):
        """
        Clear all debug logs with timeout protection.

        Variables:
            self._errors (List[Dict[str, Any]]): Cleared.
            self._warnings (List[Dict[str, Any]]): Cleared.
            self._info (List[Dict[str, Any]]): Cleared.
            self._stats (Dict[str, int]): Cleared.
        """
        try:
            if self._lock.acquire(timeout=5.0):
                try:
                    self._errors.clear()
                    self._warnings.clear() 
                    self._info.clear()
                    self._stats.clear()
                    self._emit_stderr("[DEBUG] Debug logs cleared")
                finally:
                    self._lock.release()
            else:
                self._emit_stderr("[DEBUG] Failed to clear logs - timeout acquiring lock")
        except Exception as e:
            self._emit_stderr(f"[DEBUG] Error clearing logs: {e}")
    
    def clear_debug_view_safe(self):
        """
        Safe version that recreates data structures if lock fails.
        """
        try:
            self.clear_debug_view()
        except:
            self._errors = []
            self._warnings = []
            self._info = []
            self._stats = defaultdict(int)
            self._emit_stderr("[DEBUG] Debug logs force-cleared (lock bypass)")

    def enable(self):
        """
        Enable debug logging.

        Variables:
            self._enabled (bool): Set to True.
        """
        self._enabled = True
        self._emit_stderr("[DEBUG] Debug logging enabled", force=True)

    def disable(self):
        """
        Disable debug logging.

        Variables:
            self._enabled (bool): Set to False.
        """
        self._emit_stderr("[DEBUG] Debug logging disabled")
        self._enabled = False

    def get_lock_status(self) -> Dict[str, Any]:
        """Get current lock status for debugging."""
        import time
        return {
            "lock_owner": self._lock_owner,
            "lock_held_duration": time.time() - self._lock_acquired_time if self._lock_acquired_time > 0 else 0,
            "lock_acquired": self._lock.locked() if hasattr(self._lock, 'locked') else "unknown"
        }

    def export_to_file(self, filepath: str = "debug_log.json"):
        """
        Export debug logs to a JSON file.

        Args:
            filepath (str): Path to the file where logs will be exported.

        Returns:
            str: The filepath where logs were exported.
        """
        return self.export_to_file_paginated(filepath)
    
    def export_to_file_paginated(
        self,
        filepath: str = "debug_log.json",
        max_errors: Optional[int] = None,
        max_warnings: Optional[int] = None,
        max_info: Optional[int] = None,
        format: str = "auto"
    ):
        """
        Export paginated debug logs to a file using fastest method available.

        Args:
            filepath (str): Path to the file where logs will be exported.
            max_errors (Optional[int]): Maximum number of errors to export. None for all.
            max_warnings (Optional[int]): Maximum number of warnings to export. None for all.
            max_info (Optional[int]): Maximum number of info logs to export. None for all.
            format (str): Export format: 'json', 'pickle', 'gzip-pickle', 'auto' (default: 'auto').

        Returns:
            str: The filepath where logs were exported.
        """
        import time
        try:
            self._emit_stderr("[DEBUG] export_debug_logs attempting lock acquisition...")
            current_status = self.get_lock_status()
            self._emit_stderr(f"[DEBUG] Current lock status: {current_status}")
            
            acquired = self._lock.acquire(timeout=5.0)
            if not acquired:
                self._emit_stderr("[DEBUG] Lock timeout - falling back to lock-free export")
                return self._export_lockfree(filepath, max_errors, max_warnings, max_info, format)
            
            self._lock_owner = "export_debug_logs"
            self._lock_acquired_time = time.time()
            self._emit_stderr("[DEBUG] Lock acquired by export_debug_logs")
            
            try:
                debug_data = self.get_debug_view_paginated(
                    max_errors=max_errors,
                    max_warnings=max_warnings,
                    max_info=max_info
                )
            finally:
                self._lock_owner = "none"
                self._lock_acquired_time = 0
                self._lock.release()
                self._emit_stderr("[DEBUG] Lock released by export_debug_logs")
        except Exception as e:
            self._emit_stderr(f"[DEBUG] Exception in export: {e}")
            return self._export_lockfree(filepath, max_errors, max_warnings, max_info, format)
            
        if format == "auto":
            total_items = (debug_data['summary']['returned_errors'] + 
                         debug_data['summary']['returned_warnings'] + 
                         debug_data['summary']['returned_info'])
            if total_items > 1000:
                format = "gzip-pickle"
            elif total_items > 100:
                format = "pickle"
            else:
                format = "json"
        
        if format == "gzip-pickle":
            return self._export_gzip_pickle(debug_data, filepath)
        elif format == "pickle":
            return self._export_pickle(debug_data, filepath)
        else:
            return self._export_json(debug_data, filepath)
    
    def _export_lockfree(self, filepath: str, max_errors: Optional[int], max_warnings: Optional[int], max_info: Optional[int], format: str) -> str:
        """
        Lock-free export method that creates a snapshot without acquiring locks.
        """
        errors_snapshot = list(self._errors)
        warnings_snapshot = list(self._warnings) 
        info_snapshot = list(self._info)
        
        if max_errors is not None:
            errors_snapshot = errors_snapshot[:max_errors]
        if max_warnings is not None:
            warnings_snapshot = warnings_snapshot[:max_warnings] 
        if max_info is not None:
            info_snapshot = info_snapshot[:max_info]
            
        debug_data = {
            'summary': {
                'total_errors': len(self._errors),
                'total_warnings': len(self._warnings), 
                'total_info': len(self._info),
                'returned_errors': len(errors_snapshot),
                'returned_warnings': len(warnings_snapshot),
                'returned_info': len(info_snapshot)
            },
            'all_errors': errors_snapshot,
            'all_warnings': warnings_snapshot,
            'all_info': info_snapshot
        }
        
        if format == "auto":
            total_items = len(errors_snapshot) + len(warnings_snapshot) + len(info_snapshot)
            if total_items > 1000:
                format = "gzip-pickle"
            elif total_items > 100:
                format = "pickle"
            else:
                format = "json"
        
        if format == "gzip-pickle":
            return self._export_gzip_pickle(debug_data, filepath)
        elif format == "pickle":
            return self._export_pickle(debug_data, filepath)
        else:
            return self._export_json(debug_data, filepath)
    
    def _export_gzip_pickle(self, debug_data: Dict[str, Any], filepath: str) -> str:
        if not filepath.endswith('.pkl.gz'):
            filepath = filepath.replace('.json', '.pkl.gz')
        
        with gzip.open(filepath, 'wb') as f:
            pickle.dump(debug_data, f, protocol=pickle.HIGHEST_PROTOCOL)
        
        file_size = os.path.getsize(filepath)
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, gzip-pickle format)"
        )
        return filepath
    
    def _export_pickle(self, debug_data: Dict[str, Any], filepath: str) -> str:
        """Export using pickle (fast for medium data)."""
        if not filepath.endswith('.pkl'):
            filepath = filepath.replace('.json', '.pkl')
        
        with open(filepath, 'wb') as f:
            pickle.dump(debug_data, f, protocol=pickle.HIGHEST_PROTOCOL)
        
        file_size = os.path.getsize(filepath)
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, pickle format)"
        )
        return filepath
    
    def _export_json(self, debug_data: Dict[str, Any], filepath: str) -> str:
        """Export using JSON (human readable but slower)."""
        with open(filepath, 'w') as f:
            json.dump(debug_data, f, separators=(',', ':'), default=str)
        
        file_size = os.path.getsize(filepath)
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, JSON format)"
        )
        return filepath


debug_logger = DebugLogger()
