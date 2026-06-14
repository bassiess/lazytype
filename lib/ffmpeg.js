import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

// Zoekt ffmpeg/ffprobe: eerst in tools/, anders op PATH.
function findTool(rootDir, exeName) {
  const toolsDir = path.join(rootDir, "tools");
  if (fs.existsSync(toolsDir)) {
    const stack = [toolsDir];
    while (stack.length) {
      const dir = stack.pop();
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) stack.push(full);
        else if (entry.name.toLowerCase() === exeName) return full;
      }
    }
  }
  return exeName; // hoop op PATH
}

let ffmpegPath = "ffmpeg.exe";
let ffprobePath = "ffprobe.exe";

export function initFfmpeg(rootDir) {
  ffmpegPath = findTool(rootDir, "ffmpeg.exe");
  ffprobePath = findTool(rootDir, "ffprobe.exe");
  return { ffmpegPath, ffprobePath };
}

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { windowsHide: true });
    let stderr = "";
    let stdout = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${path.basename(cmd)} faalde (code ${code}): ${stderr.slice(-800)}`));
    });
  });
}

// Whisper.cpp wil 16kHz mono 16-bit WAV.
export async function toWav16k(inputFile, outputFile) {
  await run(ffmpegPath, ["-y", "-i", inputFile, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", outputFile]);
  return outputFile;
}

// Compacte mp3 voor de OpenAI API (25MB-limiet daar).
export async function toMp3Small(inputFile, outputFile) {
  await run(ffmpegPath, ["-y", "-i", inputFile, "-ar", "16000", "-ac", "1", "-b:a", "64k", outputFile]);
  return outputFile;
}

export async function probeDuration(inputFile) {
  try {
    const { stdout } = await run(ffprobePath, [
      "-v", "error",
      "-show_entries", "format=duration",
      "-of", "default=noprint_wrappers=1:nokey=1",
      inputFile,
    ]);
    return parseFloat(stdout.trim()) || null;
  } catch {
    return null;
  }
}
