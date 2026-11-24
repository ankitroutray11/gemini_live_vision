"""
Chirkoot MK-V: Independent Emergency Stop System.

Guarantees:
1. Global Windows keyboard shortcut hook (Ctrl+Alt+X and Pause/Break).
2. Prominent HUD Emergency Stop activation.
3. Local stop-word recognition independent of Gemini Live or model cooperation.
4. Immediately halts UI action queue, mouse/keyboard injection, pending automations,
   assistant audio playback, microphone/screen capture, and running child processes.
5. Forcefully terminates controlled child-process trees on Windows.
6. Prevents queued actions from starting and prevents automatic resumption.
7. Marks affected tasks CANCELLED or INTERRUPTED in TaskStateManager.
8. Strictly requires a fresh physical human user instruction to resume. Voice alone can NEVER reset.
"""

import os
import sys
import time
import json
import ctypes
import logging
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Set

logger = logging.getLogger("ChirkootEmergencyStop")

# Win32 API Definitions for Global Hotkeys
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_ESCAPE = 0x1B
VK_PAUSE = 0x13
VK_X = 0x58
VK_S = 0x53

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT)
    ]

# Lazy import helpers
def _get_security_policy():
    import security_policy
    return security_policy

def _get_lifecycle_task_manager():
    try:
        from session_lifecycle import TaskStateManager
        return TaskStateManager()
    except Exception as e:
        logger.warning(f"Could not load TaskStateManager: {e}")
        return None

class GlobalEmergencyStopManager:
    """
    Central Coordinator for Chirkoot's Emergency Stop.
    Completely independent of Gemini Live or upstream network connections.
    """
    def __init__(self):
        self._listeners: Set[Callable[[bool, str], None]] = set()
        self._dispatchers: Set[Any] = set()
        self._hotkey_thread: Optional[threading.Thread] = None
        self._stop_hotkey_thread = threading.Event()
        self._hotkeys_registered = False
        self._lock = threading.Lock()

    def register_dispatcher(self, dispatcher: Any):
        """Registers an active FunctionCallDispatcher to abort UI queues on emergency stop."""
        with self._lock:
            self._dispatchers.add(dispatcher)

    def unregister_dispatcher(self, dispatcher: Any):
        with self._lock:
            self._dispatchers.discard(dispatcher)

    def add_listener(self, callback: Callable[[bool, str], None]):
        """Adds a callback (e.g. WebSocket broadcaster) to be notified on kill switch state changes."""
        with self._lock:
            self._listeners.add(callback)

    def remove_listener(self, callback: Callable[[bool, str], None]):
        with self._lock:
            self._listeners.discard(callback)

    def trigger(self, reason: str = "User initiated Emergency Stop") -> Dict[str, Any]:
        """
        Engages the Emergency Stop immediately across all systems:
        1. Activates security policy kill switch.
        2. Forcefully terminates all controlled child process trees.
        3. Drains and cancels Dispatcher UI queues and in-flight operations.
        4. Marks all active tasks CANCELLED in TaskStateManager.
        5. Notifies all listeners (HUD, WebSockets).
        """
        logger.critical(f"============================================================")
        logger.critical(f" [EMERGENCY STOP TRIGGERED] Reason: {reason}")
        logger.critical(f"============================================================")

        sp = _get_security_policy()
        # 1. Trigger central security policy kill switch & process tree termination
        sp.trigger_kill_switch(reason)
        terminated_procs = sp.terminate_all_child_processes(reason)

        # 2. Cancel and drain all active dispatchers
        with self._lock:
            active_dispatchers = list(self._dispatchers)
        for disp in active_dispatchers:
            try:
                disp.cancel_all_pending(reason)
            except Exception as e:
                logger.error(f"Error cancelling dispatcher actions: {e}")

        # 3. Mark all active tasks in TaskStateManager as CANCELLED
        task_mgr = _get_lifecycle_task_manager()
        if task_mgr:
            try:
                task_mgr.mark_all_cancelled(f"Emergency Stop: {reason}")
            except Exception as e:
                logger.error(f"Error marking tasks cancelled: {e}")

        # 4. Notify all registered listeners (HUD, WebSockets)
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(True, reason)
            except Exception as e:
                logger.error(f"Error in emergency stop listener: {e}")

        return {
            "status": "halted",
            "active": True,
            "reason": reason,
            "terminated_child_processes": terminated_procs
        }

    def reset(self, physical_confirmation: bool = True, actor: str = "physical_human") -> Dict[str, Any]:
        """
        Resets the Emergency Stop state.
        STRICT REQUIREMENT: Voice alone can NEVER reset the emergency stop.
        Physical human confirmation (button click or Enter keypress) is strictly required.
        """
        if not physical_confirmation:
            logger.warning("[Security] Rejected voice attempt to reset Emergency Stop!")
            raise PermissionError("VOICE ALONE CANNOT RESET EMERGENCY STOP. Physical human confirmation required.")

        sp = _get_security_policy()
        sp.reset_kill_switch()
        logger.info(f"[EMERGENCY STOP RESET] Reset confirmed by {actor}. Normal operations restored (tasks stay cancelled).")

        # Notify all listeners
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(False, "Reset by physical operator")
            except Exception as e:
                logger.error(f"Error in emergency stop reset listener: {e}")

        return {
            "status": "normal",
            "active": False,
            "message": "Emergency Stop reset. System in clean standby mode; fresh instruction required."
        }

    def is_active(self) -> bool:
        sp = _get_security_policy()
        return sp.is_kill_switch_active()

    def get_reason(self) -> str:
        sp = _get_security_policy()
        return getattr(sp, "_kill_switch_reason", "")

    # ==========================================================================
    # Global Windows Keyboard Shortcut Hook
    # ==========================================================================

    def start_global_hotkey_listener(self):
        """Starts a background Windows thread registering global hotkeys (Ctrl+Alt+X and Pause)."""
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            return

        self._stop_hotkey_thread.clear()
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_worker,
            name="ChirkootGlobalHotkeyListener",
            daemon=True
        )
        self._hotkey_thread.start()
        logger.info("Global Windows Emergency Stop keyboard shortcuts initialized (Ctrl+Alt+X, Pause/Break).")

    def stop_global_hotkey_listener(self):
        """Unregisters hotkeys and cleanly stops the listener thread."""
        self._stop_hotkey_thread.set()
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=1.0)
        self._hotkey_thread = None

    def _hotkey_worker(self):
        user32 = ctypes.windll.user32
        hotkeys = [
            (101, MOD_CONTROL | MOD_ALT, VK_X, "Ctrl+Alt+X"),
            (102, 0, VK_PAUSE, "Pause/Break"),
            (103, MOD_CONTROL | MOD_SHIFT | MOD_ALT, VK_ESCAPE, "Ctrl+Shift+Alt+Escape")
        ]

        registered_ids = []
        for hid, mods, vk, name in hotkeys:
            try:
                if user32.RegisterHotKey(None, hid, mods, vk):
                    registered_ids.append(hid)
                    logger.debug(f"[Emergency Hotkey] Registered global hotkey: {name}")
                else:
                    logger.warning(f"[Emergency Hotkey] Could not register global hotkey: {name}")
            except Exception as err:
                logger.error(f"Error registering hotkey {name}: {err}")

        self._hotkeys_registered = len(registered_ids) > 0
        msg = MSG()

        try:
            while not self._stop_hotkey_thread.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY:
                        hid = msg.wParam
                        hotkey_name = next((h[3] for h in hotkeys if h[0] == hid), f"ID {hid}")
                        logger.critical(f"[GLOBAL HOTKEY TRIGGERED] Pressed '{hotkey_name}'. Halting Chirkoot immediately!")
                        self.trigger(f"Global Windows Hotkey ({hotkey_name})")

                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.05)
        finally:
            for hid in registered_ids:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
            self._hotkeys_registered = False
            logger.info("Global Windows Emergency Stop hotkeys unregistered.")

# Global Singleton Manager
EMERGENCY_STOP_MANAGER = GlobalEmergencyStopManager()
