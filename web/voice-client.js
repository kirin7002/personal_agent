/** Shared audio transport. Call start() only from an explicit user gesture.
 * onEvent(event): every server event; onState(status, detail): lifecycle;
 * onError(Error): transport/start errors. Rendering and takeover belong to callers.
 */
export class VoiceSession {
  constructor({ onEvent = () => {}, onState = () => {}, onError = () => {} } = {}) {
    Object.assign(this, { onEvent, onState, onError, connected: false, closed: false, sources: new Set(), nextAudio: 0, playing: false });
  }
  send(data) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(data)); }
  async start() {
    if (this.started || this.closed) return;
    this.started = true; this.onState('connecting');
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('浏览器不支持麦克风，请使用 localhost 或 HTTPS');
      this.audio = new AudioContext(); await this.audio.resume();
      if (this.closed) return;
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
      if (this.closed) { this.stream.getTracks().forEach(track => track.stop()); return; }
      await this.audio.audioWorklet.addModule('/static/pcm-worklet.js');
      if (this.closed) return;
      this.capture = new AudioWorkletNode(this.audio, 'pcm-capture');
      this.mic = this.audio.createMediaStreamSource(this.stream);
      this.mic.connect(this.capture); this.mute = this.audio.createGain(); this.mute.gain.value = 0; this.capture.connect(this.mute).connect(this.audio.destination);
      this.capture.port.onmessage = event => {
        if (!this.connected || this.closed) return;
        const bytes = new Uint8Array(event.data); let str = ''; for (const byte of bytes) str += String.fromCharCode(byte);
        this.send({ type: 'audio', audio: btoa(str) });
      };
      this.socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/voice`);
      this.socket.onmessage = event => { try { this.receive(JSON.parse(event.data)); } catch (error) { this.onError(error); } };
      this.socket.onclose = () => { if (!this.closed) this.stop('stopped', { reason: 'disconnected' }); };
      this.socket.onerror = () => { this.onError(new Error('语音连接失败，请检查服务配置')); this.stop(); };
    } catch (error) { this.stop(); throw error; }
  }
  receive(event) {
    if (this.closed) return;
    if (event.type === 'voice.ready') { this.connected = true; this.onState('connected', event); }
    else if (event.type === 'voice.busy' || event.type === 'voice.released') this.stop(event.type.split('.')[1], event);
    else if (event.type === 'audio' && this.connected) this.play(event.audio);
    else if (event.type === 'voice.invalidate' || event.type === 'speech.started') this.stopPlayback();
    else if (event.type === 'error' && event.code === 'Model.AccessDenied') this.stop('stopped', event);
    this.onEvent(event);
  }
  play(encoded) {
    if (this.closed || !this.connected || !this.audio) return;
    const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
    if (bytes.length < 2) return;
    const view = new DataView(bytes.buffer), buffer = this.audio.createBuffer(1, Math.floor(bytes.length / 2), 24000), channel = buffer.getChannelData(0);
    for (let i = 0; i < channel.length; i++) channel[i] = view.getInt16(i * 2, true) / 32768;
    const source = this.audio.createBufferSource(); source.buffer = buffer; source.connect(this.audio.destination);
    const when = Math.max(this.audio.currentTime + 0.03, this.nextAudio); this.nextAudio = when + buffer.duration;
    this.sources.add(source);
    if (!this.playing) { this.playing = true; this.send({ type: 'playback', busy: true }); }
    source.onended = () => { this.sources.delete(source); if (!this.sources.size && this.playing) { this.playing = false; if (this.connected) this.send({ type: 'playback', busy: false }); } };
    source.start(when);
  }
  stopPlayback() {
    for (const source of this.sources) { source.onended = null; try { source.stop(); } catch {} }
    this.sources.clear(); this.nextAudio = 0;
    if (this.playing && this.connected) this.send({ type: 'playback', busy: false });
    this.playing = false;
  }
  stop(status = 'stopped', detail = {}) {
    if (this.closed) return;
    this.closed = true; this.connected = false; this.stopPlayback(); this.socket?.close();
    this.stream?.getTracks().forEach(track => track.stop()); this.mic?.disconnect(); this.capture?.disconnect(); this.mute?.disconnect();
    if (this.audio && this.audio.state !== 'closed') this.audio.close().catch(() => {});
    this.onState(status, detail);
  }
}
