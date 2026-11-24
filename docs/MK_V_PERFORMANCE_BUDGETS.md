# Chirkoot MK-V Performance Budgets & SLA Targets

**Version:** 5.0.0-PROD  
**Target:** Chirkoot Desktop Multimodal Agent  
**Measurement Baseline:** Windows 11 x64, Intel Core i7 / AMD Ryzen 7 (6+ cores), 16 GB RAM, 100 Mbps broadband connection.  

---

## 1. Quantitative Performance SLA Matrix

| Metric Category | Target Parameter | Baseline (MK-IV) | Production SLA (MK-V) | Critical Threshold |
| :--- | :--- | :---: | :---: | :---: |
| **Conversational Latency** | **End-of-Speech to First Audio (p50)** | ~850 ms | **< 650 ms** | > 1,000 ms |
| | **End-of-Speech to First Audio (p95)** | ~1,450 ms | **< 1,100 ms** | > 1,600 ms |
| **Wake-Word Accuracy** | **False Accepts per Hour (Office <60dB)** | ~3.8 / hr | **< 0.2 / hr** | > 1.0 / hr |
| | **False Rejects on Clear Wake Call** | ~14% | **< 2.5%** | > 5.0% |
| **Session Reliability** | **Reconnect Time with Resumption Handle** | ~1,800 ms | **< 450 ms** | > 1,000 ms |
| | **Reconnect Time with Transcript Reseed** | ~3,200 ms | **< 1,200 ms** | > 2,500 ms |
| | **Session Uptime (Sliding Window)** | 15 min (Capped) | **Indefinite (>8 hrs)** | < 2 hrs |
| **Tool Execution** | **Standard Tool Execution Success Rate** | ~92.0% | **> 98.5%** | < 95.0% |
| | **UI Action Verification Rate (OCR/Diff)**| ~84.0% | **> 95.0%** | < 90.0% |
| | **HITL Confirmation Roundtrip Latency** | N/A (New) | **< 80 ms** | > 200 ms |
| **Token Consumption** | **Tokens / Hour (Audio-Only Dialogue)** | ~45,000 | **< 35,000** | > 60,000 |
| | **Tokens / Hour (Audio + 1 FPS 720p Video)**| Capped / OOM | **< 120,000** | > 180,000 |
| **Host System Footprint** | **Average CPU Usage (6-core host)** | ~11.5% | **< 7.5%** | > 15.0% |
| | **Peak CPU Usage (during OCR scan)** | ~38.0% | **< 25.0%** | > 45.0% |
| | **Total Combined RAM (Backend + HUD)** | ~480 MB | **< 380 MB** | > 600 MB |
| | **GPU 3D Engine Usage** | ~8.0% | **< 4.5%** | > 12.0% |

---

## 2. Measurement Methodologies & Test Harnesses

### 2.1 Conversational Turn Latency (End-of-Speech to First Audio)
- **Definition:** The elapsed wall-clock duration from when the user ceases speaking (VAD speech-end marker) to when the first decoded audio PCM sample is rendered through the host speakers.
- **Measurement Harness:**
  1. Synthetic 16 kHz audio utterance injected via WebSocket test client.
  2. Timestamp recorded at the last input audio packet.
  3. Client listener registers high-resolution timestamp (`performance.now()`) when the first `server_content.model_turn` audio chunk is received and scheduled for playback.
  4. 100 consecutive turns measured across diverse prompt lengths.

### 2.2 Wake-Word Accuracy & False Accept Rate
- **Definition:** Number of times the agent transitions from Standby to Active listening in the absence of an intentional user wake call.
- **Measurement Harness:**
  1. 8-hour continuous ambient audio playback test consisting of YouTube podcasts, standard television news broadcasts, and ambient office keyboard/chatter (<60 dB SPL).
  2. Total false-wake activations logged automatically to `audit_log.jsonl`.
  3. False Reject Test: 50 distinct speakers or acoustic variations speaking "Hey Chirkoot" at 1-meter distance.

### 2.3 Session Reconnection & Recovery Time
- **Definition:** The time interval between an abrupt WebSocket drop (or `GoAway` frame) and the successful re-establishment of the upstream session.
- **Measurement Harness:**
  1. Automated test script terminates the TCP socket during an active session.
  2. Measure time until `client.aio.live.connect` returns and the first valid frame response is confirmed.
  3. Validate context retention by querying a previously stated fact.

### 2.4 Tool Execution & UI Verification Rate
- **Definition:** Percentage of tool invocations that successfully complete their target OS modification and produce verifiable visual or system state changes.
- **Measurement Harness:**
  1. 50 automated UI interaction tests (opening Notepad, typing a unique GUID, clicking Save, closing window).
  2. Verification performed by performing an OCR scan of the screen post-action to confirm GUID appearance.

### 2.5 Token Consumption Budgeting
- **Audio-Only Budget:** With sliding window context compression, rolling conversational history is capped at 8 turns (~2,000 tokens of context). Average continuous voice input generates ~500 tokens per minute. Target: < 35,000 tokens/hr.
- **Vision Budget:** 1 FPS 720p JPEG frames generate approximately 258 tokens per frame. With sliding window compression enabled, server-side frame eviction limits active vision buffer to recent frames, staying under 120,000 tokens/hr.

### 2.6 System Resource Footprint
- **Process Accounting:** Measured via `psutil` sampling both the Python FastAPI server process and the child Chromium `--app` process every 500 ms over a 2-hour continuous test session.
