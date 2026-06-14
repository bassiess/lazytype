// Pipeline-test voor live-modus: streamt test-audio.wav als nep-microfoon
// in realtime-chunks over de WebSocket en print wat de server terugstuurt.
import fs from "node:fs";
import WebSocket from "ws";

const file = process.argv[2] || "C:\\Users\\bniese\\lazy typing\\test-audio.wav";
const lang = process.argv[3] || "auto";
const wav = fs.readFileSync(file);
const pcm = wav.subarray(44); // header eraf, rest is 16kHz mono PCM16
const t0 = Date.now();
const stamp = () => `[${((Date.now() - t0) / 1000).toFixed(1)}s]`;

const ws = new WebSocket(`ws://localhost:3000/ws/live?engine=local&language=${lang}`);
const CHUNK = 8000; // 4000 samples = 250ms
let offset = 0;

ws.on("open", () => {
  console.log("verbonden — start streamen");
  const iv = setInterval(() => {
    if (offset >= pcm.length) {
      clearInterval(iv);
      // stuur 1,5s stilte zodat stiltedetectie het segment kan afronden
      const silence = Buffer.alloc(16000 * 3);
      ws.send(silence.subarray(0, silence.length / 2));
      setTimeout(() => {
        ws.send(silence.subarray(silence.length / 2));
        setTimeout(() => ws.send(JSON.stringify({ type: "stop" })), 600);
      }, 750);
      return;
    }
    ws.send(pcm.subarray(offset, offset + CHUNK));
    offset += CHUNK;
  }, 250);
});

ws.on("message", (d) => console.log(stamp(), "<<", d.toString()));
ws.on("close", () => { console.log(stamp(), "verbinding gesloten"); process.exit(0); });
ws.on("error", (e) => { console.error("fout:", e.message); process.exit(1); });

setTimeout(() => { console.error("TIMEOUT"); process.exit(1); }, 180000);
