<?php
/**
 * POST /api/transcribe.php
 * Managed transcriptie voor Pro- en trial-sleutels.
 * Verifieert de HMAC-sleutel, stuurt audio naar Groq, geeft tekst terug.
 *
 * POST-velden:
 *   license     string  verplicht — LZT.… sleutel
 *   file        file    verplicht — WAV-audiobestand (max 25 MB)
 *   language    string  optioneel — ISO-taalcode of "auto" (default: auto)
 *   postprocess string  optioneel — "off" | "ai" | "translate" (default: off)
 *   prompt      string  optioneel — woordenboek-hint voor Whisper
 *   command     string  optioneel — instructie voor command-mode
 */
require_once __DIR__ . '/config.php';

header('Content-Type: application/json');

// ── Helpers ───────────────────────────────────────────────────────────────
function b64d(string $s): string {
    return base64_decode(str_replace(['-', '_'], ['+', '/'], $s) . str_repeat('=', (-strlen($s)) % 4));
}
function b64e_raw(string $bytes): string {
    return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
}

// ── Licentieverificatie ───────────────────────────────────────────────────
$license = trim($_POST['license'] ?? '');
if (!$license) {
    http_response_code(402);
    echo json_encode(['error' => 'Geen licentiesleutel — vul je sleutel in via het tray-menu']);
    exit;
}
$parts = explode('.', $license, 3);
if (count($parts) !== 3 || $parts[0] !== 'LZT') {
    http_response_code(402);
    echo json_encode(['error' => 'Ongeldig sleutelformaat']);
    exit;
}
[, $pb, $sig] = $parts;
$expected = b64e_raw(hash_hmac('sha256', $pb, LICENSE_SECRET, true));
if (!hash_equals($expected, $sig)) {
    http_response_code(403);
    echo json_encode(['error' => 'Ongeldige sleutel']);
    exit;
}
$payload = json_decode(b64d($pb), true);
$tier    = $payload['tier'] ?? '';
$exp     = intval($payload['exp'] ?? 0);
if ($exp && time() > $exp) {
    http_response_code(402);
    echo json_encode(['error' => 'Proef verlopen — verleng op lazytype.com']);
    exit;
}
if (!in_array($tier, ['trial', 'pro', 'lifetime'], true)) {
    http_response_code(403);
    echo json_encode(['error' => 'Personal-sleutel vereist je eigen Groq-key in de app']);
    exit;
}

// ── Device binding check (max 2 apparaten per sleutel) ────────────────────
$device = trim($_POST['device'] ?? '');
if ($device) {
    require_once __DIR__ . '/db.php';
    try {
        init_db();
        $db = get_db();
        if ($tier === 'trial') {
            $stmt = $db->prepare("SELECT device, device_2 FROM trials WHERE license_key = ? LIMIT 1");
            $stmt->execute([$license]);
            $row = $stmt->fetch();
            if ($row !== false) {
                $d1 = $row['device']   ?? '';
                $d2 = $row['device_2'] ?? '';
                $known = ($device === $d1 || $device === $d2 || !$d1 || !$d2);
                if (!$known) {
                    http_response_code(403);
                    echo json_encode(['error' => 'Sleutel al in gebruik op 2 apparaten']);
                    exit;
                }
            }
        } else {
            $stmt = $db->prepare("SELECT device_id, device_id_2 FROM purchases WHERE license_key = ? LIMIT 1");
            $stmt->execute([$license]);
            $row = $stmt->fetch();
            if ($row !== false) {
                $d1 = $row['device_id']   ?? '';
                $d2 = $row['device_id_2'] ?? '';
                $known = ($device === $d1 || $device === $d2 || !$d1 || !$d2);
                if (!$known) {
                    http_response_code(403);
                    echo json_encode(['error' => 'Sleutel al geactiveerd op 2 apparaten']);
                    exit;
                }
            }
        }
    } catch (\Exception $e) {
        // DB-fout: transcriptie doorzetten zonder device check
    }
}

// ── Audio ontvangen ───────────────────────────────────────────────────────
if (empty($_FILES['file']) || $_FILES['file']['error'] !== UPLOAD_ERR_OK) {
    http_response_code(400);
    echo json_encode(['error' => 'Geen audiobestand ontvangen']);
    exit;
}
if ($_FILES['file']['size'] > 25 * 1024 * 1024) {
    http_response_code(413);
    echo json_encode(['error' => 'Audiobestand te groot (max 25 MB)']);
    exit;
}
$wav = $_FILES['file']['tmp_name'];

// ── Groq API aanroepen ────────────────────────────────────────────────────
$language = $_POST['language'] ?? 'auto';
$prompt   = $_POST['prompt']   ?? '';

$fields = [
    'model'           => 'whisper-large-v3-turbo',
    'response_format' => 'json',
    'file'            => new CURLFile($wav, 'audio/wav', 'audio.wav'),
];
if ($language && $language !== 'auto') {
    $fields['language'] = $language;
}
if ($prompt) {
    $fields['prompt'] = $prompt;
}

$ch = curl_init('https://api.groq.com/openai/v1/audio/transcriptions');
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST           => true,
    CURLOPT_HTTPHEADER     => ['Authorization: Bearer ' . GROQ_API_KEY],
    CURLOPT_POSTFIELDS     => $fields,
    CURLOPT_TIMEOUT        => 60,
]);
$body       = curl_exec($ch);
$http_code  = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curl_error = curl_error($ch);
curl_close($ch);

if ($curl_error) {
    http_response_code(502);
    echo json_encode(['error' => "Verbindingsfout met Groq: $curl_error"]);
    exit;
}
if ($http_code !== 200) {
    http_response_code(502);
    $groq_msg = json_decode($body, true)['error']['message'] ?? substr($body, 0, 200);
    echo json_encode(['error' => "Groq-fout ($http_code): $groq_msg"]);
    exit;
}

$data = json_decode($body, true);
echo json_encode(['text' => trim($data['text'] ?? '')]);
