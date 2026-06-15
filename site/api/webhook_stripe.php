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
        case 'customer.subscription.updated':
            handle_sub_updated($db, $event['data']['object']);
            break;
        case 'invoice.payment_failed':
            handle_payment_failed($db, $event['data']['object']);
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
    $session_id   = $session['id'] ?? '';
    if (!$session_id) return;
    $email        = $session['customer_details']['email'] ?? $session['customer_email'] ?? '';
    $mode         = $session['mode'] ?? 'payment';   // 'payment' | 'subscription'
    $plan         = $mode === 'subscription' ? 'pro' : 'lifetime';
    $amount       = (int)($session['amount_total'] ?? 0);
    $currency     = $session['currency'] ?? 'eur';
    $customer_id  = $session['customer'] ?? null;
    $sub_id       = $session['subscription'] ?? null;
    $pi_id        = $session['payment_intent'] ?? null;   // voor refund-matching

    if (!$email) return;

    // Idempotency check
    $chk = $db->prepare('SELECT id FROM purchases WHERE stripe_session_id = ?');
    $chk->execute([$session_id]);
    if ($chk->fetch()) return;

    // Genereer HMAC-ondertekende sleutel (LZT.… formaat)
    $key = generate_license_key($email, $plan);

    $db->prepare('INSERT INTO purchases
        (stripe_session_id, stripe_customer_id, stripe_subscription_id,
         email, plan, amount_cents, currency, license_key, stripe_payment_intent)
        VALUES (?,?,?,?,?,?,?,?,?)')
       ->execute([$session_id, $customer_id, $sub_id,
                  $email, $plan, $amount, $currency, $key, $pi_id]);

    send_key_email($email, $plan, $key);
}

function handle_sub_cancelled(PDO $db, array $sub): void {
    $db->prepare("UPDATE purchases SET status='cancelled', cancelled_at=NOW()
                  WHERE stripe_subscription_id=?")
       ->execute([$sub['id']]);
}

function handle_sub_updated(PDO $db, array $sub): void {
    $sub_id = $sub['id'] ?? '';
    $status = $sub['status'] ?? '';
    if (!$sub_id) return;
    if ($status === 'active') {
        $db->prepare("UPDATE purchases SET status='active' WHERE stripe_subscription_id=?")
           ->execute([$sub_id]);
    } elseif (in_array($status, ['past_due', 'unpaid', 'paused'], true)) {
        $db->prepare("UPDATE purchases SET status='suspended' WHERE stripe_subscription_id=?")
           ->execute([$sub_id]);
    }
}

function handle_payment_failed(PDO $db, array $invoice): void {
    $sub_id = $invoice['subscription'] ?? '';
    if (!$sub_id) return;
    $db->prepare("UPDATE purchases SET status='suspended' WHERE stripe_subscription_id=? AND status='active'")
       ->execute([$sub_id]);
}

function handle_refund(PDO $db, array $charge): void {
    // Match exact op de opgeslagen payment-intent (betrouwbaar). Pas als die
    // ontbreekt, val terug op de customer-id — maar nooit op een lege waarde,
    // anders zouden álle rijen met lege customer ge-refund worden.
    $pi   = $charge['payment_intent'] ?? '';
    $cust = $charge['customer'] ?? '';
    if ($pi) {
        $n = $db->prepare("UPDATE purchases SET status='refunded' WHERE stripe_payment_intent = ?");
        $n->execute([$pi]);
        if ($n->rowCount() > 0) return;
    }
    if ($cust) {
        $db->prepare("UPDATE purchases SET status='refunded' WHERE stripe_customer_id = ?")
           ->execute([$cust]);
    }
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
    if ($plan === 'lifetime') {
        $label = 'Lifetime';
        $note  = "Je licentie is permanent geldig — geen maandelijkse kosten.";
    } else {
        $label = 'Pro';
        $note  = "Je abonnement verlengt automatisch. Annuleren kan via je Stripe-portal.";
    }
    $subj  = "Je Lazytype {$label}-sleutel";
    $body  = "Hallo,\r\n\r\n"
           . "Bedankt voor je aanschaf van Lazytype {$label}!\r\n\r\n"
           . "Je licentiesleutel:\r\n\r\n"
           . "  {$key}\r\n\r\n"
           . "Voer hem in via: tray-menu → Abonnement-sleutel invoeren…\r\n\r\n"
           . $note . "\r\n"
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
