# Chirkoot Version Changelog & Evolution History

All notable changes to the Chirkoot Desktop Multimodal Agent are documented in this file.

---

## [5.0.0-PROD] — MK-V Production Security Hardened Baseline (Current)

### Added
- **Local Security Policy Dispatcher (`security_policy.py`):**
  - Three-tier tool risk partitioning: Tier 1 (Read-Only), Tier 2 (Reversible UI), Tier 3 (High-Impact Side Effects).
  - Physical Human-In-The-Loop (HITL) authorization gate: pauses execution of Tier 3 and tainted Tier 2 calls, presenting an on-screen HUD modal requiring explicit click or `Enter`/`Esc` keypress. Voice authorization alone is strictly rejected.
  - Dynamic `TaintTracker`: 60-second sliding decay window tracking exposure to untrusted inputs (screen frames, OCR, file reads).
- **Immutable Core Code Protection:**
  - `is_immutable_core_path()` permanently locks core files (`server.py`, `security_policy.py`, `learning_engine.py`, `tools.py`, `desktop_app.py`, `Chirkoot.exe`, `ChirkootLauncher.cs`) against agent modification.
- **Process Tree Sandboxing & Output Capping:**
  - `execute_sandboxed_command()` uses native Windows `taskkill /F /T /PID <pid>` on command timeout, eliminating orphaned background processes.
  - Command output is strictly capped at 8 KB (8,192 bytes) and 100 lines.
- **Hardware-Backed DPAPI Credential Vault:**
  - API keys encrypted with the logged-in user's master key via native Windows DPAPI (`CryptProtectData`/`CryptUnprotectData` via `ctypes.windll.crypt32`) and stored in `%LOCALAPPDATA%\Chirkoot\vault\credentials.dat`.
  - Sensitive files (`.env`, `.chirkoot_profile`, SSH keys, browser profiles, AWS/cloud credentials) strictly blocked from `read_file` and `write_file`.
- **Localhost WebSocket Hardening (CSWSH Prevention):**
  - Strict `Origin` header validation on `ws://127.0.0.1:8000/ws` rejecting foreign web origins with HTTP 403 / WS 1008.
  - Ephemeral 32-byte cryptographic token (`?token=...`) required on handshake.
  - Single active client session gatekeeper.
- **Multi-Tier Emergency Kill Switch:**
  - Client-side Web Speech recognition stop-word interceptor (*"stop"*, *"halt"*, *"cancel"*, *"ruko"*, *"bas"*) halts streaming before audio reaches Gemini.
  - Global hardware hotkeys (`Ctrl+Shift+X` and double-tap `Escape` within 400ms).
  - HUD Emergency Stop button and REST API (`POST /api/kill-switch` / `POST /api/kill-switch/reset`).
- **Memory Provenance & Scoped Top-K Retrieval:**
  - Source provenance tagging on every memory entry (`user_explicit`, `screen_derived`, `ocr_derived`, `inferred`). Only user-explicit inputs modify agent preferences.
  - Top-$K$ semantic retrieval (`get_scoped_memories(query, top_k=6)`) replacing full memory dumps.
  - Review & Forget UI modal and REST endpoints (`GET /api/memories`, `DELETE /api/memories/{category}/{key}`).
- **Gemini Live 3.8 Lifecycles & Always-On Continuity:**
  - `ContextWindowCompressionConfig(sliding_window=types.SlidingWindow())` lifts 15-minute / 2-minute session time walls.
  - `SessionResumptionConfig(handle=...)` captures dynamic resumption handles (2-hour validity) for sub-second background reconnects.
  - Graceful `GoAway` frame handling with automatic background reconnection.
  - Rolling conversational transcript buffer (8 turns) re-seeds context on cold restarts via `send_client_content()`.
- **Subprocess Isolation for Custom Dynamic Skills:**
  - Dynamic tools executed in isolated Python subprocesses with base64 arguments via `execute_sandboxed_python_script`.
  - Safe Mode (`--safe-mode` / `CHIRKOOT_SAFE_MODE=1`) disabling all custom dynamic tools.

### Changed
- Relocated mutable user data, custom skills, backups, logs, and vault from repository root to `%LOCALAPPDATA%\Chirkoot`.
- Upgraded HUD branding and initial status telemetry to `CHIRKOOT MK-V`.

---

## [4.0.0] — MK-IV Tactical Neural HUD & Always-On Wake Word
- Futuristic JARVIS-themed tactical HUD with animated Arc Reactor visualizer, audio spectrum rays, and CRT scanlines.
- Continuous Web Speech API wake-word detector with English, Hindi, and Hinglish regex patterns.
- 2.3-second rolling pre-roll audio buffer (`wakePreRollBuffer`) ensuring command words are never clipped on wake.
- Native Chrome and Edge App Mode launcher with fallback to PyWebView.
- Authenticode SHA-256 self-signed C# wrapper executable (`Chirkoot.exe`).

---

## [3.0.0] — MK-III Self-Learning Engine & Dynamic Skills
- Implemented `learning_engine.py` allowing Chirkoot to inspect its own architecture and rewrite its code on command.
- Dynamic custom skills loader (`custom_skills/`) creating new Python functions at runtime.
- Pre-modification code backups and AST syntax validation (`ast.parse()`).
- Initial persistent memory bank (`memory.json`).

---

## [2.0.0] — MK-II Multimodal Vision & Computer Use
- Screen Radar display uplink (1 FPS 720p JPEG stream via `getDisplayMedia`).
- Native Win32 mouse control (`click_screen`), typing (`type_text`), and hotkeys (`press_hotkey`) via `user32.dll`.
- Optical Character Recognition on live screen via `RapidOCR` with coordinate normalization (0–1000).

---

## [1.0.0] — MK-I Initial Prototype
- Basic bidirectional audio streaming over WebSocket with early Gemini Live prototype models.
- Minimal text and audio input terminal.
