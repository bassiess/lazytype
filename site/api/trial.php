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

try {
    init_db();
    $db = get_db();

    // Geef bestaande niet-verlopen sleutel terug
    $stmt = $db->prepare("SELECT license_key FROM trials WHERE email = ? AND expires_at > NOW() LIMIT 1");
    $stmt->execute([$email]);
    $row = $stmt->fetch();
    if ($row) {
        echo json_encode(['ok' => true, 'key' => $row['license_key']]);
        exit;
    }

    $key = make_trial_key($email);
    $db->prepare("
        INSERT INTO trials (email, device, license_key, expires_at)
        VALUES (?, ?, ?, DATE_ADD(NOW(), INTERVAL 14 DAY))
        ON DUPLICATE KEY UPDATE license_key = VALUES(license_key), expires_at = VALUES(expires_at), device = VALUES(device)
    ")->execute([$email, $device, $key]);

    // Bevestigingsmail
    $subject = 'Je Lazytype-proefsleutel (14 dagen gratis)';
    $msg = implode("\r\n", [
        "Hallo,",
        "",
        "Hier is je 14-daagse proefsleutel voor Lazytype:",
        "",
        $key,
        "",
        "De sleutel is al ingesteld in de app. Wil je hem later handmatig invoeren:",
        "tray-menu → Abonnement-sleutel invoeren…",
        "",
        "Na 14 dagen kun je upgraden op lazytype.com.",
        "",
        "Succes met dicteren!",
        "Team Lazytype",
    ]);
    $headers = "From: " . MAIL_FROM_NAME . " <" . MAIL_FROM . ">\r\nContent-Type: text/plain; charset=UTF-8";
    @mail($email, $subject, $msg, $headers);

    echo json_encode(['ok' => true, 'key' => $key]);

} catch (Exception $e) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Serverfout']);
}
