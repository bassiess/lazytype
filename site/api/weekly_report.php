<?php
// Weekly report — run via cron: 0 8 * * 1 (every Monday 08:00)
// URL access: /api/weekly_report.php?token=<ADMIN_PASSWORD>
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

$token = $_GET['token'] ?? '';
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

$dl_week    = (int)$db->query("SELECT COUNT(*) FROM downloads WHERE created_at >= '$week'")->fetchColumn();
$dl_month   = (int)$db->query("SELECT COUNT(*) FROM downloads WHERE created_at >= '$month'")->fetchColumn();
$dl_total   = (int)$db->query("SELECT COUNT(*) FROM downloads")->fetchColumn();

$v_week     = (int)$db->query("SELECT COUNT(*) FROM page_views WHERE created_at >= '$week'")->fetchColumn();
$v_uniq_w   = (int)$db->query("SELECT COUNT(DISTINCT ip) FROM page_views WHERE created_at >= '$week'")->fetchColumn();
$v_month    = (int)$db->query("SELECT COUNT(*) FROM page_views WHERE created_at >= '$month'")->fetchColumn();

$rev_week   = (int)$db->query("SELECT COALESCE(SUM(amount_cents),0) FROM purchases WHERE status='active' AND created_at >= '$week'")->fetchColumn();
$rev_month  = (int)$db->query("SELECT COALESCE(SUM(amount_cents),0) FROM purchases WHERE status='active' AND created_at >= '$month'")->fetchColumn();
$new_users  = (int)$db->query("SELECT COUNT(*) FROM purchases WHERE status='active' AND created_at >= '$week'")->fetchColumn();

$sources = $db->query("
    SELECT utm_source, referrer, COUNT(*) as cnt
    FROM page_views
    WHERE created_at >= '$week'
    GROUP BY utm_source, LEFT(referrer,80)
    ORDER BY cnt DESC
    LIMIT 8
")->fetchAll();

$purchases = $db->query("
    SELECT email, plan, amount_cents, created_at
    FROM purchases
    WHERE created_at >= '$week'
    ORDER BY created_at DESC
    LIMIT 20
")->fetchAll();

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
