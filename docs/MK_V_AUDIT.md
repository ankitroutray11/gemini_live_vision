# Chirkoot MK-V Architectural & Security Audit

**Document Version:** 5.0.0-PROD  
**Target System:** Chirkoot Desktop Multimodal Agent (MK-IV Baseline to MK-V Production Hardened)  
**Host Environment:** Windows 11 (AMD64), Python 3.12.10, `google-genai` SDK v2.31.0  
**Audit Scope:** Gemini Live API, Trust Boundaries, Audio Pipeline, Vision Pipeline, Desktop Automation, Self-Learning, Memory Engine, Windows Packaging.

---

## 1. Gemini Live API Integration Audit

### 1.1 Model & SDK Specification
- **Configured Model:** `gemini-3.8-live` (verified in `server.py`).
- **Official SDK:** `google-genai` version `2.31.0` utilizing `client.aio.live.connect(model=..., config=...)`.
- **Protocol:** Bidirectional WebSocket streaming PCM audio (16 kHz, 16-bit mono input; 24 kHz, 16-bit mono output) and real-time JPEG video frames.

### 1.2 Session Lifecycles & Time Caps
- **Uncompressed Session Timeouts:** Without active compression, Gemini Live sessions are strictly capped by Google infrastructure at **15 minutes** for audio-only streams and **2 minutes** once video frames are transmitted.
- **Connection Lifetime:** Underlying WebSocket connections have a maximum continuous lifetime of approximately **10 minutes**. Google servers issue a `GoAway` frame before terminating the transport.
- **MK-V Implementation:**
  - `ContextWindowCompressionConfig(sliding_window=types.SlidingWindow())` is enabled, eliminating the 15-minute / 2-minute hard time wall.
  - `SessionResumptionConfig(handle=...)` captures dynamic resumption handles (`new_handle`) from `session_resumption_update` frames.
  - Handles remain valid for up to **2 hours**, allowing sub-second connection recovery without losing active conversational context.
  - When a `GoAway` notification is received (`response.go_away`), the server sets `session_active = False` to gracefully drain inflight tasks and immediately reconnects in the background.
  - A local rolling transcript buffer (`rolling_transcript`, sliding window of 8 turns) persists conversational context across hard restarts or expired handles via `session.send_client_content()`.

### 1.3 LiveConnectConfig Compatibility & Model Constraints
- **Supported Fields in `google-genai` 2.31.0:**
  `response_modalities`, `speech_config`, `system_instruction`, `tools`, `session_resumption`, `input_audio_transcription`, `output_audio_transcription`, `context_window_compression`, `media_resolution`, `temperature`, `top_p`, `top_k`, `max_output_tokens`.
- **Unsupported / Restricted Fields:**
  - `transparent=True` in `SessionResumptionConfig` is **unsupported** in Gemini Developer API mode (`ValueError: transparent parameter is only supported in Gemini Enterprise Agent Platform mode`). MK-V strictly configures `types.SessionResumptionConfig(handle=...)`.
  - Passing raw dictionaries as tool declarations fails inside `google-genai` Pydantic validators; all tools must be passed as callable Python functions with type annotations and docstrings.

### 1.4 Tool Scheduling & Response Handling
- Tool execution in `server.py` is executed via `asyncio.to_thread()` to prevent blocking the asyncio event loop.
- Tool responses are dispatched to Gemini via `session.send_tool_response(function_responses=[types.FunctionResponse(id=..., name=..., response={"result": ...})])`.
- If an action is denied by the user via the Human-In-The-Loop (HITL) gate, a structured rejection message is returned so the model can acknowledge the denial rather than timing out.

---

## 2. Security & Trust Boundaries Audit

### 2.1 Exposed Tool Catalog & Categorization
A total of **35 tools** (32 built-in, 3 dynamic skills) are exposed to Gemini:
- **Tier 1 (Read-Only Inspection):** Auto-allowed and logged to `audit_log.jsonl`.
- **Tier 2 (Reversible UI Automation):** Auto-allowed under clean conditions; requires HITL confirmation if tainted by untrusted inputs within 60 seconds.
- **Tier 3 (High-Impact Side Effects):** Always requires physical human confirmation via HUD button or `Enter`/`Esc` keypress.

### 2.2 Attack Vectors & Indirect Prompt Injection
- **Untrusted Input Channels:** Desktop screen contents, OCR text, files on disk, and ambient audio from room speakers.
- **Persistence Risks:** Previously, malicious instructions could modify `custom_skills/` or `memory.json` to persist across restarts.
- **MK-V Mitigations:**
  1. `TaintTracker`: Tracks exposure to screen frames, OCR, and file reads in a 60-second sliding decay.
  2. `Immutable Core`: Core files (`server.py`, `tools.py`, `learning_engine.py`, `security_policy.py`, `desktop_app.py`, `Chirkoot.exe`) are read-only to tools.
  3. Physical HITL Confirmation: High-risk calls pause execution and require physical approval; voice approval alone is explicitly rejected.

### 2.3 Command & File Sandboxing
- **Sensitive Path Deny-List:** `read_file` and `write_file` reject access to `.env`, `.chirkoot_profile`, `.ssh`, `.aws`, browser cookies, and the DPAPI vault.
- **Process Tree Termination:** Commands executed via `run_command` are killed using native Windows `taskkill /F /T /PID <pid>` on timeout, preventing orphaned background worker processes.
- **Output Capping:** Command output is strictly capped at **8 KB** and **100 lines** to prevent context flooding.
- **DPAPI Credential Storage:** `GEMINI_API_KEY` is encrypted using the Windows user's master key via native DPAPI (`CryptProtectData`) and stored in `%LOCALAPPDATA%\Chirkoot\vault\credentials.dat`.

### 2.4 Localhost WebSocket Security (CSWSH Prevention)
- Browsers do not enforce Same-Origin Policy on WebSockets. Any malicious site on the internet could attempt `ws://127.0.0.1:8000/ws`.
- **MK-V Safeguards:**
  1. **Strict Origin Validation:** Foreign web origins are rejected with HTTP 403 / WS 1008.
  2. **Per-Launch Launch Token:** 32-byte cryptographically secure token (`?token=...`) required on handshake.
  3. **Single Client Gatekeeper:** Rejects secondary concurrent client sessions.

### 2.5 Multi-Tier Emergency Stop
- Client-side Web Speech recognition locally intercepts stop words (*"stop"*, *"halt"*, *"cancel"*, *"ruko"*, *"bas"*), aborting streaming before audio reaches Gemini.
- Global hotkeys (`Ctrl+Shift+X` and double-tap `Escape` within 400ms).
- Cockpit UI Emergency Stop button and REST API (`POST /api/kill-switch`).

---

## 3. Audio Pipeline Audit

### 3.1 ScriptProcessorNode vs AudioWorklet
- **Current Baseline:** `static/app.js` uses `AudioContext.createScriptProcessor(2048, 1, 1)`.
- **Limitation:** `ScriptProcessorNode` is deprecated in Web Audio specifications and runs on the browser's main UI thread. High UI render loads (DOM redraws, visualizer canvas) cause audio buffer drops and glitching.
- **Target Architecture:** Transition to an isolated `AudioWorkletNode` running in a dedicated Web Audio rendering thread.

### 3.2 Sample-Rate Conversion & Aliasing
- **Current Baseline:** Input audio is resampled from browser native rate (44.1 kHz or 48 kHz) to 16 kHz using linear interpolation (`downsampleTo16k`).
- **Limitation:** Linear interpolation lacks a low-pass anti-aliasing filter, introducing high-frequency harmonic distortion.
- **Target Architecture:** Polyphase FIR decimation or `OfflineAudioContext` hardware-accelerated resampling.

### 3.3 Wake Word Engine & False-Wake Risks
- **Current Baseline:** Uses browser `window.webkitSpeechRecognition` with broad regex patterns (`/^(?:hey|hi|hello|ok|suno|bhai)?\s*(?:chirkoot|circuit|jarvis)\b/i`).
- **Limitations:**
  - High false-wake rate in noisy environments (television, background speech).
  - Web Speech API in Chromium routes audio through cloud servers, adding latency and network dependencies.
  - PyWebView (WebView2) lacks native `webkitSpeechRecognition`, forcing open mic fallback.
- **Target Architecture:** Embed a local, zero-network neural wake word engine (openWakeWord / ONNX) running client-side or backend-side.

### 3.4 Rolling Pre-Roll Buffer
- While asleep in wake-word mode, audio is accumulated into a rolling circular buffer (`wakePreRollBuffer`, 2.3 seconds capacity).
- Upon wake detection, the pre-roll buffer is flushed to the WebSocket, ensuring Gemini receives the initial command words without clipping.

---

## 4. Vision Pipeline Audit

### 4.1 Browser getDisplayMedia vs Server-Side Capture
- **Current Baseline:** Video feed captured via browser `navigator.mediaDevices.getDisplayMedia`.
- **Limitations:**
  - Requires explicit user prompt / permission dialog on every application startup.
  - Captures the Chirkoot HUD itself unless the user manually selects another application window or uses a secondary monitor.
  - Cannot capture when minimized or when the user switches to secure system dialogs (UAC).
- **Target Architecture:** Server-side capture using Windows Graphics Capture (WGC) API or DXGI Desktop Duplication (`dxcam`), allowing background window capture, HUD exclusion (`WDA_EXCLUDEFROMCAPTURE`), and zero browser prompts.

### 4.2 Transmission Metrics & Resolution
- Frame capture interval: **1 FPS** (1,000 ms).
- Resolution: Scaled to **1280x720** via offscreen HTML5 canvas.
- Compression: JPEG quality **0.72**, encoded to Base64 and transmitted over local WebSocket.

### 4.3 OCR Architecture & Disk Churn
- **Current Baseline:** `ocr_screen` writes the active frame to `%TEMP%\chirkoot_live_screen.jpg` before invoking `RapidOCR`.
- **Limitation:** Continuous disk writes cause unnecessary SSD churn.
- **Target Architecture:** Stream raw frame bytes directly to RapidOCR memory buffers (`numpy.ndarray`).

---

## 5. Desktop Automation Audit

### 5.1 Native Windows Win32 API
- Screen clicks and keystrokes are executed using `ctypes.windll.user32` (`mouse_event`, `keybd_event`, `SetCursorPos`).
- Normalized coordinates: Coordinates are mapped on a 0–1000 scale to physical pixels based on `GetSystemMetrics(0)` and `GetSystemMetrics(1)`.

### 5.2 Per-Monitor DPI & Multi-Display Scaling
- **Limitation:** Win32 `GetSystemMetrics` returns primary monitor resolution and does not account for per-monitor DPI scaling factors (e.g. 125%, 150%) or multi-monitor virtual desktop coordinates.
- **Target Architecture:** Implement `SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)` and query `GetDpiForWindow` / `EnumDisplayMonitors`.

### 5.3 UIPI (User Interface Privilege Isolation)
- Standard non-elevated processes cannot send synthetic mouse or keyboard events to elevated (Administrator) windows (e.g. Task Manager, elevated Terminal).
- Chirkoot runs at standard user integrity level by default, appropriately preventing unprivileged privilege escalation.

### 5.4 Clipboard Management & Unicode Typing
- **Limitation:** `type_text` uses `pyperclip.copy(text)` and synthetic `Ctrl+V` keystrokes, overwriting the user's existing clipboard contents.
- **Fallback Limitation:** Character fallback uses `VkKeyScan`, which fails on non-ASCII Unicode characters (emojis, Devanagari).
- **Target Architecture:** Preserve and restore the user's clipboard (`OpenClipboard` / `GetClipboardData`), and implement `SendInput` with `KEYEVENTF_UNICODE` (`0x0004`).

---

## 6. Self-Learning & Dynamic Skills Audit

### 6.1 Subprocess Isolation & Code Execution
- Custom dynamic skills in `%LOCALAPPDATA%\Chirkoot\custom_skills\` are executed in isolated Python subprocesses with base64 arguments via `execute_sandboxed_python_script`.
- Eliminates memory corruption risks and prevents dynamic code from tampering with server internals.

### 6.2 Code Modification & AST Verification
- `modify_source_code` creates timestamped backups in `%LOCALAPPDATA%\Chirkoot\backups\` before modifying target files.
- Python syntax is verified using `ast.parse()` before applying changes.
- Safe Mode (`--safe-mode`) boots without loading dynamic custom tools.

---

## 7. Memory Bank Audit

### 7.1 Provenance & Scoped Retrieval
- Runtime storage relocated to `%LOCALAPPDATA%\Chirkoot\data\memory.json`.
- Entries are tagged with source provenance (`user_explicit`, `screen_derived`, `ocr_derived`, `inferred`).
- Scoped top-$K$ retrieval (`get_scoped_memories(query, top_k=6)`) prevents prompt flooding.
- Review & Forget UI modal and REST endpoints allow granular user auditing and deletion.

### 7.2 Persistence Scalability
- **Limitation:** Flat JSON file storage is prone to race conditions under concurrent writes and lacks full-text index search.
- **Target Architecture:** Migrate to SQLite with FTS5 and sqlite-vec vector embeddings.

---

## 8. Windows Packaging & Distribution Audit

### 8.1 C# Launcher (`ChirkootLauncher.cs`)
- Compiled to `Chirkoot.exe` with Authenticode SHA-256 self-signed certificate.
- Dynamically locates user-profile Python 3.12 or system `python.exe`.
- Launches `desktop_app.py` in Chromium Native App Mode (`--app=http://127.0.0.1:8000`).

### 8.2 Deployment Isolation
- Isolated Chromium profile: `--user-data-dir=.chirkoot_profile` prevents cache collisions.
- User data isolated in `%LOCALAPPDATA%\Chirkoot`.
- **Target Architecture:** Package with embedded Python runtime to eliminate external Python installation dependencies.
