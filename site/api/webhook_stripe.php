<?php
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

$payload = file_get_contents('php://input');
$sig     = $_SERVER['HTTP_STRIPE_SIGNATURE'] ?? '';

if (!stripe_sig_ok($payload, $sig, STRIPE_WEBHOOK_SECRET)) {
    http_response_code(400); exit('bad signature');
}

$event = json_decode($payload, true);
if (!$event) { http_response_code(400); exit('bad json'); }

try {
    init_db();
    $db = get_db();

    switch ($event['type']) {
        case 'checkout.session.completed':
            handle_checkout($db, $event['data']['object']);
            break;
        case 'customer.subscription.deleted':
            handle_sub_cancelled($db, $event['data']['object']);
            break;
        case 'charge.refunded':
            handle_refund($db, $event['data']['object']);
            break;
    }

    http_response_code(200);
    echo 'ok';

} catch (Exception $e) {
    error_log('webhook error: ' . $e->getMessage());
    http_response_code(500);
    echo 'error';
}

// ── Handlers ────────────────────────────────────────────────────────────────

function handle_checkout(PDO $db, array $session): void {
    $session_id   = $session['id'];
    $email        = $session['customer_details']['email'] ?? $session['customer_email'] ?? '';
    $mode         = $session['mode'];   // 'payment' | 'subscription'
    $plan         = $mode === 'subscription' ? 'pro' : 'personal';
    $amount       = (int)($session['amount_total'] ?? 0);
    $currency     = $session['currency'] ?? 'eur';
    $customer_id  = $session['customer'] ?? null;
    $sub_id       = $session['subscription'] ?? null;

    if (!$email) return;

    // Idempotency check
    $chk = $db->prepare('SELECT id FROM purchases WHERE stripe_session_id = ?');
    $chk->execute([$session_id]);
    if ($chk->fetch()) return;

    // Genereer HMAC-ondertekende sleutel (LZT.… formaat)
    $key = generate_license_key($email, $plan);

    $db->prepare('INSERT INTO purchases
        (stripe_session_id, stripe_customer_id, stripe_subscription_id,
         email, plan, amount_cents, currency, license_key)
        VALUES (?,?,?,?,?,?,?,?)')
       ->execute([$session_id, $customer_id, $sub_id,
                  $email, $plan, $amount, $currency, $key]);

    send_key_email($email, $plan, $key);
}

function handle_sub_cancelled(PDO $db, array $sub): void {
    $db->prepare("UPDATE purchases SET status='cancelled', cancelled_at=NOW()
                  WHERE stripe_subscription_id=?")
       ->execute([$sub['id']]);
}

function handle_refund(PDO $db, array $charge): void {
    $pi = $charge['payment_intent'] ?? null;
    if (!$pi) return;
    $db->prepare("UPDATE purchases SET status='refunded'
                  WHERE stripe_session_id IN (
                      SELECT id FROM (SELECT id FROM purchases WHERE stripe_session_id LIKE ?) t
                  ) OR stripe_customer_id=?")
       ->execute(["%$pi%", $charge['customer'] ?? '']);
}

// ── Sleutelgeneratie (HMAC-ondertekend, LZT.… formaat) ──────────────────────

function b64e_lzt(string $bytes): string {
    return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
}

function generate_license_key(string $email, string $tier): string {
    $now     = time();
    $payload = json_encode([
        'id'    => b64e_lzt(random_bytes(6)),
        'email' => $email,
        'tier'  => $tier,
        'iat'   => $now,
        'exp'   => 0,   // geen vervaldatum — Pro-annulering via subscription webhook
    ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    $pb  = b64e_lzt($payload);
    $sig = b64e_lzt(hash_hmac('sha256', $pb, LICENSE_SECRET, true));
    return "LZT.{$pb}.{$sig}";
}

// ── Stripe signature verification ────────────────────────────────────────────

function stripe_sig_ok(string $payload, string $sig_header, string $secret): bool {
    $ts   = null;
    $sigs = [];
    foreach (explode(',', $sig_header) as $part) {
        if (str_starts_with($part, 't='))  $ts     = substr($part, 2);
        if (str_starts_with($part, 'v1=')) $sigs[] = substr($part, 3);
    }
    if (!$ts || empty($sigs)) return false;
    if (abs(time() - (int)$ts) > 300) return false;
    $expected = hash_hmac('sha256', $ts . '.' . $payload, $secret);
    foreach ($sigs as $s) {
        if (hash_equals($expected, $s)) return true;
    }
    return false;
}

// ── Email ────────────────────────────────────────────────────────────────────

function send_key_email(string $to, string $plan, string $key): void {
    $label = $plan === 'pro' ? 'Pro' : 'Personal';
    $byok  = $plan === 'personal'
        ? "\r\nPersonal: voeg je eigen gratis Groq-key toe via tray-menu → API-key instellen.\r\n"
        : '';
    $subj  = "Je Lazytype {$label}-sleutel";
    $body  = "Hallo,\r\n\r\n"
           . "Bedankt voor je aanschaf van Lazytype {$label}!\r\n\r\n"
           . "Je licentiesleutel:\r\n\r\n"
           . "  {$key}\r\n\r\n"
           . "Voer hem in via: tray-menu → Abonnement-sleutel invoeren…\r\n"
           . $byok
           . "\r\nVragen? Antwoord op deze e-mail.\r\n\r\n"
           . "Succes met dicteren!\r\n"
           . "Team Lazytype\r\n";

    $headers = implode("\r\n", [
        'From: ' . MAIL_FROM_NAME . ' <' . MAIL_FROM . '>',
        'Reply-To: ' . MAIL_FROM,
        'Content-Type: text/plain; charset=UTF-8',
        'X-Mailer: Lazytype',
    ]);

    mail($to, $subj, $body, $headers);
}
