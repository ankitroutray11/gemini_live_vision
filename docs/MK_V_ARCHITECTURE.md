# Chirkoot MK-V System Architecture & Tool Specification

**Version:** 5.0.0-PROD  
**Target:** Chirkoot Desktop Multimodal Agent  

---

## 1. System Architecture & Security Boundaries

```
========================================================================================================
                                     GOOGLE CLOUD INFRASTRUCTURE
========================================================================================================
                                                  │
                                                  │ WSS: api.generativelanguage.googleapis.com
                                                  │ Upstream Bidi WebSocket
                                                  │ (16kHz PCM Audio In / 24kHz PCM Audio Out / 720p Video)
                                                  ▼
========================================================================================================
                                        LOCAL WINDOWS 11 HOST BOUNDARY
========================================================================================================

  ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
  │                                    LOCAL BACKEND PROCESS (server.py)                              │
  │                                                                                                  │
  │  ┌─────────────────────────┐         ┌─────────────────────────┐     ┌────────────────────────┐  │
  │  │ Localhost HTTP / WS     │         │ Gemini Live Client      │     │ Context & Session      │  │
  │  │ Handshake Gatekeeper    │◄───────►│ Orchestrator            │◄───►│ Resumption Manager     │  │
  │  │ - Origin Validator      │         │ - Async Send/Recv Loops │     │ - Sliding Window Comp. │  │
  │  │ - 32B Launch Token Auth │         │ - Audio/Video Buffers   │     │ - Rolling Transcript   │  │
  │  │ - Single Client Sentry  │         │ - GoAway Reconnect Loop │     │ - Handle Auto-Refresh  │  │
  │  └────────────┬────────────┘         └────────────┬────────────┘     └────────────────────────┘  │
  │               │                                   │                                              │
  │               │                                   ▼ Function Call Event                          │
  │               │                      ┌────────────────────────────────────────┐                  │
  │               │                      │   FUNCTION CALL RESOLVER               │                  │
  │               │                      │   (Resolves tool name & arguments)     │                  │
  │               │                      └────────────────────┬───────────────────┘                  │
  │               │                                           │                                      │
  │               │                                           ▼                                      │
  │               │                      ┌────────────────────────────────────────┐                  │
  │               │                      │   LOCAL SECURITY POLICY DISPATCHER     │                  │
  │               │                      │                                        │                  │
  │               │                      │ 1. Emergency Kill Switch Sentry        │                  │
  │               │                      │ 2. Dynamic Taint Tracker (60s Decay)   │                  │
  │               │                      │ 3. Immutable Core Code Protector       │                  │
  │               │                      │ 4. Sensitive Path Traversal Sandbox    │                  │
  │               │                      │ 5. Tool Risk Tier Classifier           │                  │
  │               │                      └────────────┬───────────────────────────┘                  │
  │               │                                   │                                              │
  │               │               ┌───────────────────┴───────────────────┐                          │
  │               │               ▼                                       ▼                          │
  │               │      [ Tier 1 & Clean Tier 2 ]             [ Tier 3 & Tainted Tier 2 ]           │
  │               │      Auto-Execute & Audit Log              HITL Confirmation Required            │
  │               │               │                                       │                          │
  │               │               │                                       ▼                          │
  │               │               │                          Pause Tool & Request HUD Modal          │
  │               │               │                          (Physical Click / Enter / Esc)          │
  │               │               │                                       │                          │
  │               │               ├───────────────────────────────────────┘                          │
  │               │               ▼                                                                  │
  │               │  ┌─────────────────────────────────────────────────────────┐                     │
  │               │  │             SANDBOXED TOOL EXECUTION ENGINE             │                     │
  │               │  │ - Win32 Mouse & Keyboard Controller (user32.dll)        │                     │
  │               │  │ - RapidOCR Onnx Runtime Engine (rapidocr_onnxruntime)   │                     │
  │               │  │ - Process-Tree-Terminated Shell (taskkill /F /T /PID)   │                     │
  │               │  │ - Subprocess-Isolated Dynamic Skills (custom_skills/)   │                     │
  │               │  │ - Provenance-Tagged Memory Bank (learning_engine.py)    │                     │
  │               │  └─────────────────────────────────────────────────────────┘                     │
  └───────────────┼──────────────────────────────────────────────────────────────────────────────────┘
                  │
                  │ WS: ws://127.0.0.1:8000/ws?token=<token>
                  │ Local Inter-Process Communication
                  ▼
  ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
  │                                    HUD COCKPIT PROCESS (desktop_app.py)                          │
  │                                    Chromium Native App Mode / PyWebView                          │
  │                                                                                                  │
  │  - Web Audio In/Out (ScriptProcessor / AudioWorklet, 16kHz PCM Mic, 24kHz PCM Playback)          │
  │  - Display Uplink (Screen Radar getDisplayMedia, Canvas 720p 1 FPS JPEG Encoder)                 │
  │  - Tactical Cockpit Controls (Always-On vs Hey Chirkoot, Voice vs Silent Text Reply)             │
  │  - Policy Confirmation Modal (Displays exact tool name, arguments, risk tier, taint badge)       │
  │  - Memory Review & Forget Modal (Audit & delete learned facts by provenance)                     │
  │  - Emergency Stop Controls (Client-side speech stop interceptor, Ctrl+Shift+X, Esc+Esc button)   │
  └──────────────────────────────────────────────────────────────────────────────────────────────────┘
                  │
                  │ File IO / Storage Protection
                  ▼
  ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
  │                           ISOLATED USER RUNTIME STORE (%LOCALAPPDATA%\Chirkoot)                  │
  │                                                                                                  │
  │  ├── data/memory.json                # Persistent memory with provenance stamps                  │
  │  ├── data/custom_skills.json         # Dynamic tool metadata and registrations                   │
  │  ├── custom_skills/*.py              # Subprocess-isolated dynamic skill modules                 │
  │  ├── backups/                        # Automatic pre-modification source code backups           │
  │  ├── logs/audit_log.jsonl            # Security and policy audit event logs                      │
  │  └── vault/credentials.dat           # DPAPI-encrypted Gemini API key                            │
  └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Gemini LiveConnectConfig Specification (SDK 2.31.0)

Configured in `server.py` and validated against `google-genai` version `2.31.0`:

```python
config = types.LiveConnectConfig(
    response_modalities=[types.Modality.AUDIO],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
        )
    ),
    system_instruction=types.Content(
        parts=[types.Part(text=system_instruction)]
    ),
    tools=ALL_TOOLS,
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        sliding_window=types.SlidingWindow()
    ),
    session_resumption=types.SessionResumptionConfig(
        handle=latest_resumption_handle
    )
)
```

### Verified Constraints:
1. `session_resumption`: In Gemini Developer API mode, `transparent` is **unsupported**. Only `handle` should be supplied.
2. `context_window_compression`: `sliding_window=types.SlidingWindow()` enables server-side token eviction, removing session duration caps.
3. `tools`: Must be a list of callable Python functions with standard type hints and docstrings.

---

## 3. Comprehensive Tool Catalog (All 35 Declared Tools)

The following declarations represent the exact tool signatures and schemas exposed to Gemini:

### Category 1: Screen & OS Automation (9 Tools)

1. `click_screen(x: int, y: int, button: str = 'left', clicks: int = 1, normalized: bool = True) -> str`
   - **Description:** Clicks on a target element on the user's screen using native Windows mouse events.
   - **Parameters:** `x` (int, 0–1000), `y` (int, 0–1000), `button` ('left', 'right', 'middle'), `clicks` (1 or 2), `normalized` (bool).
   - **Tier:** Tier 2 (Reversible UI Automation).

2. `type_text(text: str, press_enter: bool = False) -> str`
   - **Description:** Types text at the current cursor position or active input field.
   - **Parameters:** `text` (str), `press_enter` (bool).
   - **Tier:** Tier 2 (Reversible UI Automation).

3. `press_hotkey(keys: str) -> str`
   - **Description:** Presses a key or key combination on the Windows keyboard natively (e.g. 'win', 'alt,tab', 'ctrl,s', 'enter').
   - **Parameters:** `keys` (str).
   - **Tier:** Tier 2 (Reversible UI Automation).

4. `scroll_screen(direction: str = 'down', amount: int = 5) -> str`
   - **Description:** Scrolls the active window or page up or down.
   - **Parameters:** `direction` ('down' or 'up'), `amount` (int).
   - **Tier:** Tier 2 (Reversible UI Automation).

5. `switch_to_app(app_name: str) -> str`
   - **Description:** Brings an open application window directly to the front and activates it.
   - **Parameters:** `app_name` (str).
   - **Tier:** Tier 2 (Reversible UI Automation).

6. `minimize_active_window() -> str`
   - **Description:** Minimizes the current foreground window to reveal background applications.
   - **Tier:** Tier 2 (Reversible UI Automation).

7. `maximize_active_window() -> str`
   - **Description:** Maximizes the active foreground window to fill the entire screen.
   - **Tier:** Tier 2 (Reversible UI Automation).

8. `restore_active_window() -> str`
   - **Description:** Restores the active window down to a floating/normal window.
   - **Tier:** Tier 2 (Reversible UI Automation).

9. `toggle_fullscreen_window() -> str`
   - **Description:** Toggles fullscreen state on the active window using native Windows hotkeys.
   - **Tier:** Tier 2 (Reversible UI Automation).

---

### Category 2: OCR & Visual Inspection (3 Tools)

10. `ocr_screen(region_or_query: Optional[str] = None) -> str`
    - **Description:** Performs Optical Character Recognition on the live screen with normalized coordinates.
    - **Parameters:** `region_or_query` (optional str).
    - **Tier:** Tier 1 (Read-Only Inspection; marks session tainted).

11. `ocr_image(image_path: str) -> str`
    - **Description:** Performs local OCR on an image file, extracting text and bounding boxes.
    - **Parameters:** `image_path` (str).
    - **Tier:** Tier 1 (Read-Only Inspection; marks session tainted).

12. `find_text_on_screen(target_text: str, click: bool = False) -> str`
    - **Description:** Searches the user's screen for a specific text string using OCR, and optionally clicks it.
    - **Parameters:** `target_text` (str), `click` (bool).
    - **Tier:** Tier 1 (if `click=False`) / Tier 2 (if `click=True`).

---

### Category 3: Host System & Filesystem (6 Tools)

13. `run_command(command: str) -> str`
    - **Description:** Executes a shell command with sandboxed process tree timeout and output capping.
    - **Parameters:** `command` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

14. `read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str`
    - **Description:** Reads file contents from disk with path sandbox enforcement.
    - **Parameters:** `path` (str), `start_line` (optional int), `end_line` (optional int).
    - **Tier:** Tier 1 (Read-Only; marks session tainted).

15. `write_file(path: str, content: str) -> str`
    - **Description:** Creates a new file or overwrites an existing file with the given content.
    - **Parameters:** `path` (str), `content` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

16. `list_directory(path: Optional[str] = '.') -> str`
    - **Description:** Lists files and folders inside the specified directory.
    - **Parameters:** `path` (optional str).
    - **Tier:** Tier 1 (Read-Only Inspection).

17. `system_diagnostics() -> str`
    - **Description:** Returns current host hardware diagnostics (CPU, RAM, Battery, OS).
    - **Tier:** Tier 1 (Read-Only Inspection).

18. `open_app_or_url(target: str) -> str`
    - **Description:** Launches a desktop application or opens a website URL in the default browser.
    - **Parameters:** `target` (str).
    - **Tier:** Tier 2 (Reversible UI Automation).

---

### Category 4: Self-Learning & Codebase Integrity (8 Tools)

19. `inspect_self() -> str`
    - **Description:** Inspects Chirkoot's architecture, active modules, tool count, and security status.
    - **Tier:** Tier 1 (Read-Only Inspection).

20. `read_code_file(filepath: str, start_line: int = 1, end_line: int = 120) -> str`
    - **Description:** Reads lines from source code with sensitive file sandboxing.
    - **Parameters:** `filepath` (str), `start_line` (int), `end_line` (int).
    - **Tier:** Tier 1 (Read-Only Inspection).

21. `modify_source_code(filepath: str, target_snippet: str, replacement_snippet: str) -> str`
    - **Description:** Modifies a file with Immutable Core protection, backup, and AST syntax verification.
    - **Parameters:** `filepath` (str), `target_snippet` (str), `replacement_snippet` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

22. `rollback_code(filepath: str) -> str`
    - **Description:** Restores the most recent backup for the given source file.
    - **Parameters:** `filepath` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

23. `create_custom_tool(tool_name: str, description: str, python_code: str) -> str`
    - **Description:** Creates a new dynamic Python tool in `%LOCALAPPDATA%\Chirkoot\custom_skills\`.
    - **Parameters:** `tool_name` (str), `description` (str), `python_code` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

24. `list_custom_tools() -> str`
    - **Description:** Lists all custom tools stored in the user AppData directory.
    - **Tier:** Tier 1 (Read-Only Inspection).

25. `remember_insight(category: str, key: str, insight: str, provenance: str = 'user_explicit', source_context: str = '') -> str`
    - **Description:** Stores a learned insight with strict provenance tracking into persistent memory.
    - **Parameters:** `category` (str), `key` (str), `insight` (str), `provenance` (str), `source_context` (str).
    - **Tier:** Tier 2 (Reversible Persistence).

26. `recall_memory(query: str = '', category: str = '') -> str`
    - **Description:** Recalls stored memories with provenance annotations.
    - **Parameters:** `query` (optional str), `category` (optional str).
    - **Tier:** Tier 1 (Read-Only Inspection).

---

### Category 5: Memory Bank & Diagnostic Governance (6 Tools)

27. `list_memories() -> List[Dict[str, Any]]`
    - **Description:** Returns a structured catalog of all stored memories for user review & pruning.
    - **Tier:** Tier 1 (Read-Only Inspection).

28. `delete_memory(category: str, key: str) -> str`
    - **Description:** Removes a specific memory entry from the persistent bank (Review & Forget).
    - **Parameters:** `category` (str), `key` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

29. `run_self_tests() -> str`
    - **Description:** Verifies that all Python files compile cleanly without syntax errors.
    - **Tier:** Tier 1 (Read-Only Inspection).

30. `restart_chirkoot_server() -> str`
    - **Description:** Gracefully signals server reload.
    - **Tier:** Tier 2 (Reversible System Action).

31. `get_codebase_guide() -> str`
    - **Description:** Returns an architectural guide of Chirkoot's codebase.
    - **Tier:** Tier 1 (Read-Only Inspection).

32. `self_learn_and_implement(feature_title: str, file_to_modify: str, target_code_snippet: str, new_code_snippet: str, explanation: str = '') -> str`
    - **Description:** High-level self-modification tool with Immutable Core safeguards.
    - **Parameters:** `feature_title` (str), `file_to_modify` (str), `target_code_snippet` (str), `new_code_snippet` (str), `explanation` (str).
    - **Tier:** Tier 3 (High Risk Side-Effecting; Mandatory HITL).

---

### Category 6: Dynamic Custom Skills (3 Sample Tools)

33. `calc_cube(**kwargs)`
    - **Description:** Computes the cube of a number in an isolated Python subprocess.
    - **Tier:** Tier 1 (Calculated in Subprocess).

34. `get_system_uptime(**kwargs)`
    - **Description:** Returns system boot time and uptime in an isolated Python subprocess.
    - **Tier:** Tier 1 (Read-Only Subprocess).

35. `test_echo_tool(**kwargs)`
    - **Description:** Diagnostic echo tool executed in an isolated Python subprocess.
    - **Tier:** Tier 1 (Read-Only Subprocess).
