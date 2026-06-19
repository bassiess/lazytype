<?php
/**
 * POST /api/trial.php — proefaanvraag met e-mailverificatie (2-staps).
 *
 *   1) Zonder 'code'  → genereert een 6-cijferige code, mailt die, antwoordt
 *                       {ok, code_sent:true}. GEEN sleutel.
 *   2) Met 'code'     → verifieert de code en geeft pas dán de proefsleutel terug.
 *
 * Beleid:
 *   • Eén gratis proef per e-mailadres.
 *   • Na het verlopen van de proef is het adres 6 maanden geblokkeerd voor nieuwe
 *     proeven; daarna mag het één nieuwe proef.
 */
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

header('Content-Type: application/json');

const TRIAL_DAYS      = 14;
const COOLDOWN_MONTHS = 6;
const CODE_TTL_MIN    = 15;
const CODE_MAX_TRIES  = 5;

$email  = trim(strtolower($_POST['email']  ?? ''));
$device = trim($_POST['device'] ?? '');
$code   = trim($_POST['code']   ?? '');

if (!$email || !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    http_response_code(400);
    echo json_encode(['ok' => false, 'error' => 'Ongeldig e-mailadres']);
    exit;
}

function b64e(string $bytes): string {
    return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
}

function make_trial_key(string $email): string {
    $now = time();
    $exp = $now + TRIAL_DAYS * 86400;
    $payload = json_encode([
        'id'    => b64e(random_bytes(6)),
        'email' => $email,
        'tier'  => 'trial',
        'iat'   => $now,
        'exp'   => $exp,
    ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    $pb  = b64e($payload);
    $sig = b64e(hash_hmac('sha256', $pb, LICENSE_SECRET, true));
    return "LZT.{$pb}.{$sig}";
}

function code_hash(string $code): string {
    return hash_hmac('sha256', $code, LICENSE_SECRET);
}

function mail_send(string $to, string $subject, string $body): bool {
    $headers = implode("\r\n", [
        'From: ' . MAIL_FROM_NAME . ' <' . MAIL_FROM . '>',
        'Reply-To: ' . MAIL_FROM,
        'MIME-Version: 1.0',
        'Content-Type: text/plain; charset=UTF-8',
        'Content-Transfer-Encoding: 8bit',
        'X-Mailer: Lazytype',
        'Message-ID: <' . bin2hex(random_bytes(8)) . '@lazytype.com>',
        'Date: ' . date(DATE_RFC2822),
    ]);
    return (bool)@mail($to, $subject, $body, $headers, '-f' . MAIL_FROM);
}

function send_code_email(string $to, string $code): bool {
    $body = implode("\r\n", [
        "Hallo,", "",
        "Je verificatiecode voor je Lazytype-proefperiode is:", "",
        "    {$code}", "",
        "Vul deze code in de app in om je " . TRIAL_DAYS . "-daagse proef te starten.",
        "De code is " . CODE_TTL_MIN . " minuten geldig.", "",
        "Heb je dit niet aangevraagd? Dan kun je deze mail negeren.", "",
        "Team Lazytype",
    ]);
    return mail_send($to, 'Je Lazytype-verificatiecode', $body);
}

function send_key_email(string $to, string $key): bool {
    $body = implode("\r\n", [
        "Hallo,", "",
        "Je proefperiode is gestart! Je licentiesleutel (als backup):", "",
        "    {$key}", "",
        "De sleutel is al in de app ingesteld. Handmatig invoeren kan via:",
        "tray-menu -> Abonnement-sleutel invoeren...", "",
        "Succes met dicteren!", "Team Lazytype",
    ]);
    return mail_send($to, 'Je Lazytype-proefsleutel', $body);
}

// active | cooldown(until) | again | new
function eligibility(PDO $db, string $email): array {
    $stmt = $db->prepare("SELECT license_key, expires_at FROM trials WHERE email = ? LIMIT 1");
    $stmt->execute([$email]);
    $row = $stmt->fetch();
    if (!$row) return ['status' => 'new'];
    if (strtotime($row['expires_at']) > time()) return ['status' => 'active', 'row' => $row];
    $cooldown_end = strtotime($row['expires_at'] . ' +' . COOLDOWN_MONTHS . ' months');
    if (time() < $cooldown_end) return ['status' => 'cooldown', 'until' => $cooldown_end];
    return ['status' => 'again', 'row' => $row];
}

try {
    init_db();
    $db = get_db();
    $ip_hash = hash('sha256', $_SERVER['REMOTE_ADDR'] ?? '');

    // Actief abonnement? Dan geen proef nodig.
    $chk = $db->prepare("SELECT id FROM purchases WHERE email = ? AND status = 'active' LIMIT 1");
    $chk->execute([$email]);
    if ($chk->fetch()) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Je hebt al een actief Lazytype-abonnement voor dit e-mailadres']);
        exit;
    }

    $elig = eligibility($db, $email);
    if ($elig['status'] === 'cooldown') {
        http_response_code(403);
        $until = date('j-n-Y', $elig['until']);
        echo json_encode(['ok' => false, 'error' => "Dit e-mailadres heeft de gratis proef al gebruikt. Een nieuwe proef kan vanaf {$until}, of upgrade nu op lazytype.com."]);
        exit;
    }

    // ── STAP 1: verificatiecode aanvragen ─────────────────────────────────────
    if ($code === '') {
        // Rate limit: max 10 code-aanvragen per IP per dag (tegen mail-bombing).
        $rc = $db->prepare("SELECT COUNT(*) FROM rate_limits WHERE key_hash = ? AND endpoint = 'trial_code' AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)");
        $rc->execute([$ip_hash]);
        if ((int)$rc->fetchColumn() >= 10) {
            http_response_code(429);
            echo json_encode(['ok' => false, 'error' => 'Te veel aanvragen. Probeer het later opnieuw.']);
            exit;
        }
        $db->prepare("INSERT INTO rate_limits (key_hash, endpoint) VALUES (?, 'trial_code')")->execute([$ip_hash]);

        $code6 = str_pad((string)random_int(0, 999999), 6, '0', STR_PAD_LEFT);
        $db->prepare("INSERT INTO email_codes (email, code_hash, attempts, expires_at)
                      VALUES (?, ?, 0, DATE_ADD(NOW(), INTERVAL " . CODE_TTL_MIN . " MINUTE))
                      ON DUPLICATE KEY UPDATE code_hash = VALUES(code_hash), attempts = 0, expires_at = VALUES(expires_at), created_at = NOW()")
           ->execute([$email, code_hash($code6)]);

        $sent = send_code_email($email, $code6);
        echo json_encode(['ok' => true, 'code_sent' => true, 'mail_sent' => $sent]);
        exit;
    }

    // ── STAP 2: code verifiëren + sleutel uitgeven ────────────────────────────
    $cs = $db->prepare("SELECT code_hash, attempts, expires_at FROM email_codes WHERE email = ? LIMIT 1");
    $cs->execute([$email]);
    $crow = $cs->fetch();
    if (!$crow || strtotime($crow['expires_at']) < time()) {
        http_response_code(403);
        echo json_encode(['ok' => false, 'error' => 'Geen geldige code. Vraag een nieuwe verificatiecode aan.']);
        exit;
    }
    if ((int)$crow['attempts'] >= CODE_MAX_TRIES) {
        $db->prepare("DELETE FROM email_codes WHERE email = ?")->execute([$email]);
        http_response_code(429);
        echo json_encode(['ok' => false, 'error' => 'Te veel onjuiste pogingen. Vraag een nieuwe code aan.']);
        exit;
    }
    if (!hash_equals($crow['code_hash'], code_hash($code))) {
        $db->prepare("UPDATE email_codes SET attempts = attempts + 1 WHERE email = ?")->execute([$email]);
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Onjuiste code.']);
        exit;
    }
    // Code klopt → eenmalig gebruiken.
    $db->prepare("DELETE FROM email_codes WHERE email = ?")->execute([$email]);

    // Sleutel uitgeven o.b.v. eligibility (kan hier niet 'cooldown' zijn).
    if ($elig['status'] === 'active') {
        $key = $elig['row']['license_key'];                       // herstel: zelfde sleutel
    } elseif ($elig['status'] === 'again') {
        $key = make_trial_key($email);                            // nieuwe proef na 6 mnd
        $db->prepare("UPDATE trials SET license_key = ?, expires_at = DATE_ADD(NOW(), INTERVAL " . TRIAL_DAYS . " DAY), device = ?, device_2 = NULL, reminded_at = NULL WHERE email = ?")
           ->execute([$key, $device, $email]);
    } else { // new
        $key = make_trial_key($email);
        $db->prepare("INSERT INTO trials (email, device, license_key, expires_at)
                      VALUES (?, ?, ?, DATE_ADD(NOW(), INTERVAL " . TRIAL_DAYS . " DAY))")
           ->execute([$email, $device, $key]);
    }

    send_key_email($email, $key);
    echo json_encode(['ok' => true, 'key' => $key]);

} catch (Exception $e) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Serverfout']);
}
