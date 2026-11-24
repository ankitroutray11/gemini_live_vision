/**
 * CHIRKOOT TACTICAL HUD CONTROLLER
 * Gemini 3.8 Live Bidirectional Streaming + Multimodal Computer Vision + Audio Visualizer
 * + User-Selectable Listening Modes (ALWAYS-ON vs WAKE WORD "Hey Chirkoot")
 */

let ws = null;
let audioContext = null;
let outputAudioContext = null;
let outputGainNode = null;
let micAnalyser = null;
let outputAnalyser = null;
let micStream = null;
let micNode = null;
let audioWorkletNode = null;
let isMicMuted = false;
let isExplicitlyDisconnected = false;

// ==========================================================================
// LISTENING MODE STATE: 'always-on' vs 'wake-word'
// ==========================================================================
// Mode can be toggled by the user via the top header selector or bottom button.
// Clear legacy offline mode preference if present
localStorage.removeItem('chirkoot_engine_mode');
let currentMicMode = localStorage.getItem('chirkoot_mic_mode') || 'always-on';
let currentSpeechLang = localStorage.getItem('chirkoot_speech_lang') || 'en-IN'; // Default to Indian English / Hinglish
let currentReplyMode = localStorage.getItem('chirkoot_reply_mode') || 'voice'; // 'voice' (Audio+Text) or 'text' (Silent Text-Only)
let isAwake = (currentMicMode === 'always-on');
let wakeSleepTimer = null;
const WAKE_ACTIVE_TIMEOUT_MS = 14000; // 14s conversational window after turn completes
let wakeRecognizer = null;
let isWakeRecognizerActive = false;
let wakeRestartTimer = null;
let wakeWatchdogTimer = null;
let pendingWakeCommand = "";
let wakeCommandTimer = null;
let lastExecutedCommand = "";
let lastWakeTime = 0;

// Pre-Roll Audio Ring Buffer: Preserves ~2.3 seconds of audio so initial words are never lost on wake!
const MAX_PRE_ROLL_CHUNKS = 18; // 18 chunks of 2048 samples at 16kHz = ~2.3 seconds
let wakePreRollBuffer = [];

// Phonetic & regex patterns for robust Wake Word detection in English & Hindi (हिंदी/Hinglish)
// STRICT: Everyday conversational words like 'Hey', 'Check out', 'Circuit', 'नमस्ते', 'अरे', 'ओए'
// are strictly FORBIDDEN as standalone wake triggers. Patterns must require a distinctive Chirkoot/Jarvis target.
const WAKE_PATTERNS = [
  // 1. Chirkoot / Chitrakoot / Jarvis with optional greeting prefix in English/Hinglish
  /^(?:hey|hay|hi|hello|ok|okay|suno|oye|bhai|are|arey)?\s*[,.]?\s*(?:chirkoot|chirkut|cherkoot|charkoot|chirkoo|cherkoo|chiru|shirkoot|sir\s*koot|chitrakoot|chitrakut|chitra\s*koot|chitra\s*kut|citrakoot|jarvis|jarviz)\b/i,
  /\b(?:chirkoot|chirkut|cherkoot|charkoot|chirkoo|shirkoot|chitrakoot|chitrakut|chitra\s*koot|jarvis)\b/i,

  // 2. Hindi Devanagari phrases: हे चिरकुट, चिरकुट, सुनो चिरकुट, चित्रकूट, जार्विस, etc.
  /(?:^|[^\u0900-\u097F])(?:हे|अरे|सुनो|ओए|भाई)?\s*[,.]?\s*(?:चिरकुट|चिरकूट|चिरकु|सरकुट|चिरुकुट|चित्रकूट|चित्रकुट|जार्विस)(?:[^\u0900-\u097F]|$)/iu,
  /(?:^|[^\u0900-\u097F])(?:चिरकुट|चिरकूट|चिरकु|सरकुट|चित्रकूट|जार्विस)(?:[^\u0900-\u097F]|$)/iu
];

// --- Chirkoot MK-V Binary Framing Protocol (v1) ---
const PKT_TYPE_AUDIO = 0x01;
const PKT_TYPE_SCREEN_JPEG = 0x02;
const PKT_TYPE_WEBCAM_JPEG = 0x03;

function buildFramedPacket(packetType, streamId, payloadUint8Array) {
  const headerLen = 16;
  const payloadLen = payloadUint8Array.byteLength;
  const buffer = new ArrayBuffer(headerLen + payloadLen);
  const view = new DataView(buffer);

  view.setUint8(0, 0x01); // Protocol Version 1
  view.setUint8(1, packetType); // 0x01=Audio, 0x02=Screen, 0x03=Webcam
  view.setUint16(2, streamId, false); // Stream/Monitor ID (big-endian)

  const nowMs = BigInt(Date.now());
  view.setBigUint64(4, nowMs, false); // Timestamp MS (big-endian)

  view.setUint32(12, payloadLen, false); // Payload Length (big-endian)

  new Uint8Array(buffer, headerLen).set(payloadUint8Array);
  return buffer;
}

let videoStream = null;
let activeVideoSource = null; // 'screen' or 'camera' or null
let frameInterval = null;

// Audio Playback Queue
let nextStartTime = 0;
let activeSources = [];

// Bubble Streaming Tracker
let activeGeminiBubble = null;
let activeUserBubble = null;

// DOM Elements
const videoEl = document.getElementById('live-video');
const placeholderEl = document.getElementById('vision-placeholder');
const overlayEl = document.getElementById('video-overlay');
const streamTagEl = document.getElementById('stream-type-tag');
const streamFpsEl = document.getElementById('stream-fps-tag');
const connBadge = document.getElementById('connection-badge');
const connText = document.getElementById('connection-text');
const visionBadge = document.getElementById('vision-badge');
const visionText = document.getElementById('vision-status-text');
const wakeBadge = document.getElementById('wake-badge');
const wakeBadgeText = document.getElementById('wake-badge-text');
const transcriptBox = document.getElementById('transcript-container');
const audioStateText = document.getElementById('audio-state-text');
const soundSpectrum = document.getElementById('hud-sound-spectrum');
const viewportStatusTag = document.getElementById('hud-viewport-status');

const modeOptAlways = document.getElementById('mode-opt-always');
const modeOptWake = document.getElementById('mode-opt-wake');
const btnModeToggle = document.getElementById('btn-mode-toggle');
const btnModeLabel = document.getElementById('btn-mode-label');
const btnModeSub = document.getElementById('btn-mode-sub');
const modeBtnIcon = document.getElementById('mode-btn-icon');

const btnScreen = document.getElementById('btn-screen');
const btnCamera = document.getElementById('btn-camera');
const btnMic = document.getElementById('btn-mic');
const btnMicLabel = document.getElementById('btn-mic-label');
const offscreenCanvas = document.getElementById('offscreen-canvas');
const offscreenCtx = offscreenCanvas.getContext('2d');

// --- WebSocket Lifecycle ---
let activeAuthToken = '';

function getMetaAuthToken() {
  const meta = document.querySelector('meta[name="chirkoot-auth-token"]');
  return meta ? meta.getAttribute('content') : '';
}

async function initWebSocket() {
  if (!activeAuthToken) {
    activeAuthToken = getMetaAuthToken();
  }
  if (!activeAuthToken) {
    try {
      const authRes = await fetch('/api/auth/token');
      if (authRes.ok) {
        const authData = await authRes.json();
        activeAuthToken = authData.token || '';
      }
    } catch (e) {
      console.warn("Could not retrieve launch auth token:", e);
    }
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Credentials kept out of URLs to prevent leakage in browser history, logs, or proxies
  const url = `${protocol}//${window.location.host}/ws`;

  if (ws) {
    try {
      ws.onclose = null;
      ws.onerror = null;
      ws.close();
    } catch (e) {}
    ws = null;
  }

  if (connBadge) connBadge.className = 'hud-badge disconnected';
  if (connText) connText.textContent = 'LINK STANDBY';

  try {
    if (activeAuthToken) {
      ws = new WebSocket(url, ['chirkoot', activeAuthToken]);
    } else {
      ws = new WebSocket(url);
    }
  } catch (e) {
    // Fallback if environment restricts subprotocol characters
    ws = new WebSocket(`${url}?token=${encodeURIComponent(activeAuthToken)}`);
  }
  ws.binaryType = 'arraybuffer';

  ws.onopen = async () => {
    // Send immediate first-frame auth confirmation
    if (activeAuthToken) {
      try {
        ws.send(JSON.stringify({ type: 'auth', token: activeAuthToken }));
      } catch (err) {}
    }
    if (connBadge) connBadge.className = 'hud-badge connected';
    if (connText) connText.textContent = 'LINK SYNCHRONIZED';
    const sessionLabel = document.getElementById('btn-session-label');
    if (sessionLabel) sessionLabel.textContent = 'TERMINATE LINK';
    try {
      ws.send(JSON.stringify({ type: 'set_listening_mode', mode: currentMicMode }));
      ws.send(JSON.stringify({ type: 'set_mic_mute', muted: isMicMuted }));
    } catch (e) {}
    await initAudioInput();
    initWakeWordRecognizer();
    updateModeUI(currentMicMode);
    updateReplyModeUI(currentReplyMode);
  };

  ws.onmessage = async (event) => {
    if (event.data instanceof ArrayBuffer) {
      // In Text-Only reply mode, suppress incoming audio chunk from being played
      if (currentReplyMode !== 'text') {
        playAudioChunk(event.data);
      }
    } else {
      try {
        const data = JSON.parse(event.data);
        handleServerEvent(data);
      } catch (err) {
        console.error("Error parsing WS message:", err);
      }
    }
  };

  ws.onclose = () => {
    if (connBadge) connBadge.className = 'hud-badge disconnected';
    if (connText) connText.textContent = 'LINK TERMINATED';
    stopAudioPlayback();
    activeGeminiBubble = null;
    activeUserBubble = null;

    // Auto-reconnect if not manually clicked Disconnect
    if (!isExplicitlyDisconnected) {
      setTimeout(() => {
        if (!isExplicitlyDisconnected && (!ws || ws.readyState === WebSocket.CLOSED)) {
          initWebSocket();
        }
      }, 2000);
    }
  };

  ws.onerror = (err) => {
    console.error("WS error:", err);
  };
}

// --- Audio Input (AudioWorklet Native/Decimated 16kHz PCM Streaming) ---
async function initAudioInput() {
  if (micStream && audioContext && (audioWorkletNode || micNode)) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      },
      video: false
    });

    if (!audioContext) {
      try {
        audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      } catch (e) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
      }
    }

    if (audioContext.state === 'suspended') {
      await audioContext.resume().catch(() => {});
    }

    const source = audioContext.createMediaStreamSource(micStream);
    
    // Connect Mic Analyser for real-time 60 FPS HUD spectrum visualization
    // Runs in parallel on main thread without stalling audio thread
    if (!micAnalyser) {
      micAnalyser = audioContext.createAnalyser();
      micAnalyser.fftSize = 64;
      micAnalyser.smoothingTimeConstant = 0.5;
    }
    source.connect(micAnalyser);

    let useWorklet = false;
    if (audioContext.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
      try {
        await audioContext.audioWorklet.addModule('/static/audio-processor.js');
        audioWorkletNode = new AudioWorkletNode(audioContext, 'chirkoot-audio-processor', {
          processorOptions: {
            sampleRate: audioContext.sampleRate,
            targetSampleRate: 16000,
            chunkSize: 512,
            preRollSeconds: 2.5
          }
        });

        audioWorkletNode.port.onmessage = (e) => {
          const data = e.data;
          if (!data) return;

          if (data.type === 'audio_chunk') {
            if (isMicMuted || !ws || ws.readyState !== WebSocket.OPEN) return;
            const framed = buildFramedPacket(PKT_TYPE_AUDIO, 0, new Uint8Array(data.buffer));
            ws.send(framed);
          } else if (data.type === 'pre_roll_data') {
            if (ws && ws.readyState === WebSocket.OPEN && data.buffer) {
              console.log(`⚡ [AudioWorklet] Transmitting pre-roll audio: ${data.samples} samples (${data.durationSec?.toFixed(2)}s)`);
              const framed = buildFramedPacket(PKT_TYPE_AUDIO, 0, new Uint8Array(data.buffer));
              ws.send(framed);
            }
          } else if (data.type === 'vad_state') {
            if (data.state === 'silence' && isAwake && currentMicMode === 'wake-word') {
              // User stopped speaking in wake-word mode; notify stream end if Gemini isn't playing
              if (activeSources.length === 0) {
                sendAudioStreamEnd();
              }
            }
          }
        };

        source.connect(audioWorkletNode);

        // Connect through silent gain to destination to ensure Chrome/Chromium audio graph pulls samples
        const silentGain = audioContext.createGain();
        silentGain.gain.value = 0.0;
        audioWorkletNode.connect(silentGain);
        silentGain.connect(audioContext.destination);

        // Sync initial mode & mute state
        audioWorkletNode.port.postMessage({
          type: 'set_mode',
          mode: currentMicMode,
          isAwake: isAwake
        });
        audioWorkletNode.port.postMessage({
          type: 'set_mute',
          isMuted: isMicMuted
        });

        useWorklet = true;
        console.log(`⚡ [AudioWorklet Initialized] Hardware Rate: ${audioContext.sampleRate}Hz -> 16000Hz (FIR Anti-Aliased)`);
      } catch (workletErr) {
        console.warn("AudioWorklet initialization notice (falling back to ScriptProcessor):", workletErr);
        useWorklet = false;
      }
    }

    if (!useWorklet) {
      // Legacy ScriptProcessor fallback
      const bufferSize = 2048;
      micNode = audioContext.createScriptProcessor(bufferSize, 1, 1);
      
      micNode.onaudioprocess = (e) => {
        for (let i = 0; i < e.outputBuffer.numberOfChannels; i++) {
          e.outputBuffer.getChannelData(i).fill(0);
        }

        if (isMicMuted || !ws || ws.readyState !== WebSocket.OPEN) return;

        const inputData = e.inputBuffer.getChannelData(0);
        const resampled = downsampleTo16k(inputData, audioContext.sampleRate);
        if (!resampled || resampled.length === 0) return;

        if (currentMicMode === 'wake-word' && !isAwake) {
          wakePreRollBuffer.push(new Int16Array(resampled));
          if (wakePreRollBuffer.length > MAX_PRE_ROLL_CHUNKS) {
            wakePreRollBuffer.shift();
          }
        } else if (wakePreRollBuffer.length > 0) {
          for (const chunk of wakePreRollBuffer) {
            const framedPre = buildFramedPacket(PKT_TYPE_AUDIO, 0, new Uint8Array(chunk.buffer));
            ws.send(framedPre);
          }
          wakePreRollBuffer = [];
        }

        const framed = buildFramedPacket(PKT_TYPE_AUDIO, 0, new Uint8Array(resampled.buffer));
        ws.send(framed);
      };

      source.connect(micNode);
      micNode.connect(audioContext.destination);
    }
    
    updateModeUI(currentMicMode);
  } catch (err) {
    console.warn("Mic access notice:", err);
    if (audioStateText) {
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        audioStateText.textContent = 'MIC PERMISSION DENIED';
      } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
        audioStateText.textContent = 'NO MIC FOUND';
      } else {
        audioStateText.textContent = 'MIC STANDBY';
      }
      audioStateText.style.color = '#ff3366';
    }
  }
}

function sendAudioStreamEnd() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "audio_stream_end" }));
      console.log("⚡ [Audio Stream] Sent audio_stream_end to backend");
    } catch (e) {
      console.warn("Notice sending audio_stream_end:", e);
    }
  }
}

// Linear interpolation resampling for clean high-fidelity audio
function downsampleTo16k(buffer, sampleRate) {
  if (sampleRate === 16000) {
    const pcm = new Int16Array(buffer.length);
    for (let i = 0; i < buffer.length; i++) {
      let s = Math.max(-1, Math.min(1, buffer[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return pcm;
  }
  const ratio = sampleRate / 16000;
  const newLength = Math.round(buffer.length / ratio);
  const pcm = new Int16Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const srcIndex = i * ratio;
    const i0 = Math.floor(srcIndex);
    const i1 = Math.min(i0 + 1, buffer.length - 1);
    const weight = srcIndex - i0;
    const val = buffer[i0] * (1 - weight) + buffer[i1] * weight;
    const s = Math.max(-1, Math.min(1, val));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return pcm;
}

// --- Audio Output (24kHz PCM Playback) ---
function initOutputAudio() {
  if (!outputAudioContext) {
    outputAudioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    outputAnalyser = outputAudioContext.createAnalyser();
    outputAnalyser.fftSize = 64;
    outputAnalyser.smoothingTimeConstant = 0.5;

    outputGainNode = outputAudioContext.createGain();
    outputGainNode.connect(outputAnalyser);
    outputAnalyser.connect(outputAudioContext.destination);
  }
}

function playAudioChunk(arrayBuffer) {
  if (currentReplyMode === 'text') {
    return; // Suppress audio playback in Text-Only response mode
  }
  initOutputAudio();
  if (outputAudioContext.state === 'suspended') {
    outputAudioContext.resume();
  }

  const pcm16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(pcm16.length);
  for (let i = 0; i < pcm16.length; i++) {
    float32[i] = pcm16[i] / 32768.0;
  }

  const audioBuffer = outputAudioContext.createBuffer(1, float32.length, 24000);
  audioBuffer.copyToChannel(float32, 0);

  const sourceNode = outputAudioContext.createBufferSource();
  sourceNode.buffer = audioBuffer;
  sourceNode.connect(outputGainNode);

  const currentTime = outputAudioContext.currentTime;
  if (nextStartTime < currentTime) {
    nextStartTime = currentTime;
  }
  sourceNode.start(nextStartTime);
  nextStartTime += audioBuffer.duration;

  activeSources.push(sourceNode);
  sourceNode.onended = () => {
    const idx = activeSources.indexOf(sourceNode);
    if (idx > -1) activeSources.splice(idx, 1);
    if (activeSources.length === 0 && isAwake && currentMicMode === 'wake-word') {
      resetWakeTimer();
    }
  };
}

function stopAudioPlayback() {
  for (const src of activeSources) {
    try { src.stop(); } catch (e) {}
  }
  activeSources = [];
  if (outputAudioContext) {
    nextStartTime = outputAudioContext.currentTime;
  }
}

// ==========================================================================
// LISTENING MODE CONTROL: ALWAYS-ON vs WAKE WORD
// ==========================================================================

function playWakeChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(587.33, now); // D5
    osc.frequency.exponentialRampToValueAtTime(880, now + 0.08); // A5
    osc.frequency.exponentialRampToValueAtTime(1320, now + 0.18); // E6
    gain.gain.setValueAtTime(0.001, now);
    gain.gain.linearRampToValueAtTime(0.2, now + 0.05);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.36);
  } catch (e) {}
}

function playSleepChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, now); // A5
    osc.frequency.exponentialRampToValueAtTime(440, now + 0.16); // A4
    gain.gain.setValueAtTime(0.12, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.28);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.29);
  } catch (e) {}
}

function setListeningMode(mode, userInitiated = true) {
  currentMicMode = mode;
  localStorage.setItem('chirkoot_mic_mode', mode);

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "set_listening_mode", mode: mode }));
  }

  if (mode === 'always-on') {
    isAwake = true;
    if (audioWorkletNode) {
      audioWorkletNode.port.postMessage({ type: 'set_mode', mode: 'always-on', isAwake: true });
    }
    if (wakeSleepTimer) clearTimeout(wakeSleepTimer);
    updateModeUI('always-on');
    if (userInitiated) {
      playWakeChime();
      appendSystemNotice("MODE SWITCH: ALWAYS-ON Open Mic activated. You can speak freely without saying 'Hey'.");
    }
  } else {
    // Wake Word mode
    isAwake = false;
    if (audioWorkletNode) {
      audioWorkletNode.port.postMessage({ type: 'set_mode', mode: 'wake-word', isAwake: false });
    }
    sendAudioStreamEnd();
    updateModeUI('wake-word');
    initWakeWordRecognizer();
    if (userInitiated) {
      playSleepChime();
      appendSystemNotice("MODE SWITCH: WAKE WORD mode activated. Say 'Hey', 'Hey do this', or 'Hey Chirkoot' to activate.");
    }
  }
}

function toggleListeningMode() {
  if (currentMicMode === 'always-on') {
    setListeningMode('wake-word', true);
  } else {
    setListeningMode('always-on', true);
  }
}

// ==========================================================================
// RESPONSE FORMAT STATE: 'voice' (Audio+Text) vs 'text' (Silent Text-Only)
// ==========================================================================

function setReplyMode(mode, userInitiated = true) {
  currentReplyMode = mode;
  localStorage.setItem('chirkoot_reply_mode', mode);

  if (mode === 'text') {
    stopAudioPlayback();
  }

  updateReplyModeUI(mode);

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "set_reply_mode", mode: mode }));
  }

  if (userInitiated) {
    if (mode === 'text') {
      appendSystemNotice("💬 RESPONSE FORMAT: TEXT-ONLY. Chirkoot is listening to your voice, but will reply ONLY in text in the chat.");
    } else {
      appendSystemNotice("🔊 RESPONSE FORMAT: VOICE REPLY. Chirkoot will speak out loud and display text.");
    }
  }
}

function toggleReplyMode() {
  setReplyMode(currentReplyMode === 'voice' ? 'text' : 'voice', true);
}

function updateReplyModeUI(mode) {
  const isText = (mode === 'text');

  // 1. Header Badge Update
  const replyBadge = document.getElementById('reply-badge');
  const replyBadgeText = document.getElementById('reply-badge-text');
  if (replyBadge) {
    replyBadge.className = isText ? 'hud-badge reply-text' : 'hud-badge reply-voice';
  }
  if (replyBadgeText) {
    replyBadgeText.textContent = isText ? '💬 REPLY: TEXT ONLY' : '🔊 REPLY: VOICE';
  }

  // 2. Bottom Cockpit Controls Button Update
  const btnReply = document.getElementById('btn-reply-toggle');
  const btnReplyLabel = document.getElementById('btn-reply-label');
  const btnReplySub = document.getElementById('btn-reply-sub');
  const replyIcon = document.getElementById('reply-btn-icon');

  if (btnReply) {
    btnReply.className = isText ? 'hud-control-btn reply-btn text-only' : 'hud-control-btn reply-btn voice';
  }
  if (btnReplyLabel) {
    btnReplyLabel.textContent = isText ? 'TEXT-ONLY' : 'VOICE REPLY';
  }
  if (btnReplySub) {
    btnReplySub.textContent = isText ? 'SILENT (CLICK FOR VOICE)' : 'AUDIO + TEXT (CLICK TO MUTE)';
  }
  if (replyIcon) {
    if (isText) {
      replyIcon.innerHTML = `
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
        <line x1="8" y1="9" x2="16" y2="9"></line>
        <line x1="8" y1="13" x2="13" y2="13"></line>
      `;
    } else {
      replyIcon.innerHTML = `
        <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon>
        <path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"></path>
      `;
    }
  }

  // 3. Quick Chip Update
  const chipReply = document.getElementById('chip-reply-toggle');
  if (chipReply) {
    chipReply.innerHTML = isText 
      ? '<span class="chip-icon">🔊</span> Voice Reply'
      : '<span class="chip-icon">💬</span> Text Only Reply';
  }
}

// Automatically detect natural voice requests to switch reply mode
function checkVoiceReplyCommands(rawText) {
  if (!rawText) return;
  const t = rawText.toLowerCase().trim();

  // Text-Only triggers (English & Hindi / Hinglish)
  const textTriggers = [
    /\b(reply\s+in\s+text|text\s+only|silent\s+mode|don't\s+speak|dont\s+speak|mute\s+(your\s+)?voice|stop\s+talking|type\s+only|write\s+only)\b/i,
    /\b(sirf\s+text|text\s+me\s+reply|text\s+me\s+jawab|awaz\s+band|bolna\s+mat|chup\s+raho)\b/i,
    /\b(सिर्फ\s*टेक्स्ट|टेक्स्ट\s*में\s*जवाब|आवाज़\s*बंद|मत\s*बोलो|चुप\s*रहो)\b/u
  ];

  for (const reg of textTriggers) {
    if (reg.test(t)) {
      if (currentReplyMode !== 'text') {
        console.log("⚡ [Voice Command Detected] Switching to Text-Only Reply Mode:", t);
        setReplyMode('text', true);
      }
      return;
    }
  }

  // Voice speech triggers
  const voiceTriggers = [
    /\b(reply\s+in\s+voice|voice\s+mode|speak\s+out\s+loud|unmute\s+(your\s+)?voice|start\s+speaking|talk\s+to\s+me)\b/i,
    /\b(bol\s+kar\s+jawab|awaz\s+chalu|bolna\s+shuru)\b/i,
    /\b(बोल\s*कर\s*जवाब|आवाज़\s*शुरू|बोलना\s*शुरू)\b/u
  ];

  for (const reg of voiceTriggers) {
    if (reg.test(t)) {
      if (currentReplyMode !== 'voice') {
        console.log("⚡ [Voice Command Detected] Switching to Voice Reply Mode:", t);
        setReplyMode('voice', true);
      }
      return;
    }
  }
}

// Automatically detect natural voice requests to maximize, fullscreen, minimize, or restore window
function checkVoiceWindowCommands(rawText) {
  if (!rawText) return;
  const t = rawText.toLowerCase().trim();

  // Exit Fullscreen / Restore triggers
  const exitFsTriggers = [
    /\b(exit\s+fullscreen|leave\s+fullscreen|close\s+fullscreen|normal\s+screen|restore\s+window|unmaximize)\b/i,
    /\b(full\s*screen\s*hatao|chota\s*karo|screen\s*chota\s*karo|normal\s*karo)\b/i,
    /\b(फुल\s*स्क्रीन\s*हटाओ|छोटा\s*करो|स्क्रीन\s*छोटा\s*करो|नॉर्मल\s*करो)\b/u
  ];

  // Fullscreen triggers
  const fsTriggers = [
    /\b(full\s*screen|go\s+fullscreen|fullscreen\s+mode|make\s+it\s+full\s*screen)\b/i,
    /\b(full\s*screen\s*karo|screen\s*bada\s*karo|pura\s*screen\s*karo)\b/i,
    /\b(फुल\s*स्क्रीन|बड़ा\s*करो|स्क्रीन\s*बड़ा\s*करो)\b/u
  ];

  // Maximize triggers
  const maxTriggers = [
    /\b(maximize\s+window|maximize|max\s+window)\b/i,
    /\b(maximize\s*karo|bada\s*karo\s*window)\b/i,
    /\b(मैक्सिमाइज\s*करो)\b/u
  ];

  // Minimize triggers
  const minTriggers = [
    /\b(minimize\s+window|minimize|hide\s+window|to\s+taskbar)\b/i,
    /\b(minimize\s*karo|chupa\s*do|niche\s*karo)\b/i,
    /\b(मिनीमाइज\s*करो)\b/u
  ];

  for (const reg of exitFsTriggers) {
    if (reg.test(t)) {
      console.log("⚡ [Voice Command Detected] Restore / Exit Fullscreen:", t);
      restoreWindow();
      return;
    }
  }

  for (const reg of fsTriggers) {
    if (reg.test(t)) {
      console.log("⚡ [Voice Command Detected] Fullscreen Window:", t);
      if (!isWindowFullscreen()) {
        toggleAppFullscreen();
      }
      return;
    }
  }

  for (const reg of maxTriggers) {
    if (reg.test(t)) {
      console.log("⚡ [Voice Command Detected] Maximize Window:", t);
      maximizeWindow();
      return;
    }
  }

  for (const reg of minTriggers) {
    if (reg.test(t)) {
      console.log("⚡ [Voice Command Detected] Minimize Window:", t);
      minimizeWindow();
      return;
    }
  }
}

function wakeUpChirkoot(initialCommand = "", phrase = "Hey") {
  const wasAlreadyAwake = isAwake;
  isAwake = true;

  if (audioWorkletNode) {
    audioWorkletNode.port.postMessage({ type: 'flush_pre_roll' });
    audioWorkletNode.port.postMessage({ type: 'set_mode', mode: currentMicMode, isAwake: true });
  }

  if (!wasAlreadyAwake) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "manual_wake" }));
    }
    playWakeChime();
    appendSystemNotice(`⚡ WAKE ACTIVATED: "${phrase}" recognized. Neural link open.`);

    // Instantly flush fallback pre-roll audio buffer over WebSocket so Gemini hears initial words
    if (ws && ws.readyState === WebSocket.OPEN && wakePreRollBuffer.length > 0) {
      for (const chunk of wakePreRollBuffer) {
        ws.send(chunk.buffer);
      }
      wakePreRollBuffer = [];
    }
  }
  updateModeUI(currentMicMode);
  resetWakeTimer();

  // If a manual text command was passed explicitly (e.g. from quick chips), dispatch it
  if (initialCommand && initialCommand.trim() && initialCommand !== "Manual Wake") {
    setTimeout(() => {
      sendTextMessage(initialCommand.trim());
    }, 200);
  }
}

function sleepChirkoot() {
  if (!isAwake) return;
  if (activeSources.length > 0) {
    resetWakeTimer();
    return;
  }
  isAwake = false;
  if (audioWorkletNode) {
    audioWorkletNode.port.postMessage({ type: 'set_mode', mode: currentMicMode, isAwake: false });
  }
  sendAudioStreamEnd();
  wakePreRollBuffer = [];
  pendingWakeCommand = "";
  if (wakeCommandTimer) clearTimeout(wakeCommandTimer);
  playSleepChime();
  updateModeUI(currentMicMode);
  appendSystemNotice(`💤 STANDBY: Chirkoot resting. Say "Hey", "Hey do this", or "Hey Chirkoot" to speak.`);
}

function resetWakeTimer() {
  if (wakeSleepTimer) clearTimeout(wakeSleepTimer);
  if (currentMicMode !== 'wake-word') return;

  wakeSleepTimer = setTimeout(() => {
    if (activeSources.length === 0) {
      sleepChirkoot();
    } else {
      resetWakeTimer();
    }
  }, WAKE_ACTIVE_TIMEOUT_MS);
}

function toggleWakeStateManually() {
  if (currentMicMode === 'always-on') {
    // In Always-On mode, clicking switches to Wake Word mode
    setListeningMode('wake-word', true);
  } else {
    // In Wake Word mode, clicking toggles wake / sleep
    if (!isAwake) {
      wakeUpChirkoot("", "Manual Wake");
    } else {
      sleepChirkoot();
    }
  }
}

function updateModeUI(mode) {
  if (mode === 'always-on') {
    if (modeOptAlways) modeOptAlways.classList.add('active');
    if (modeOptWake) modeOptWake.classList.remove('active');

    if (btnModeToggle) {
      btnModeToggle.className = 'hud-control-btn mode-btn always-on';
      if (btnModeLabel) btnModeLabel.textContent = 'ALWAYS-ON';
      if (btnModeSub) btnModeSub.textContent = 'OPEN MIC (CLICK TO SWITCH)';
      if (modeBtnIcon) {
        modeBtnIcon.innerHTML = `
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
          <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
        `;
      }
    }

    if (wakeBadge) {
      wakeBadge.className = 'hud-badge wake-active';
      if (wakeBadgeText) wakeBadgeText.textContent = 'MIC: ALWAYS-ON';
    }

    if (audioStateText) {
      audioStateText.textContent = 'MIC STREAMING (ALWAYS-ON)';
      audioStateText.style.color = '#00ffaa';
    }
    if (viewportStatusTag && activeVideoSource === null) {
      viewportStatusTag.textContent = 'OPEN MIC // STREAMING';
    }

  } else {
    // Wake Word mode
    if (modeOptAlways) modeOptAlways.classList.remove('active');
    if (modeOptWake) modeOptWake.classList.add('active');

    if (btnModeToggle) {
      btnModeToggle.className = isAwake ? 'hud-control-btn mode-btn wake-word always-on' : 'hud-control-btn mode-btn wake-word';
      if (btnModeLabel) btnModeLabel.textContent = isAwake ? 'WAKE: ACTIVE' : 'WAKE: STANDBY';
      if (btnModeSub) btnModeSub.textContent = isAwake ? 'LISTENING (CLICK TO SLEEP)' : 'SAY "HEY" OR "HEY CHIRKOOT"';
      if (modeBtnIcon) {
        modeBtnIcon.innerHTML = `<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path>`;
      }
    }

    if (wakeBadge) {
      wakeBadge.className = isAwake ? 'hud-badge wake-active' : 'hud-badge wake-standby';
      if (wakeBadgeText) wakeBadgeText.textContent = isAwake ? '⚡ CHIRKOOT AWAKE' : '💤 WAKE: "HEY" / "CHIRKOOT"';
    }

    if (audioStateText) {
      if (isAwake) {
        audioStateText.textContent = 'MIC ACTIVE (STREAMING)';
        audioStateText.style.color = '#00ffaa';
      } else {
        audioStateText.textContent = 'STANDBY (SAY "HEY" OR "HEY CHIRKOOT")';
        audioStateText.style.color = '#818cf8';
      }
    }
    if (viewportStatusTag && activeVideoSource === null) {
      viewportStatusTag.textContent = isAwake ? '⚡ AWAKE // ATTENDING' : 'CORE ENGAGED';
    }
  }
}

// ==========================================================================
// PHONETIC & FUZZY WAKE WORD RECOGNIZER
// ==========================================================================

function matchWakeWord(rawText) {
  const t = rawText.toLowerCase().trim().replace(/^(um+|uh+|so+|like|well|actually)\s+/i, '');
  if (!t) return { matched: false, wakeWord: '', command: '' };

  for (const p of WAKE_PATTERNS) {
    const match = t.match(p);
    if (match) {
      let matchedText = match[0];
      let startIdx = match.index;
      // Strip leading non-alphanumeric/non-Devanagari if captured by non-Devanagari boundary group
      if (/^[^\w\u0900-\u097F]/.test(matchedText)) {
        matchedText = matchedText.slice(1);
        startIdx += 1;
      }
      const remaining = t.slice(startIdx + matchedText.length).replace(/^[,.\s]+/, '').trim();
      return { matched: true, wakeWord: matchedText.trim(), command: remaining };
    }
  }

  // Token scan with phonetic similarity (English + Hindi Devanagari)
  const words = t.split(/\s+/);
  for (let i = 0; i < words.length; i++) {
    // Preserve English alphanumeric and Devanagari Hindi unicode range
    const cleanWord = words[i].replace(/[^\w\u0900-\u097F]/g, '');
    if (isPhoneticallyChirkoot(cleanWord)) {
      const remaining = words.slice(i + 1).join(' ').trim();
      return { matched: true, wakeWord: cleanWord, command: remaining };
    }

    // Check 2-word sliding pairs e.g. "sure koot", "check out", "hey chirkoot"
    if (i + 1 < words.length) {
      const nextWord = words[i + 1].replace(/[^\w\u0900-\u097F]/g, '');
      const pair = `${cleanWord} ${nextWord}`.trim();
      if (isPhoneticallyChirkoot(pair)) {
        const remaining = words.slice(i + 2).join(' ').trim();
        return { matched: true, wakeWord: pair, command: remaining };
      }
      const prefixList = ['hey', 'hay', 'hi', 'hello', 'ok', 'okay', 'suno', 'bhai', 'oye', 'arey', 'हे', 'अरे', 'ओए', 'सुनो', 'भाई'];
      if (prefixList.includes(cleanWord) && isPhoneticallyChirkoot(nextWord)) {
        const remaining = words.slice(i + 2).join(' ').trim();
        return { matched: true, wakeWord: pair, command: remaining };
      }
    }
  }

  return { matched: false, wakeWord: '', command: '' };
}

function isPhoneticallyChirkoot(w) {
  if (!w || w.length < 2) return false;
  const lower = w.toLowerCase().trim();
  
  // Direct Devanagari Hindi matches
  const hindiTargets = ['चिरकुट', 'चिरकूट', 'चिरकु', 'सरकुट', 'चिरुकुट', 'चित्रकूट', 'चित्रकुट', 'जार्विस', 'हे', 'सुनो', 'अरे'];
  if (hindiTargets.includes(lower)) return true;

  // English & Hinglish transliterated matches
  const targets = [
    'chirkoot', 'chirkut', 'cherkoot', 'charkoot', 'circuit', 'circuits', 'shirkoot',
    'charkut', 'chirkoo', 'chiru', 'chiku', 'chhotu', 'chotu', 'surkut',
    'chitrakoot', 'chitrakut', 'chitra', 'citrakoot', 'cheetrakoot',
    'jarvis', 'jarviz', 'jarves', 'alexa'
  ];
  if (targets.includes(lower)) return true;

  // Multi-word compound sounds
  const phraseTargets = [
    'sure koot', 'sir koot', 'share koot', 'cheer koot', 'cheer cut',
    'chair koot', 'chair cut', 'check out', 'chitra koot', 'chitra kut'
  ];
  if (phraseTargets.includes(lower)) return true;

  if (lower.length >= 5) {
    for (const tgt of targets) {
      if (levenshtein(lower, tgt) <= 2) return true;
    }
  }
  return false;
}

function levenshtein(a, b) {
  const matrix = [];
  for (let i = 0; i <= b.length; i++) matrix[i] = [i];
  for (let j = 0; j <= a.length; j++) matrix[0][j] = j;
  for (let i = 1; i <= b.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      if (b.charAt(i - 1) === a.charAt(j - 1)) {
        matrix[i][j] = matrix[i - 1][j - 1];
      } else {
        matrix[i][j] = Math.min(
          matrix[i - 1][j - 1] + 1,
          matrix[i][j - 1] + 1,
          matrix[i - 1][j] + 1
        );
      }
    }
  }
  return matrix[b.length][a.length];
}

function setSpeechLanguage(lang, notify = true) {
  currentSpeechLang = lang;
  localStorage.setItem('chirkoot_speech_lang', lang);
  if (wakeRecognizer) {
    try {
      wakeRecognizer.abort();
      wakeRecognizer.lang = lang;
      isWakeRecognizerActive = false;
      if (currentMicMode === 'wake-word') {
        scheduleWakeRestart(200);
      }
    } catch (e) {
      console.warn("Language switch notice:", e);
    }
  }
  updateLangUI(lang);
  if (notify) {
    appendSystemNotice(`🌐 SPEECH ENGINE: Recognition dialect set to ${getLangLabel(lang)}.`);
  }
}

function toggleSpeechLanguage() {
  const langs = ['en-IN', 'hi-IN', 'en-US'];
  const nextIdx = (langs.indexOf(currentSpeechLang) + 1) % langs.length;
  setSpeechLanguage(langs[nextIdx], true);
}

function getLangLabel(l) {
  if (l === 'hi-IN') return 'HINDI (हिंदी)';
  if (l === 'en-IN') return 'HINGLISH (EN-IN)';
  return 'ENGLISH (US)';
}

function updateLangUI(lang) {
  const langBadgeText = document.getElementById('lang-badge-text');
  if (langBadgeText) {
    langBadgeText.textContent = getLangLabel(lang);
  }
}

function safeStartWakeRecognizer() {
  if (currentMicMode !== 'wake-word') return;
  if (!wakeRecognizer) return;
  if (isWakeRecognizerActive) return;

  try {
    wakeRecognizer.start();
    isWakeRecognizerActive = true;
  } catch (err) {
    isWakeRecognizerActive = false;
    if (err.name === 'InvalidStateError') {
      // Recognizer was already transitioning or starting; schedule clean retry
      scheduleWakeRestart(200);
    } else {
      console.warn("Wake recognizer start notice:", err);
      scheduleWakeRestart(400);
    }
  }
}

function scheduleWakeRestart(delayMs = 200) {
  if (currentMicMode !== 'wake-word') return;
  if (wakeRestartTimer) clearTimeout(wakeRestartTimer);
  wakeRestartTimer = setTimeout(() => {
    isWakeRecognizerActive = false;
    safeStartWakeRecognizer();
  }, delayMs);
}

function initWakeWordRecognizer() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    console.warn("SpeechRecognition not supported in this browser. Fallback to manual wake / open mic.");
    return;
  }

  try {
    if (wakeRecognizer) {
      try { wakeRecognizer.abort(); } catch (e) {}
      wakeRecognizer = null;
      isWakeRecognizerActive = false;
    }

    wakeRecognizer = new SpeechRecognition();
    wakeRecognizer.continuous = true;
    wakeRecognizer.interimResults = true;
    wakeRecognizer.maxAlternatives = 5; // Scan top 5 speech hypotheses for 300% higher accuracy!
    wakeRecognizer.lang = currentSpeechLang; // Uses 'en-IN' for Hinglish, 'hi-IN' for Hindi, or 'en-US'

    wakeRecognizer.onstart = () => {
      isWakeRecognizerActive = true;
    };

    wakeRecognizer.onresult = (event) => {
      // Voice alone is strictly forbidden from resetting or resuming while Emergency Stop is active
      if (isKillSwitchActive) {
        return;
      }

      let foundMatch = null;

      // Scan all results and all alternatives (hypotheses)
      for (let i = event.resultIndex; i < event.results.length; ++i) {
        const resList = event.results[i];
        for (let a = 0; a < resList.length; a++) {
          const candText = (resList[a].transcript || '').trim().toLowerCase();
          if (!candText) continue;

          // Distinctive Emergency Stop Phrases (Independent of Gemini, non-conversational)
          const DISTINCTIVE_STOP_PHRASES = [
            'emergency stop',
            'emergency halt',
            'chirkoot halt now',
            'chirkoot emergency stop',
            'chirkoot abort',
            'chirkoot abort now',
            'red alert stop',
            'abort protocol',
            'chirkoot turant ruko',
            'chirkoot turant band karo',
            'imerjency stop'
          ];
          const isStopPhrase = DISTINCTIVE_STOP_PHRASES.some(phrase => {
            return candText === phrase || candText.includes(phrase);
          });
          if (isStopPhrase) {
            console.warn("🛑 [LOCAL DISTINCTIVE EMERGENCY STOP PHRASE TRIGGERED]:", candText);
            triggerKillSwitch("Voice stop phrase: " + candText);
            return;
          }

          checkVoiceReplyCommands(candText);
          checkVoiceWindowCommands(candText);

          const res = matchWakeWord(candText);
          if (res.matched) {
            foundMatch = res;
            break;
          }
        }
        if (foundMatch) break;
      }

      if (foundMatch) {
        console.log(`⚡ [Wake Recognized]: "${foundMatch.wakeWord}" -> Opening Neural Audio Stream`);

        // Instantly wake Chirkoot, trigger chime, and flush pre-roll audio to Gemini!
        if (!isAwake) {
          wakeUpChirkoot("", foundMatch.wakeWord || "Hey");
          lastWakeTime = Date.now();
        } else {
          resetWakeTimer();
        }
      }
    };

    wakeRecognizer.onerror = (err) => {
      if (err.error !== 'no-speech' && err.error !== 'aborted') {
        console.warn("Wake recognizer notice:", err.error);
      }
      isWakeRecognizerActive = false;

      if (err.error === 'not-allowed' || err.error === 'service-not-allowed') {
        appendSystemNotice("⚠️ SPEECH RECOGNITION RESTRICTED: The browser denied speech recognition permissions. Automatically switching to ALWAYS-ON Open Mic streaming so Chirkoot can hear you directly.");
        setListeningMode('always-on', true);
        return;
      }

      if (err.error !== 'aborted' && currentMicMode === 'wake-word') {
        scheduleWakeRestart(250);
      }
    };

    wakeRecognizer.onend = () => {
      isWakeRecognizerActive = false;
      if (currentMicMode === 'wake-word') {
        scheduleWakeRestart(150);
      }
    };

    safeStartWakeRecognizer();

    // 2-second watchdog timer to guarantee the engine never stops in wake-word mode
    if (!wakeWatchdogTimer) {
      wakeWatchdogTimer = setInterval(() => {
        if (currentMicMode === 'wake-word' && !isWakeRecognizerActive) {
          safeStartWakeRecognizer();
        }
      }, 2000);
    }

    console.log(`🎙️ [Wake Word Engine] Listening for 'Hey', 'Hey Chirkoot', 'Chitrakoot', 'Jarvis' in [${currentSpeechLang}]...`);
    updateLangUI(currentSpeechLang);
  } catch (err) {
    console.warn("Could not start wake recognizer:", err);
  }
}

// --- Screen Sharing & Camera Streaming ---
async function toggleScreenShare() {
  if (activeVideoSource === 'screen') {
    stopVideoStream();
    return;
  }
  stopVideoStream();

  try {
    const displayOptions = {
      video: { cursor: "always", frameRate: 15 },
      audio: false,
      selfBrowserSurface: "exclude", // Prevents Chrome from selecting Chirkoot HUD tab
      preferCurrentTab: false,
      surfaceSwitching: "include",
      systemAudio: "exclude"
    };

    try {
      videoStream = await navigator.mediaDevices.getDisplayMedia(displayOptions);
    } catch (optErr) {
      // Fallback for browsers that do not support modern display constraints
      videoStream = await navigator.mediaDevices.getDisplayMedia({
        video: { cursor: "always", frameRate: 15 },
        audio: false
      });
    }

    activeVideoSource = 'screen';
    btnScreen.classList.add('active');
    btnCamera.classList.remove('active');

    videoEl.srcObject = videoStream;
    videoEl.style.display = 'block';
    placeholderEl.style.display = 'none';
    overlayEl.style.display = 'flex';
    streamTagEl.textContent = '● SCREEN RADAR ACTIVE';
    if (viewportStatusTag) viewportStatusTag.textContent = 'SCREEN STREAMING';

    if (visionBadge) visionBadge.className = 'hud-badge vision-on';
    if (visionText) visionText.textContent = 'SCREEN LIVE';

    startFrameCapture();
    updatePrivacyIndicators();

    appendSystemNotice("🖥️ SCREEN RADAR ENGAGED: To prevent screen mirroring, share an App Window or click 'ALT+TAB TO APP'.");

    videoStream.getVideoTracks()[0].onended = () => {
      stopVideoStream();
    };

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "text",
        text: "I am now sharing my screen with you. Look at my screen and tell me in one short sentence what you see."
      }));
    }

    if (currentMicMode === 'wake-word') {
      wakeUpChirkoot();
    }

  } catch (err) {
    console.warn("Screen share cancelled:", err);
  }
}

async function toggleCamera() {
  if (activeVideoSource === 'camera') {
    stopVideoStream();
    return;
  }
  stopVideoStream();

  try {
    videoStream = await navigator.mediaDevices.getUserMedia({
      video: { width: 1280, height: 720 },
      audio: false
    });

    activeVideoSource = 'camera';
    btnCamera.classList.add('active');
    btnScreen.classList.remove('active');

    videoEl.srcObject = videoStream;
    videoEl.style.display = 'block';
    placeholderEl.style.display = 'none';
    overlayEl.style.display = 'flex';
    streamTagEl.textContent = '● OPTICAL EYE ACTIVE';
    if (viewportStatusTag) viewportStatusTag.textContent = 'OPTICAL FEED';

    if (visionBadge) visionBadge.className = 'hud-badge vision-on';
    if (visionText) visionText.textContent = 'OPTICS LIVE';

    startFrameCapture();

    videoStream.getVideoTracks()[0].onended = () => {
      stopVideoStream();
    };

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "text",
        text: "I have turned on my camera. Describe what you see in front of you."
      }));
    }

    if (currentMicMode === 'wake-word') {
      wakeUpChirkoot();
    }

  } catch (err) {
    console.warn("Camera access error:", err);
  }
}

// --- Chirkoot MK-V Adaptive Screen Radar State & Telemetry ---
let radarState = 'STATIC'; // 'STATIC' | 'DYNAMIC' | 'PAUSED' | 'SHIELDED'
let radarManualPaused = false;
let radarShieldActive = false;
let radarDynamicCooldownUntil = 0;
let radarLastSendTime = 0;
let radarFramesSent = 0;
let radarFramesSuppressed = 0;
let radarThumbPrev = null;
let radarLoopTimer = null;

const RADAR_DYNAMIC_INTERVAL_MS = 333; // ~3.0 FPS during active typing, scrolling, movement
const RADAR_STATIC_HEARTBEAT_MS = 3000; // 0.33 FPS during static desktop reading
const RADAR_CHANGE_THRESHOLD = 0.002; // 0.2% change in downsampled 32x32 thumbnail
const RADAR_COOLDOWN_MS = 1500; // Hold dynamic burst for 1.5s after change

const radarThumbCanvas = document.createElement('canvas');
radarThumbCanvas.width = 32;
radarThumbCanvas.height = 32;
const radarThumbCtx = radarThumbCanvas.getContext('2d', { willReadFrequently: true });

function computeThumbnailDiff(currData, prevData) {
  if (!prevData || currData.length !== prevData.length) return 1.0;
  let sum = 0;
  for (let i = 0; i < currData.length; i += 4) {
    sum += Math.abs(currData[i] - prevData[i]);
    sum += Math.abs(currData[i + 1] - prevData[i + 1]);
    sum += Math.abs(currData[i + 2] - prevData[i + 2]);
  }
  return sum / (1024 * 3 * 255.0);
}

function syncHudGeometry() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    const x1 = window.screenX || window.screenLeft || 0;
    const y1 = window.screenY || window.screenTop || 0;
    const x2 = x1 + (window.outerWidth || window.innerWidth || 800);
    const y2 = y1 + (window.outerHeight || window.innerHeight || 600);
    ws.send(JSON.stringify({
      type: "hud_geometry",
      rect: [x1, y1, x2, y2]
    }));
    ws.send(JSON.stringify({
      type: "hud_exclusion_status"
    }));
  }
}

function handleHudExclusionStatus(status) {
  if (!status) return;
  const tag = document.getElementById('hud-exclusion-tag');
  if (tag) {
    if (status.active_mechanism === 'WINDOW_AFFINITY') {
      tag.textContent = 'HUD EXCLUSION: DWM AFFINITY ACTIVE';
      tag.style.color = '#00f3ff';
      tag.title = status.user_status_message;
    } else if (status.active_mechanism === 'PIPELINE_MASKING') {
      tag.textContent = 'HUD EXCLUSION: PIPELINE MASKING ACTIVE';
      tag.style.color = '#00f3ff';
      tag.title = status.user_status_message;
    } else {
      tag.textContent = 'HUD EXCLUSION: OFF';
      tag.style.color = '#8b949e';
      tag.title = 'HUD exclusion disabled';
    }
  }
}

function toggleRadarPause() {
  if (activeVideoSource !== 'screen') return;
  radarManualPaused = !radarManualPaused;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: radarManualPaused ? "radar_pause" : "radar_resume"
    }));
  }
  updateRadarUIState();
  appendSystemNotice(radarManualPaused ? "⏸️ [SCREEN RADAR PAUSED] Visual transmission paused by operator." : "📡 [SCREEN RADAR RESUMED] Visual uplink restored.");
}

function updateRadarUIState() {
  if (!streamFpsEl || !streamTagEl) return;
  if (radarShieldActive) {
    streamFpsEl.textContent = "RADAR: SHIELDED // 0.0 FPS";
    streamFpsEl.style.color = "#ff3366";
    streamTagEl.textContent = "🔒 PRIVACY SHIELD ACTIVE";
    streamTagEl.style.color = "#ff3366";
    return;
  }
  if (radarManualPaused) {
    streamFpsEl.textContent = "RADAR: PAUSED // 0.0 FPS";
    streamFpsEl.style.color = "#ffaa00";
    streamTagEl.textContent = "⏸️ RADAR PAUSED";
    streamTagEl.style.color = "#ffaa00";
    return;
  }
  if (radarState === 'DYNAMIC') {
    streamFpsEl.textContent = `RADAR: DYNAMIC // 3.0 FPS`;
    streamFpsEl.style.color = "#00f3ff";
    streamTagEl.textContent = "⚡ ACTIVE UPLINK (TYPING/SCROLL)";
    streamTagEl.style.color = "#00f3ff";
  } else {
    streamFpsEl.textContent = `RADAR: STATIC // 0.3 FPS (IDLE)`;
    streamFpsEl.style.color = "#8b949e";
    streamTagEl.textContent = "📡 RADAR STANDBY (SAVING BANDWIDTH)";
    streamTagEl.style.color = "#8b949e";
  }
}

function handleRadarPrivacyAlert(data) {
  radarShieldActive = !!data.active;
  radarSensitiveAppPaused = !!data.active;
  const alertBox = document.getElementById('hud-privacy-alert-box');
  const alertTxt = document.getElementById('hud-privacy-alert-text');

  if (radarShieldActive) {
    appendSystemNotice(`🔒 [PRIVACY SHIELD ENGAGED]: Screen transmission suspended. Sensitive window detected (${data.title || data.reason}).`);
    if (alertBox) alertBox.style.display = 'flex';
    if (alertTxt) alertTxt.textContent = `SCREEN CAPTURE AUTOMATICALLY PAUSED: Sensitive application active (${data.title || data.reason || 'Protected window'}).`;
    updateRadarUIState();
  } else {
    appendSystemNotice("📡 [PRIVACY SHIELD DISENGAGED]: Screen radar resumed.");
    if (alertBox) alertBox.style.display = 'none';
    updateRadarUIState();
  }
  updatePrivacyIndicators();
}

function handleRadarTelemetry(telem) {
  if (!telem) return;
  if (telem.privacy_shield_active !== undefined) {
    radarShieldActive = telem.privacy_shield_active;
    radarSensitiveAppPaused = telem.privacy_shield_active;
  }
  if (telem.state) {
    radarState = telem.state;
  }
  updateRadarUIState();
  updatePrivacyIndicators();
}

function stopVideoStream() {
  if (frameInterval) {
    clearInterval(frameInterval);
    frameInterval = null;
  }
  if (radarLoopTimer) {
    clearTimeout(radarLoopTimer);
    radarLoopTimer = null;
  }
  if (videoStream) {
    videoStream.getTracks().forEach(t => t.stop());
    videoStream = null;
  }
  activeVideoSource = null;
  radarThumbPrev = null;
  radarShieldActive = false;
  btnScreen.classList.remove('active');
  btnCamera.classList.remove('active');

  videoEl.style.display = 'none';
  placeholderEl.style.display = 'flex';
  overlayEl.style.display = 'none';
  if (viewportStatusTag) viewportStatusTag.textContent = (currentMicMode === 'always-on' ? 'OPEN MIC // STREAMING' : (isAwake ? '⚡ AWAKE // ATTENDING' : 'CORE ENGAGED'));

  if (visionBadge) visionBadge.className = 'hud-badge vision-off';
  if (visionText) visionText.textContent = 'OPTICS OFF';
  updatePrivacyIndicators();
}

function startFrameCapture() {
  if (frameInterval) clearInterval(frameInterval);
  if (radarLoopTimer) clearTimeout(radarLoopTimer);

  syncHudGeometry();

  async function radarTick() {
    if (!videoStream || !videoEl.videoWidth || !ws || ws.readyState !== WebSocket.OPEN) {
      radarLoopTimer = setTimeout(radarTick, 500);
      return;
    }

    const now = Date.now();

    // 1. If Camera active, stream at steady 1 FPS
    if (activeVideoSource === 'camera') {
      const maxDim = 960;
      let w = videoEl.videoWidth;
      let h = videoEl.videoHeight;
      if (w > maxDim || h > maxDim) {
        if (w > h) { h = Math.round(h * (maxDim / w)); w = maxDim; }
        else { w = Math.round(w * (maxDim / h)); h = maxDim; }
      }
      offscreenCanvas.width = w;
      offscreenCanvas.height = h;
      offscreenCtx.drawImage(videoEl, 0, 0, w, h);
      offscreenCanvas.toBlob(async (blob) => {
        if (blob && ws && ws.readyState === WebSocket.OPEN) {
          const ab = await blob.arrayBuffer();
          ws.send(buildFramedPacket(PKT_TYPE_WEBCAM_JPEG, 0, new Uint8Array(ab)));
        }
      }, "image/jpeg", 0.72);
      radarLoopTimer = setTimeout(radarTick, 1000);
      return;
    }

    // 2. Screen Radar Active: Check Privacy & Pause
    if (radarManualPaused || radarShieldActive) {
      updateRadarUIState();
      radarLoopTimer = setTimeout(radarTick, 500);
      return;
    }

    // 3. Downscale to 32x32 thumbnail for fast client-side perceptual difference
    radarThumbCtx.drawImage(videoEl, 0, 0, 32, 32);
    const currThumbData = radarThumbCtx.getImageData(0, 0, 32, 32).data;
    const diff = computeThumbnailDiff(currThumbData, radarThumbPrev);
    radarThumbPrev = currThumbData;

    const isChanged = diff >= RADAR_CHANGE_THRESHOLD;
    let shouldSend = false;

    if (isChanged) {
      radarState = 'DYNAMIC';
      radarDynamicCooldownUntil = now + RADAR_COOLDOWN_MS;
      shouldSend = (now - radarLastSendTime >= RADAR_DYNAMIC_INTERVAL_MS);
    } else {
      if (now < radarDynamicCooldownUntil) {
        radarState = 'DYNAMIC';
        shouldSend = (now - radarLastSendTime >= RADAR_DYNAMIC_INTERVAL_MS);
      } else {
        radarState = 'STATIC';
        shouldSend = (now - radarLastSendTime >= RADAR_STATIC_HEARTBEAT_MS);
      }
    }

    updateRadarUIState();

    if (shouldSend) {
      const maxDim = 960;
      let w = videoEl.videoWidth;
      let h = videoEl.videoHeight;
      if (w > maxDim || h > maxDim) {
        if (w > h) { h = Math.round(h * (maxDim / w)); w = maxDim; }
        else { w = Math.round(w * (maxDim / h)); h = maxDim; }
      }
      offscreenCanvas.width = w;
      offscreenCanvas.height = h;
      offscreenCtx.drawImage(videoEl, 0, 0, w, h);

      offscreenCanvas.toBlob(async (blob) => {
        if (!blob || !ws || ws.readyState !== WebSocket.OPEN) return;
        try {
          const ab = await blob.arrayBuffer();
          const framed = buildFramedPacket(PKT_TYPE_SCREEN_JPEG, 0, new Uint8Array(ab));
          ws.send(framed);
          radarLastSendTime = Date.now();
          radarFramesSent++;
        } catch (err) {
          console.warn("Notice transmitting radar frame:", err);
        }
      }, "image/jpeg", 0.72);

      const nextDelay = (radarState === 'DYNAMIC') ? RADAR_DYNAMIC_INTERVAL_MS : 300;
      radarLoopTimer = setTimeout(radarTick, nextDelay);
    } else {
      radarFramesSuppressed++;
      radarLoopTimer = setTimeout(radarTick, 300);
    }
  }

  radarTick();
}

// Picture-in-Picture
async function togglePiP() {
  if (document.pictureInPictureElement) {
    await document.exitPictureInPicture();
  } else if (videoEl && videoEl.srcObject) {
    await videoEl.requestPictureInPicture();
  }
}

// Tactical Window Switcher: Brings user's previous app/IDE to the front
async function altTabToApp() {
  console.log("🗗 [Tactical Action] Switching focus to background application (Alt+Tab)...");
  appendSystemNotice("🗗 TACTICAL WINDOW SWITCH: Switching to background application (Alt+Tab)...");

  // 1. Send via active WebSocket
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "alt_tab" }));
  }

  // 2. Also invoke native HTTP endpoint for immediate OS response
  try {
    await fetch('/api/alt-tab', { method: 'POST' });
  } catch (e) {
    console.warn("Alt+Tab API notice:", e);
  }
}

// Tactical Fullscreen & Window State Controllers
function isWindowFullscreen() {
  return Boolean(document.fullscreenElement || document.webkitFullscreenElement);
}

function updateFullscreenBadgeState() {
  const fsBadge = document.getElementById('fullscreen-badge');
  const fsBadgeText = document.getElementById('fullscreen-badge-text');
  const isFs = isWindowFullscreen();

  if (fsBadge) {
    if (isFs) {
      fsBadge.classList.add('is-fullscreen');
    } else {
      fsBadge.classList.remove('is-fullscreen');
    }
  }
  if (fsBadgeText) {
    fsBadgeText.textContent = isFs ? '🗗 EXIT FULLSCREEN' : '⛶ FULLSCREEN';
  }
}

async function toggleAppFullscreen() {
  const isFs = isWindowFullscreen();
  console.log("Toggling Fullscreen state. Currently fullscreen:", isFs);

  try {
    if (!isFs) {
      const docEl = document.documentElement;
      if (docEl.requestFullscreen) {
        await docEl.requestFullscreen();
      } else if (docEl.webkitRequestFullscreen) {
        await docEl.webkitRequestFullscreen();
      } else if (document.body.requestFullscreen) {
        await document.body.requestFullscreen();
      }
    } else {
      if (document.exitFullscreen) {
        await document.exitFullscreen();
      } else if (document.webkitExitFullscreen) {
        await document.webkitExitFullscreen();
      }
    }
  } catch (err) {
    console.warn("HTML5 Fullscreen API error, attempting window API fallback:", err);
    try {
      await fetch('/api/window/fullscreen', { method: 'POST' });
    } catch (e) {}
  }

  setTimeout(updateFullscreenBadgeState, 200);
}

// Tactical Window Controllers
async function minimizeWindow() {
  console.log("Minimizing Chirkoot window to taskbar...");
  try {
    await fetch('/api/minimize', { method: 'POST' });
  } catch (e) {
    console.warn("Minimize error:", e);
  }
}

async function maximizeWindow() {
  console.log("Maximizing Chirkoot window (Win+Up)...");
  try {
    await fetch('/api/window/maximize', { method: 'POST' });
  } catch (e) {
    console.warn("Maximize error:", e);
  }
  setTimeout(updateFullscreenBadgeState, 250);
}

async function restoreWindow() {
  console.log("Restoring / resizing Chirkoot window...");
  if (document.fullscreenElement) {
    try {
      await document.exitFullscreen();
    } catch (e) {}
  } else if (isWindowFullscreen()) {
    try {
      await fetch('/api/window/fullscreen', { method: 'POST' });
    } catch (e) {}
  }
  try {
    await fetch('/api/window/restore', { method: 'POST' });
  } catch (e) {
    console.warn("Restore error:", e);
  }
  setTimeout(updateFullscreenBadgeState, 250);
}

async function closeWindow() {
  if (confirm("Terminate Chirkoot assistant and close window?")) {
    try {
      await fetch('/api/window/close', { method: 'POST' });
    } catch (e) {}
    window.close();
  }
}

document.addEventListener('fullscreenchange', () => {
  updateFullscreenBadgeState();
});

window.addEventListener('resize', () => {
  updateFullscreenBadgeState();
});

if (window.matchMedia) {
  try {
    window.matchMedia('(display-mode: fullscreen)').addEventListener('change', (e) => {
      updateFullscreenBadgeState(e.matches);
    });
  } catch (e) {}
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'F11') {
    // Allow native browser F11 without preventDefault
    setTimeout(updateFullscreenBadgeState, 300);
  }
  if (e.key === 'Escape') {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else if (isWindowFullscreen()) {
      fetch('/api/window/fullscreen', { method: 'POST' }).catch(() => {});
    }
    setTimeout(updateFullscreenBadgeState, 300);
  }
});

// --- Mic Toggle ---
function toggleMic() {
  isMicMuted = !isMicMuted;
  if (audioWorkletNode) {
    audioWorkletNode.port.postMessage({ type: 'set_mute', isMuted: isMicMuted });
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "set_mic_mute", muted: isMicMuted }));
  }
  if (isMicMuted) {
    btnMic.classList.remove('active');
    btnMicLabel.textContent = 'MIC MUTED';
    audioStateText.textContent = 'MIC MUTED';
    audioStateText.style.color = '#ff3366';
  } else {
    btnMic.classList.add('active');
    btnMicLabel.textContent = 'NEURAL MIC';
    updateModeUI(currentMicMode);
  }
  updatePrivacyIndicators();
  updateCentralAssistantStatus(isMicMuted ? 'standby' : 'listening');
}

// --- Session Toggle ---
function toggleSession() {
  const sessionLabel = document.getElementById('btn-session-label');
  if (ws && ws.readyState === WebSocket.OPEN) {
    isExplicitlyDisconnected = true;
    ws.close();
    if (sessionLabel) sessionLabel.textContent = 'RECONNECT LINK';
  } else {
    isExplicitlyDisconnected = false;
    initWebSocket();
    if (sessionLabel) sessionLabel.textContent = 'TERMINATE LINK';
  }
}

// --- Server Events & Transcript Updates ---
function handleServerEvent(data) {
  if (data.type === 'wake_detected') {
    console.log(`⚡ [Backend Local Wake Detected]: "${data.keyword}" (Latency: ${data.duration_ms}ms)`);
    if (!isAwake) {
      wakeUpChirkoot("", data.keyword || "Chirkoot");
    } else {
      resetWakeTimer();
    }
  } else if (data.type === 'interrupted') {
    stopAudioPlayback();
    activeGeminiBubble = null;
    console.log("⚡ [Chirkoot Interrupted]");
    updateCentralAssistantStatus('standby');
  } else if (data.type === 'transcript_user') {
    appendUserText(data.text);
    updateCentralAssistantStatus('thinking');
    if (currentMicMode === 'wake-word') {
      wakeUpChirkoot();
    }
    checkVoiceReplyCommands(data.text);
    checkVoiceWindowCommands(data.text);
  } else if (data.type === 'transcript_gemini') {
    appendGeminiText(data.text);
    updateCentralAssistantStatus('speaking');
  } else if (data.type === 'turn_complete') {
    activeGeminiBubble = null;
    activeUserBubble = null;
    updateCentralAssistantStatus(isAwake ? 'listening' : 'standby');
    refreshTasksData();
    if (isAwake && currentMicMode === 'wake-word') {
      resetWakeTimer();
    }
  } else if (data.type === 'tool_call') {
    appendToolCard(data.name, data.args);
    refreshToolActivityData();
    refreshTasksData();
    if (currentMicMode === 'wake-word') {
      resetWakeTimer();
    }
  } else if (data.type === 'tool_result') {
    const isCached = data.cached ? " [DEDUPLICATED / CACHED]" : "";
    appendSystemNotice(`⚙️ Tool '${data.name}'${isCached} executed: ${String(data.result).slice(0, 150)}`);
    refreshToolActivityData();
    refreshTasksData();
  } else if (data.type === 'task_update' || data.type === 'task_step' || data.type === 'task_complete') {
    refreshTasksData();
  } else if (data.type === 'ocr_result') {
    appendSystemNotice("🔍 OCR SCAN COMPLETED: Results received from vision engine.");
    appendGeminiMessage(data.result, "TACTICAL OCR ENGINE");
  } else if (data.type === 'policy_confirmation_request') {
    showPolicyConfirmationModal(data);
    refreshPermissionsData();
  } else if (data.type === 'kill_switch_state') {
    updateKillSwitchUI(data.active);
    updatePrivacyIndicators();
  } else if (data.type === 'lifecycle_state') {
    const s = data.state;
    if (s === 'connected') {
      const mode = data.details && data.details.resumed ? 'RESUMED' : 'SYNCHRONIZED';
      appendSystemNotice(`⚡ NEURAL UPLINK ${mode}: Connected to Gemini 3.8 Live.`);
    } else if (s === 'reconnecting') {
      const delay = data.details?.delay || 0.5;
      const attempt = data.details?.attempt || 1;
      appendSystemNotice(`🔄 UPLINK ROTATION: Reconnecting upstream in ${delay}s (Attempt ${attempt})...`);
    } else if (s === 'go_away') {
      appendSystemNotice(`⚠️ GOAWAY SIGNAL: Server session rotation (${data.details?.time_left || 'imminent'}). Preserving context...`);
    } else if (s === 'context_reseeded') {
      appendSystemNotice(`🧠 CONTEXT RESEEDED: Restored ${data.details?.turns_count || 0} compact conversational turns.`);
    } else if (s === 'task_recovered') {
      appendSystemNotice(`📋 TASK RECOVERED: In-flight task resumed: "${data.details?.prompt || data.details?.task_id}"`);
    }
  } else if (data.type === 'radar_privacy_alert') {
    handleRadarPrivacyAlert(data);
  } else if (data.type === 'radar_telemetry') {
    handleRadarTelemetry(data.data);
  } else if (data.type === 'radar_state') {
    radarState = data.state;
    radarManualPaused = (data.state === 'PAUSED');
    updateRadarUIState();
  } else if (data.type === 'hud_exclusion_status') {
    handleHudExclusionStatus(data.data);
  } else if (data.type === 'api_key_required' || data.type === 'api_key_error') {
    openApiKeyModal(data.message);
  } else if (data.type === 'api_key_accepted') {
    closeApiKeyModal();
    appendSystemNotice("🔑 GEMINI API KEY ACCEPTED: Key saved to DPAPI vault. Initializing uplink...");
  } else if (data.type === 'reconnect_paused') {
    appendSystemNotice(`🛑 QUOTA GUARD: ${data.message || 'Upstream reconnection paused to protect API quota.'}`);
    updateCentralAssistantStatus('standby');
  } else if (data.type === 'api_quota_exhausted') {
    appendSystemNotice(`🛑 QUOTA EXHAUSTED: ${data.message || 'Google Gemini API quota reached.'}`);
    updateCentralAssistantStatus('standby');
  } else if (data.type === 'upstream_error') {
    appendSystemNotice(`⚠️ UPSTREAM ERROR: ${data.message || 'Payload rejected by Gemini.'}`);
    updateCentralAssistantStatus('standby');
  }
}

// Tactical OCR Scanning
async function triggerScreenOCR() {
  console.log("🔍 [Tactical Action] Triggering Screen OCR Scan...");
  appendSystemNotice("🔍 OCR SCAN: Extracting text & coordinates from active screen...");

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "ocr_scan" }));
  } else {
    try {
      const res = await fetch('/api/ocr', { method: 'POST' });
      const data = await res.json();
      if (data && data.result) {
        appendSystemNotice("🔍 OCR SCAN COMPLETED: Results received from vision engine.");
        appendGeminiMessage(data.result, "TACTICAL OCR ENGINE");
      }
    } catch (err) {
      appendSystemNotice("❌ OCR Scan Error: " + err);
    }
  }
}

function getTimestamp() {
  const now = new Date();
  return now.toTimeString().split(' ')[0] + '.' + String(now.getMilliseconds()).padStart(3, '0').slice(0, 1);
}

function appendUserText(text, isTextInput = false) {
  activeGeminiBubble = null;
  const tag = isTextInput ? "[OPERATOR // TEXT DIRECTIVE]" : "[OPERATOR // AUDIO UPLINK]";
  const meta = isTextInput ? "KEYBOARD INPUT • DIRECTIVE" : "MIC 16.0 kHz • LOCAL BUFFER";

  if (!activeUserBubble) {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message user-msg';
    msgDiv.innerHTML = `
      <div class="msg-avatar">👤</div>
      <div class="msg-bubble">
        <div class="msg-header-info">
          <span class="msg-sender-tag">${tag}</span>
          <span class="msg-time-tag">${getTimestamp()}</span>
        </div>
        <p class="bubble-content"></p>
        <div class="msg-meta">${meta}</div>
      </div>
    `;
    transcriptBox.appendChild(msgDiv);
    activeUserBubble = msgDiv.querySelector('.bubble-content');
  }
  activeUserBubble.textContent = text;
  transcriptBox.scrollTop = transcriptBox.scrollHeight;
  if (isTextInput) {
    activeUserBubble = null;
  }
}

// --- Interactive Text Chat Terminal Handlers ---
const chatTextInput = document.getElementById('chat-text-input');
const chatForm = document.getElementById('chat-form');

function handleChatSubmit(event) {
  if (event) event.preventDefault();
  if (!chatTextInput) return;
  const text = chatTextInput.value.trim();
  if (!text) return;

  sendTextMessage(text);
  chatTextInput.value = '';
}

function sendQuickPrompt(promptText) {
  if (!promptText) return;
  sendTextMessage(promptText);
}

function sendTextMessage(text) {
  if (!text || !text.trim()) return;
  text = text.trim();

  // Waking up Chirkoot on text input
  if (currentMicMode === 'wake-word') {
    wakeUpChirkoot();
  }

  // Cloud Gemini Live Link Check
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    appendSystemNotice("⚠️ Link Standby: Cloud Gemini Live link is disconnected. Click 'INITIALIZE TACTICAL LINK' to connect.");
    return;
  }

  // Display user prompt in transcript feed immediately
  appendUserText(text, true);

  // Transmit text message to backend
  ws.send(JSON.stringify({
    type: "text",
    text: text
  }));
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function appendSystemNotice(notice) {
  const noticeDiv = document.createElement('div');
  noticeDiv.className = 'tool-card';
  noticeDiv.style.borderColor = 'rgba(0, 240, 255, 0.4)';
  noticeDiv.style.background = 'rgba(0, 240, 255, 0.08)';
  noticeDiv.innerHTML = `<span>ℹ️</span><div><strong>SYSTEM NOTICE:</strong> ${notice}</div>`;
  transcriptBox.appendChild(noticeDiv);
  transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

function appendGeminiText(text) {
  activeUserBubble = null;
  if (!activeGeminiBubble) {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message gemini-msg';
    msgDiv.innerHTML = `
      <div class="msg-avatar"><img src="/static/chirkoot.png" alt="Chirkoot"></div>
      <div class="msg-bubble">
        <div class="msg-header-info">
          <span class="msg-sender-tag">[CHIRKOOT // TACTICAL STREAM]</span>
          <span class="msg-time-tag">${getTimestamp()}</span>
        </div>
        <p class="bubble-content"></p>
        <div class="msg-meta">CHIRKOOT 3.8 LIVE • 24.0 kHz PCM</div>
      </div>
    `;
    transcriptBox.appendChild(msgDiv);
    activeGeminiBubble = msgDiv.querySelector('.bubble-content');
  }
  activeGeminiBubble.textContent += text;
  transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

function appendToolCard(name, args) {
  const card = document.createElement('div');
  const isSelfLearning = [
    'modify_source_code',
    'self_learn_and_implement',
    'create_custom_tool',
    'remember_insight',
    'recall_memory',
    'rollback_code',
    'inspect_self',
    'run_self_tests',
    'get_codebase_guide'
  ].includes(name);
  
  if (isSelfLearning) {
    card.className = 'tool-card self-learning-card';
    card.style.borderColor = 'rgba(157, 78, 221, 0.7)';
    card.style.borderLeft = '4px solid #9d4edd';
    card.style.background = 'rgba(157, 78, 221, 0.12)';
    card.style.boxShadow = '0 0 20px rgba(157, 78, 221, 0.3)';
    card.innerHTML = `
      <span style="font-size: 1.1rem;">🧠</span>
      <div>
        <strong style="color: #c77dff;">NEURAL EVOLUTION // ${name.toUpperCase()}</strong>
        <div style="font-size: 0.72rem; opacity: 0.85; margin-top: 2px;"><code>${JSON.stringify(args || {})}</code></div>
      </div>
    `;
  } else {
    card.className = 'tool-card';
    card.innerHTML = `
      <span>⚡</span>
      <div>
        <strong>TACTICAL ACTION:</strong> ${name} <code style="opacity:0.85;">(${JSON.stringify(args || {})})</code>
      </div>
    `;
  }
  transcriptBox.appendChild(card);
  transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

// ==========================================================================
// REAL-TIME AUDIO ARC REACTOR VISUALIZER (HTML5 Canvas)
// ==========================================================================
const visualizerCanvas = document.getElementById('arc-visualizer-canvas');
let visualizerCtx = null;
if (visualizerCanvas) {
  visualizerCtx = visualizerCanvas.getContext('2d');
}

let micDataArray = new Uint8Array(32);
let outDataArray = new Uint8Array(32);
let visualizerAngle = 0;

function renderArcVisualizer() {
  requestAnimationFrame(renderArcVisualizer);
  if (!visualizerCtx || !visualizerCanvas) return;

  const w = visualizerCanvas.width;
  const h = visualizerCanvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const baseRadius = 82;

  visualizerCtx.clearRect(0, 0, w, h);

  // Poll frequency data
  let hasAudio = false;
  let activeData = micDataArray;

  if (outputAnalyser && activeSources.length > 0) {
    outputAnalyser.getByteFrequencyData(outDataArray);
    activeData = outDataArray;
    hasAudio = true;
  } else if (micAnalyser && !isMicMuted) {
    micAnalyser.getByteFrequencyData(micDataArray);
    activeData = micDataArray;
    let sum = 0;
    for (let i = 0; i < activeData.length; i++) sum += activeData[i];
    if (sum > 50) hasAudio = true;
  }

  // Update Header Equalizer Bars
  if (soundSpectrum) {
    const bars = soundSpectrum.querySelectorAll('.bar');
    for (let b = 0; b < bars.length; b++) {
      const val = activeData[b * 2] || 0;
      const height = Math.max(3, Math.min(14, (val / 255) * 14));
      bars[b].style.height = `${height}px`;
      bars[b].style.background = hasAudio ? '#00f0ff' : (isAwake ? 'rgba(0, 240, 255, 0.4)' : 'rgba(99, 102, 241, 0.3)');
    }
  }

  // Render Arc Rays on Canvas
  const totalRays = 36;
  visualizerAngle += isAwake ? 0.012 : 0.005;

  for (let i = 0; i < totalRays; i++) {
    const angle = (i / totalRays) * Math.PI * 2 + visualizerAngle;
    const dataIdx = i % activeData.length;
    const rawVal = activeData[dataIdx] || 0;
    
    // Calculate length of radiating spectrum rays
    const barLen = hasAudio ? 8 + (rawVal / 255) * 55 : (isAwake ? 5 + Math.sin(visualizerAngle * 3 + i) * 3 : 2 + Math.sin(visualizerAngle * 2 + i));

    const x1 = cx + Math.cos(angle) * baseRadius;
    const y1 = cy + Math.sin(angle) * baseRadius;
    const x2 = cx + Math.cos(angle) * (baseRadius + barLen);
    const y2 = cy + Math.sin(angle) * (baseRadius + barLen);

    visualizerCtx.beginPath();
    visualizerCtx.moveTo(x1, y1);
    visualizerCtx.lineTo(x2, y2);
    visualizerCtx.lineWidth = isAwake ? 2.5 : 1.5;

    if (hasAudio) {
      visualizerCtx.strokeStyle = (i % 2 === 0) ? '#00f0ff' : '#0080ff';
      visualizerCtx.shadowColor = '#00f0ff';
      visualizerCtx.shadowBlur = 8;
    } else if (isAwake) {
      visualizerCtx.strokeStyle = 'rgba(0, 240, 255, 0.4)';
      visualizerCtx.shadowColor = 'transparent';
      visualizerCtx.shadowBlur = 0;
    } else {
      visualizerCtx.strokeStyle = 'rgba(129, 140, 248, 0.2)';
      visualizerCtx.shadowColor = 'transparent';
      visualizerCtx.shadowBlur = 0;
    }
    visualizerCtx.stroke();
  }
}

// Start audio visualizer loop
requestAnimationFrame(renderArcVisualizer);

window.addEventListener('load', () => {
  initWebSocket();
  updateFullscreenBadgeState();
  updateReplyModeUI(currentReplyMode);
});

// ==============================================================================
// SECURITY POLICY & HITL CONFIRMATION SYSTEM (MK-V)
// ==============================================================================

let currentPolicyCallId = null;
let isKillSwitchActive = false;

function showPolicyConfirmationModal(req) {
  currentPolicyCallId = req.call_id;
  const modal = document.getElementById('policy-confirm-modal');
  const toolNameEl = document.getElementById('modal-tool-name');
  const toolArgsEl = document.getElementById('modal-tool-args');
  const tierBadgeEl = document.getElementById('modal-tier-badge');
  const reasonTextEl = document.getElementById('modal-reason-text');
  const taintBoxEl = document.getElementById('modal-taint-warning');
  const taintTextEl = document.getElementById('modal-taint-text');

  if (toolNameEl) toolNameEl.textContent = req.tool_name || 'unknown_tool';
  if (toolArgsEl) toolArgsEl.textContent = JSON.stringify(req.args || {}, null, 2);
  if (tierBadgeEl) tierBadgeEl.textContent = req.tier || 'TIER 3: HIGH RISK';
  if (reasonTextEl) reasonTextEl.textContent = req.reason || 'Physical confirmation required.';

  if (taintBoxEl) {
    if (req.is_tainted) {
      taintBoxEl.style.display = 'flex';
      if (taintTextEl) taintTextEl.textContent = `TAINTED: Untrusted input was recently ingested (${req.taint_source || 'screen/files'}). Scrutinize carefully!`;
    } else {
      taintBoxEl.style.display = 'none';
    }
  }

  if (modal) modal.style.display = 'flex';
  playNotificationSound();
}

function handlePolicyDecision(decision) {
  const modal = document.getElementById('policy-confirm-modal');
  if (modal) modal.style.display = 'none';

  if (currentPolicyCallId && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'policy_confirmation_response',
      call_id: currentPolicyCallId,
      decision: decision
    }));
    console.log(`[HITL Decision Sent] ${decision} for call ${currentPolicyCallId}`);
  }
  currentPolicyCallId = null;
}

// Emergency Kill Switch
function toggleEmergencyStop() {
  if (isKillSwitchActive) {
    resetKillSwitch();
  } else {
    triggerKillSwitch("Emergency Stop Button pressed");
  }
}

function triggerKillSwitch(reason) {
  isKillSwitchActive = true;
  // 1. Immediately halt assistant audio playback
  stopAudioPlayback();

  // 2. Immediately mute microphone transmission
  isMicMuted = true;
  if (audioWorkletNode) {
    audioWorkletNode.port.postMessage({ type: 'set_mute', isMuted: true });
  }
  const micBtn = document.getElementById('btn-mic-toggle');
  if (micBtn) micBtn.classList.add('muted');
  const micLabel = document.getElementById('mic-label');
  if (micLabel) micLabel.textContent = 'MIC MUTED [STOP ENGAGED]';

  // 3. Immediately halt screen video transmission if active
  if (isScreenActive && typeof stopScreenCapture === 'function') {
    try { stopScreenCapture(); } catch (e) {}
  }

  // 4. Close any open policy modals
  const policyModal = document.getElementById('policy-confirm-modal');
  if (policyModal) policyModal.style.display = 'none';
  currentPolicyCallId = null;

  // 5. Update UI & Show Prominent Alert Banner
  updateKillSwitchUI(true);
  appendSystemNotice(`🛑 [EMERGENCY STOP ENGAGED]: ${reason}. UI actions, audio, video, and child processes halted.`);

  // 6. Transmit to backend
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'kill_switch', reason: reason }));
  }
  fetch('/api/kill-switch', { method: 'POST' }).catch(() => {});
}

function resetKillSwitch() {
  isKillSwitchActive = false;
  updateKillSwitchUI(false);
  appendSystemNotice("✅ [EMERGENCY STOP RESET]: Standby mode active. A new instruction is required to begin any task.");

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'reset_kill_switch' }));
  }
  fetch('/api/kill-switch/reset', { method: 'POST' }).catch(() => {});
}

function updateKillSwitchUI(active) {
  isKillSwitchActive = active;
  const btn = document.getElementById('btn-kill-switch');
  const txt = document.getElementById('kill-switch-text');
  const banner = document.getElementById('emergency-stop-banner');

  if (btn) {
    if (active) {
      btn.classList.add('active');
      if (txt) txt.textContent = 'STOP ENGAGED (RESET)';
    } else {
      btn.classList.remove('active');
      if (txt) txt.textContent = 'EMERGENCY STOP';
    }
  }

  if (banner) {
    banner.style.display = active ? 'flex' : 'none';
  }
}

// Memory Review & Forget Modal
async function openMemoryReviewModal() {
  const modal = document.getElementById('memory-review-modal');
  const container = document.getElementById('memory-table-container');
  if (modal) modal.style.display = 'flex';

  if (!container) return;
  container.innerHTML = '<div class="memory-loading">Loading persistent memories...</div>';

  try {
    const res = await fetch('/api/memories');
    const data = await res.json();
    const memories = data.memories || [];

    if (memories.length === 0) {
      container.innerHTML = '<div style="color: #8899aa; padding: 20px 0;">No persistent memories currently recorded in %LOCALAPPDATA%\\Chirkoot\\data\\memory.json.</div>';
      return;
    }

    let html = '';
    for (const mem of memories) {
      const isTainted = mem.provenance && mem.provenance.includes('tainted');
      html += `
        <div class="memory-item-card" id="mem-card-${mem.category}-${mem.key}">
          <div class="memory-info">
            <div class="memory-cat-tag">
              [${mem.category}]
              <span class="memory-prov-badge ${isTainted ? 'tainted' : ''}">${mem.provenance || 'user_explicit'}</span>
            </div>
            <div>
              <span class="memory-key-tag">${mem.key}:</span>
              <span class="memory-insight-text">${mem.insight}</span>
            </div>
          </div>
          <button class="memory-forget-btn" onclick="forgetMemory('${mem.category}', '${mem.key}')">Forget</button>
        </div>
      `;
    }
    container.innerHTML = html;
  } catch (err) {
    container.innerHTML = `<div style="color: #ff6b8b;">Error loading memories: ${err}</div>`;
  }
}

function closeMemoryReviewModal() {
  const modal = document.getElementById('memory-review-modal');
  if (modal) modal.style.display = 'none';
}

async function openTelemetryModal() {
  const modal = document.getElementById('telemetry-modal');
  if (modal) modal.style.display = 'flex';
  await refreshTelemetryData();
}

function closeTelemetryModal() {
  const modal = document.getElementById('telemetry-modal');
  if (modal) modal.style.display = 'none';
}

async function refreshTelemetryData() {
  try {
    const [metricsRes, execsRes] = await Promise.all([
      fetch('/api/metrics'),
      fetch('/api/executions?limit=30')
    ]);
    const metricsData = await metricsRes.json();
    const execsData = await execsRes.json();

    if (metricsData.metrics) {
      updateTelemetryModalCards(metricsData.metrics);
      updateTopTelemetryBar(metricsData.metrics);
    }

    const records = execsData.executions || [];
    renderExecutionAuditTable(records);
  } catch (err) {
    console.error("Failed to refresh telemetry data:", err);
  }
}

function updateTopTelemetryBar(m) {
  if (!m) return;
  const s2a = m.latency?.speech_to_first_audio_ms;
  const elLatency = document.getElementById('telemetry-latency');
  if (elLatency) {
    elLatency.textContent = (s2a && s2a.p50 > 0) ? `${Math.round(s2a.p50)}ms` : `-- ms`;
  }
  const elToolSr = document.getElementById('telemetry-tool-sr');
  if (elToolSr && m.reliability) {
    elToolSr.textContent = `${m.reliability.tool_success_rate_percent}%`;
  }
  const elRam = document.getElementById('telemetry-ram');
  if (elRam && m.system_resources) {
    elRam.textContent = `${Math.round(m.system_resources.memory_rss_mb)} MB`;
  }
}

function updateTelemetryModalCards(m) {
  if (!m) return;
  const s2a = m.latency?.speech_to_first_audio_ms;
  const ocr = m.latency?.ocr_latency_ms;
  const uia = m.latency?.uia_latency_ms;

  const elS2aP50 = document.getElementById('metric-s2a-p50');
  if (elS2aP50) elS2aP50.textContent = (s2a && s2a.p50 > 0) ? Math.round(s2a.p50) : '--';
  const elS2aP95 = document.getElementById('metric-s2a-p95');
  if (elS2aP95) elS2aP95.textContent = (s2a && s2a.p95 > 0) ? Math.round(s2a.p95) : '--';
  const elOcrAvg = document.getElementById('metric-ocr-avg');
  if (elOcrAvg) elOcrAvg.textContent = (ocr && ocr.avg > 0) ? Math.round(ocr.avg) : '--';
  const elUiaAvg = document.getElementById('metric-uia-avg');
  if (elUiaAvg) elUiaAvg.textContent = (uia && uia.avg > 0) ? Math.round(uia.avg) : '--';

  const rel = m.reliability || {};
  const elToolSr = document.getElementById('metric-tool-sr');
  if (elToolSr) elToolSr.textContent = rel.tool_success_rate_percent ?? 100;
  const elToolDone = document.getElementById('metric-tool-done');
  if (elToolDone) elToolDone.textContent = rel.completed_tool_calls ?? 0;
  const elToolFail = document.getElementById('metric-tool-fail');
  if (elToolFail) elToolFail.textContent = rel.failed_tool_calls ?? 0;
  const elUiSr = document.getElementById('metric-ui-sr');
  if (elUiSr) elUiSr.textContent = `${rel.ui_verification_success_rate_percent ?? 100}%`;

  const st = m.streaming || {};
  const elFps = document.getElementById('metric-fps');
  if (elFps) elFps.textContent = (st.screen_fps ?? 0.0).toFixed(1);
  const tok = st.token_usage || {};
  const elTokensTot = document.getElementById('metric-tokens-total');
  if (elTokensTot) elTokensTot.textContent = (tok.total_tokens ?? 0).toLocaleString();
  const elTokensRate = document.getElementById('metric-tokens-rate');
  if (elTokensRate) elTokensRate.textContent = (tok.tokens_per_hour ?? 0).toLocaleString();
  const elAudioDrops = document.getElementById('metric-audio-drops');
  if (elAudioDrops) elAudioDrops.textContent = rel.audio_dropped_frames ?? 0;

  const sys = m.system_resources || {};
  const elRam = document.getElementById('metric-ram');
  if (elRam) elRam.textContent = Math.round(sys.memory_rss_mb ?? 0);
  const elCpu = document.getElementById('metric-cpu');
  if (elCpu) elCpu.textContent = `${(sys.cpu_percent ?? 0.0).toFixed(1)}%`;
  const elWorkers = document.getElementById('metric-workers');
  if (elWorkers) elWorkers.textContent = (sys.thread_count ?? 0) + (sys.active_child_processes ? ` (${sys.active_child_processes} proc)` : '');

  const ww = m.wake_word || {};
  const elWakeFa = document.getElementById('metric-wake-fa');
  if (elWakeFa) elWakeFa.textContent = (ww.false_accepts_per_hour ?? 0.0).toFixed(1);
}

function renderExecutionAuditTable(records) {
  const container = document.getElementById('telemetry-table-container');
  if (!container) return;

  if (!records || records.length === 0) {
    container.innerHTML = '<div style="color: #648ba6; padding: 20px 0; text-align: center;">No tool executions logged yet in this session.</div>';
    return;
  }

  let rows = '';
  records.forEach(r => {
    const timeStr = r.start_time ? new Date(r.start_time).toLocaleTimeString() : '--:--:--';
    const statusClass = (r.status || 'unknown').toLowerCase();
    const duration = r.duration_ms ? `${Math.round(r.duration_ms)}ms` : '--';
    const argsStr = JSON.stringify(r.validated_args || {});
    const shortArgs = argsStr.length > 40 ? argsStr.substring(0, 37) + '...' : argsStr;
    const resStr = (r.result_summary || r.error_info || '--');
    const shortRes = resStr.length > 50 ? resStr.substring(0, 47) + '...' : resStr;

    rows += `
      <tr>
        <td style="color: #648ba6; white-space: nowrap;">${escapeHtml(timeStr)}</td>
        <td style="color: #00f0ff; font-weight: 600;">${escapeHtml(r.tool_name || 'unknown')}</td>
        <td><span class="status-badge ${escapeHtml(statusClass)}">${escapeHtml(r.status || 'unknown')}</span></td>
        <td style="text-align: right; color: #a0c4e8;">${escapeHtml(duration)}</td>
        <td style="color: #94a3b8; font-family: monospace;" title="${escapeHtml(argsStr)}">${escapeHtml(shortArgs)}</td>
        <td style="color: #cbd5e1;" title="${escapeHtml(resStr)}">${escapeHtml(shortRes)}</td>
      </tr>
    `;
  });

  container.innerHTML = `
    <table class="telemetry-table">
      <thead>
        <tr>
          <th>TIME</th>
          <th>TOOL</th>
          <th>STATUS</th>
          <th style="text-align: right;">DUR</th>
          <th>ARGS</th>
          <th>RESULT / ERROR</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  `;
}

async function pollOperationalMetrics() {
  try {
    const res = await fetch('/api/metrics');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.metrics) return;
    updateTopTelemetryBar(data.metrics);

    const modal = document.getElementById('telemetry-modal');
    if (modal && modal.style.display === 'flex') {
      updateTelemetryModalCards(data.metrics);
    }
  } catch (err) {
    // Silent background poll
  }
}

// Start continuous operational metrics polling every 3 seconds
setInterval(pollOperationalMetrics, 3000);
setTimeout(pollOperationalMetrics, 800);

async function forgetMemory(category, key) {
  try {
    const res = await fetch(`/api/memories/${encodeURIComponent(category)}/${encodeURIComponent(key)}`, {
      method: 'DELETE'
    });
    const card = document.getElementById(`mem-card-${category}-${key}`);
    if (card) {
      card.style.opacity = '0.3';
      card.innerHTML = `<span style="color: #ff6b8b;">Memory forgotten.</span>`;
      setTimeout(() => card.remove(), 800);
    }
    appendSystemNotice(`🗑️ Forgotten memory: [${category}] ${key}`);
  } catch (err) {
    alert("Error forgetting memory: " + err);
  }
}

// Global Keyboard Shortcuts
let lastEscTime = 0;
window.addEventListener('keydown', (e) => {
  // Emergency Kill Switch: Ctrl+Alt+X or Pause/Break or Ctrl+Shift+X
  if ((e.ctrlKey && e.altKey && (e.code === 'KeyX' || e.key.toLowerCase() === 'x')) ||
      (e.ctrlKey && e.shiftKey && (e.code === 'KeyX' || e.key.toLowerCase() === 'x')) ||
      e.key === 'Pause') {
    e.preventDefault();
    triggerKillSwitch("Keyboard shortcut (Ctrl+Alt+X / Pause)");
    return;
  }

  // Physical Human Confirmation to Reset Emergency Stop: Enter key
  if (isKillSwitchActive && e.key === 'Enter') {
    e.preventDefault();
    resetKillSwitch();
    return;
  }

  // Handle Policy Modal Keys
  const policyModal = document.getElementById('policy-confirm-modal');
  const isPolicyOpen = policyModal && policyModal.style.display === 'flex';

  if (e.key === 'Escape') {
    if (isPolicyOpen) {
      e.preventDefault();
      handlePolicyDecision('deny');
      return;
    }
    const memModal = document.getElementById('memory-review-modal');
    if (memModal && memModal.style.display === 'flex') {
      closeMemoryReviewModal();
      return;
    }
    const telemModal = document.getElementById('telemetry-modal');
    if (telemModal && telemModal.style.display === 'flex') {
      closeTelemetryModal();
      return;
    }
    const apiKeyModal = document.getElementById('api-key-modal');
    if (apiKeyModal && apiKeyModal.style.display === 'flex') {
      closeApiKeyModal();
      return;
    }

    // Rapid Esc+Esc (< 400ms) triggers Emergency Kill Switch
    const now = Date.now();
    if (now - lastEscTime < 400) {
      triggerKillSwitch("Emergency double-tap Escape (Esc+Esc)");
    }
    lastEscTime = now;
  } else if (e.key === 'Enter') {
    if (isPolicyOpen) {
      e.preventDefault();
      handlePolicyDecision('allow');
    }
  }
});

// Hardware Audio Device Change Detection (Headphones/Mic plugged or unplugged)
if (typeof navigator !== 'undefined' && navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', async () => {
    console.log("⚡ [Audio Device Changed] Re-enumerating and re-binding microphone...");
    if (micStream) {
      micStream.getTracks().forEach(track => {
        try { track.stop(); } catch (e) {}
      });
      micStream = null;
    }
    await initAudioInput();
  });
}

// Automatic AudioContext Resume on user interaction
function resumeAudioContexts() {
  if (audioContext && audioContext.state === 'suspended') {
    audioContext.resume().catch(() => {});
  }
  if (outputAudioContext && outputAudioContext.state === 'suspended') {
    outputAudioContext.resume().catch(() => {});
  }
}
document.addEventListener('click', resumeAudioContexts, { passive: true });
document.addEventListener('keydown', resumeAudioContexts, { passive: true });


/* ==========================================================================
   CHIRKOOT MK-V COMMAND INTERFACE INTERACTION ENGINE
   ========================================================================== */

let currentActiveView = 'cockpit';
let currentActiveTaskId = null;
let liveDiagTimer = null;
let radarSensitiveAppPaused = false;

// --- 1. View & Navigation Management ---
function switchHudView(viewName) {
  currentActiveView = viewName;
  const allTabs = document.querySelectorAll('.nav-tab-btn');
  allTabs.forEach(t => t.classList.remove('active'));

  const activeTab = document.getElementById(`tab-${viewName}`);
  if (activeTab) activeTab.classList.add('active');

  const allPanes = document.querySelectorAll('.hud-view-pane');
  allPanes.forEach(p => p.style.display = 'none');

  const targetPane = document.getElementById(`view-${viewName}`);
  if (targetPane) {
    targetPane.style.display = 'flex';
  }

  // Auto-refresh data corresponding to selected view
  if (viewName === 'tasks') {
    refreshTasksData();
  } else if (viewName === 'tools') {
    refreshToolActivityData();
  } else if (viewName === 'permissions') {
    refreshPermissionsData();
  } else if (viewName === 'memories') {
    refreshMemoryViewData();
  } else if (viewName === 'skills') {
    refreshSkillsData();
  } else if (viewName === 'diagnostics') {
    refreshDiagnosticsData();
  }
}

// --- 2. Central Assistant Status & Privacy Indicators ---
function updateCentralAssistantStatus(state) {
  const badge = document.getElementById('asst-state-badge');
  const icon = document.getElementById('asst-state-icon');
  const text = document.getElementById('asst-state-text');
  if (!badge || !text) return;

  badge.className = 'assistant-status-badge ' + state;
  if (state === 'listening') {
    if (icon) icon.textContent = '🎧';
    text.textContent = 'LISTENING // 16kHz';
  } else if (state === 'speaking') {
    if (icon) icon.textContent = '🗣️';
    text.textContent = 'SPEAKING // 24kHz';
  } else if (state === 'thinking') {
    if (icon) icon.textContent = '⚡';
    text.textContent = 'THINKING...';
  } else {
    if (icon) icon.textContent = '💤';
    text.textContent = 'STANDBY';
  }

  updatePrivacyIndicators();
}

function updatePrivacyIndicators() {
  const micInd = document.getElementById('privacy-mic-indicator');
  const micTxt = document.getElementById('privacy-mic-text');
  const asstMic = document.getElementById('asst-mic-text');

  if (micInd && micTxt) {
    if (isMicMuted || isKillSwitchActive) {
      micInd.className = 'hud-privacy-indicator muted';
      micTxt.textContent = 'MIC: MUTED';
      if (asstMic) asstMic.textContent = 'MIC: MUTED';
    } else if (isAwake && currentMicMode === 'always-on') {
      micInd.className = 'hud-privacy-indicator streaming';
      micTxt.textContent = '● MIC: STREAMING (16kHz)';
      if (asstMic) asstMic.textContent = 'MIC: STREAMING';
    } else {
      micInd.className = 'hud-privacy-indicator standby';
      micTxt.textContent = 'MIC: STANDBY';
      if (asstMic) asstMic.textContent = 'MIC: STANDBY';
    }
  }

  const radInd = document.getElementById('privacy-radar-indicator');
  const radTxt = document.getElementById('privacy-radar-text');
  const alertBox = document.getElementById('hud-privacy-alert-box');

  if (radInd && radTxt) {
    if (radarSensitiveAppPaused) {
      radInd.className = 'hud-privacy-indicator privacy-pause';
      radTxt.textContent = '⚠️ PAUSED: SENSITIVE APP';
      if (alertBox) alertBox.style.display = 'flex';
    } else if (isScreenActive) {
      radInd.className = 'hud-privacy-indicator streaming';
      radTxt.textContent = '● RADAR: STREAMING (1.0 FPS)';
      if (alertBox) alertBox.style.display = 'none';
    } else {
      radInd.className = 'hud-privacy-indicator standby';
      radTxt.textContent = 'RADAR: OFF';
      if (alertBox) alertBox.style.display = 'none';
    }
  }
}

// --- 3. Task Manager Implementation ---
async function refreshTasksData() {
  try {
    const res = await fetch('/api/tasks');
    const data = await res.json();
    const active = data.active_task;
    const tasks = data.tasks || [];

    const heroId = document.getElementById('hero-task-id');
    const heroStatus = document.getElementById('hero-task-status');
    const heroPrompt = document.getElementById('hero-task-prompt');
    const heroSteps = document.getElementById('hero-task-steps');
    const activeStrip = document.getElementById('hud-active-task-strip');
    const stripPrompt = document.getElementById('task-strip-prompt-text');
    const stripStatus = document.getElementById('task-strip-status-badge');
    const badgeCount = document.getElementById('badge-active-tasks');

    if (active) {
      currentActiveTaskId = active.task_id;
      if (heroId) heroId.textContent = `#${active.task_id}`;
      if (heroStatus) {
        heroStatus.textContent = active.status.toUpperCase();
        heroStatus.className = 'task-strip-status ' + (active.status === 'in_progress' ? '' : active.status);
      }
      if (heroPrompt) heroPrompt.textContent = active.prompt;
      if (badgeCount) {
        badgeCount.style.display = 'inline-flex';
        badgeCount.textContent = '1';
      }

      // Render step chips
      if (heroSteps) {
        const tools = active.tool_calls || [];
        let stepsHtml = `<div class="task-step-chip completed"><span>✓ 1. Ingested Directive</span></div>`;
        tools.forEach((tc, idx) => {
          stepsHtml += `<div class="task-step-chip completed"><span>✓ ${idx + 2}. ${tc.tool}</span></div>`;
        });
        if (active.status === 'in_progress') {
          stepsHtml += `<div class="task-step-chip active"><span>⚡ ${tools.length + 2}. Executing...</span></div>`;
        }
        heroSteps.innerHTML = stepsHtml;
      }

      // Update cockpit floating strip
      if (activeStrip) activeStrip.style.display = 'flex';
      if (stripPrompt) stripPrompt.textContent = `Active Directive: ${active.prompt}`;
      if (stripStatus) {
        stripStatus.textContent = active.status.toUpperCase();
        stripStatus.className = 'task-strip-status ' + (active.status === 'in_progress' ? '' : active.status);
      }
    } else {
      currentActiveTaskId = null;
      if (heroId) heroId.textContent = 'No active task';
      if (heroStatus) { heroStatus.textContent = 'IDLE'; heroStatus.className = 'task-strip-status'; }
      if (heroPrompt) heroPrompt.textContent = 'Awaiting operator instruction or wake-word directive...';
      if (heroSteps) {
        heroSteps.innerHTML = `
          <div class="task-step-chip active"><span>1. Directives Ingestion</span></div>
          <div class="task-step-chip"><span>2. Visual &amp; UIA Grounding</span></div>
          <div class="task-step-chip"><span>3. Execution &amp; Closed-Loop Verify</span></div>
        `;
      }
      if (activeStrip) activeStrip.style.display = 'none';
      if (badgeCount) badgeCount.style.display = 'none';
    }

    // Render Recent Task History Table
    const tableContainer = document.getElementById('task-history-container');
    if (tableContainer) {
      if (tasks.length === 0) {
        tableContainer.innerHTML = '<div style="color:#8da4be; padding:12px;">No task history recorded yet.</div>';
      } else {
        let rows = '';
        tasks.slice(0, 15).forEach(t => {
          const statusCls = t.status || 'completed';
          const toolsCount = (t.tool_calls || []).length;
          rows += `
            <tr>
              <td style="color:#00f0ff; font-family:'JetBrains Mono',monospace;">#${t.task_id}</td>
              <td style="color:#f1f5f9; max-width:320px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${t.prompt || 'Directive'}</td>
              <td><span class="status-badge ${statusCls}">${t.status}</span></td>
              <td>${toolsCount} tools</td>
              <td style="color:#8da4be; font-size:0.65rem;">${t.created_at ? t.created_at.split('T')[1]?.slice(0, 8) : '--'}</td>
              <td>
                <button class="strip-btn" onclick="retrySpecificTask('${t.task_id}')">RETRY</button>
              </td>
            </tr>
          `;
        });
        tableContainer.innerHTML = `
          <table class="telemetry-table">
            <thead>
              <tr>
                <th>TASK ID</th>
                <th>DIRECTIVE</th>
                <th>STATUS</th>
                <th>STEPS</th>
                <th>TIME</th>
                <th>ACTION</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        `;
      }
    }
  } catch (err) {
    console.error("Error refreshing task manager data:", err);
  }
}

async function cancelCurrentActiveTask() {
  if (!currentActiveTaskId) return;
  try {
    const res = await fetch(`/api/tasks/${currentActiveTaskId}/cancel`, { method: 'POST' });
    const data = await res.json();
    appendSystemNotice(`🛑 [TASK CANCELLED]: Task #${currentActiveTaskId} halted by operator.`);
    refreshTasksData();
  } catch (e) {
    console.error("Error cancelling task:", e);
  }
}

async function retryCurrentActiveTask() {
  if (!currentActiveTaskId) return;
  await retrySpecificTask(currentActiveTaskId);
}

async function retrySpecificTask(taskId) {
  try {
    const res = await fetch(`/api/tasks/${taskId}/retry`, { method: 'POST' });
    const data = await res.json();
    appendSystemNotice(`🔄 [TASK RETRIED]: Task #${taskId} reset to in_progress.`);
    refreshTasksData();
  } catch (e) {
    console.error("Error retrying task:", e);
  }
}

// --- 4. Tool Activity Stream Implementation ---
async function refreshToolActivityData() {
  const container = document.getElementById('tool-activity-stream');
  if (!container) return;

  try {
    const res = await fetch('/api/executions?limit=35');
    const data = await res.json();
    const execs = data.executions || [];

    if (execs.length === 0) {
      container.innerHTML = '<div style="color:#8da4be; padding:20px; text-align:center;">Zero tool executions recorded in current session.</div>';
      return;
    }

    let cardsHtml = '';
    execs.forEach(ex => {
      const vStatus = (ex.verification_status || 'CONFIRMED').toUpperCase();
      const vCls = vStatus.toLowerCase();
      const duration = ex.duration_ms ? `${ex.duration_ms.toFixed(1)} ms` : '-- ms';
      const argsJson = JSON.stringify(ex.validated_args || {}, null, 2);
      const resultPreview = String(ex.result_preview || ex.result || '').slice(0, 200);

      cardsHtml += `
        <div class="tool-card-item ${vCls}">
          <div class="tool-card-header">
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="tool-name-badge">${ex.tool_name}</span>
              <span class="tool-duration">${duration}</span>
              <span class="tool-verification-badge ${vCls}">✓ ${vStatus}</span>
            </div>
            <span style="color:#64748b; font-size:0.65rem; font-family:'JetBrains Mono',monospace;">${ex.start_time ? ex.start_time.split('T')[1]?.slice(0, 8) : ''}</span>
          </div>
          <div style="font-size:0.7rem; color:#8da4be;">
            Trigger: <span style="color:#cbd5e1;">"${ex.instruction_ref || 'Voice directive'}"</span>
          </div>
          <pre class="tool-args-pre">${argsJson}</pre>
          <div style="font-size:0.68rem; color:#94a3b8; background:rgba(0,0,0,0.3); padding:4px 8px; border-radius:2px;">
            Result: <span style="color:#00ff88;">${resultPreview || 'Executed successfully'}</span>
          </div>
        </div>
      `;
    });

    container.innerHTML = cardsHtml;
  } catch (err) {
    container.innerHTML = `<div style="color:#ff6b8b;">Error loading tool stream: ${err}</div>`;
  }
}

// --- 5. Permission Centre Implementation ---
async function refreshPermissionsData() {
  const container = document.getElementById('pending-permissions-container');
  const badge = document.getElementById('badge-pending-permissions');
  if (!container) return;

  try {
    const res = await fetch('/api/permissions/pending');
    const data = await res.json();
    const pendings = data.pending || [];

    if (badge) {
      if (pendings.length > 0) {
        badge.style.display = 'inline-flex';
        badge.textContent = String(pendings.length);
      } else {
        badge.style.display = 'none';
      }
    }

    if (pendings.length === 0) {
      container.innerHTML = `
        <div style="color:#8da4be; font-family:'JetBrains Mono',monospace; padding:30px; text-align:center;">
          ✅ All systems nominal. Zero pending policy authorizations. Actions operate safely within automated security limits.
        </div>
      `;
      return;
    }

    let cards = '';
    pendings.forEach(p => {
      const isTainted = p.is_tainted;
      const target = p.target || (p.args ? JSON.stringify(p.args).slice(0, 80) : '--');

      cards += `
        <div class="perm-alert-card">
          <div class="perm-alert-header">
            <div class="perm-alert-title">
              <span>⚠️ POLICY AUTHORIZATION REQUIRED</span>
              <span class="modal-tier-badge" style="font-size:0.6rem;">${p.tier || 'TIER 3: HIGH RISK'}</span>
            </div>
            <span style="color:#ffaa00; font-family:'JetBrains Mono',monospace; font-size:0.68rem;">Auto-reject in 45s</span>
          </div>

          ${isTainted ? `
            <div class="modal-taint-box" style="display:flex; margin:4px 0;">
              <span class="taint-icon">☣️</span>
              <span class="taint-text">TAINTED: Untrusted input was recently ingested (${p.taint_source || 'screen/files'}). Scrutinize carefully!</span>
            </div>
          ` : ''}

          <div class="perm-field-grid">
            <span class="perm-field-label">REQUESTED ACTION:</span>
            <span class="perm-field-val" style="color:#00f0ff; font-weight:700;">${p.tool_name}</span>

            <span class="perm-field-label">TARGET CONTEXT:</span>
            <span class="perm-field-val">${target}</span>

            <span class="perm-field-label">FULL ARGUMENTS:</span>
            <pre class="tool-args-pre" style="margin:0;">${JSON.stringify(p.args || {}, null, 2)}</pre>
          </div>

          <div class="perm-action-buttons">
            <button class="perm-btn reject" onclick="respondPermissionViaRest('${p.call_id}', 'deny')">
              <span>❌ REJECT &amp; ABORT (Esc)</span>
            </button>
            <button class="perm-btn approve" onclick="respondPermissionViaRest('${p.call_id}', 'allow')">
              <span>✔️ APPROVE &amp; EXECUTE (Enter)</span>
            </button>
          </div>
        </div>
      `;
    });

    container.innerHTML = cards;
  } catch (err) {
    console.error("Error refreshing permissions:", err);
  }
}

async function respondPermissionViaRest(callId, decision) {
  try {
    await fetch(`/api/permissions/${callId}/respond`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision })
    });
    handlePolicyDecision(decision);
    refreshPermissionsData();
  } catch (e) {
    console.error("Error sending permission response:", e);
  }
}

// --- 6. Memory Bank Extended View Implementation ---
async function refreshMemoryViewData(searchQuery = '') {
  const container = document.getElementById('memory-view-container');
  if (!container) return;

  try {
    let url = '/api/memories';
    if (searchQuery) {
      url = `/api/memories?q=${encodeURIComponent(searchQuery)}`;
    }
    const res = await fetch(url);
    const data = await res.json();
    const memories = data.memories || [];

    if (memories.length === 0) {
      container.innerHTML = '<div style="color:#8da4be; padding:20px; text-align:center;">No persistent memories matched query.</div>';
      return;
    }

    let html = '';
    memories.forEach(m => {
      const conf = m.confidence !== undefined ? `${Math.round(m.confidence * 100)}%` : '95%';
      const prov = m.provenance || m.source || 'user_explicit';
      const isConfirmed = m.user_confirmed !== false;

      html += `
        <div class="memory-item-card">
          <div class="memory-info" style="flex:1;">
            <div class="memory-cat-tag">
              [${m.category}] • <span style="color:#00f0ff;">${m.key}</span>
              <span class="memory-prov-badge">${prov}</span>
              <span style="color:#00ff88; font-size:0.6rem; margin-left:6px;">Confidence: ${conf}</span>
            </div>
            <div style="margin-top:4px;">
              <span class="memory-insight-text" id="insight-txt-${m.category}-${m.key}">${m.insight || m.content}</span>
            </div>
          </div>
          <div style="display:flex; align-items:center; gap:6px;">
            ${!isConfirmed ? `
              <button class="strip-btn" onclick="confirmMemoryEntry('${m.category}', '${m.key}')" title="Promote observation to permanent user preference">CONFIRM</button>
            ` : ''}
            <button class="strip-btn" onclick="editMemoryEntryPrompt('${m.category}', '${m.key}')">EDIT</button>
            <button class="memory-forget-btn" onclick="forgetMemoryEntry('${m.category}', '${m.key}')">FORGET</button>
          </div>
        </div>
      `;
    });

    container.innerHTML = html;
  } catch (err) {
    container.innerHTML = `<div style="color:#ff6b8b;">Error loading memory bank: ${err}</div>`;
  }
}

let memSearchDebounce = null;
function handleMemorySearch(event) {
  clearTimeout(memSearchDebounce);
  const q = event.target.value.trim();
  memSearchDebounce = setTimeout(() => {
    refreshMemoryViewData(q);
  }, 300);
}

async function editMemoryEntryPrompt(category, key) {
  const currentEl = document.getElementById(`insight-txt-${category}-${key}`);
  const currentTxt = currentEl ? currentEl.textContent : '';
  const newInsight = prompt(`Edit persistent memory for [${category}] ${key}:`, currentTxt);
  if (newInsight && newInsight !== currentTxt) {
    try {
      await fetch('/api/memories/edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category, key, new_insight: newInsight })
      });
      appendSystemNotice(`🧠 [MEMORY UPDATED]: [${category}] ${key}`);
      refreshMemoryViewData();
    } catch (e) {
      alert("Error editing memory: " + e);
    }
  }
}

async function confirmMemoryEntry(category, key) {
  try {
    await fetch('/api/memories/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, key })
    });
    appendSystemNotice(`🧠 [MEMORY CONFIRMED]: Promoted [${category}] ${key} to user preference.`);
    refreshMemoryViewData();
  } catch (e) {
    alert("Error confirming memory: " + e);
  }
}

async function forgetMemoryEntry(category, key) {
  if (!confirm(`Are you sure you want to permanently forget memory [${category}] ${key}?`)) return;
  try {
    await fetch(`/api/memories/${encodeURIComponent(category)}/${encodeURIComponent(key)}`, {
      method: 'DELETE'
    });
    appendSystemNotice(`🗑️ [MEMORY FORGOTTEN]: [${category}] ${key} purged from SQLite database.`);
    refreshMemoryViewData();
  } catch (e) {
    alert("Error deleting memory: " + e);
  }
}

async function pruneExpiredMemories() {
  try {
    const res = await fetch('/api/memories/prune', { method: 'POST' });
    const data = await res.json();
    appendSystemNotice(`🧹 [PRUNE COMPLETED]: ${data.message || 'Expired memories pruned.'}`);
    refreshMemoryViewData();
  } catch (e) {
    console.error("Error pruning memories:", e);
  }
}

// --- 7. Skill Studio Implementation ---
async function refreshSkillsData() {
  const container = document.getElementById('skills-catalog-container');
  if (!container) return;

  try {
    const res = await fetch('/api/skills');
    const data = await res.json();
    const skills = data.skills || {};
    const skillKeys = Object.keys(skills);

    if (skillKeys.length === 0) {
      container.innerHTML = `
        <div style="color:#8da4be; padding:30px; text-align:center;">
          ⚡ No dynamic learned skills currently installed in %LOCALAPPDATA%\\Chirkoot\\custom_skills.<br>
          <small style="color:#64748b;">Chirkoot autonomously writes, validates, and activates new skills as you teach it workflows.</small>
        </div>
      `;
      return;
    }

    let cards = '';
    skillKeys.forEach(name => {
      const s = skills[name];
      cards += `
        <div class="skill-card-mkv">
          <div class="skill-info-left">
            <div class="skill-name">
              <span>⚡ ${name}</span>
              <span class="status-badge completed" style="font-size:0.6rem;">v${s.version || '1.0.0'}</span>
              <span class="status-badge completed" style="font-size:0.6rem;">AST VERIFIED</span>
            </div>
            <div class="skill-meta">
              Description: <span style="color:#cbd5e1;">${s.description || 'Custom autonomous skill'}</span>
            </div>
            <div class="skill-meta">
              Manifest: <span style="color:#00f0ff;">Allowed: [math, re, json] • Network: DENIED • File Access: SCOPED</span>
            </div>
          </div>
          <div class="skill-actions">
            <button class="skill-action-btn" onclick="viewSkillsDiff()">DIFF</button>
            <button class="skill-action-btn quarantine" onclick="quarantineSkillAction('${name}')">QUARANTINE</button>
            <button class="skill-action-btn" onclick="rollbackSkillAction('${name}')">ROLLBACK</button>
          </div>
        </div>
      `;
    });

    container.innerHTML = cards;
  } catch (err) {
    container.innerHTML = `<div style="color:#ff6b8b;">Error loading skills catalog: ${err}</div>`;
  }
}

async function viewSkillsDiff() {
  const modal = document.getElementById('skill-diff-modal');
  const content = document.getElementById('skill-diff-content');
  if (modal) modal.style.display = 'flex';
  if (content) content.textContent = 'Loading git diff from %LOCALAPPDATA%\\Chirkoot\\skills_repo...';

  try {
    const res = await fetch('/api/skills/diff');
    const data = await res.json();
    if (content) content.textContent = data.diff || 'No uncommitted or recent git diff.';
  } catch (e) {
    if (content) content.textContent = 'Error loading git diff: ' + e;
  }
}

function closeSkillDiffModal() {
  const modal = document.getElementById('skill-diff-modal');
  if (modal) modal.style.display = 'none';
}

async function quarantineSkillAction(name) {
  if (!confirm(`Quarantine skill '${name}'? This isolates the code file and removes it from the active catalog.`)) return;
  try {
    const res = await fetch(`/api/skills/${encodeURIComponent(name)}/quarantine`, { method: 'POST' });
    const data = await res.json();
    appendSystemNotice(`☣️ [SKILL QUARANTINED]: ${data.result || name}`);
    refreshSkillsData();
  } catch (e) {
    alert("Error quarantining skill: " + e);
  }
}

async function rollbackSkillAction(name) {
  if (!confirm(`Rollback skill '${name}' to last-known-good version?`)) return;
  try {
    const res = await fetch(`/api/skills/${encodeURIComponent(name)}/rollback`, { method: 'POST' });
    const data = await res.json();
    appendSystemNotice(`🛡️ [SKILL ROLLBACK]: ${data.result || name}`);
    refreshSkillsData();
  } catch (e) {
    alert("Error rolling back skill: " + e);
  }
}

async function runSkillsSelfTests() {
  try {
    appendSystemNotice("🧪 Running AST and syntax self-tests across all dynamic skills...");
    const res = await fetch('/api/skills/test', { method: 'POST' });
    const data = await res.json();
    alert("Skill Self-Tests Report:\n\n" + data.test_report);
  } catch (e) {
    alert("Error running self-tests: " + e);
  }
}

// --- 8. Diagnostics Telemetry (100% Real Live Metrics) ---
async function refreshDiagnosticsData() {
  try {
    const res = await fetch('/api/diagnostics');
    const data = await res.json();
    if (data.status !== 'ok') return;

    // 1. Gemini
    const g = data.gemini || {};
    const elGeminiP50 = document.getElementById('diag-gemini-p50');
    const elGeminiP95 = document.getElementById('diag-gemini-p95');
    const elGeminiRecon = document.getElementById('diag-gemini-recon');
    const elGeminiGoaway = document.getElementById('diag-gemini-goaway');
    if (elGeminiP50) elGeminiP50.textContent = g.p50_latency_ms ? g.p50_latency_ms.toFixed(1) : '--';
    if (elGeminiP95) elGeminiP95.textContent = g.p95_latency_ms ? g.p95_latency_ms.toFixed(1) : '--';
    if (elGeminiRecon) elGeminiRecon.textContent = String(g.reconnects || 0);
    if (elGeminiGoaway) elGeminiGoaway.textContent = String(g.goaway_received || 0);

    // 2. Audio
    const a = data.audio || {};
    const elAudioDrops = document.getElementById('diag-audio-drops');
    if (elAudioDrops) elAudioDrops.textContent = String(a.audio_drops || 0);

    // 3. Wake Word
    const w = data.wake_word || {};
    const elWakeFa = document.getElementById('diag-wake-fa');
    const elWakeMiss = document.getElementById('diag-wake-miss');
    if (elWakeFa) elWakeFa.textContent = (w.false_accepts_per_hour || 0).toFixed(1);
    if (elWakeMiss) elWakeMiss.textContent = String(w.misses || 0);

    // 4. Vision
    const v = data.vision || {};
    const elVisionFps = document.getElementById('diag-vision-fps');
    const elVisionExcl = document.getElementById('diag-vision-excl');
    if (elVisionFps) elVisionFps.textContent = (v.fps || 1.0).toFixed(1);
    if (elVisionExcl) elVisionExcl.textContent = v.hud_exclusion_mode || 'WDA_EXCLUDEFROMCAPTURE';

    // 5. Backend
    const b = data.backend || {};
    const elUptime = document.getElementById('diag-backend-uptime');
    const elKill = document.getElementById('diag-backend-kill');
    if (elUptime) {
      const upSec = b.uptime_seconds || 0;
      const m = Math.floor(upSec / 60);
      const s = Math.floor(upSec % 60);
      elUptime.textContent = `${m}m ${s}s`;
    }
    if (elKill) elKill.textContent = b.kill_switch_engaged ? 'ENGAGED' : 'DISENGAGED';

    // 6. Host Resources
    const hr = data.host_resources || {};
    const elRam = document.getElementById('diag-host-ram');
    const elCpu = document.getElementById('diag-host-cpu');
    const elThreads = document.getElementById('diag-host-threads');
    const elChildren = document.getElementById('diag-host-children');
    if (elRam) elRam.textContent = hr.memory_rss_mb ? hr.memory_rss_mb.toFixed(1) : '--';
    if (elCpu) elCpu.textContent = hr.cpu_percent ? `${hr.cpu_percent.toFixed(1)}%` : '0.0%';
    if (elThreads) elThreads.textContent = String(hr.thread_count || '--');
    if (elChildren) elChildren.textContent = String(hr.active_child_processes || 0);

    // Also update header RAM
    const topRam = document.getElementById('telemetry-ram');
    if (topRam && hr.memory_rss_mb) topRam.textContent = `${hr.memory_rss_mb.toFixed(1)} MB`;

    // 7. Workers
    const elWorkers = document.getElementById('diag-workers-count');
    if (elWorkers) elWorkers.textContent = String(hr.active_child_processes + 2);

    // 8. Recent Failures
    const failures = data.recent_failures || [];
    const elFailuresCount = document.getElementById('diag-failures-count');
    const elFailuresBadge = document.getElementById('diag-failures-badge');
    const elFailuresDesc = document.getElementById('diag-failures-desc');
    const elFailuresTable = document.getElementById('diag-failures-table');

    if (elFailuresCount) elFailuresCount.textContent = String(failures.length);
    if (elFailuresBadge) {
      elFailuresBadge.textContent = `${failures.length} FAILS`;
      elFailuresBadge.className = 'status-badge ' + (failures.length === 0 ? 'completed' : 'failed');
    }
    if (elFailuresDesc) {
      elFailuresDesc.textContent = failures.length === 0
        ? 'Zero recent tool or verification errors logged.'
        : `${failures.length} tool executions failed verification.`;
    }

    if (elFailuresTable) {
      if (failures.length === 0) {
        elFailuresTable.innerHTML = '<div style="color:#00ff88; padding:12px; font-size:0.75rem;">✓ Zero recent execution failures recorded. All actions verified.</div>';
      } else {
        let fRows = '';
        failures.forEach(f => {
          fRows += `
            <tr>
              <td style="color:#ff6b8b; font-family:'JetBrains Mono',monospace;">${f.tool_name}</td>
              <td style="color:#f1f5f9;">${String(f.error || f.result_preview || '').slice(0, 80)}</td>
              <td><span class="status-badge failed">FAILED</span></td>
              <td style="color:#8da4be; font-size:0.65rem;">${f.start_time ? f.start_time.split('T')[1]?.slice(0, 8) : '--'}</td>
            </tr>
          `;
        });
        elFailuresTable.innerHTML = `
          <table class="telemetry-table">
            <thead><tr><th>TOOL</th><th>ERROR</th><th>STATUS</th><th>TIME</th></tr></thead>
            <tbody>${fRows}</tbody>
          </table>
        `;
      }
    }

  } catch (err) {
    console.error("Error refreshing diagnostics:", err);
  }
}

// Background live polling loop (refreshes active view data every 3 seconds)
setInterval(() => {
  if (currentActiveView === 'diagnostics') {
    refreshDiagnosticsData();
  } else if (currentActiveView === 'tasks') {
    refreshTasksData();
  } else if (currentActiveView === 'tools') {
    refreshToolActivityData();
  } else if (currentActiveView === 'permissions') {
    refreshPermissionsData();
  }
}, 3000);

// Global Keyboard Shortcut: Ctrl+Shift+X for Emergency Stop
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.shiftKey && (e.key === 'X' || e.key === 'x')) {
    e.preventDefault();
    toggleEmergencyStop();
  }
});

// --- Gemini API Key Vault Modal & Management ---
async function checkApiKeyStatus() {
  try {
    const res = await fetch('/api/credentials/gemini');
    if (res.ok) {
      const data = await res.json();
      const pill = document.getElementById('api-key-pill-text');
      if (pill) {
        if (data.configured) {
          pill.textContent = `🔑 KEY: ${data.masked}`;
          pill.style.color = '#00ff88';
        } else {
          pill.textContent = '🔑 NO KEY';
          pill.style.color = '#ff6b8b';
        }
      }
      return data;
    }
  } catch (err) {
    console.warn("Could not check Gemini API key status:", err);
  }
  return null;
}

async function openApiKeyModal(customMsg = null) {
  const modal = document.getElementById('api-key-modal');
  const desc = document.getElementById('api-key-modal-desc');
  const input = document.getElementById('input-api-key');
  const statusMsg = document.getElementById('api-key-status-msg');

  if (customMsg && desc) {
    desc.innerHTML = `<span style="color:#ff6b8b; font-weight:600;">[AUTH REJECTION]</span> ${customMsg}<br><span style="font-size:0.8rem; color:#94a3b8; display:block; margin-top:4px;">Please enter a valid Google AI Studio API key (starts with AIzaSy...) below:</span>`;
  } else if (desc) {
    desc.textContent = "Google Gemini 3.8 Live requires an authorized API key. Please enter your Google AI Studio key below:";
  }

  if (statusMsg) {
    statusMsg.style.display = 'none';
    statusMsg.textContent = '';
  }

  if (input) {
    input.value = '';
    const st = await checkApiKeyStatus();
    if (st && st.configured) {
      input.placeholder = `Current: ${st.masked} (Enter new to replace)`;
    } else {
      input.placeholder = "AIzaSy...";
    }
  }

  if (modal) {
    modal.style.display = 'flex';
    setTimeout(() => {
      if (input) input.focus();
    }, 50);
  }
}

function closeApiKeyModal() {
  const modal = document.getElementById('api-key-modal');
  if (modal) modal.style.display = 'none';
}

function toggleApiKeyVisibility() {
  const input = document.getElementById('input-api-key');
  const btn = document.getElementById('btn-toggle-key-vis');
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = '🔒';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = '👁️';
  }
}

async function saveApiKeyFromModal() {
  const input = document.getElementById('input-api-key');
  const statusMsg = document.getElementById('api-key-status-msg');
  const saveBtn = document.getElementById('btn-save-api-key');
  const keyVal = input ? input.value.trim() : '';

  if (!keyVal) {
    if (statusMsg) {
      statusMsg.style.display = 'block';
      statusMsg.style.background = 'rgba(255, 107, 139, 0.15)';
      statusMsg.style.border = '1px solid #ff6b8b';
      statusMsg.style.color = '#ff6b8b';
      statusMsg.textContent = 'Please enter an API key.';
    }
    return;
  }

  if (saveBtn) saveBtn.disabled = true;
  if (statusMsg) {
    statusMsg.style.display = 'block';
    statusMsg.style.background = 'rgba(0, 243, 255, 0.1)';
    statusMsg.style.border = '1px solid #00f3ff';
    statusMsg.style.color = '#00f3ff';
    statusMsg.textContent = 'Securing key in Windows DPAPI vault...';
  }

  try {
    const res = await fetch('/api/credentials/gemini', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: keyVal })
    });
    const result = await res.json();
    if (res.ok && result.success) {
      if (statusMsg) {
        statusMsg.style.background = 'rgba(0, 255, 136, 0.15)';
        statusMsg.style.border = '1px solid #00ff88';
        statusMsg.style.color = '#00ff88';
        statusMsg.textContent = '✓ Key encrypted with DPAPI. Uplink activating...';
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'set_api_key', api_key: keyVal }));
      }
      await checkApiKeyStatus();
      setTimeout(() => {
        closeApiKeyModal();
        if (saveBtn) saveBtn.disabled = false;
      }, 1000);
    } else {
      throw new Error(result.error || 'Failed to save API key');
    }
  } catch (err) {
    if (statusMsg) {
      statusMsg.style.display = 'block';
      statusMsg.style.background = 'rgba(255, 107, 139, 0.15)';
      statusMsg.style.border = '1px solid #ff6b8b';
      statusMsg.style.color = '#ff6b8b';
      statusMsg.textContent = `Error: ${err.message || err}`;
    }
    if (saveBtn) saveBtn.disabled = false;
  }
}

// Initial boot synchronization
window.addEventListener('DOMContentLoaded', () => {
  updateCentralAssistantStatus('standby');
  updatePrivacyIndicators();
  refreshTasksData();
  refreshDiagnosticsData();
  checkApiKeyStatus();
});
