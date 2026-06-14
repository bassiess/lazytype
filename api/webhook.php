<?php
/**
 * Lazytype — Lemon Squeezy webhook.
 *
 * Betaling binnen → genereer een ondertekende licentiesleutel → mail naar de koper.
 *   • order_created (variant = Personal)        → Personal-sleutel (perpetueel)
 *   • subscription_created (variant = Pro)       → Pro-sleutel (perpetueel, ingetrokken bij opzegging)
 *   • subscription_cancelled / _expired          → trek de bijbehorende sleutel in (revoked.json)
 *
 * Setup (Lemon Squeezy → Settings → Webhooks): URL = https://lazytype.com/api/webhook.php,
 * signing secret = LS_WEBHOOK_SECRET. Zet in api/config.php:
 *   $SECRET (LAZYTYPE_LICENSE_SECRET), $LS_WEBHOOK_SECRET, $LS_VARIANT_PERSONAL, $LS_VARIANT_PRO
 */

require_once __DIR__ . '/license_check.php';

$SECRET    = getenv('LAZYTYPE_LICENSE_SECRET') ?: '';
$LS_SECRET = getenv('LS_WEBHOOK_SECRET') ?: '';
$VAR_PERSONAL = (string)(getenv('LS_VARIANT_PERSONAL') ?: '');
$VAR_PRO      = (string)(getenv('LS_VARIANT_PRO') ?: '');
if (file_exists(__DIR__ . '/config.php')) require __DIR__ . '/config.php';

// ── Signature verifiëren (HMAC-SHA256 over de ruwe body, hex) ───────────
$raw = file_get_contents('php://input');
$sig = $_SERVER['HTTP_X_SIGNATURE'] ?? '';
if ($SECRET === '' || $LS_SECRET === '') { http_response_code(500); echo 'niet geconfigureerd'; exit; }
if (!hash_equals(hash_hmac('sha256', $raw, $LS_SECRET), $sig)) { http_response_code(401); echo 'bad signature'; exit; }

$data  = json_decode($raw, true) ?: [];
$event = $data['meta']['event_name'] ?? '';
$attr  = $data['data']['attributes'] ?? [];
$subId = (string)($data['data']['id'] ?? '');
$email = $attr['user_email'] ?? ($attr['customer_email'] ?? '');
$variant = (string)($attr['variant_id'] ?? ($attr['first_order_item']['variant_id'] ?? ''));

// (uitgifte-helpers lzt_log/lzt_sub_path/lzt_revoke/lzt_email_key/lzt_issue
//  staan gedeeld in license_check.php)

// ── Events ──────────────────────────────────────────────────────────────
switch ($event) {
    case 'order_created':
        // Eenmalige aankoop. Alleen de Personal-variant hier afhandelen
        // (Pro-abonnementen genereren ook orders → die doet subscription_created).
        if ($VAR_PERSONAL === '' || $variant === $VAR_PERSONAL) {
            lzt_issue($email, 'personal', 0, $SECRET, 'Personal');
        }
        break;

    case 'subscription_created':
        // Pro-abonnement gestart → perpetuele Pro-sleutel; bij opzegging ingetrokken.
        lzt_issue($email, 'pro', 0, $SECRET, 'Pro', $subId);
        break;

    case 'subscription_cancelled':
    case 'subscription_expired':
        $rec = file_exists(lzt_sub_path($subId)) ? json_decode(@file_get_contents(lzt_sub_path($subId)), true) : null;
        if ($rec && !empty($rec['key_id'])) {
            lzt_revoke($rec['key_id']);
            lzt_log("revoke\t" . ($email ?: '?') . "\t" . $rec['key_id'] . "\tsub=$subId");
        }
        break;

    default:
        // subscription_payment_success / _updated / etc. → niets te doen (sleutel blijft geldig)
        break;
}

http_response_code(200);
echo json_encode(['ok' => true]);
