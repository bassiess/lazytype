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
    $pad = strlen($s) % 4;
    if ($pad) { $s .= str_repeat('=', 4 - $pad); }
    return base64_decode(strtr($s, '-_', '+/'));
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
        // Zelfde slot-vullende logica als verify.php: bekend → OK; lege slot → vullen;
        // beide slots bezet door andere apparaten → weigeren. (Voorheen accepteerde
        // transcribe.php élk apparaat zolang één slot leeg was én vulde nooit een slot,
        // waardoor de 2-apparaten-limiet de facto niet werd gehandhaafd.)
        if ($tier === 'trial') {
            $stmt = $db->prepare("SELECT device, device_2 FROM trials WHERE license_key = ? LIMIT 1");
            $stmt->execute([$license]);
            $row = $stmt->fetch();
            if ($row !== false) {
                $d1 = $row['device']   ?? '';
                $d2 = $row['device_2'] ?? '';
                if ($device === $d1 || $device === $d2) {
                    // bekend apparaat — OK
                } elseif (!$d1) {
                    $db->prepare("UPDATE trials SET device   = ? WHERE license_key = ?")->execute([$device, $license]);
                } elseif (!$d2) {
                    $db->prepare("UPDATE trials SET device_2 = ? WHERE license_key = ?")->execute([$device, $license]);
                } else {
                    http_response_code(403);
                    echo json_encode(['error' => 'Sleutel al in gebruik op 2 apparaten. Mail support@lazytype.com voor overdracht.']);
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
                if ($device === $d1 || $device === $d2) {
                    // bekend apparaat — OK
                } elseif (!$d1) {
                    $db->prepare("UPDATE purchases SET device_id   = ? WHERE license_key = ?")->execute([$device, $license]);
                } elseif (!$d2) {
                    $db->prepare("UPDATE purchases SET device_id_2 = ? WHERE license_key = ?")->execute([$device, $license]);
                } else {
                    http_response_code(403);
                    echo json_encode(['error' => 'Sleutel al geactiveerd op 2 apparaten. Mail support@lazytype.com voor overdracht.']);
                    exit;
                }
            }
        }
    } catch (\Exception $e) {
        // DB-fout: transcriptie doorzetten zonder device check
    }
}

// ── Rate limiting & fair-use ──────────────────────────────────────────────
// Trial   : max 150/uur per sleutel (ruimte om alles uit te proberen).
// Betaald (pro/lifetime): een ruime fair-use-cap als vangnet tegen misbruik/
// scripts — 600/uur (burst) én 8000/maand (~50 uur audio). Geen normale
// gebruiker raakt dit; het begrenst de worst-case API-kosten per sleutel.
// (Een zware échte gebruiker zit op ~2.500-3.000 calls/maand → ruim eronder.)
const TRIAL_PER_HOUR   = 150;
const PAID_PER_HOUR    = 600;
const PAID_PER_MONTH   = 8000;
if (!isset($db)) {
    require_once __DIR__ . '/db.php';
    init_db();
    $db = get_db();
}
$key_hash = hash('sha256', $license);
try {
    if ($tier === 'trial') {
        $cnt = $db->prepare("SELECT COUNT(*) FROM rate_limits WHERE key_hash = ? AND endpoint = 'transcribe' AND created_at > DATE_SUB(NOW(), INTERVAL 1 HOUR)");
        $cnt->execute([$key_hash]);
        if ((int)$cnt->fetchColumn() >= TRIAL_PER_HOUR) {
            http_response_code(429);
            echo json_encode(['error' => 'Te veel verzoeken in het afgelopen uur (proefversie: max 150/uur). Wacht even of upgrade op lazytype.com.']);
            exit;
        }
    } else {
        // Betaald: burst-cap per uur (stopt scripts/runaway-loops; mensen halen dit nooit).
        $hr = $db->prepare("SELECT COUNT(*) FROM rate_limits WHERE key_hash = ? AND endpoint = 'transcribe' AND created_at > DATE_SUB(NOW(), INTERVAL 1 HOUR)");
        $hr->execute([$key_hash]);
        if ((int)$hr->fetchColumn() >= PAID_PER_HOUR) {
            http_response_code(429);
            echo json_encode(['error' => 'Ongebruikelijk veel verzoeken in korte tijd. Wacht heel even en probeer opnieuw — bij twijfel mail support@lazytype.com.']);
            exit;
        }
        // Maand-cap (fair use): begrenst de totale API-kosten per sleutel per maand.
        $period = date('Y-m');
        $mc = $db->prepare("SELECT cnt FROM usage_counters WHERE key_hash = ? AND period = ?");
        $mc->execute([$key_hash, $period]);
        if ((int)$mc->fetchColumn() >= PAID_PER_MONTH) {
            http_response_code(429);
            echo json_encode(['error' => 'Maandelijkse fair-use-limiet bereikt (~50 uur dicteren deze maand). Heb je structureel meer nodig? Mail support@lazytype.com — we denken graag mee.']);
            exit;
        }
        $db->prepare("INSERT INTO usage_counters (key_hash, period, cnt) VALUES (?, ?, 1) ON DUPLICATE KEY UPDATE cnt = cnt + 1")
           ->execute([$key_hash, $period]);
    }
    $db->prepare("INSERT INTO rate_limits (key_hash, endpoint) VALUES (?, 'transcribe')")->execute([$key_hash]);
} catch (\Exception $e) {
    // DB-fout: transcriptie doorzetten zonder rate check (fail-open)
}

// ── Tekst-only command (AI-mode via sneltoets): geen audio, directe bewerking ──
$mode_instruction = substr(trim($_POST['instruction'] ?? ''), 0, 2000);
if ($mode_instruction !== '') {
    $mode_target = substr(trim($_POST['command'] ?? ''), 0, 8000);
    if ($mode_target === '') {
        http_response_code(400);
        echo json_encode(['error' => 'Geen tekst om te bewerken']);
        exit;
    }
    $system = 'You edit text according to an instruction. Apply the instruction to the '
            . "user's text and output ONLY the resulting text — no preamble, no quotes, "
            . 'no explanation. Keep the original language unless the instruction says otherwise.';
    $out = groq_chat($system, "Instruction: {$mode_instruction}\n\nText:\n{$mode_target}");
    echo json_encode(['text' => $out !== null ? $out : $mode_target]);
    exit;
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

// ── Input validatie ───────────────────────────────────────────────────────
$language    = trim($_POST['language']    ?? 'auto');
$prompt      = substr(trim($_POST['prompt']  ?? ''), 0, 500);
$postprocess = trim($_POST['postprocess'] ?? 'off');
$command     = substr(trim($_POST['command']  ?? ''), 0, 4000);
$context     = trim($_POST['context']  ?? '');   // email | chat | code (toon-aanpassing)

$valid_langs = ['auto','af','sq','am','ar','hy','as','az','ba','eu','be','bn','bs','br','bg',
                'my','ca','zh','hr','cs','da','nl','en','et','fo','fi','fr','gl','ka','de',
                'el','gu','ht','ha','haw','he','hi','hu','is','id','it','ja','jw','kn','kk',
                'km','ko','lo','la','lv','ln','lt','lb','mk','mg','ms','ml','mt','mi','mr',
                'mn','ne','no','nn','oc','ps','fa','pl','pt','pa','ro','ru','sa','sr','sn',
                'sd','si','sk','sl','so','es','su','sw','sv','tl','tg','ta','tt','te','th',
                'bo','tr','tk','uk','ur','uz','vi','cy','yi','yo'];
if (!in_array($language, $valid_langs, true)) {
    $language = 'auto';
}

$valid_pp = ['off','clean'];
$is_valid_pp = in_array($postprocess, $valid_pp, true)
               || preg_match('/^[a-z]{2}$/', $postprocess);
if (!$is_valid_pp) {
    $postprocess = 'off';
}

// ── Groq API aanroepen ────────────────────────────────────────────────────
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
$text = trim($data['text'] ?? '');

// ── Nabewerking via Groq chat (vertalen / opschonen / command-mode) ─────────
// De client (managed-engine) stuurt postprocess + command mee; vroeger negeerde
// de server die, waardoor vertalen/opschonen/command voor Pro+trial niet werkte.
function groq_chat(string $system, string $user): ?string {
    $payload = json_encode([
        'model'       => 'llama-3.3-70b-versatile',
        'temperature' => 0,
        'messages'    => [
            ['role' => 'system', 'content' => $system],
            ['role' => 'user',   'content' => $user],
        ],
    ]);
    $ch = curl_init('https://api.groq.com/openai/v1/chat/completions');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_HTTPHEADER     => [
            'Authorization: Bearer ' . GROQ_API_KEY,
            'Content-Type: application/json',
        ],
        CURLOPT_POSTFIELDS     => $payload,
        CURLOPT_TIMEOUT        => 45,
    ]);
    $resp = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    if ($code !== 200 || $resp === false) {
        return null;
    }
    $j = json_decode($resp, true);
    $out = trim($j['choices'][0]['message']['content'] ?? '');
    return $out !== '' ? $out : null;
}

$LANG_NAMES = [
    'af' => 'Afrikaans', 'sq' => 'Albanian', 'am' => 'Amharic', 'ar' => 'Arabic',
    'hy' => 'Armenian', 'as' => 'Assamese', 'az' => 'Azerbaijani', 'ba' => 'Bashkir',
    'eu' => 'Basque', 'be' => 'Belarusian', 'bn' => 'Bengali', 'bs' => 'Bosnian',
    'br' => 'Breton', 'bg' => 'Bulgarian', 'yue' => 'Cantonese', 'ca' => 'Catalan',
    'zh' => 'Chinese', 'hr' => 'Croatian', 'cs' => 'Czech', 'da' => 'Danish',
    'nl' => 'Dutch', 'en' => 'English', 'et' => 'Estonian', 'fo' => 'Faroese',
    'fi' => 'Finnish', 'fr' => 'French', 'gl' => 'Galician', 'ka' => 'Georgian',
    'de' => 'German', 'el' => 'Greek', 'gu' => 'Gujarati', 'ht' => 'Haitian Creole',
    'ha' => 'Hausa', 'haw' => 'Hawaiian', 'he' => 'Hebrew', 'hi' => 'Hindi',
    'hu' => 'Hungarian', 'is' => 'Icelandic', 'id' => 'Indonesian', 'it' => 'Italian',
    'ja' => 'Japanese', 'jw' => 'Javanese', 'kn' => 'Kannada', 'kk' => 'Kazakh',
    'km' => 'Khmer', 'ko' => 'Korean', 'lo' => 'Lao', 'la' => 'Latin',
    'lv' => 'Latvian', 'ln' => 'Lingala', 'lt' => 'Lithuanian', 'lb' => 'Luxembourgish',
    'mk' => 'Macedonian', 'mg' => 'Malagasy', 'ms' => 'Malay', 'ml' => 'Malayalam',
    'mt' => 'Maltese', 'mi' => 'Maori', 'mr' => 'Marathi', 'mn' => 'Mongolian',
    'my' => 'Myanmar', 'ne' => 'Nepali', 'no' => 'Norwegian', 'nn' => 'Nynorsk',
    'oc' => 'Occitan', 'ps' => 'Pashto', 'fa' => 'Persian', 'pl' => 'Polish',
    'pt' => 'Portuguese', 'pa' => 'Punjabi', 'ro' => 'Romanian', 'ru' => 'Russian',
    'sa' => 'Sanskrit', 'sr' => 'Serbian', 'sn' => 'Shona', 'sd' => 'Sindhi',
    'si' => 'Sinhala', 'sk' => 'Slovak', 'sl' => 'Slovenian', 'so' => 'Somali',
    'es' => 'Spanish', 'su' => 'Sundanese', 'sw' => 'Swahili', 'sv' => 'Swedish',
    'tl' => 'Tagalog', 'tg' => 'Tajik', 'ta' => 'Tamil', 'tt' => 'Tatar',
    'te' => 'Telugu', 'th' => 'Thai', 'bo' => 'Tibetan', 'tr' => 'Turkish',
    'tk' => 'Turkmen', 'uk' => 'Ukrainian', 'ur' => 'Urdu', 'uz' => 'Uzbek',
    'vi' => 'Vietnamese', 'cy' => 'Welsh', 'yi' => 'Yiddish', 'yo' => 'Yoruba',
];

// Toon-hint o.b.v. de actieve app (context-bewuste toon).
$tone = '';
if ($context === 'email') {
    $tone = ' Use a professional, well-structured tone suitable for an email.';
} elseif ($context === 'chat') {
    $tone = ' Use a casual, concise, conversational tone suitable for a chat message.';
}

if ($text !== '') {
    if ($command !== '') {
        // Command-mode: gesproken tekst = instructie, $command = geselecteerde tekst
        $system = 'You edit text according to a spoken instruction. Apply the instruction to '
                . "the user's text and output ONLY the resulting text — no preamble, no quotes, "
                . 'no explanation. Keep the original language unless the instruction says otherwise.';
        $edited = groq_chat($system, "Instruction: {$text}\n\nText:\n{$command}");
        if ($edited !== null) { $text = $edited; }
    } elseif ($postprocess === 'clean') {
        // In een code-editor: laat de tekst letterlijk (geen opschoning die symbolen verhaspelt).
        if ($context !== 'code') {
            $system = 'You polish raw speech-to-text dictation. Remove filler words, false starts, '
                    . 'repetitions and stutters; fix punctuation, capitalization and obvious '
                    . 'mis-transcriptions. Keep the EXACT same language as the input — never '
                    . 'translate. Preserve the original meaning and tone. Do not add, summarize, '
                    . 'explain or answer anything. Output ONLY the cleaned text.' . $tone;
            $clean = groq_chat($system, $text);
            if ($clean !== null) { $text = $clean; }
        }
    } elseif ($postprocess !== '' && $postprocess !== 'off') {
        $target = $LANG_NAMES[$postprocess] ?? $postprocess;
        $system = "You post-process raw speech-to-text dictation. Translate it into {$target}. "
                . 'Also clean it up: drop filler words, false starts and stutters, and fix '
                . 'punctuation and capitalization so it reads naturally for a native speaker. '
                . 'Preserve the original meaning and tone. Do not add, summarize, explain or '
                . "answer anything. Output ONLY the final {$target} text, nothing else." . $tone;
        $translated = groq_chat($system, $text);
        if ($translated !== null) { $text = $translated; }
    }
}

echo json_encode(['text' => $text]);
