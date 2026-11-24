# Chirkoot MK-V Implementation Roadmap

**Version:** 5.0.0-PROD  
**Target:** Chirkoot Desktop Multimodal Agent  
**Roadmap Prioritization:** P0 (Immediate Safety & Reliability) ➔ P1 (Automation & Vision Hardening) ➔ P2 (Enterprise Packaging & Scalability)  

---

## 1. Prioritization Matrix Overview

| Priority | Focus Area | Key Objectives | Risk if Deferred |
| :---: | :--- | :--- | :--- |
| **P0** | **Safety, Audio & Core Reliability** | AudioWorklet, local offline wake-word, clipboard preservation, Unicode typing. | Audio stutter during UI rendering, cloud speech false-wakes, user clipboard corruption. |
| **P1** | **Vision, Automation & Memory** | Windows Graphics Capture (WGC), DPI awareness, UI Automation (UIA), SQLite FTS5. | Browser capture permission prompts, coordinate misses on high-DPI screens, SSD churn. |
| **P2** | **Packaging & Git Lifecycle** | Embedded Python runtime, automated Git checkpointing, native desktop wrapper. | Broken host Python dependencies, lack of versioned rollbacks. |

---

## 2. Priority 0 (P0) — Immediate Safety, Audio & Core Reliability

### P0.1 AudioWorklet Migration
- **Current State:** `static/app.js` uses `AudioContext.createScriptProcessor(2048, 1, 1)` running on the main browser thread.
- **Problem:** Heavy DOM rendering, canvas visualizer loops, or window resizing causes buffer underruns, creating audible audio glitches and dropped microphone packets.
- **Task:** Implement `static/audio-processor.js` extending `AudioWorkletProcessor`. Stream raw PCM buffers over a dedicated `MessagePort` to the WebSocket worker thread.
- **Target Performance:** Zero audio underruns during 60 FPS HUD animations.

### P0.2 Polyphase FIR Anti-Aliased Resampling
- **Current State:** `downsampleTo16k` uses linear interpolation without low-pass filtering.
- **Problem:** Aliasing artifacts from 44.1 kHz / 48 kHz hardware feeds distort high-frequency consonants ("s", "t", "f"), reducing Gemini transcription accuracy.
- **Task:** Implement polyphase FIR filtering or utilize Web Audio `OfflineAudioContext` to decimate input cleanly at the Nyquist frequency (8 kHz cutoff).
- **Target Performance:** Signal-to-Noise Ratio (SNR) > 75 dB across 100 Hz – 7.5 kHz.

### P0.3 Local Offline Neural Wake Word Engine
- **Current State:** Relies on browser `window.webkitSpeechRecognition` sending audio to Google speech servers with broad regex patterns.
- **Problem:** Significant false-wake rate (averaging > 4 false wakes/hour in noisy rooms); non-functional in PyWebView / WebView2 environments.
- **Task:** Integrate `openWakeWord` or ONNX-based Keyword Spotting (KWS) locally in Python or via WebAssembly in the frontend. Model trained on "Hey Chirkoot" and "Chirkoot".
- **Target Performance:** < 0.2 false accepts per hour in standard office environments (<60 dB).

### P0.4 Clipboard Preservation & Restoration
- **Current State:** `type_text` executes `pyperclip.copy(text)` and sends `Ctrl+V`, destroying whatever was previously in the user's system clipboard.
- **Problem:** Silently destroys user clipboard contents during automated typing.
- **Task:** Query `win32clipboard.OpenClipboard()`, capture existing format data (`CF_UNICODETEXT`, bitmaps, files), execute `Ctrl+V`, and restore previous clipboard data within 80 ms.
- **Target Performance:** 100% preservation of pre-existing clipboard contents.

### P0.5 Unicode SendInput Typing
- **Current State:** Fallback typing calls `VkKeyScan(char)` and `keybd_event`, which fails on non-ASCII characters (emojis, Devanagari, symbols).
- **Problem:** Inability to type Hindi or international characters when clipboard fallback is triggered.
- **Task:** Implement `SendInput` using `KEYEVENTF_UNICODE` (`0x0004`) in `tools.py`.
- **Target Performance:** Full UTF-16 character set support without clipboard dependency.

---

## 3. Priority 1 (P1) — Automation, Vision & Memory Hardening

### P1.1 Server-Side Desktop Capture (Windows Graphics Capture / DXGI)
- **Current State:** Uses browser `navigator.mediaDevices.getDisplayMedia`.
- **Problem:** Prompts the user on every startup, cannot capture background applications, and captures the Chirkoot HUD itself unless manually configured.
- **Task:** Implement background screen capture in Python using `dxcam` (DirectX Desktop Duplication) or Windows Graphics Capture API.
- **Features:**
  - Exclude Chirkoot HUD window automatically via `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`.
  - Zero browser prompts on boot.
  - Multi-monitor monitor selection by display index.
- **Target Performance:** 1080p capture latency < 16 ms at < 3% CPU usage.

### P1.2 Per-Monitor DPI Awareness
- **Current State:** Coordinates are computed against primary screen `GetSystemMetrics(0)` and `GetSystemMetrics(1)`.
- **Problem:** Clicks miss buttons on high-DPI laptops (125%, 150%, 200%) and multi-monitor setups with mixed scaling.
- **Task:** Enable `SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)` and query `GetDpiForWindow` to compute exact physical click targets.
- **Target Performance:** 0-pixel offset error across any display scaling setting.

### P1.3 Structured Windows UI Automation (pywinauto / UIA)
- **Current State:** Pure coordinate clicking and OCR matching.
- **Problem:** Brittle automation on responsive or dynamically resizing desktop applications.
- **Task:** Expose Windows UI Automation tree navigation tools (`find_ui_element`, `click_ui_element_by_name`, `list_window_controls`) alongside raw coordinate clicks.
- **Target Performance:** > 98% UI interaction success rate on standard Win32, WPF, and UWP controls.

### P1.4 In-Memory OCR Frame Processing
- **Current State:** `ocr_screen` writes each captured frame to `%TEMP%\chirkoot_live_screen.jpg`.
- **Problem:** SSD write churn and disk I/O latency.
- **Task:** Pass raw JPEG/PNG byte streams or NumPy image arrays directly into `RapidOCR.run(img_array)`.
- **Target Performance:** OCR invocation latency reduced by 40 ms; zero disk I/O.

### P1.5 SQLite + FTS5 & Vector Embeddings Memory Store
- **Current State:** Flat `memory.json` file.
- **Problem:** Race conditions on concurrent writes, lack of full-text search indexing, and inability to perform semantic vector search.
- **Task:** Migrate `%LOCALAPPDATA%\Chirkoot\data\memory.json` to an embedded SQLite database with `FTS5` full-text search and `sqlite-vec` embeddings.
- **Target Performance:** Sub-5ms query latency across 10,000+ stored memories.

---

## 4. Priority 2 (P2) — Enterprise Packaging & Lifecycle Hardening

### P2.1 Embedded Python Runtime Distribution
- **Current State:** `ChirkootLauncher.cs` depends on an existing Python 3.12 installation in `%LOCALAPPDATA%\Programs\Python` or system `PATH`.
- **Problem:** Fails if the user does not have Python installed or has conflicting virtualenvs.
- **Task:** Bundle Python 3.12 Windows Embedded Runtime (`python-3.12.x-embed-amd64`) with pre-installed wheels in a dedicated application folder.
- **Target Performance:** True zero-dependency single-installer deployment.

### P2.2 Programmatic Git Checkpointing for Self-Learning
- **Current State:** `modify_source_code` makes static copies in `backups/`.
- **Problem:** Difficult to inspect historical diffs or perform selective multi-file rollbacks.
- **Task:** Initialize an internal Git repository in the user data directory and execute programmatic `git commit` checkpoints before every self-learning modification.
- **Target Performance:** Instant rollback (`git reset --hard HEAD~1`) and unified diff viewing.

### P2.3 Hardened Native Desktop Shell
- **Current State:** Launches Google Chrome or Microsoft Edge in `--app` mode.
- **Problem:** Chrome may attach to an existing user browser process or show external browser notifications.
- **Task:** Package HUD as a dedicated C# WebView2 or Electron application with embedded devtools and native OS titlebar controls.
