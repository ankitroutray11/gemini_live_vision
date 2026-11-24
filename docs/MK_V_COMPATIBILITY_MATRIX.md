# Chirkoot MK-V Compatibility Matrix

**Version:** 5.0.0-PROD  
**Target:** Chirkoot Desktop Multimodal Agent  

---

## 1. Operating System Compatibility

| OS Version | Architecture | Support Status | Notes / Prerequisites |
| :--- | :---: | :---: | :--- |
| **Windows 11 23H2 / 24H2** | x64 (AMD64) | **Tier 1 (Target)** | Primary development and testing environment. Full DPAPI, DirectShow, and Win32 support. |
| **Windows 11 21H2 / 22H2** | x64 (AMD64) | **Supported** | Fully compatible. |
| **Windows 10 21H2 / 22H2** | x64 (AMD64) | **Supported** | Compatible. Chrome/Edge App Mode functions identically. |
| **Windows Server 2022** | x64 (AMD64) | **Degraded** | Requires desktop experience components and audio services enabled. |
| **Windows on ARM (WoA)** | ARM64 | **Untested** | Requires emulation or ARM64 Python wheel compilation for `rapidocr_onnxruntime`. |
| **macOS / Linux** | x64 / ARM64 | **Unsupported** | MK-V uses native Windows DPAPI, Win32 `user32.dll` APIs, and Windows process trees (`taskkill`). |

---

## 2. Python Runtime Compatibility

| Python Version | Status | Verification Findings |
| :---: | :---: | :--- |
| **Python 3.12.x** | **Target / Certified** | Verified with Python 3.12.10 (AMD64). Clean syntax, fast startup, all wheels supported. |
| **Python 3.11.x** | **Supported** | Fully compatible with all dependencies (`fastapi`, `uvicorn`, `google-genai`, `rapidocr_onnxruntime`). |
| **Python 3.10.x** | **Supported** | Compatible. Older type syntax supported. |
| **Python 3.13.x** | **Caution / Experimental** | Certain binary wheels (such as `onnxruntime` or `pywin32`) may require pre-release wheels. |
| **Python 3.9 and older** | **Unsupported** | Missing modern `typing` and `asyncio` task group enhancements. |

---

## 3. Google GenAI SDK Compatibility

| SDK Version | Status | Supported Features & Restrictions |
| :---: | :---: | :--- |
| **`google-genai` 2.31.0** | **Certified Target** | Full support for `client.aio.live.connect(model="gemini-3.8-live")`, `SessionResumptionConfig`, `ContextWindowCompressionConfig`, and callable tool auto-conversion. |
| **`google-genai` < 2.0.0** | **Incompatible** | Breaking API change: older SDKs use `google.generativeai` with completely different Live WebSocket signatures. |
| **Parameter: `transparent`** | **Restricted** | Must be omitted or `None` in Developer API mode (`ValueError` if `True`). |
| **Parameter: `tools`** | **Enforced** | Must be callable Python functions with docstrings. Raw dicts fail Pydantic model validation. |

---

## 4. Frontend Browser & Shell Runtime

| Browser / Shell | Priority | Execution Mode | Feature Parity |
| :--- | :---: | :--- | :--- |
| **Google Chrome 120+** | **Priority A (Default)** | `--app=http://127.0.0.1:8000` | Full support: Web Audio, Web Speech API, `getDisplayMedia`, canvas 720p. |
| **Microsoft Edge 120+** | **Priority B (Fallback)** | `--app=http://127.0.0.1:8000` | Full support: Chromium engine functions identically to Chrome. |
| **PyWebView (WebView2)** | **Priority C (Fallback)** | Native desktop window | Lacks `webkitSpeechRecognition`; forces open mic or manual wake mode. |
| **Mozilla Firefox** | **Manual Only** | Tab in browser | Full Web Audio; lacks Web Speech API wake-word support. |

---

## 5. Audio Hardware & Drivers

| Audio Component | Supported Standards | Configuration Guidelines |
| :--- | :--- | :--- |
| **Microphone Input** | WASAPI, DirectSound, USB Audio Class | Standard 44.1 kHz or 48 kHz hardware input resampled in browser to 16 kHz 16-bit PCM. |
| **Speaker Output** | Stereo, 5.1/7.1 downmixed | 24 kHz 16-bit PCM received from Gemini output decoded via Web Audio API. |
| **Echo Cancellation** | Hardware / Browser AEC | `echoCancellation: true`, `noiseSuppression: true`, `autoGainControl: true` enabled in `getUserMedia`. |
| **ASIO Drivers** | Not directly supported | Bypassed through standard Windows Audio Endpoint. |

---

## 6. Display & GPU Topologies

| Display Configuration | Support Status | Operational Notes |
| :--- | :---: | :--- |
| **Single Monitor (1080p, 100% DPI)** | **Optimal** | Coordinate mapping 0–1000 maps 1:1 with zero pixel offset. |
| **Single Monitor (4K, 150%/200% DPI)** | **Supported** | High-DPI scaling handled by Chromium; coordinate normalization scales appropriately. |
| **Multi-Monitor (Identical DPI)** | **Supported** | `getDisplayMedia` allows user to select specific screen or application window. |
| **Multi-Monitor (Mixed DPI Scaling)** | **Degraded** | Win32 mouse clicks default to primary display coordinates; P1 roadmap addresses per-monitor DPI. |
| **Dedicated GPU (NVIDIA / AMD)** | **Supported** | RapidOCR leverages DirectML or CUDA when available; otherwise falls back to multithreaded CPU ONNX. |
| **Integrated GPU (Intel UHD / Iris)** | **Supported** | Minimal GPU impact (<5% 3D engine usage for canvas rendering). |
