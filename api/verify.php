<?php
/**
 * Lazytype — server-side licentie-verificatie (HMAC + intrekkingscheck).
 * POST {license} → {ok, tier, label, reason}
 * Geen audio, geen Groq-key nodig. Alleen LAZYTYPE_LICENSE_SECRET.
 * Gebruikt door Personal-clients om de honor-system client-check te vervangen.
 */

require_once __DIR__ . '/license_check.php';

header('Content-Type: application/json; charset=utf-8');
function vfail($msg) { echo json_encode(['ok' => false, 'reason' => $msg]); exit; }

$SECRET = getenv('LAZYTYPE_LICENSE_SECRET') ?: '';
if (file_exists(__DIR__ . '/config.php')) require __DIR__ . '/config.php';
if ($SECRET === '') { http_response_code(500); vfail('server niet geconfigureerd'); }
if ($_SERVER['REQUEST_METHOD'] !== 'POST') { http_response_code(405); vfail('alleen POST'); }

[$ok, $reason, $payload] = lzt_verify_license($_POST['license'] ?? '', $SECRET);
if (!$ok) { vfail($reason); }

$revF = __DIR__ . '/revoked.json';
$revoked = file_exists($revF) ? (json_decode(@file_get_contents($revF), true) ?: []) : [];
if (in_array($payload['id'] ?? '', $revoked, true)) { vfail('abonnement ingetrokken'); }

$tier = $payload['tier'] ?? '';
$names = ['personal' => 'Personal', 'pro' => 'Pro'];
$tierName = $names[$tier] ?? $tier;
$exp = (int)($payload['exp'] ?? 0);
$label = $tierName . ($exp ? ' · tot ' . date('Y-m-d', $exp) : '');
echo json_encode(['ok' => true, 'tier' => $tier, 'label' => $label, 'id' => $payload['id'] ?? '']);
