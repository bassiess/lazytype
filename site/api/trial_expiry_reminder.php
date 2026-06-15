<?php
/**
 * Trial expiry reminder — stuur herinneringsmail 2 dagen voor vervaldatum.
 * Cron: 0 9 * * * (dagelijks 09:00)
 * URL:  /api/trial_expiry_reminder.php?token=<ADMIN_PASSWORD>
 *
 * Mailt iedereen wiens trial over 1-2 dagen verloopt én nog niet
 * is geüpgraded (geen row in purchases voor dit e-mailadres).
 */
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

$token = $_GET['token'] ?? '';
if (php_sapi_name() !== 'cli' && !hash_equals(ADMIN_PASSWORD, $token)) {
    http_response_code(403);
    exit('Forbidden');
}

try {
    init_db();
    $db = get_db();
} catch (Exception $e) {
    error_log('trial_expiry_reminder: DB error — ' . $e->getMessage());
    exit(1);
}

// Trials die morgen of overmorgen verlopen en nog niet reminded_at hebben
$stmt = $db->query("
    SELECT t.email, t.expires_at
    FROM trials t
    LEFT JOIN purchases p ON p.email = t.email AND p.status = 'active'
    WHERE p.id IS NULL
      AND t.expires_at > NOW()
      AND t.expires_at <= DATE_ADD(NOW(), INTERVAL 2 DAY)
      AND (t.reminded_at IS NULL OR t.reminded_at < DATE_SUB(NOW(), INTERVAL 5 DAY))
    LIMIT 200
");

// Kolom toevoegen als hij nog niet bestaat (migratie)
try {
    $db->exec("ALTER TABLE trials ADD COLUMN reminded_at DATETIME NULL DEFAULT NULL");
} catch (Exception $e) {}

$rows = $stmt->fetchAll();
$sent = 0;

foreach ($rows as $row) {
    $email   = $row['email'];
    $expires = new DateTime($row['expires_at']);
    $days    = max(1, (int)ceil(($expires->getTimestamp() - time()) / 86400));
    $dayWord = $days === 1 ? 'morgen' : "over $days dagen";

    $subject = "Je Lazytype-proefperiode verloopt $dayWord";
    $msg = implode("\r\n", [
        "Hallo,",
        "",
        "Je gratis Lazytype-proefperiode verloopt $dayWord ({$expires->format('d-m-Y')}).",
        "",
        "Bevalt het dicteren? Upgrade nu en blijf dicteren zonder onderbrekingen:",
        "",
        "  https://lazytype.com/#pricing",
        "",
        "Na een upgrade werkt je app meteen door — je hoeft niets opnieuw in te stellen.",
        "",
        "Vragen? Mail ons op support@lazytype.com",
        "",
        "Team Lazytype",
    ]);
    $headers = "From: " . MAIL_FROM_NAME . " <" . MAIL_FROM . ">\r\nContent-Type: text/plain; charset=UTF-8";

    if (@mail($email, $subject, $msg, $headers)) {
        $db->prepare("UPDATE trials SET reminded_at = NOW() WHERE email = ?")
           ->execute([$email]);
        $sent++;
        echo "Herinneringsmail verstuurd → $email (verloopt {$expires->format('d-m-Y')})\n";
    } else {
        echo "Fout bij verzenden → $email\n";
    }
}

echo "\nKlaar. $sent herinneringsmail(s) verstuurd.\n";
