<?php
/**
 * POST /api/verify.php
 * Verifieert een licentiesleutel (HMAC + vervaldatum).
 * Gebruikt door de client-app als achtergrondcheck.
 *
 * POST-veld: license  (LZT.… sleutel)
 * Response:  {"ok": true/false, "tier": "...", "error": "..."}
 */
require_once __DIR__ . '/config.php';

header('Content-Type: application/json');

function b64d(string $s): string {
    return base64_decode(str_replace(['-', '_'], ['+', '/'], $s) . str_repeat('=', (-strlen($s)) % 4));
}
function b64e_raw(string $bytes): string {
    return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
}

$license = trim($_POST['license'] ?? '');
if (!$license) {
    echo json_encode(['ok' => false, 'error' => 'Geen sleutel']);
    exit;
}
$parts = explode('.', $license, 3);
if (count($parts) !== 3 || $parts[0] !== 'LZT') {
    echo json_encode(['ok' => false, 'error' => 'Ongeldig formaat']);
    exit;
}
[, $pb, $sig] = $parts;
$expected = b64e_raw(hash_hmac('sha256', $pb, LICENSE_SECRET, true));
if (!hash_equals($expected, $sig)) {
    echo json_encode(['ok' => false, 'error' => 'Handtekening klopt niet']);
    exit;
}
$payload = json_decode(b64d($pb), true);
$exp = intval($payload['exp'] ?? 0);
if ($exp && time() > $exp) {
    echo json_encode(['ok' => false, 'error' => 'Verlopen', 'tier' => $payload['tier'] ?? '']);
    exit;
}
echo json_encode(['ok' => true, 'tier' => $payload['tier'] ?? '']);
