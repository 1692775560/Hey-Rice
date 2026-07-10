class VoiceClient {
  constructor() {
    this.toggle = document.getElementById('micToggle');
    this.status = document.getElementById('voiceStatus');
    this.transcript = document.getElementById('voiceTranscript');
    this.desired = false;
    this.socket = null;
    this.stream = null;
    this.context = null;
    this.worklet = null;
    this.reconnectTimer = null;
    this.config = null;
    this.toggle.addEventListener('click', () => this.desired ? this.stop() : this.start());
  }

  async start() {
    this.toggle.disabled = true;
    this.setStatus('正在请求麦克风权限', 'connecting');
    try {
      const response = await fetch('/api/voice-config');
      this.config = await response.json();
      if (!this.config.enabled) throw new Error('语音服务已关闭');
      if (!this.config.ready) {
        throw new Error(`语音服务未配置：${this.config.missing.join(', ')}`);
      }

      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
      this.context = new AudioContext({latencyHint: 'interactive'});
      await this.context.audioWorklet.addModule('/static/pcm-worklet.js');
      const source = this.context.createMediaStreamSource(this.stream);
      this.worklet = new AudioWorkletNode(this.context, 'hey-rice-pcm');
      const mute = this.context.createGain();
      mute.gain.value = 0;
      source.connect(this.worklet).connect(mute).connect(this.context.destination);
      this.worklet.port.onmessage = event => {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
          this.socket.send(event.data);
        }
      };
      this.desired = true;
      this.toggle.setAttribute('aria-pressed', 'true');
      this.toggle.classList.add('active');
      await this.connect();
    } catch (error) {
      await this.stop(false);
      this.setStatus(error.message || '无法开启麦克风', 'error');
    } finally {
      this.toggle.disabled = false;
    }
  }

  async connect() {
    if (!this.desired) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${scheme}://${location.hostname}:${this.config.port}/ws/voice`;
    this.setStatus('正在连接语音服务', 'connecting');

    await new Promise((resolve, reject) => {
      const socket = new WebSocket(url);
      socket.binaryType = 'arraybuffer';
      this.socket = socket;
      const timeout = setTimeout(() => reject(new Error('语音服务连接超时')), 5000);
      socket.onopen = () => {
        clearTimeout(timeout);
        socket.send(JSON.stringify({type: 'start', sampleRate: 16000}));
        resolve();
      };
      socket.onerror = () => {
        clearTimeout(timeout);
        reject(new Error('无法连接语音服务'));
      };
      socket.onmessage = event => this.onMessage(event.data);
      socket.onclose = () => {
        if (!this.desired) return;
        this.setStatus('语音连接中断，正在重连', 'error');
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => this.connect().catch(() => {}), 1000);
      };
    });
  }

  onMessage(raw) {
    let event;
    try {
      event = JSON.parse(raw);
    } catch (_) {
      return;
    }
    if (event.type === 'status') {
      this.setStatus(event.message, event.state);
    } else if (event.type === 'wake_detected') {
      this.setStatus('已唤醒，我在听', 'listening');
    } else if (event.type === 'final_transcript') {
      this.transcript.textContent = event.text;
      window.dispatchEvent(new CustomEvent('hey-rice-final-transcript', {detail: event}));
    } else if (event.type === 'agent_result') {
      window.dispatchEvent(new CustomEvent('hey-rice-agent-result', {detail: event}));
    } else if (event.type === 'error') {
      this.setStatus(event.message, 'error');
      if (['voice_not_configured', 'wakeword_not_ready'].includes(event.code)) {
        this.stop(false).then(() => this.setStatus(event.message, 'error'));
      }
    }
  }

  async stop(updateStatus = true) {
    this.desired = false;
    clearTimeout(this.reconnectTimer);
    if (this.socket) {
      if (this.socket.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({type: 'stop'}));
      }
      this.socket.close();
      this.socket = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach(track => track.stop());
      this.stream = null;
    }
    if (this.context) {
      await this.context.close();
      this.context = null;
    }
    this.worklet = null;
    this.toggle.setAttribute('aria-pressed', 'false');
    this.toggle.classList.remove('active');
    if (updateStatus) this.setStatus('麦克风已关闭', 'idle');
  }

  setStatus(message, state) {
    this.status.textContent = message;
    this.status.dataset.state = state;
  }
}

window.addEventListener('DOMContentLoaded', () => new VoiceClient());
