// Geeft ruwe Float32-microfoonsamples door aan de main thread.
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      // kopiëren: het onderliggende buffer wordt door de audio-engine hergebruikt
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
