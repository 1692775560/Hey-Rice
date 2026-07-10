class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.outputRate = 16000;
    this.frame = new Int16Array(1600);
    this.frameOffset = 0;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;

    for (let index = 0; index < input.length; index += 1) {
      this.sum += input[index];
      this.count += 1;
      this.phase += this.outputRate;
      if (this.phase < sampleRate) continue;

      this.phase -= sampleRate;
      const averaged = this.sum / this.count;
      const clipped = Math.max(-1, Math.min(1, averaged));
      this.frame[this.frameOffset] = clipped < 0 ? clipped * 32768 : clipped * 32767;
      this.frameOffset += 1;
      this.sum = 0;
      this.count = 0;

      if (this.frameOffset === this.frame.length) {
        const packet = this.frame.buffer;
        this.port.postMessage(packet, [packet]);
        this.frame = new Int16Array(1600);
        this.frameOffset = 0;
      }
    }
    return true;
  }
}

registerProcessor('hey-rice-pcm', PcmProcessor);
