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
$tier = $payload['tier'] ?? '';
$exp  = intval($payload['exp'] ?? 0);
if ($exp && time() > $exp) {
    echo json_encode(['ok' => false, 'error' => 'Verlopen', 'tier' => $tier]);
    exit;
}

// Device binding: sleutel wordt gebonden aan het eerste apparaat dat zich meldt.
// Daarna worden andere apparaten geweigerd.
$device = trim($_POST['device'] ?? '');
if ($device) {
    require_once __DIR__ . '/db.php';
    try {
        init_db();
        $db = get_db();
        if ($tier === 'trial') {
            $stmt = $db->prepare("SELECT device FROM trials WHERE license_key = ? LIMIT 1");
            $stmt->execute([$license]);
            $row = $stmt->fetch();
            if ($row !== false) {
                if (!$row['device']) {
                    $db->prepare("UPDATE trials SET device = ? WHERE license_key = ?")->execute([$device, $license]);
                } elseif ($row['device'] !== $device) {
                    echo json_encode(['ok' => false, 'error' => 'Sleutel al in gebruik op een ander apparaat. Mail support@lazytype.com voor overdracht.', 'device_mismatch' => true]);
                    exit;
                }
            }
        } else {
            $stmt = $db->prepare("SELECT device_id FROM purchases WHERE license_key = ? LIMIT 1");
            $stmt->execute([$license]);
            $row = $stmt->fetch();
            if ($row !== false) {
                if (!$row['device_id']) {
                    $db->prepare("UPDATE purchases SET device_id = ? WHERE license_key = ?")->execute([$device, $license]);
                } elseif ($row['device_id'] !== $device) {
                    echo json_encode(['ok' => false, 'error' => 'Sleutel al geactiveerd op een ander apparaat. Mail support@lazytype.com voor overdracht.', 'device_mismatch' => true]);
                    exit;
                }
            }
        }
    } catch (\Exception $e) {
        // DB-fout: verificatie doorzetten zonder device check
    }
}

echo json_encode(['ok' => true, 'tier' => $tier]);
