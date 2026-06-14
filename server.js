import express from "express";
import multer from "multer";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";
import { loadEnv } from "./lib/env.js";
import { initFfmpeg, toWav16k, toMp3Small, probeDuration } from "./lib/ffmpeg.js";
import { initLocalEngine, localEngineStatus, transcribeLocal } from "./lib/localEngine.js";
import { openaiEngineStatus, transcribeOpenAI } from "./lib/openaiEngine.js";
import { startWhisperServer, whisperServerStatus } from "./lib/whisperServer.js";
import { LiveSession, liveAvailable } from "./lib/liveSession.js";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
loadEnv(ROOT);

const { ffmpegPath } = initFfmpeg(ROOT);
const local = initLocalEngine(ROOT);

const UPLOADS = path.join(ROOT, "uploads");
fs.mkdirSync(UPLOADS, { recursive: true });

const upload = multer({
  dest: UPLOADS,
  limits: { fileSize: 1024 * 1024 * 1024 }, // 1GB, net als het origineel
});

const app = express();
app.use(express.static(path.join(ROOT, "public")));

app.get("/api/status", (_req, res) => {
  res.json({
    engines: { local: localEngineStatus(), openai: openaiEngineStatus() },
    live: { ...liveAvailable(), model: whisperServerStatus().model },
  });
});

app.post("/api/transcribe", upload.single("file"), async (req, res) => {
  const started = Date.now();
  const tempFiles = [];
  try {
    if (!req.file) return res.status(400).json({ error: "Geen bestand ontvangen." });
    tempFiles.push(req.file.path);

    const engine = req.body.engine === "openai" ? "openai" : "local";
    const language = (req.body.language || "auto").toLowerCase();
    const translate = req.body.translate === "true" || req.body.translate === "1";
    const model = req.body.model || null; // alleen voor lokale engine

    const durationSec = await probeDuration(req.file.path);

    let result;
    if (engine === "openai") {
      const mp3 = req.file.path + ".mp3";
      tempFiles.push(mp3);
      await toMp3Small(req.file.path, mp3);
      result = await transcribeOpenAI(mp3, { language, translate });
    } else {
      const wav = req.file.path + ".wav";
      tempFiles.push(wav);
      await toWav16k(req.file.path, wav);
      result = await transcribeLocal(wav, { language, translate, model });
    }

    res.json({
      ...result,
      fileName: req.file.originalname,
      durationSec,
      processingSec: (Date.now() - started) / 1000,
    });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: String(err.message || err) });
  } finally {
    for (const f of tempFiles) fs.rm(f, { force: true }, () => {});
  }
});

const PORT = process.env.PORT || 3000;
const server = app.listen(PORT, () => {
  console.log(`Lazytype draait op http://localhost:${PORT}`);
  console.log(`  ffmpeg:        ${ffmpegPath}`);
  console.log(`  lokale engine: ${local.available ? local.modelPath : "NIET BESCHIKBAAR"}`);
  console.log(`  openai engine: ${openaiEngineStatus().available ? "key aanwezig" : "geen OPENAI_API_KEY"}`);
});

// Live transcriptie: browser stuurt Int16-PCM (16kHz mono) over deze socket.
const wss = new WebSocketServer({ server, path: "/ws/live" });
wss.on("connection", (ws, req) => {
  const params = new URL(req.url, "http://localhost").searchParams;
  new LiveSession(ws, {
    engine: params.get("engine") || "local",
    language: params.get("language") || "auto",
  });
});

// whisper-server houdt het model in het geheugen voor snelle live-passes.
if (local.available) {
  startWhisperServer({ cliPath: local.cliPath, rootDir: ROOT }).then((ok) => {
    console.log(`  live engine:   ${ok ? `whisper-server klaar (${whisperServerStatus().model})` : "whisper-server NIET gestart"}`);
  });
}
