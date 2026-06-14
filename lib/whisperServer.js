import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Beheert whisper-server.exe als kindproces: het model blijft in het geheugen,
// zodat live-chunks zonder laadtijd getranscribeerd kunnen worden.

const PORT = Number(process.env.WHISPER_SERVER_PORT || 8178);
let child = null;
let ready = false;
let liveModel = null;

export function whisperServerStatus() {
  return { running: Boolean(child), ready, port: PORT, model: liveModel };
}

// Live heeft snelheid nodig: voorkeur WHISPER_MODEL_LIVE, anders small, anders base,
// anders wat er ligt (kleinste eerst).
function pickLiveModel(rootDir) {
  const modelsDir = path.join(rootDir, "models");
  if (!fs.existsSync(modelsDir)) return null;
  const bins = fs.readdirSync(modelsDir).filter((f) => f.endsWith(".bin"));
  if (!bins.length) return null;
  const pref = process.env.WHISPER_MODEL_LIVE;
  if (pref && bins.includes(pref)) return path.join(modelsDir, pref);
  for (const want of ["ggml-small.bin", "ggml-base.bin"]) {
    if (bins.includes(want)) return path.join(modelsDir, want);
  }
  bins.sort((a, b) => fs.statSync(path.join(modelsDir, a)).size - fs.statSync(path.join(modelsDir, b)).size);
  return path.join(modelsDir, bins[0]);
}

export async function startWhisperServer({ cliPath, rootDir }) {
  if (!cliPath) return false;
  const modelPath = pickLiveModel(rootDir);
  if (!modelPath) return false;
  liveModel = path.basename(modelPath);
  const serverExe = path.join(path.dirname(cliPath), "whisper-server.exe");
  const threads = Math.max(2, os.cpus().length - 1);

  child = spawn(serverExe, [
    "-m", modelPath,
    "-t", String(threads),
    "--host", "127.0.0.1",
    "--port", String(PORT),
  ], { windowsHide: true });

  child.on("exit", (code) => {
    console.warn(`whisper-server gestopt (code ${code})`);
    child = null;
    ready = false;
  });
  child.on("error", (err) => {
    console.error("whisper-server kon niet starten:", err.message);
    child = null;
  });
  process.on("exit", () => { try { child?.kill(); } catch {} });

  // wachten tot hij reageert (model laden duurt even)
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      await fetch(`http://127.0.0.1:${PORT}/`, { signal: AbortSignal.timeout(1000) });
      ready = true;
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 400));
    }
  }
  console.warn("whisper-server reageert niet binnen 20s");
  return false;
}

// wavBuffer: complete WAV-file als Buffer (16kHz mono PCM16).
export async function transcribeWavBufferLocal(wavBuffer, { language = "auto", prompt = "" } = {}) {
  if (!ready) throw new Error("Lokale live-engine (whisper-server) is niet beschikbaar.");
  const form = new FormData();
  form.append("file", new Blob([wavBuffer], { type: "audio/wav" }), "live.wav");
  form.append("response_format", "json");
  form.append("language", language || "auto");
  if (prompt) form.append("prompt", prompt);

  const res = await fetch(`http://127.0.0.1:${PORT}/inference`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`whisper-server fout (${res.status}): ${(await res.text()).slice(0, 300)}`);
  const data = await res.json();
  return (data.text || "").replace(/\s+/g, " ").trim();
}
