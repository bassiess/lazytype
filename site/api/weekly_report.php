<?php
// Weekly report — run via cron: 0 8 * * 1 (every Monday 08:00)
// HTTP access: Authorization: Bearer <ADMIN_PASSWORD>
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

$auth  = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
$token = preg_replace('/^Bearer\s+/i', '', $auth);
if (php_sapi_name() !== 'cli' && !hash_equals(ADMIN_PASSWORD, $token)) {
    http_response_code(403);
    exit('Forbidden');
}

try {
    init_db();
    $db = get_db();
} catch (Exception $e) {
    error_log('weekly_report: DB error — ' . $e->getMessage());
    exit(1);
}

$now   = new DateTime();
$week  = (clone $now)->modify('-7 days')->format('Y-m-d');
$month = (clone $now)->modify('-30 days')->format('Y-m-d');

function q_count(PDO $db, string $table, string $since): int {
    $s = $db->prepare("SELECT COUNT(*) FROM $table WHERE created_at >= ?");
    $s->execute([$since]);
    return (int)$s->fetchColumn();
}

$dl_week  = q_count($db, 'downloads',  $week);
$dl_month = q_count($db, 'downloads',  $month);
$dl_total = (int)$db->query("SELECT COUNT(*) FROM downloads")->fetchColumn();

$v_week   = q_count($db, 'page_views', $week);
$v_month  = q_count($db, 'page_views', $month);

$stmt = $db->prepare("SELECT COUNT(DISTINCT ip) FROM page_views WHERE created_at >= ?");
$stmt->execute([$week]);
$v_uniq_w = (int)$stmt->fetchColumn();

$stmt = $db->prepare("SELECT COALESCE(SUM(amount_cents),0) FROM purchases WHERE status='active' AND created_at >= ?");
$stmt->execute([$week]);
$rev_week = (int)$stmt->fetchColumn();

$stmt = $db->prepare("SELECT COALESCE(SUM(amount_cents),0) FROM purchases WHERE status='active' AND created_at >= ?");
$stmt->execute([$month]);
$rev_month = (int)$stmt->fetchColumn();

$stmt = $db->prepare("SELECT COUNT(*) FROM purchases WHERE status='active' AND created_at >= ?");
$stmt->execute([$week]);
$new_users = (int)$stmt->fetchColumn();

$stmt = $db->prepare("
    SELECT utm_source, referrer, COUNT(*) as cnt
    FROM page_views
    WHERE created_at >= ?
    GROUP BY utm_source, LEFT(referrer,80)
    ORDER BY cnt DESC
    LIMIT 8
");
$stmt->execute([$week]);
$sources = $stmt->fetchAll();

$stmt = $db->prepare("
    SELECT email, plan, amount_cents, created_at
    FROM purchases
    WHERE created_at >= ?
    ORDER BY created_at DESC
    LIMIT 20
");
$stmt->execute([$week]);
$purchases = $stmt->fetchAll();

function fmt_eur(int $cents): string {
    return '€' . number_format($cents / 100, 2, ',', '.');
}

$src_lines = '';
foreach ($sources as $s) {
    $label = $s['utm_source'] ?: ($s['referrer'] ? (parse_url($s['referrer'], PHP_URL_HOST) ?: $s['referrer']) : 'Direct');
    $src_lines .= "  " . str_pad($label, 28) . $s['cnt'] . " bezoeken\n";
}
if (!$src_lines) $src_lines = "  Geen data.\n";

$pur_lines = '';
foreach ($purchases as $p) {
    $pur_lines .= sprintf("  %s  %-10s  %s  %s\n",
        substr($p['created_at'], 0, 10),
        ucfirst($p['plan']),
        fmt_eur((int)$p['amount_cents']),
        $p['email']
    );
}
if (!$pur_lines) $pur_lines = "  Geen nieuwe aankopen.\n";

$week_label = (clone $now)->modify('-7 days')->format('d M') . ' – ' . $now->format('d M Y');
$subject    = 'Lazytype weekly · ' . $week_label;

$body  = "Lazytype — Weekrapport\n";
$body .= "$week_label\n";
$body .= str_repeat('=', 48) . "\n\n";
$body .= "BEZOEKERS (afgelopen 7 dagen)\n";
$body .= "  Paginaweergaven   : $v_week\n";
$body .= "  Unieke bezoekers  : $v_uniq_w\n";
$body .= "  Afgelopen 30 dagen: $v_month\n\n";
$body .= "DOWNLOADS\n";
$body .= "  Afgelopen 7 dagen : $dl_week\n";
$body .= "  Afgelopen 30 dagen: $dl_month\n";
$body .= "  Totaal            : $dl_total\n\n";
$body .= "OMZET\n";
$body .= "  Deze week         : " . fmt_eur($rev_week)  . "\n";
$body .= "  Afgelopen 30 dagen: " . fmt_eur($rev_month) . "\n";
$body .= "  Nieuwe klanten    : $new_users\n\n";
$body .= "HERKOMST (top 8 deze week)\n$src_lines\n";
$body .= "NIEUWE AANKOPEN\n$pur_lines\n";
$body .= str_repeat('-', 48) . "\n";
$body .= "Volledig admin panel: https://lazytype.com/admin/\n";
$body .= "Dit rapport wordt elke maandag om 08:00 verzonden.\n";

$headers  = 'From: ' . MAIL_FROM_NAME . ' <' . MAIL_FROM . ">\r\n";
$headers .= "Content-Type: text/plain; charset=UTF-8\r\n";

$ok = mail('bas@niese.nu', $subject, $body, $headers);
echo $ok ? "OK — rapport verstuurd naar bas@niese.nu\n" : "Fout bij e-mail verzending.\n";
