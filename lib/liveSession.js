import { pcm16ToWav } from "./wav.js";
import { transcribeWavBufferLocal, whisperServerStatus } from "./whisperServer.js";
import { transcribeWavBufferOpenAI, openaiEngineStatus } from "./openaiEngine.js";

const SAMPLE_RATE = 16000;
const MIN_PASS_SEC = 1.2;       // whisper heeft minimaal ~1s nodig
const NEW_AUDIO_SEC = 2.0;      // her-transcribeer zodra er zoveel nieuwe audio is
const MAX_SEGMENT_SEC = 18;     // hard afkappen, anders worden passes te traag
const SILENCE_RMS = 350;        // int16-RMS waaronder we het stil noemen
const SILENCE_SEC = 0.9;        // zo lang stil = einde van een zin/segment
const MIN_COMMIT_SEC = 3.0;     // niet eerder committen dan dit

// Whisper hallucineert tokens als [BLANK_AUDIO] of (muziek) op stilte — wegfilteren.
function cleanText(text) {
  return text
    .replace(/\[[^\]]*\]/g, "")
    .replace(/\([^)]*\)/g, "")
    .replace(/♪/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function liveAvailable() {
  return {
    local: whisperServerStatus().ready,
    openai: openaiEngineStatus().available,
  };
}

// Eén WebSocket-verbinding = één LiveSession.
export class LiveSession {
  constructor(ws, { engine = "local", language = "auto" } = {}) {
    this.ws = ws;
    this.engine = engine === "openai" ? "openai" : "local";
    this.language = language;
    this.chunks = [];          // Int16-PCM Buffers van het huidige segment
    this.samples = 0;          // totaal samples in chunks
    this.samplesAtLastPass = 0;
    this.busy = false;
    this.stopped = false;
    this.silentSamples = 0;    // aaneengesloten stille samples aan het eind
    this.hadSpeech = false;
    this.committed = [];       // afgeronde segmentteksten
    this.timer = setInterval(() => this.tick(), 400);

    ws.on("message", (data, isBinary) => {
      if (isBinary) this.onAudio(data);
      else this.onControl(data.toString());
    });
    ws.on("close", () => this.destroy());
  }

  send(obj) {
    if (this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  onAudio(buf) {
    if (this.stopped) return;
    this.chunks.push(buf);
    this.samples += buf.length / 2;

    // RMS van deze chunk voor stiltedetectie
    let sum = 0;
    for (let i = 0; i < buf.length - 1; i += 2) {
      const v = buf.readInt16LE(i);
      sum += v * v;
    }
    const rms = Math.sqrt(sum / (buf.length / 2));
    if (rms < SILENCE_RMS) {
      this.silentSamples += buf.length / 2;
    } else {
      this.silentSamples = 0;
      this.hadSpeech = true;
    }
  }

  onControl(raw) {
    try {
      const msg = JSON.parse(raw);
      if (msg.type === "stop") {
        this.stopped = true;
        this.tick(); // direct laatste pass triggeren
      }
    } catch { /* genegeerd */ }
  }

  get segmentSec() { return this.samples / SAMPLE_RATE; }
  get newAudioSec() { return (this.samples - this.samplesAtLastPass) / SAMPLE_RATE; }

  wantsFinalize() {
    if (this.stopped) return true;
    if (this.segmentSec >= MAX_SEGMENT_SEC) return true;
    const silentSec = this.silentSamples / SAMPLE_RATE;
    return this.hadSpeech && this.segmentSec >= MIN_COMMIT_SEC && silentSec >= SILENCE_SEC;
  }

  async tick() {
    if (this.busy) return;
    const finalize = this.wantsFinalize();
    if (!finalize && this.newAudioSec < NEW_AUDIO_SEC) return;
    if (this.segmentSec < MIN_PASS_SEC) {
      if (this.stopped) this.finish();
      return;
    }

    this.busy = true;
    const snapshotChunks = this.chunks.slice();
    const snapshotSamples = this.samples;
    try {
      const pcm = Buffer.concat(snapshotChunks);
      const wav = pcm16ToWav(pcm, SAMPLE_RATE);
      const transcribe = this.engine === "openai" ? transcribeWavBufferOpenAI : transcribeWavBufferLocal;
      // context van eerdere zinnen verbetert spelling/continuïteit aanzienlijk
      const prompt = this.committed.join(" ").slice(-200);
      const text = cleanText(await transcribe(wav, { language: this.language, prompt }));
      this.samplesAtLastPass = snapshotSamples;

      if (finalize) {
        if (text) this.committed.push(text);
        // verwijder de gesnapshotte samples; audio die tijdens de pass binnenkwam blijft staan
        let toDrop = snapshotSamples;
        while (toDrop > 0 && this.chunks.length) {
          const c = this.chunks[0];
          const cSamples = c.length / 2;
          if (cSamples <= toDrop) { this.chunks.shift(); toDrop -= cSamples; }
          else { this.chunks[0] = c.subarray(toDrop * 2); toDrop = 0; }
        }
        this.samples -= snapshotSamples - toDrop;
        this.samplesAtLastPass = 0;
        this.hadSpeech = false;
        this.silentSamples = 0;
        this.send({ type: "committed", text });
        // bij stop pas afronden als de hele buffer verwerkt is — de volgende
        // tick pakt het restant op dat tijdens deze pass binnenkwam
        if (this.stopped && this.segmentSec < MIN_PASS_SEC) this.finish();
      } else {
        this.send({ type: "partial", text });
      }
    } catch (err) {
      this.send({ type: "error", message: String(err.message || err) });
      if (this.stopped) this.finish();
    } finally {
      this.busy = false;
    }
  }

  finish() {
    this.send({ type: "done", fullText: this.committed.join(" ") });
    this.destroy();
    try { this.ws.close(); } catch { /* al dicht */ }
  }

  destroy() {
    clearInterval(this.timer);
  }
}
