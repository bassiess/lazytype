<?php
/**
 * Lazytype — managed transcriptie-proxy (abonnement).
 *
 * Controleert een ondertekende licentiesleutel + misbruik-plafonds, en doet dan
 * server-side de transcriptie (+ optioneel vertalen/opschonen) via Groq, zodat de
 * Groq-key NOOIT in de client zit.
 *
 * Deploy:  public_html/api/transcribe.php  (+ license_check.php + config.php)
 * Config (env of api/config.php):
 *     LAZYTYPE_LICENSE_SECRET   — zelfde geheim als admin .env
 *     GROQ_API_KEY              — jouw Groq-key (server-side)
 * Na deploy testen:  https://lazytype.com/api/transcribe.php?selftest=1
 */

require_once __DIR__ . '/license_check.php';

// ── Zelftest-endpoint (geen curl/Groq nodig) ────────────────────────────
if (($_GET['selftest'] ?? '') === '1') {
    header('Content-Type: application/json');
    echo json_encode(lzt_selftest());
    exit;
}

header('Content-Type: application/json; charset=utf-8');
function fail($code, $msg) { http_response_code($code); echo json_encode(['error' => $msg]); exit; }

// ── Config ──────────────────────────────────────────────────────────────
$SECRET = getenv('LAZYTYPE_LICENSE_SECRET') ?: '';
$GROQ   = getenv('GROQ_API_KEY') ?: '';
if (file_exists(__DIR__ . '/config.php')) require __DIR__ . '/config.php';
if ($SECRET === '' || $GROQ === '') fail(500, 'server niet geconfigureerd');
if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail(405, 'alleen POST');

// ── Licentie verifiëren ─────────────────────────────────────────────────
[$ok, $reason, $payload] = lzt_verify_license($_POST['license'] ?? '', $SECRET);
if (!$ok) fail(402, "abonnement ongeldig: $reason");
// Ingetrokken sleutel (bv. abonnement opgezegd → webhook schreef revoked.json) → weiger
$revF = __DIR__ . '/revoked.json';
$revoked = file_exists($revF) ? (json_decode(@file_get_contents($revF), true) ?: []) : [];
if (in_array($payload['id'] ?? '', $revoked, true)) fail(402, 'abonnement ingetrokken');
$tier = $payload['tier'] ?? '';
$caps = lzt_caps();
if (!isset($caps[$tier])) fail(403, 'tier zonder managed toegang');

// ── Audio + misbruik-plafonds ───────────────────────────────────────────
if (!isset($_FILES['file']) || $_FILES['file']['error'] !== UPLOAD_ERR_OK) fail(400, 'geen audio ontvangen');
$bytes = (int)$_FILES['file']['size'];
if ($bytes > MAX_AUDIO_BYTES) fail(413, 'audio te lang (max ~2 min per dictaat)');
$secs = (int)ceil($bytes / 32000);   // schatting @ 16kHz mono int16

$device = preg_replace('/[^A-Za-z0-9_\-]/', '', $_POST['device'] ?? '');
$statePath = lzt_state_path(__DIR__ . '/state', $payload['id'] ?? 'unknown');
$state = lzt_load_state($statePath);

[$devOk, $state] = lzt_device_check($state, $device, $caps[$tier]['devices']);
if (!$devOk) fail(403, 'te veel apparaten op deze sleutel — neem contact op');

[$quotaOk, $state] = lzt_quota_check($state, $secs, $caps[$tier]['daily_sec'], date('Y-m-d'));
if (!$quotaOk) fail(429, 'daglimiet voor deze sleutel bereikt — probeer het morgen weer');

lzt_save_state($statePath, $state);  // reserveer verbruik (telt de poging, voorkomt hammering)

$_allowed_langs = ['auto','nl','en','de','fr','es','it','pt','zh','ja','ko','pl','ru','tr','ar'];
$language = in_array($_POST['language'] ?? '', $_allowed_langs, true) ? $_POST['language'] : 'auto';
$postproc = strtolower($_POST['postprocess'] ?? 'off');
$prompt   = substr($_POST['prompt'] ?? '', 0, 600);   // eigen woordenboek (Whisper-bias)
$command  = $_POST['command'] ?? '';                  // command mode: selectie om te bewerken

// ── 1) Transcriberen via Groq ───────────────────────────────────────────
function groq_transcribe($path, $language, $groq, $prompt = '', $fname = 'audio.wav', $mime = 'audio/wav') {
    $post = [
        'file'            => new CURLFile($path, $mime, $fname),
        'model'           => 'whisper-large-v3-turbo',
        'response_format' => 'text',
        'temperature'     => '0',
    ];
    if ($language && $language !== 'auto') $post['language'] = $language;
    if ($prompt !== '') $post['prompt'] = $prompt;
    $ch = curl_init('https://api.groq.com/openai/v1/audio/transcriptions');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => ["Authorization: Bearer $groq"],
        CURLOPT_POSTFIELDS => $post, CURLOPT_TIMEOUT => 60,
    ]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return [$code, $body];
}

// Bestandsformaat doorgeven aan Groq (desktop=wav, iPhone=mp4, Android=webm)
$allowedExt = ['wav', 'webm', 'mp4', 'm4a', 'mp3', 'mpeg', 'mpga', 'ogg', 'oga', 'flac'];
$ext = strtolower(pathinfo($_FILES['file']['name'] ?? 'audio.wav', PATHINFO_EXTENSION));
if (!in_array($ext, $allowedExt, true)) $ext = 'wav';
$mime = $_FILES['file']['type'] ?: 'application/octet-stream';
[$code, $text] = groq_transcribe($_FILES['file']['tmp_name'], $language, $GROQ, $prompt, "audio.$ext", $mime);
if ($code !== 200) fail(502, 'transcriptie mislukt');
$text = trim($text);

// ── Command mode: pas de gesproken instructie toe op de meegestuurde selectie ──
if ($command !== '') {
    $result = groq_command($text, $command, $GROQ);
    @file_put_contents(__DIR__ . '/usage.log',
        date('c') . "\t" . ($payload['id'] ?? '?') . "\t$tier\t{$secs}s\tcommand\n", FILE_APPEND | LOCK_EX);
    echo json_encode(['text' => $result]);
    exit;
}

// ── 2) Optioneel: vertalen / opschonen via Groq chat ────────────────────
$LANG_NAMES = ['nl'=>'Dutch','en'=>'English','de'=>'German','fr'=>'French','es'=>'Spanish','it'=>'Italian','pt'=>'Portuguese'];

function groq_postprocess($text, $mode, $groq, $names) {
    if ($mode === '' || $mode === 'off' || trim($text) === '') return $text;
    if ($mode === 'clean') {
        $system = "You polish raw speech-to-text dictation. Remove filler words, false ".
                  "starts, repetitions and stutters; fix punctuation, capitalization and ".
                  "obvious mis-transcriptions. Keep the EXACT same language as the input — ".
                  "never translate. Preserve meaning and tone. Do not add, summarize, explain ".
                  "or answer anything. Output ONLY the cleaned text.";
    } else {
        $target = $names[$mode] ?? $mode;
        $system = "You post-process raw speech-to-text dictation. Translate it into $target. ".
                  "Also clean it up: drop filler words, false starts and stutters, and fix ".
                  "punctuation and capitalization so it reads naturally. Preserve meaning and ".
                  "tone. Do not add, summarize, explain or answer anything. Output ONLY the ".
                  "final $target text, nothing else.";
    }
    $ch = curl_init('https://api.groq.com/openai/v1/chat/completions');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => ["Authorization: Bearer $groq", "Content-Type: application/json"],
        CURLOPT_POSTFIELDS => json_encode([
            'model' => 'llama-3.3-70b-versatile', 'temperature' => 0,
            'messages' => [['role'=>'system','content'=>$system], ['role'=>'user','content'=>$text]],
        ]),
        CURLOPT_TIMEOUT => 30,
    ]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($code !== 200) return $text;   // faal stil → ruwe transcriptie
    $out = trim(json_decode($body, true)['choices'][0]['message']['content'] ?? '');
    return $out !== '' ? $out : $text;
}

function groq_command($instruction, $text, $groq) {
    $instruction = trim($instruction);
    if ($instruction === '') return $text;
    $system = "You edit text according to a spoken instruction. Apply the instruction to the ".
              "user's text and output ONLY the resulting text — no preamble, no quotes, no ".
              "explanation. Keep the original language unless the instruction says otherwise.";
    $ch = curl_init('https://api.groq.com/openai/v1/chat/completions');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => ["Authorization: Bearer $groq", "Content-Type: application/json"],
        CURLOPT_POSTFIELDS => json_encode([
            'model' => 'llama-3.3-70b-versatile', 'temperature' => 0,
            'messages' => [['role'=>'system','content'=>$system],
                           ['role'=>'user','content'=>"Instruction: $instruction\n\nText:\n$text"]],
        ]),
        CURLOPT_TIMEOUT => 30,
    ]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($code !== 200) return $text;
    $out = trim(json_decode($body, true)['choices'][0]['message']['content'] ?? '');
    return $out !== '' ? $out : $text;
}

$text = groq_postprocess($text, $postproc, $GROQ, $LANG_NAMES);

// ── Lichte gebruiksregistratie ──────────────────────────────────────────
@file_put_contents(__DIR__ . '/usage.log',
    date('c') . "\t" . ($payload['id'] ?? '?') . "\t$tier\t{$secs}s\t$language\n",
    FILE_APPEND | LOCK_EX);

echo json_encode(['text' => $text]);
