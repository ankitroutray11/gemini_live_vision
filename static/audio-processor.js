/**
 * CHIRKOOT MK-V AUDIO WORKLET PROCESSOR
 * Dedicated real-time audio thread processor for microphone capture.
 * 
 * Features:
 * 1. Dedicated audio-thread execution (zero UI stalling, eliminates ScriptProcessorNode).
 * 2. High-performance FIR anti-aliasing low-pass filter (cutoff ~6.8 kHz) with Blackman window.
 * 3. Exact streaming fractional resampler for 48kHz, 44.1kHz, 96kHz -> 16kHz.
 * 4. 16-bit signed little-endian PCM format conversion.
 * 5. Fixed-capacity circular pre-roll buffer (~2.5 seconds / 40,000 samples) preserving pre-wake speech.
 * 6. Zero-copy transferable ArrayBuffer delivery to main thread.
 * 7. Integrated real-time Voice Activity Detection (VAD) with adaptive noise-floor estimation.
 */

class ChirkootAudioProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();

    // 1. Configuration & Sample Rates
    const opts = (options && options.processorOptions) || {};
    // Use global sampleRate (from AudioWorkletGlobalScope) or passed option
    this.inputSampleRate = opts.sampleRate || (typeof sampleRate !== 'undefined' ? sampleRate : 48000);
    this.targetSampleRate = opts.targetSampleRate || 16000;
    this.chunkSize = opts.chunkSize || 512; // 512 samples @ 16kHz = 32ms per chunk
    this.preRollSeconds = opts.preRollSeconds || 2.5; // ~2.5s pre-roll

    this.needsResampling = (Math.abs(this.inputSampleRate - this.targetSampleRate) > 1);
    this.resampleRatio = this.inputSampleRate / this.targetSampleRate;
    this.resamplePos = 0.0;

    // 2. Anti-Aliasing FIR Low-Pass Filter Design
    // Cutoff at 6.8 kHz eliminates aliasing folding back into 0-8 kHz band at 16 kHz
    this.numTaps = 51;
    this.firTaps = this.designLowPassFIR(this.numTaps, 6800, this.inputSampleRate);
    this.firHistory = new Float32Array(this.numTaps - 1);
    this.extendedBuffer = new Float32Array((this.numTaps - 1) + 128);

    // 3. Circular Pre-Roll Buffer (Wake Word Mode Pre-Speech Preservation)
    // Capacity = 40,000 samples = 2.5s @ 16kHz = 80 KB
    this.preRollCapacity = Math.round(this.preRollSeconds * this.targetSampleRate);
    this.preRollBuffer = new Int16Array(this.preRollCapacity);
    this.preRollWrite = 0;
    this.preRollCount = 0;

    // 4. Active Streaming Chunks (for Always-On or Woken-Up speech)
    this.outChunk = new Int16Array(this.chunkSize);
    this.outChunkIndex = 0;

    // 5. Operating State
    this.mode = 'always-on'; // 'always-on' or 'wake-word'
    this.isAwake = true;
    this.isMuted = false;

    // 6. Adaptive Voice Activity Detection (VAD)
    this.noiseFloor = 0.015;
    this.isSpeaking = false;
    this.speechFrames = 0;
    this.silenceFrames = 0;
    this.minSpeechFrames = 2; // ~64ms sustained energy to trigger speech
    this.silenceTimeoutFrames = 40; // ~1.28s silence to trigger end-of-speech

    // 7. Message Port Communication from Main Thread
    this.port.onmessage = (event) => {
      const data = event.data;
      if (!data) return;

      switch (data.type) {
        case 'set_mode':
          this.mode = data.mode || this.mode;
          if (typeof data.isAwake === 'boolean') {
            this.isAwake = data.isAwake;
          }
          break;

        case 'flush_pre_roll':
          this.flushPreRoll();
          break;

        case 'set_mute':
          this.isMuted = !!data.isMuted;
          break;

        case 'reset_vad':
          this.isSpeaking = false;
          this.speechFrames = 0;
          this.silenceFrames = 0;
          break;
      }
    };
  }

  /**
   * Generates a linear-phase FIR low-pass filter using a normalized Blackman window.
   */
  designLowPassFIR(numTaps, cutoffHz, fs) {
    const fc = Math.min(0.48, cutoffHz / fs);
    const mid = (numTaps - 1) / 2.0;
    const h = new Float32Array(numTaps);
    let sum = 0.0;

    for (let i = 0; i < numTaps; i++) {
      const m = i - mid;
      const sinc = (m === 0) ? (2.0 * fc) : (Math.sin(2.0 * Math.PI * fc * m) / (Math.PI * m));
      // Blackman window coefficients: a0=0.42, a1=0.5, a2=0.08
      const w = 0.42 - 0.5 * Math.cos((2.0 * Math.PI * i) / (numTaps - 1))
                     + 0.08 * Math.cos((4.0 * Math.PI * i) / (numTaps - 1));
      const val = sinc * w;
      h[i] = val;
      sum += val;
    }

    // Normalize to unit DC gain (0 dB passband)
    if (sum !== 0.0) {
      for (let i = 0; i < numTaps; i++) {
        h[i] /= sum;
      }
    }
    return h;
  }

  /**
   * Flushes accumulated circular pre-roll buffer in chronological order as a transferable buffer.
   */
  flushPreRoll() {
    if (this.preRollCount <= 0) return;

    const count = this.preRollCount;
    const pcm = new Int16Array(count);
    const start = (this.preRollWrite - count + this.preRollCapacity) % this.preRollCapacity;

    for (let i = 0; i < count; i++) {
      pcm[i] = this.preRollBuffer[(start + i) % this.preRollCapacity];
    }

    this.preRollCount = 0; // Clear after flush

    // Transfer underlying ArrayBuffer to main thread with zero copy
    this.port.postMessage({
      type: 'pre_roll_data',
      buffer: pcm.buffer,
      samples: count,
      durationSec: count / this.targetSampleRate
    }, [pcm.buffer]);
  }

  /**
   * Pushes a single 16-bit PCM sample into the circular pre-roll buffer.
   */
  pushPreRollSample(sample) {
    this.preRollBuffer[this.preRollWrite] = sample;
    this.preRollWrite = (this.preRollWrite + 1) % this.preRollCapacity;
    if (this.preRollCount < this.preRollCapacity) {
      this.preRollCount++;
    }
  }

  /**
   * Handles chunk completion, VAD analysis, and transferable buffer posting.
   */
  dispatchChunk() {
    // 1. Calculate RMS energy for VAD
    let sumSq = 0.0;
    for (let i = 0; i < this.chunkSize; i++) {
      const normalized = this.outChunk[i] / 32768.0;
      sumSq += normalized * normalized;
    }
    const rms = Math.sqrt(sumSq / this.chunkSize);

    // 2. Adaptive VAD State Machine
    const speechThreshold = Math.max(0.015, this.noiseFloor * 2.8);
    if (rms > speechThreshold) {
      this.silenceFrames = 0;
      this.speechFrames++;
      if (!this.isSpeaking && this.speechFrames >= this.minSpeechFrames) {
        this.isSpeaking = true;
        this.port.postMessage({
          type: 'vad_state',
          state: 'speech',
          rms: rms
        });
      }
    } else {
      this.speechFrames = 0;
      this.silenceFrames++;
      // Slowly adapt noise floor towards current baseline
      this.noiseFloor = this.noiseFloor * 0.995 + rms * 0.005;
      if (this.isSpeaking && this.silenceFrames >= this.silenceTimeoutFrames) {
        this.isSpeaking = false;
        this.port.postMessage({
          type: 'vad_state',
          state: 'silence',
          rms: rms,
          silenceDurationSec: (this.silenceFrames * this.chunkSize) / this.targetSampleRate
        });
      }
    }

    // 3. Post chunk to main thread as zero-copy transferable buffer
    const bufferToSend = this.outChunk.buffer;
    this.port.postMessage({
      type: 'audio_chunk',
      buffer: bufferToSend,
      rms: rms,
      isSpeaking: this.isSpeaking
    }, [bufferToSend]);

    // 4. Allocate fresh chunk buffer for next cycle
    this.outChunk = new Int16Array(this.chunkSize);
    this.outChunkIndex = 0;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true; // Keep processor alive
    }

    const inputChannel = input[0];
    const inputLength = inputChannel.length; // Standard Web Audio quantum is 128 samples

    // Zero out outputs to prevent mic monitor echo through speakers
    if (outputs[0] && outputs[0][0]) {
      outputs[0][0].fill(0);
    }

    if (this.isMuted) {
      return true;
    }

    if (!this.needsResampling) {
      // Direct 1:1 passthrough (input is already 16 kHz)
      for (let i = 0; i < inputLength; i++) {
        const s = Math.max(-1.0, Math.min(1.0, inputChannel[i]));
        const pcmSample = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);

        if (this.mode === 'wake-word' && !this.isAwake) {
          this.pushPreRollSample(pcmSample);
        }
        this.outChunk[this.outChunkIndex++] = pcmSample;
        if (this.outChunkIndex >= this.chunkSize) {
          this.dispatchChunk();
        }
      }
      return true;
    }

    // Streaming FIR Filter + Resampling to 16 kHz
    // 1. Prepare extended buffer: [history (numTaps - 1), input (inputLength)]
    const historyLen = this.numTaps - 1;
    this.extendedBuffer.set(this.firHistory, 0);
    this.extendedBuffer.set(inputChannel, historyLen);

    // 2. Save last historyLen samples of inputChannel for next block
    this.firHistory.set(inputChannel.subarray(inputLength - historyLen));

    // 3. Convolve extendedBuffer with firTaps to get filtered block
    const filtered = new Float32Array(inputLength);
    for (let i = 0; i < inputLength; i++) {
      let acc = 0.0;
      for (let k = 0; k < this.numTaps; k++) {
        acc += this.firTaps[k] * this.extendedBuffer[i + historyLen - k];
      }
      filtered[i] = acc;
    }

    // 4. Fractional Resampling with Linear Interpolation on Bandlimited Signal
    while (this.resamplePos < inputLength) {
      const i0 = Math.floor(this.resamplePos);
      const i1 = Math.min(i0 + 1, inputLength - 1);
      const frac = this.resamplePos - i0;
      const val = filtered[i0] * (1.0 - frac) + filtered[i1] * frac;

      // Convert to 16-bit signed PCM
      const s = Math.max(-1.0, Math.min(1.0, val));
      const pcmSample = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);

      if (this.mode === 'wake-word' && !this.isAwake) {
        this.pushPreRollSample(pcmSample);
      }
      this.outChunk[this.outChunkIndex++] = pcmSample;
      if (this.outChunkIndex >= this.chunkSize) {
        this.dispatchChunk();
      }

      this.resamplePos += this.resampleRatio;
    }

    // Carry over remaining fractional phase to next audio quantum
    this.resamplePos -= inputLength;

    return true;
  }
}

registerProcessor('chirkoot-audio-processor', ChirkootAudioProcessor);
