import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

let cliPath = null;
let modelPath = null;

function findWhisperCli(rootDir) {
  const toolsDir = path.join(rootDir, "tools");
  if (!fs.existsSync(toolsDir)) return null;
  const stack = [toolsDir];
  const found = {}; // main.exe is een deprecated stub — alleen als laatste redmiddel
  while (stack.length) {
    const dir = stack.pop();
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else found[entry.name.toLowerCase()] ??= full;
    }
  }
  return found["whisper-cli.exe"] || found["main.exe"] || null;
}

let modelsDir = null;

export function listModels() {
  if (!modelsDir || !fs.existsSync(modelsDir)) return [];
  return fs.readdirSync(modelsDir)
    .filter((f) => f.endsWith(".bin"))
    .map((f) => ({ file: f, sizeMB: Math.round(fs.statSync(path.join(modelsDir, f)).size / 1e6) }))
    .sort((a, b) => a.sizeMB - b.sizeMB);
}

function findModel(rootDir) {
  const preferred = process.env.WHISPER_MODEL; // bijv. ggml-small.bin
  const bins = listModels().map((m) => m.file);
  if (preferred && bins.includes(preferred)) return path.join(modelsDir, preferred);
  // grootste model = beste kwaliteit van wat er ligt
  return bins.length ? path.join(modelsDir, bins[bins.length - 1]) : null;
}

export function initLocalEngine(rootDir) {
  cliPath = findWhisperCli(rootDir);
  modelsDir = path.join(rootDir, "models");
  modelPath = findModel(rootDir);
  return { available: Boolean(cliPath && modelPath), cliPath, modelPath };
}

export function localEngineStatus() {
  return {
    available: Boolean(cliPath && modelPath),
    model: modelPath ? path.basename(modelPath) : null,
    models: listModels(),
  };
}

// wavFile moet al 16kHz mono WAV zijn.
export async function transcribeLocal(wavFile, { language = "auto", translate = false, model = null } = {}) {
  if (!cliPath || !modelPath) throw new Error("Lokale engine niet beschikbaar (whisper-cli of model ontbreekt).");

  // optioneel model per aanvraag, mits het echt in models/ ligt
  let useModel = modelPath;
  if (model && listModels().some((m) => m.file === model)) {
    useModel = path.join(modelsDir, model);
  }

  const outBase = path.join(path.dirname(wavFile), path.basename(wavFile, path.extname(wavFile)) + "-out");
  const threads = Math.max(2, os.cpus().length - 1);
  const args = [
    "-m", useModel,
    "-f", wavFile,
    "-l", language || "auto",
    "-t", String(threads),
    "-oj",           // schrijf <outBase>.json
    "-of", outBase,
    "-np",           // geen prints behalve resultaat
  ];
  if (translate) args.push("-tr"); // Whisper vertaalt alleen richting Engels

  await new Promise((resolve, reject) => {
    const proc = spawn(cliPath, args, { windowsHide: true });
    let output = "";
    proc.stdout.on("data", (d) => (output += d));
    proc.stderr.on("data", (d) => (output += d));
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`whisper-cli faalde (code ${code}, args: ${JSON.stringify(args)}): ${output.slice(-800)}`));
    });
  });

  const jsonPath = outBase + ".json";
  const raw = JSON.parse(fs.readFileSync(jsonPath, "utf8"));
  fs.rmSync(jsonPath, { force: true });

  const segments = (raw.transcription || [])
    .map((t) => ({
      start: (t.offsets?.from ?? 0) / 1000,
      end: (t.offsets?.to ?? 0) / 1000,
      text: (t.text || "").trim(),
    }))
    .filter((s) => s.text.length > 0);

  return {
    engine: "local",
    model: path.basename(useModel),
    language: raw.result?.language || language,
    text: segments.map((s) => s.text).join(" "),
    segments,
  };
}
