// Streaming microphone resampler. Emits 20 ms frames of 16 kHz mono PCM16 LE.
class PcmCapture extends AudioWorkletProcessor {
  constructor() { super(); this.samples = []; this.position = 0; this.frame = []; this.ratio = sampleRate / 16000; }
  process(inputs) {
    const input = inputs[0]?.[0]; if (!input) return true;
    for (const value of input) this.samples.push(value);
    while (this.position + 1 < this.samples.length) {
      const i = Math.floor(this.position), alpha = this.position - i;
      const value = this.samples[i] * (1 - alpha) + this.samples[i + 1] * alpha;
      this.frame.push(Math.max(-1, Math.min(1, value)));
      this.position += this.ratio;
      if (this.frame.length === 320) {
        const buffer = new ArrayBuffer(640), view = new DataView(buffer);
        this.frame.forEach((v, n) => view.setInt16(n * 2, Math.round(v < 0 ? v * 32768 : v * 32767), true));
        this.port.postMessage(buffer, [buffer]); this.frame = [];
      }
    }
    const consumed = Math.floor(this.position); this.samples.splice(0, consumed); this.position -= consumed;
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
