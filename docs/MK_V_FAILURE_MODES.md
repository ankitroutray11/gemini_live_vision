# Chirkoot MK-V Failure Modes and Effects Analysis (FMEA)

**Version:** 5.0.0-PROD  
**Target:** Chirkoot Desktop Multimodal Agent  

---

## 1. Failure Modes Summary Table

| ID | Failure Mode | Severity | Probability | Detection Mechanism | Recovery Strategy |
| :---: | :--- | :---: | :---: | :--- | :--- |
| **FM-01** | Upstream Gemini `GoAway` Frame Received | Medium | High | `response.go_away` in stream. | Graceful drain, sub-second reconnection using active `session_resumption` handle. |
| **FM-02** | Session Resumption Handle Expiration (>2 hrs) | Low | Medium | Upstream connect handshake rejection. | Fallback to cold start; re-seed conversational context via `rolling_transcript`. |
| **FM-03** | Localhost CSWSH Attempt by Malicious Webpage | Critical | Medium | `Origin` header check / missing launch token. | Immediate rejection with HTTP 403 / WS 1008; security audit event logged. |
| **FM-04** | Indirect Prompt Injection via Screen / OCR | High | High | Dynamic `TaintTracker` trigger. | Tool escalated to Tier 3; physical confirmation modal prompted; voice denied. |
| **FM-05** | Web Speech API False Wake-Up in Ambient Noise | Medium | High | Web speech regex match without command. | 12-second sleep timer auto-reverts to standby if no follow-up command arrives. |
| **FM-06** | Audio Buffer Drop during Heavy UI Render | Medium | Medium | Web Audio buffer underrun. | Maintained 2048-sample buffer size; AudioWorklet migration planned in P0 roadmap. |
| **FM-07** | Automated Action Target is Elevated (UIPI Block) | Medium | Low | Win32 `SetCursorPos` / `keybd_event` fails. | Return error message to Gemini indicating target window requires elevation. |
| **FM-08** | Shell Command Timeout / Infinite Loop | High | Medium | 15.0s timer in `execute_sandboxed_command`. | Forceful process tree termination via `taskkill /F /T /PID`; context capped at 8 KB. |
| **FM-09** | Infinite Screen Mirroring (HUD Captures HUD) | Low | High | Visual feedback loop in viewport. | Anti-self-view guidance banner displayed; ALT+TAB helper button offered. |
| **FM-10** | DPAPI Key Decryption Failure on Host | High | Low | Windows `CryptUnprotectData` fails. | Prompt user in console or HUD to re-enter API key safely; fallback to user prompt. |

---

## 2. Detailed Failure Mode Analysis & Mitigations

### FM-01: Upstream Gemini GoAway Handling
- **Root Cause:** Google Gemini Live infrastructure enforces connection rotation approximately every 10 minutes by sending a `GoAway` frame.
- **Symptom:** Without handling, the WebSocket is abruptly severed, causing silence and frozen interaction.
- **MK-V Recovery:**
  1. `receive_from_gemini` captures `response.go_away`.
  2. Sets `session_active = False`, cleanly completing inflight tasks.
  3. The outer connection manager loop immediately attempts reconnect using `types.SessionResumptionConfig(handle=latest_resumption_handle)`.
  4. Context, system instructions, and tools remain synchronized seamlessly without disturbing the local browser HUD connection.

### FM-02: Resumption Handle Expiry / Reconnect Failure
- **Root Cause:** Session resumption handles expire after 2 hours or become invalid upon backend restart.
- **Symptom:** Upstream server returns a connection error when attempting to resume.
- **MK-V Recovery:**
  1. `server.py` invalidates `latest_resumption_handle = None`.
  2. Initiates a fresh cold-start connection.
  3. Re-seeds conversational history by compiling the last 8 user/agent turns from `rolling_transcript` into `session.send_client_content(turns=..., turn_complete=False)`.

### FM-03: Cross-Site WebSocket Hijacking (CSWSH)
- **Root Cause:** Web browser opens a malicious site (`evil-domain.com`) that executes background JavaScript attempting to connect to `ws://127.0.0.1:8000/ws`.
- **Symptom:** Attacker attempts to hijack computer control tools or exfiltrate private desktop state.
- **MK-V Mitigation:**
  1. `validate_origin()` inspects the HTTP `Origin` header. External origins are immediately terminated with HTTP 403 / WS Close Code 1008.
  2. Handshake requires `?token=<32-byte-token>`.
  3. Single-client gatekeeper blocks any secondary connection attempt.

### FM-04: Indirect Prompt Injection via Screen / OCR
- **Root Cause:** Malicious text on screen or in documents instructs the model to execute destructive commands.
- **Symptom:** Gemini attempts to call `run_command` or `modify_source_code`.
- **MK-V Mitigation:**
  1. Local Security Policy Dispatcher intercepts the call.
  2. High-impact tools (Tier 3) cannot run automatically.
  3. Physical modal appears on the HUD displaying the exact command payload and taint warnings.
  4. Execution is blocked unless the user physically clicks "Allow" or presses `Enter`.

### FM-05: False Wakes & Ambient Speech Bleed
- **Root Cause:** Television or nearby conversation triggers Web Speech recognition patterns.
- **Symptom:** HUD switches to listening state inadvertently.
- **MK-V Mitigation:**
  1. Audio is only streamed to Gemini if wake-word criteria are met.
  2. A 12-second sleep countdown timer (`wakeSleepTimer`) automatically returns Chirkoot to standby if no speech or command is delivered.
  3. Emergency stop interceptor immediately halts if any stop phrase is uttered.

### FM-06: Main-Thread Audio Glitches (ScriptProcessorNode)
- **Root Cause:** Browser executes JavaScript audio processing on the same UI thread as canvas rendering.
- **Symptom:** Choppy audio during heavy visual animations or window resizing.
- **MK-V Mitigation:**
  1. Visualizer FFT size is kept light (`fftSize = 64`, `smoothingTimeConstant = 0.5`).
  2. Audio buffer size tuned to 2048 samples.
  3. Migration to isolated `AudioWorkletNode` prioritized as P0.

### FM-07: User Interface Privilege Isolation (UIPI) Blocking
- **Root Cause:** Windows security isolates standard user processes from sending synthetic input to elevated Administrator windows.
- **Symptom:** Clicks and keystrokes have no effect inside Task Manager or elevated command prompts.
- **MK-V Mitigation:**
  1. Action completes cleanly without crashing.
  2. Chirkoot reports the limitation back to the user or model, advising the user to interact with the elevated window manually.

### FM-08: Shell Command Timeout & Orphaned Process Trees
- **Root Cause:** A command executed via `run_command` hangs, prompts for interactive input, or launches a long-running daemon.
- **Symptom:** Hanging thread, background resource leak.
- **MK-V Mitigation:**
  1. 15.0-second strict timeout in `execute_sandboxed_command`.
  2. Native `taskkill /F /T /PID <pid>` forcefully kills the entire process tree, terminating all spawned children.
  3. Output capped at 8 KB / 100 lines.
