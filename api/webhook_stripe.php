<?php
/**
 * Lazytype — Stripe webhook (alternatief voor Lemon Squeezy).
 *
 * LET OP: Stripe is GEEN merchant-of-record → jij regelt zelf de EU-BTW
 * (via Stripe Tax of een VAT-OSS-aangifte). Lemon Squeezy/Paddle nemen dat
 * uit handen. Verder werkt Stripe prima en goedkoper qua fees.
 *
 *   Personal = eenmalige betaling   (Checkout mode = payment)
 *   Pro      = abonnement           (Checkout mode = subscription)
 *
 * Setup: Stripe → Developers → Webhooks → endpoint
 *   https://lazytype.com/api/webhook_stripe.php
 *   events: checkout.session.completed, customer.subscription.deleted
 *   signing secret (whsec_…) → STRIPE_WEBHOOK_SECRET in api/config.php
 */

require_once __DIR__ . '/license_check.php';

$SECRET = getenv('LAZYTYPE_LICENSE_SECRET') ?: '';
$STRIPE_SECRET = getenv('STRIPE_WEBHOOK_SECRET') ?: '';
if (file_exists(__DIR__ . '/config.php')) require __DIR__ . '/config.php';
if ($SECRET === '' || $STRIPE_SECRET === '') { http_response_code(500); echo 'niet geconfigureerd'; exit; }

// ── Stripe-signature verifiëren: header "t=…,v1=…", HMAC-SHA256("t.body") ──
$raw = file_get_contents('php://input');
$header = $_SERVER['HTTP_STRIPE_SIGNATURE'] ?? '';
$t = null; $v1 = null;
foreach (explode(',', $header) as $part) {
    [$k, $val] = array_pad(explode('=', $part, 2), 2, '');
    if ($k === 't') $t = $val;
    if ($k === 'v1') $v1 = $val;
}
if (!$t || !$v1) { http_response_code(400); echo 'no signature'; exit; }
if (abs(time() - (int)$t) > 300) { http_response_code(400); echo 'stale'; exit; }   // replay-bescherming
if (!hash_equals(hash_hmac('sha256', $t . '.' . $raw, $STRIPE_SECRET), $v1)) {
    http_response_code(401); echo 'bad signature'; exit;
}

$event = json_decode($raw, true) ?: [];
$type  = $event['type'] ?? '';
$obj   = $event['data']['object'] ?? [];

switch ($type) {
    case 'checkout.session.completed':
        $email = $obj['customer_details']['email'] ?? ($obj['customer_email'] ?? '');
        if (($obj['mode'] ?? '') === 'subscription') {
            lzt_issue($email, 'pro', 0, $SECRET, 'Pro', (string)($obj['subscription'] ?? ''));
        } else {  // mode = payment → eenmalige Personal
            lzt_issue($email, 'personal', 0, $SECRET, 'Personal');
        }
        break;

    case 'customer.subscription.deleted':
        $subId = (string)($obj['id'] ?? '');
        $rec = file_exists(lzt_sub_path($subId)) ? json_decode(@file_get_contents(lzt_sub_path($subId)), true) : null;
        if ($rec && !empty($rec['key_id'])) {
            lzt_revoke($rec['key_id']);
            lzt_log("revoke-stripe\t" . ($rec['email'] ?? '?') . "\t" . $rec['key_id'] . "\tsub=$subId");
        }
        break;

    default:
        break;
}

http_response_code(200);
echo json_encode(['ok' => true]);
