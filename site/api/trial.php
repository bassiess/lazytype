<?php
/**
 * POST /api/trial.php
 * Geeft een 14-daagse proefsleutel terug voor het opgegeven e-mailadres.
 * Elk e-mailadres krijgt maximaal één proefsleutel.
 */
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

header('Content-Type: application/json');

$email  = trim(strtolower($_POST['email']  ?? ''));
$device = trim($_POST['device'] ?? '');

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
    $exp = $now + 14 * 86400;
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

function send_trial_email(string $to, string $key, bool $recovery = false): bool {
    $subject = $recovery
        ? 'Je Lazytype-proefsleutel (opnieuw toegestuurd)'
        : 'Je Lazytype-proefsleutel (14 dagen gratis)';
    $intro = $recovery
        ? 'Hier is (opnieuw) je proefsleutel voor Lazytype:'
        : 'Hier is je 14-daagse proefsleutel voor Lazytype:';
    $msg = implode("\r\n", [
        "Hallo,",
        "",
        $intro,
        "",
        $key,
        "",
        "De sleutel is al ingesteld in de app. Wil je hem later handmatig invoeren:",
        "tray-menu → Abonnement-sleutel invoeren…",
        "",
        "Je proefperiode loopt door tot de oorspronkelijke einddatum — opnieuw aanvragen",
        "start de 14 dagen niet opnieuw.",
        "",
        "Succes met dicteren!",
        "Team Lazytype",
    ]);
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
    return (bool)@mail($to, $subject, $msg, $headers, '-f' . MAIL_FROM);
}

try {
    init_db();
    $db = get_db();

    // Weiger trial als dit e-mailadres al een actief abonnement heeft
    $chk = $db->prepare("SELECT id FROM purchases WHERE email = ? AND status = 'active' LIMIT 1");
    $chk->execute([$email]);
    if ($chk->fetch()) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Je hebt al een actief Lazytype-abonnement voor dit e-mailadres']);
        exit;
    }

    $ip_hash = hash('sha256', $_SERVER['REMOTE_ADDR'] ?? '');

    // Lichte rate-limit op het VERSTUREN van mail (nieuw + opnieuw), tegen inbox-spam.
    $mail_rate_ok = function () use ($db, $ip_hash): bool {
        $c = $db->prepare("SELECT COUNT(*) FROM rate_limits WHERE key_hash = ? AND endpoint = 'trial_mail' AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)");
        $c->execute([$ip_hash]);
        return (int)$c->fetchColumn() < 6;
    };
    $log_mail = function () use ($db, $ip_hash) {
        $db->prepare("INSERT INTO rate_limits (key_hash, endpoint) VALUES (?, 'trial_mail')")->execute([$ip_hash]);
    };

    // Eén gratis proef per e-mailadres — OOIT. Bestaat er al een proef voor dit adres?
    $stmt = $db->prepare("SELECT license_key, expires_at FROM trials WHERE email = ? LIMIT 1");
    $stmt->execute([$email]);
    $row = $stmt->fetch();
    if ($row) {
        if (strtotime($row['expires_at']) > time()) {
            // Nog geldig → geef dezelfde sleutel terug (de app slaat 'm op). De vervaldatum
            // blijft ONGEWIJZIGD — opnieuw aanvragen verlengt/herstart de proef NIET. De
            // sleutel-mail wordt (met rate-limit) opnieuw verstuurd zodat je 'm in je inbox hebt.
            $mail_sent = false;
            if ($mail_rate_ok()) {
                $mail_sent = send_trial_email($email, $row['license_key'], true);
                $log_mail();
            }
            echo json_encode(['ok' => true, 'key' => $row['license_key'], 'mail_sent' => $mail_sent]);
        } else {
            // Verlopen → GEEN nieuwe proef (anti-misbruik: niet eindeloos te herhalen)
            http_response_code(403);
            echo json_encode(['ok' => false, 'error' => 'Dit e-mailadres heeft de gratis proefperiode al gebruikt. Upgrade op lazytype.com om door te gaan.']);
        }
        exit;
    }

    // ── Rate limiting: max 3 NIEUWE trials per IP per dag ─────────────────────
    $ip_cnt = $db->prepare("SELECT COUNT(*) FROM rate_limits WHERE key_hash = ? AND endpoint = 'trial' AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)");
    $ip_cnt->execute([$ip_hash]);
    if ((int)$ip_cnt->fetchColumn() >= 3) {
        http_response_code(429);
        echo json_encode(['ok' => false, 'error' => 'Te veel aanvragen van dit IP-adres. Probeer morgen opnieuw.']);
        exit;
    }
    $db->prepare("INSERT INTO rate_limits (key_hash, endpoint) VALUES (?, 'trial')")->execute([$ip_hash]);

    // Nieuw adres → precies één proef aanmaken. Plain INSERT (nooit verlengen).
    $key = make_trial_key($email);
    $db->prepare("INSERT INTO trials (email, device, license_key, expires_at)
                  VALUES (?, ?, ?, DATE_ADD(NOW(), INTERVAL 14 DAY))")
       ->execute([$email, $device, $key]);

    $mail_sent = false;
    if ($mail_rate_ok()) {
        $mail_sent = send_trial_email($email, $key, false);
        $log_mail();
    }

    echo json_encode(['ok' => true, 'key' => $key, 'mail_sent' => $mail_sent]);

} catch (Exception $e) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Serverfout']);
}
