<?php
// Bezoeker-tracking endpoint — aangeroepen door de JS-beacon op elke pagina
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

header('Access-Control-Allow-Origin: *');
http_response_code(204); // No Content — browser verwacht geen antwoord

// Bots filteren op user-agent
$ua = $_SERVER['HTTP_USER_AGENT'] ?? '';
foreach (['bot','crawler','spider','slurp','prerender','facebookexternalhit',
          'curl','wget','python','go-http','headless','phantom'] as $kw) {
    if (stripos($ua, $kw) !== false) exit;
}

$raw       = file_get_contents('php://input');
$data      = $raw ? (json_decode($raw, true) ?? []) : [];
$page      = substr($data['p'] ?? '/', 0, 500);
$referrer  = substr($data['r'] ?? '', 0, 500);
$utm_src   = substr($data['s'] ?? '', 0, 200);
$utm_med   = substr($data['m'] ?? '', 0, 200);
$utm_camp  = substr($data['c'] ?? '', 0, 200);
$ip        = trim(explode(',', $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? '')[0]);

try {
    init_db();
    get_db()
        ->prepare('INSERT INTO page_views
            (page, referrer, utm_source, utm_medium, utm_campaign, ip, user_agent)
            VALUES (?,?,?,?,?,?,?)')
        ->execute([$page, $referrer, $utm_src, $utm_med, $utm_camp, $ip, substr($ua, 0, 500)]);
} catch (Exception $e) {
    // stil falen — tracking mag de site nooit breken
}
