// 按键对讲(push-to-talk):按住麦克风按钮说话,松开发送。无需唤醒词。
class VoiceClient {
  constructor() {
    this.toggle = document.getElementById('micToggle');
    this.status = document.getElementById('voiceStatus');
    this.transcript = document.getElementById('voiceTranscript');
    this.socket = null;
    this.stream = null;
    this.context = null;
    this.worklet = null;
    this.config = null;
    this.armed = false;      // 已获权限+连接,随时可按住说话
    this.arming = false;     // 正在初始化麦克风
    this.recording = false;  // 正在按住录音
    this.pressing = false;   // 指针当前按下

    // 按住说话:按下开录、松开/移开/取消都视为松手。
    this.toggle.addEventListener('pointerdown', e => { e.preventDefault(); this.onPressStart(); });
    this.toggle.addEventListener('pointerup', () => this.onPressEnd());
    this.toggle.addEventListener('pointercancel', () => this.onPressEnd());
    this.toggle.addEventListener('pointerleave', () => { if (this.pressing) this.onPressEnd(); });
    // 长按可能弹出的系统上下文菜单,禁掉。
    this.toggle.addEventListener('contextmenu', e => e.preventDefault());
  }

  async onPressStart() {
    this.pressing = true;
    if (!this.armed) {
      await this.arm();
    }
    // 初始化过程中若已松手,则只保持就绪,不录音。
    if (this.armed && this.pressing) {
      this.beginRecording();
    }
  }

  onPressEnd() {
    this.pressing = false;
    if (this.recording) {
      this.endRecording();
    }
  }

  // 首次按下:请求权限 + 建立连接 + 音频管线。
  async arm() {
    if (this.arming || this.armed) return;
    this.arming = true;
    this.toggle.disabled = true;
    this.setStatus('正在准备麦克风…', 'connecting');
    try {
      // 先在用户手势内请求麦克风权限(getUserMedia 需要用户手势,故放在最前)。
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });

      const response = await fetch('/api/voice-config');
      this.config = await response.json();
      if (!this.config.enabled) throw new Error('语音服务已关闭');
      if (!this.config.ready) throw new Error(`语音服务未配置：${this.config.missing.join(', ')}`);

      this.context = new AudioContext({latencyHint: 'interactive'});
      await this.context.audioWorklet.addModule('/static/pcm-worklet.js');
      const source = this.context.createMediaStreamSource(this.stream);
      this.worklet = new AudioWorkletNode(this.context, 'hey-rice-pcm');
      const mute = this.context.createGain();
      mute.gain.value = 0;
      source.connect(this.worklet).connect(mute).connect(this.context.destination);
      // 只在按住录音时把音频发给网关。
      this.worklet.port.onmessage = event => {
        if (this.recording && this.socket && this.socket.readyState === WebSocket.OPEN) {
          this.socket.send(event.data);
        }
      };
      await this.connect();
      this.armed = true;
      this.setStatus('麦克风就绪，按住说话', 'idle');
    } catch (error) {
      await this.disarm(false);
      this.setStatus(this._friendlyError(error), 'error');
    } finally {
      this.arming = false;
      this.toggle.disabled = false;
    }
  }

  _friendlyError(error) {
    const name = error && error.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return '麦克风被拦住了：请在地址栏权限图标里把「麦克风」设为允许,再按一次';
    }
    if (name === 'NotFoundError') {
      return '没找到麦克风设备,请检查是否插好/被占用';
    }
    if (location.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(location.hostname)) {
      return '当前不是安全地址,麦克风需用 https 或 127.0.0.1 打开';
    }
    return (error && error.message) || '无法开启麦克风';
  }

  async connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${scheme}://${location.hostname}:${this.config.port}/ws/voice`;
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
      socket.onerror = () => { clearTimeout(timeout); reject(new Error('无法连接语音服务')); };
      socket.onmessage = event => this.onMessage(event.data);
      socket.onclose = () => {
        if (!this.armed) return;
        this.armed = false;
        this.recording = false;
        this.toggle.classList.remove('active');
        this.setStatus('语音连接已断开，请再按一次', 'error');
      };
    });
  }

  beginRecording() {
    if (!this.armed || this.recording) return;
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this.recording = true;
    this.socket.send(JSON.stringify({type: 'speak_start'}));
    this.toggle.setAttribute('aria-pressed', 'true');
    this.toggle.classList.add('active');
    this.setStatus('正在录音，请说…', 'listening');
    this.transcript.textContent = '……';
  }

  endRecording() {
    if (!this.recording) return;
    this.recording = false;
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({type: 'speak_end'}));
    }
    this.toggle.setAttribute('aria-pressed', 'false');
    this.toggle.classList.remove('active');
    this.setStatus('正在识别…', 'connecting');
  }

  onMessage(raw) {
    let event;
    try { event = JSON.parse(raw); } catch (_) { return; }
    if (event.type === 'status') {
      this.setStatus(event.message, event.state);
    } else if (event.type === 'final_transcript') {
      this.transcript.textContent = event.text;
      window.dispatchEvent(new CustomEvent('hey-rice-final-transcript', {detail: event}));
    } else if (event.type === 'agent_result') {
      window.dispatchEvent(new CustomEvent('hey-rice-agent-result', {detail: event}));
    } else if (event.type === 'error') {
      this.setStatus(event.message, 'error');
      if (event.code === 'voice_not_configured') {
        this.disarm(false).then(() => this.setStatus(event.message, 'error'));
      }
    }
  }

  async disarm(updateStatus = true) {
    this.armed = false;
    this.recording = false;
    this.pressing = false;
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
