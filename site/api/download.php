<?php
// Logs the download, then redirects to the actual .exe
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

try {
    init_db();
    $db  = get_db();
    $ip  = trim(explode(',', $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? '')[0]);
    $ua  = substr($_SERVER['HTTP_USER_AGENT'] ?? '', 0, 500);
    $ref = substr($_SERVER['HTTP_REFERER']    ?? '', 0, 500);
    $db->prepare('INSERT INTO downloads (ip, user_agent, referer) VALUES (?,?,?)')
       ->execute([$ip, $ua, $ref]);
} catch (Exception $e) {
    error_log('download log: ' . $e->getMessage());
}

header('Location: /downloads/Lazytype.exe');
exit;
