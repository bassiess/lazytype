<?php
/**
 * Lazytype — pure licentie- en misbruiklogica (geen side effects, geen curl).
 * Wordt ge-require'd door transcribe.php en is los testbaar (lzt_selftest()).
 *
 * Misbruik-plafonds beschermen je Groq-rekening: een sleutel is maar een string,
 * dus zonder caps kan één gedeelde €4-sleutel onbeperkt kosten maken.
 */

const MAX_AUDIO_BYTES = 4000000;  // ~125s @ 16kHz mono int16 — afkapping per dictaat

function lzt_caps() {
    // Alleen Pro gebruikt de managed-proxy (Personal = BYOK, trial = lokaal).
    // Ruime plafonds: normaal gebruik raakt ze nooit; key-sharing/abuse wél.
    return [
        'pro' => ['daily_sec' => 18000, 'devices' => 2],   // 5 uur/dag, 2 apparaten
    ];
}

function b64url_encode($b) { return rtrim(strtr(base64_encode($b), '+/', '-_'), '='); }
function b64url_decode($s) { return base64_decode(strtr($s, '-_', '+/') . str_repeat('=', (4 - strlen($s) % 4) % 4)); }

/** Verifieer een sleutel (zelfde HMAC als license.py). Geeft [ok, reden, payload]. */
function lzt_verify_license($key, $secret) {
    $parts = explode('.', trim($key));
    if (count($parts) !== 3 || $parts[0] !== 'LZT') return [false, 'ongeldig formaat', null];
    [$prefix, $pb, $sig] = $parts;
    $expected = b64url_encode(hash_hmac('sha256', $pb, $secret, true));
    if (!hash_equals($expected, $sig)) return [false, 'handtekening klopt niet', null];
    $payload = json_decode(b64url_decode($pb), true);
    if (!is_array($payload)) return [false, 'payload onleesbaar', null];
    $exp = (int)($payload['exp'] ?? 0);
    if ($exp && time() > $exp) return [false, 'verlopen', $payload];
    return [true, 'geldig', $payload];
}

/** Genereer een ondertekende sleutel (server-side, voor de webhook). exp=0 = perpetueel.
 *  Zelfde formaat als license.py → verifieert met lzt_verify_license en de client leest 'm. */
function lzt_generate($email, $tier, $exp, $secret) {
    $payload = ['id' => bin2hex(random_bytes(6)), 'email' => $email,
                'tier' => $tier, 'iat' => time(), 'exp' => (int)$exp];
    $pb = b64url_encode(json_encode($payload, JSON_UNESCAPED_SLASHES));
    return ['LZT.' . $pb . '.' . b64url_encode(hash_hmac('sha256', $pb, $secret, true)), $payload];
}

// ── Gedeelde uitgifte-helpers (webhook.php + webhook_stripe.php) ─────────
function lzt_log($line) {
    @file_put_contents(__DIR__ . '/issued.log', date('c') . "\t" . $line . "\n", FILE_APPEND | LOCK_EX);
}

function lzt_sub_path($id) {
    $dir = __DIR__ . '/subs';
    if (!is_dir($dir)) @mkdir($dir, 0700, true);
    return "$dir/" . preg_replace('/[^A-Za-z0-9_\-]/', '', (string)$id) . '.json';
}

function lzt_revoke($keyId) {
    if (!$keyId) return;
    $f = __DIR__ . '/revoked.json';
    $rev = file_exists($f) ? (json_decode(@file_get_contents($f), true) ?: []) : [];
    if (!in_array($keyId, $rev, true)) { $rev[] = $keyId; @file_put_contents($f, json_encode($rev), LOCK_EX); }
}

function lzt_email_key($to, $key, $tierName) {
    if (!$to) return;
    $to = filter_var(trim($to), FILTER_VALIDATE_EMAIL);
    if (!$to) return;
    $subject = "Je Lazytype $tierName-licentie";
    $body = "Bedankt voor je aankoop van Lazytype $tierName!\n\n" .
            "Je licentiesleutel:\n\n$key\n\n" .
            "Activeren: open Lazytype, klik op het systeemvak-icoon → " .
            "\"Abonnement-sleutel invoeren…\" en plak de sleutel.\n\n" .
            "Vragen? Antwoord op deze mail of schrijf naar bas@niese.nu.\n\n— Lazytype";
    $headers = "From: Lazytype <noreply@lazytype.com>\r\n" .
               "Content-Type: text/plain; charset=utf-8\r\n";
    @mail($to, $subject, $body, $headers);
}

function lzt_issue($email, $tier, $exp, $secret, $tierName, $subId = '') {
    [$key, $payload] = lzt_generate($email, $tier, $exp, $secret);
    lzt_email_key($email, $key, $tierName);
    lzt_log("$tier\t$email\t" . $payload['id'] . ($subId ? "\tsub=$subId" : ''));
    if ($subId) @file_put_contents(lzt_sub_path($subId), json_encode(['key_id' => $payload['id'], 'email' => $email]), LOCK_EX);
    return $payload;
}

/** Per-sleutel status (devices + dagelijks verbruik) als JSON-bestand. */
function lzt_state_path($dir, $id) {
    if (!is_dir($dir)) @mkdir($dir, 0700, true);
    $safe = preg_replace('/[^A-Za-z0-9_\-]/', '', (string)$id) ?: 'unknown';
    return "$dir/$safe.json";
}

function lzt_load_state($path) {
    $raw = @file_get_contents($path);
    $d = $raw ? json_decode($raw, true) : null;
    if (!is_array($d)) $d = [];
    if (!isset($d['devices']) || !is_array($d['devices'])) $d['devices'] = [];
    if (!isset($d['usage']) || !is_array($d['usage'])) $d['usage'] = [];
    return $d;
}

function lzt_save_state($path, $state) {
    $cut = date('Y-m-d', time() - 7 * 86400);   // bewaar alleen laatste 7 dagen
    $keep = [];
    foreach ($state['usage'] as $day => $sec) if ($day >= $cut) $keep[$day] = $sec;
    $state['usage'] = $keep;
    @file_put_contents($path, json_encode($state), LOCK_EX);
}

/** Device-binding. Geeft [allowed, state]. Leeg device → niet binden (legacy client). */
function lzt_device_check($state, $device, $max) {
    if ($device === '') return [true, $state];
    if (in_array($device, $state['devices'], true)) return [true, $state];
    if (count($state['devices']) >= $max) return [false, $state];
    $state['devices'][] = $device;
    return [true, $state];
}

/** Daglimiet. Geeft [allowed, state]. */
function lzt_quota_check($state, $secs, $daily_cap, $today) {
    $used = (int)($state['usage'][$today] ?? 0);
    if ($used + $secs > $daily_cap) return [false, $state];
    $state['usage'][$today] = $used + $secs;
    return [true, $state];
}

/** Interne zelftest — geen curl/Groq nodig. Aanroepbaar via ?selftest=1. */
function lzt_selftest() {
    $sec = 'SELFTEST_secret_xyz';
    $payload = ['id' => 'testid01', 'email' => 't@t', 'tier' => 'pro', 'iat' => time(), 'exp' => 0];
    $pb = b64url_encode(json_encode($payload, JSON_UNESCAPED_SLASHES));
    $key = "LZT.$pb." . b64url_encode(hash_hmac('sha256', $pb, $sec, true));

    $checks = [];
    $checks['verify_geldig']            = (lzt_verify_license($key, $sec)[0] === true);
    $checks['verkeerd_geheim_weigert']  = (lzt_verify_license($key, 'fout')[0] === false);
    $checks['sabotage_weigert']         = (lzt_verify_license(substr($key, 0, -2) . 'xx', $sec)[0] === false);

    $caps = lzt_caps()['pro'];
    $st = ['devices' => [], 'usage' => []];
    [$d1, $st] = lzt_device_check($st, 'devA', $caps['devices']);
    [$d2, $st] = lzt_device_check($st, 'devB', $caps['devices']);
    [$d3, $st] = lzt_device_check($st, 'devC', $caps['devices']);   // 3e bij max 2 → weiger
    $checks['device_cap'] = ($d1 && $d2 && !$d3);

    $st2 = ['devices' => [], 'usage' => []];
    [$q1, $st2] = lzt_quota_check($st2, 100, $caps['daily_sec'], '2026-06-14');
    [$q2, $st2] = lzt_quota_check($st2, $caps['daily_sec'], $caps['daily_sec'], '2026-06-14');  // over plafond
    $checks['quota_cap'] = ($q1 && !$q2);

    return ['ok' => !in_array(false, $checks, true), 'checks' => $checks];
}
