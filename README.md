# Gemini Live Multimodal Assistant (Screen Share & Camera)

An interactive conversational AI assistant replicating the **Gemini Live Multimodal experience on Android** (with screen sharing and camera vision) directly on your Windows desktop.

Powered by the official **Google Gemini 3.8 Live API** (`gemini-3.8-live`).

---

## Capabilities

1. **🖥️ Live Screen Sharing ("Ask About This Screen")**:
   - Click **Screen Share** to stream your screen (entire display, code editor, or specific browser window) to Gemini at 1 FPS.
   - Point to code, terminal outputs, error traces, designs, or documents and ask verbally:
     - *"What does this error message mean on my screen?"*
     - *"Explain this diagram in my browser."*
     - *"Help me debug the function shown on line 45."*

2. **📷 Live Camera / Video Mode**:
   - Click **Camera** to stream your webcam directly into Gemini Live.
   - Point your camera at anything and converse out loud in real time.

3. **🎙️ Natural Spoken Audio & Barge-In (Interruption)**:
   - Real-time full-duplex microphone streaming (16kHz PCM) and spoken voice playback (24kHz PCM).
   - Interrupt Gemini mid-sentence anytime by speaking.

4. **⚡ Autonomous Pair Programming & OS Tools**:
   - While watching your screen, Gemini can invoke host tools:
     - `run_command`: Executes PowerShell commands.
     - `read_file` / `write_file`: Inspects and updates code.
     - `system_diagnostics`: Reads hardware load.
     - `open_app_or_url`: Launches apps and URLs.

5. **🗗 Floating Picture-in-Picture (PiP)**:
   - Pop out the video stream into a floating desktop window while working in VS Code or browsing.

---

## How to Run

1. **One-Click Launch**:
   Run `gemini_live.bat` from the project folder.
   It will start the local server and automatically open `http://localhost:8000` in your browser.

2. **Terminal Launch**:
   ```bash
   python server.py
   ```
   Then open `http://localhost:8000` in Chrome or Edge.

---

## Technical Architecture

```
[ Browser Web Audio / getDisplayMedia / getUserMedia ]
                         |
           (16kHz PCM Audio & 1 FPS JPEG Frames)
                         v
       [ FastAPI WebSocket Proxy (server.py) ]
                         |
           (Continuous WebSocket Streaming)
                         v
     [ Google Gemini 3.8 Live (gemini-3.8-live) ]
                         |
                         +---> 24kHz Spoken Audio Stream (Aoede Voice)
                         +---> Autonomous Tool Calling (run_command, etc.)
```
