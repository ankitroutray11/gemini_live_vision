# Chirkoot MK-V Known Limitations & Technical Debt

**Version:** 5.0.0-PROD  
**Target:** Chirkoot Desktop Multimodal Agent  

---

## 1. Architectural & Technical Limitations Inventory

### 1.1 Audio Subsystem Limitations
- **Deprecated Web Audio ScriptProcessorNode:**
  - Audio capture in `static/app.js` uses `AudioContext.createScriptProcessor(2048, 1, 1)`, which is deprecated by W3C standards.
  - The node executes on the browser's main JavaScript UI thread. Under heavy rendering load (such as canvas animations, window resizes, or rapid DOM mutations), the audio thread can experience buffer underruns resulting in dropped audio frames.
- **Linear Resampling Aliasing:**
  - `downsampleTo16k` uses linear interpolation to convert browser audio (typically 44.1 kHz or 48 kHz) to the 16 kHz required by Gemini Live.
  - Linear interpolation lacks a low-pass anti-aliasing filter, introducing high-frequency harmonic distortion.
- **Web Speech API Wake-Word Fragility:**
  - Wake-word detection currently relies on `window.webkitSpeechRecognition`.
  - In Chromium browsers, this API routes speech audio through external Google Cloud speech recognition services, introducing internet latency, privacy concerns, and bandwidth overhead.
  - PyWebView (WebView2) lacks native `webkitSpeechRecognition`, disabling the wake-word feature when running under that fallback shell.
  - High susceptibility to false-wake activations in ambient noise or when TV/video audio is playing.

---

### 1.2 Vision & Screen Capture Limitations
- **Browser getDisplayMedia Permission Gate:**
  - Screen capture is initiated via `navigator.mediaDevices.getDisplayMedia`. This requires an interactive user permission prompt on every boot.
  - The browser cannot capture desktop windows if the host OS enters secure desktop mode (e.g. UAC prompts, Windows lock screen).
- **HUD Self-Capture Loop:**
  - When capturing the entire desktop display, the Chirkoot HUD window captures itself unless the user moves Chirkoot to a secondary monitor or manually selects a specific target application window in the browser picker.
- **OCR Frame Disk Churn:**
  - `ocr_screen` writes the captured frame to `%TEMP%\chirkoot_live_screen.jpg` before invoking `RapidOCR`.
  - While functional, repeated disk writes cause unnecessary SSD wear and add ~40 ms of file system latency per scan.

---

### 1.3 Desktop Automation & Input Limitations
- **Lack of Structured Windows UI Automation (UIA):**
  - Chirkoot automates applications using coordinate-based mouse clicks (`user32.mouse_event`) and OCR text searches rather than querying structured UI Automation trees (`pywinauto` / `UIAutomationCore`).
  - Dynamic UI reflows or responsive layouts can shift buttons, causing missed clicks if the model relies on stale coordinates.
- **Per-Monitor DPI Scaling Offsets:**
  - Coordinate normalization (0–1000) calculates pixel positions using primary monitor metrics (`GetSystemMetrics(0)` and `GetSystemMetrics(1)`).
  - On multi-monitor setups with disparate scaling factors (e.g., 4K laptop at 150% scaling + 1080p external monitor at 100% scaling), clicks on secondary displays may suffer from coordinate offsets.
- **User Interface Privilege Isolation (UIPI):**
  - Chirkoot runs at standard user integrity. Windows UIPI blocks synthetic mouse and keyboard events from being delivered to elevated Administrator applications (e.g., Task Manager, elevated PowerShell, Services console).
- **Clipboard Overwriting in `type_text`:**
  - `type_text` copies text to the system clipboard and triggers `Ctrl+V`. This overwrites whatever data the user previously had copied to their clipboard without saving and restoring it.
- **Unicode Fallback Typing:**
  - When the clipboard is unavailable, typing falls back to `VkKeyScan(char)`. `VkKeyScan` can only translate ASCII and ANSI characters mapped to the active keyboard layout, failing on emojis and international Unicode characters (such as Devanagari script).

---

### 1.4 Memory & Data Persistence Limitations
- **Flat JSON Memory Bank:**
  - Memories are stored in `%LOCALAPPDATA%\Chirkoot\data\memory.json`.
  - The JSON format does not support ACID transactions, making it vulnerable to partial writes or file corruption if the process terminates abruptly during a save.
- **Lack of Native Vector Search:**
  - Memory recall currently relies on substring and token matching (`get_scoped_memories(query, top_k=6)`). It lacks true semantic vector embeddings (e.g. via `sqlite-vec`), limiting associative recall.

---

### 1.5 Windows Packaging & Deployment Limitations
- **External Python Installation Dependency:**
  - `ChirkootLauncher.cs` locates an existing Python 3.12 executable on the host machine. If Python is not installed or dependencies are missing, Chirkoot fails to launch.
- **Chrome / Edge App-Mode Dependency:**
  - Chirkoot launches Google Chrome or Microsoft Edge with `--app=http://127.0.0.1:8000`. If neither browser is installed, it falls back to PyWebView or the default browser tab.
