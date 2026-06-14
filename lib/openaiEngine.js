import fs from "node:fs";
import path from "node:path";

export function openaiEngineStatus() {
  return { available: Boolean(process.env.OPENAI_API_KEY) };
}

// Voor live-modus: transcribeer een WAV-buffer rechtstreeks uit het geheugen.
export async function transcribeWavBufferOpenAI(wavBuffer, { language = "auto", prompt = "" } = {}) {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("OPENAI_API_KEY ontbreekt.");

  const form = new FormData();
  form.append("model", "whisper-1");
  form.append("response_format", "json");
  form.append("file", new Blob([wavBuffer], { type: "audio/wav" }), "live.wav");
  if (prompt) form.append("prompt", prompt);
  if (language && language !== "auto") form.append("language", language);

  const res = await fetch("https://api.openai.com/v1/audio/transcriptions", {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
  });
  if (!res.ok) throw new Error(`OpenAI API-fout (${res.status}): ${(await res.text()).slice(0, 300)}`);
  const data = await res.json();
  return (data.text || "").replace(/\s+/g, " ").trim();
}

// audioFile: pad naar een compacte mp3 (zie toMp3Small) — de API accepteert max 25MB.
export async function transcribeOpenAI(audioFile, { language = "auto", translate = false } = {}) {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("OPENAI_API_KEY ontbreekt — zet hem in .env om de OpenAI-engine te gebruiken.");

  const stat = fs.statSync(audioFile);
  if (stat.size > 25 * 1024 * 1024) {
    throw new Error("Bestand is na compressie nog groter dan 25MB — te lang voor de OpenAI API in deze POC.");
  }

  const endpoint = translate
    ? "https://api.openai.com/v1/audio/translations" // vertaalt naar Engels
    : "https://api.openai.com/v1/audio/transcriptions";

  const form = new FormData();
  form.append("model", "whisper-1");
  form.append("response_format", "verbose_json");
  form.append(
    "file",
    new Blob([fs.readFileSync(audioFile)], { type: "audio/mpeg" }),
    path.basename(audioFile)
  );
  if (!translate && language && language !== "auto") form.append("language", language);

  const res = await fetch(endpoint, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`OpenAI API-fout (${res.status}): ${body.slice(0, 500)}`);
  }

  const data = await res.json();
  const segments = (data.segments || []).map((s) => ({
    start: s.start,
    end: s.end,
    text: (s.text || "").trim(),
  }));

  return {
    engine: "openai",
    model: "whisper-1",
    language: data.language || language,
    text: data.text || segments.map((s) => s.text).join(" "),
    segments,
  };
}
