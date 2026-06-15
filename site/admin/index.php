<?php
session_start();
require_once __DIR__ . '/../api/config.php';
require_once __DIR__ . '/../api/db.php';

function h($v): string { return htmlspecialchars((string)$v, ENT_QUOTES, 'UTF-8'); }
function eur(int $c): string { return '€' . number_format($c / 100, 2, ',', '.'); }
function fmt(int $n): string { return number_format($n, 0, ',', '.'); }

function parse_source(string $ref, string $utm): string {
    if ($utm) return ucfirst($utm);
    if (!$ref) return 'Direct';
    $host = strtolower(parse_url($ref, PHP_URL_HOST) ?? '');
    $host = preg_replace('/^www\./', '', $host);
    if (str_contains($host, 'google'))    return 'Google';
    if (str_contains($host, 'bing'))      return 'Bing';
    if (str_contains($host, 'facebook'))  return 'Facebook';
    if (str_contains($host, 'instagram')) return 'Instagram';
    if (str_contains($host, 'twitter') || str_contains($host, 'x.com')) return 'X / Twitter';
    if (str_contains($host, 'linkedin'))  return 'LinkedIn';
    if (str_contains($host, 'reddit'))    return 'Reddit';
    if (str_contains($host, 'youtube'))   return 'YouTube';
    if (str_contains($host, 'producthunt')) return 'Product Hunt';
    return $host ?: 'Onbekend';
}

// ── Auth ─────────────────────────────────────────────────────────────────────
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['pw'])) {
    if (hash_equals(ADMIN_PASSWORD, $_POST['pw'])) {
        $_SESSION['lt_admin'] = true;
        header('Location: /admin/'); exit;
    }
    $login_err = 'Incorrect wachtwoord.';
}
if (isset($_GET['logout'])) { session_destroy(); header('Location: /admin/'); exit; }
$ok = !empty($_SESSION['lt_admin']);

// ── Data ──────────────────────────────────────────────────────────────────────
$vs = []; $ps = []; $pv_chart = []; $dl_chart = [];
$top_pages = []; $top_sources = []; $purchases = [];
$db_err = null;

if ($ok) {
    try {
        init_db();
        $db = get_db();

        // ── Visitor stats ──
        $vs['total']       = (int)$db->query('SELECT COUNT(*) FROM page_views')->fetchColumn();
        $vs['uniq_total']  = (int)$db->query('SELECT COUNT(DISTINCT ip) FROM page_views')->fetchColumn();
        $vs['today']       = (int)$db->query("SELECT COUNT(*) FROM page_views WHERE DATE(created_at)=CURDATE()")->fetchColumn();
        $vs['uniq_today']  = (int)$db->query("SELECT COUNT(DISTINCT ip) FROM page_views WHERE DATE(created_at)=CURDATE()")->fetchColumn();
        $vs['7d']          = (int)$db->query("SELECT COUNT(*) FROM page_views WHERE created_at>=NOW()-INTERVAL 7 DAY")->fetchColumn();
        $vs['uniq_7d']     = (int)$db->query("SELECT COUNT(DISTINCT ip) FROM page_views WHERE created_at>=NOW()-INTERVAL 7 DAY")->fetchColumn();
        $vs['30d']         = (int)$db->query("SELECT COUNT(*) FROM page_views WHERE created_at>=NOW()-INTERVAL 30 DAY")->fetchColumn();

        // ── Visitor chart (last 30 days) ──
        $pv_raw = $db->query("
            SELECT DATE(created_at) d, COUNT(*) views, COUNT(DISTINCT ip) uniq
            FROM page_views WHERE created_at>=NOW()-INTERVAL 30 DAY
            GROUP BY DATE(created_at)
        ")->fetchAll();
        $pv_idx = [];
        foreach ($pv_raw as $r) $pv_idx[$r['d']] = $r;
        for ($i = 29; $i >= 0; $i--) {
            $day = date('Y-m-d', strtotime("-{$i} days"));
            $pv_chart[$day] = $pv_idx[$day] ?? ['views' => 0, 'uniq' => 0];
        }

        // ── Top pages (last 30 days) ──
        $top_pages = $db->query("
            SELECT page, COUNT(*) n, COUNT(DISTINCT ip) uniq
            FROM page_views WHERE created_at>=NOW()-INTERVAL 30 DAY
            GROUP BY page ORDER BY n DESC LIMIT 15
        ")->fetchAll();

        // ── Top sources (last 30 days) — aggregated in PHP ──
        $src_raw = $db->query("
            SELECT referrer, utm_source, COUNT(*) n
            FROM page_views WHERE created_at>=NOW()-INTERVAL 30 DAY
            GROUP BY referrer, utm_source
        ")->fetchAll();
        $src_agg = [];
        foreach ($src_raw as $r) {
            $s = parse_source($r['referrer'], $r['utm_source']);
            $src_agg[$s] = ($src_agg[$s] ?? 0) + (int)$r['n'];
        }
        arsort($src_agg);
        $top_sources = array_slice($src_agg, 0, 12, true);

        // ── Purchase / download stats ──
        $ps['dl_all']    = (int)$db->query('SELECT COUNT(*) FROM downloads')->fetchColumn();
        $ps['dl_today']  = (int)$db->query("SELECT COUNT(*) FROM downloads WHERE DATE(created_at)=CURDATE()")->fetchColumn();
        $ps['dl_7d']     = (int)$db->query("SELECT COUNT(*) FROM downloads WHERE created_at>=NOW()-INTERVAL 7 DAY")->fetchColumn();
        $ps['purchases'] = (int)$db->query('SELECT COUNT(*) FROM purchases')->fetchColumn();
        $ps['active_pro']= (int)$db->query("SELECT COUNT(*) FROM purchases WHERE plan='pro' AND status='active'")->fetchColumn();
        $ps['personal']  = (int)$db->query("SELECT COUNT(*) FROM purchases WHERE plan='personal'")->fetchColumn();
        $ps['mrr']       = $ps['active_pro'] * 500;
        $ps['revenue']   = (int)$db->query("SELECT COALESCE(SUM(amount_cents),0) FROM purchases")->fetchColumn();
        $ps['refunded']  = (int)$db->query("SELECT COUNT(*) FROM purchases WHERE status='refunded'")->fetchColumn();

        // ── Download chart (last 30 days) ──
        $dl_raw = $db->query("
            SELECT DATE(created_at) d, COUNT(*) n
            FROM downloads WHERE created_at>=NOW()-INTERVAL 30 DAY
            GROUP BY DATE(created_at)
        ")->fetchAll(PDO::FETCH_KEY_PAIR);
        for ($i = 29; $i >= 0; $i--) {
            $day = date('Y-m-d', strtotime("-{$i} days"));
            $dl_chart[$day] = $dl_raw[$day] ?? 0;
        }

        // ── Purchases table ──
        $q = trim($_GET['q'] ?? '');
        if ($q) {
            $stmt = $db->prepare("SELECT * FROM purchases WHERE email LIKE ? OR license_key LIKE ? ORDER BY created_at DESC LIMIT 200");
            $stmt->execute(["%$q%", "%$q%"]);
        } else {
            $stmt = $db->query("SELECT * FROM purchases ORDER BY created_at DESC LIMIT 200");
        }
        $purchases = $stmt->fetchAll();

    } catch (Exception $e) {
        $db_err = $e->getMessage();
    }
}

$pv_max = $pv_chart ? max(array_map(fn($r) => $r['views'], $pv_chart)) : 0;
$dl_max = $dl_chart ? max(array_values($dl_chart)) : 0;
$src_max = $top_sources ? max(array_values($top_sources)) : 0;
?>
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — Lazytype</title>
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--paper:#f7f6f3;--bg:#fbfaf8;--card:#fff;--ink:#191a1e;--ink2:#3c4049;--muted:#6b6f7b;--faint:#9a9ea9;--line:#e8e6e0;--accent:#5a46e0;--accent-soft:#efecfd;--ok:#1f9d57;--ok-soft:#dcfce7;--warn:#d97706;--danger:#dc2626}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:'Inter',system-ui,sans-serif;font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}

/* topbar */
.topbar{background:var(--card);border-bottom:1px solid var(--line);height:54px;display:flex;align-items:center;justify-content:space-between;padding:0 26px;position:sticky;top:0;z-index:10}
.topbar .brand{font-weight:700;font-size:15px;display:flex;align-items:center;gap:9px;color:var(--ink)}
.topbar .brand img{width:24px;height:24px;border-radius:6px}
.badge-env{background:var(--accent-soft);color:var(--accent);font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:99px;letter-spacing:.05em}
.topbar .right{display:flex;align-items:center;gap:20px;font-size:13px;color:var(--muted)}
.topbar .right a{color:var(--muted)}
.topbar .right a:hover{color:var(--ink)}

/* layout */
.main{max-width:1220px;margin:0 auto;padding:24px 22px 80px}
.section-head{display:flex;align-items:center;gap:10px;margin:28px 0 14px}
.section-head h2{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.section-head .line{flex:1;height:1px;background:var(--line)}

/* stat cards */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:11px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:16px 18px}
.stat .lbl{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.stat .val{font-size:28px;font-weight:700;letter-spacing:-.02em;color:var(--ink);line-height:1}
.stat .sub{font-size:11.5px;color:var(--muted);margin-top:4px}
.stat.c-accent .val{color:var(--accent)}
.stat.c-green  .val{color:var(--ok)}

/* grid panels */
.grid3{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:14px;margin-bottom:14px}
.grid2{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:14px}
.grid2r{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:20px}
.panel h3{font-size:13px;font-weight:600;color:var(--ink2);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.panel h3 .cnt{background:var(--paper);border:1px solid var(--line);border-radius:99px;font-size:11px;padding:1px 8px;color:var(--muted);font-weight:500}

/* bar chart */
.chart{display:flex;align-items:flex-end;gap:2px;height:80px}
.bw{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;position:relative;cursor:default}
.bw .b{border-radius:3px 3px 0 0;min-height:2px;transition:opacity .15s}
.bw .b.views{background:var(--accent);opacity:.65}
.bw .b.dls  {background:var(--ok);opacity:.7}
.bw:hover .b{opacity:1}
.bw::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 5px);left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;font-size:11px;padding:4px 8px;border-radius:7px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .15s;z-index:5}
.bw:hover::after{opacity:1}
.chart-empty{display:flex;align-items:center;justify-content:center;height:80px;color:var(--faint);font-size:13px}
.chart-foot{display:flex;justify-content:space-between;margin-top:6px;font-size:11px;color:var(--faint)}

/* horizontal bar (sources / pages) */
.hbar-list{display:flex;flex-direction:column;gap:9px}
.hbar-row{display:flex;align-items:center;gap:10px;font-size:13px}
.hbar-label{width:130px;flex-shrink:0;color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}
.hbar-track{flex:1;background:var(--paper);border-radius:99px;height:7px;overflow:hidden}
.hbar-fill{height:100%;border-radius:99px;background:var(--accent);opacity:.7}
.hbar-val{flex-shrink:0;font-size:12px;color:var(--muted);width:36px;text-align:right}

/* table */
.tbl-wrap{overflow-x:auto}
.search-row{display:flex;gap:9px;margin-bottom:13px}
.search-row input{flex:1;border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-family:inherit;font-size:13.5px;color:var(--ink);background:var(--card);outline:none}
.search-row input:focus{border-color:var(--accent)}
.search-row button{background:var(--ink);color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:13.5px;font-weight:600;cursor:pointer;font-family:inherit}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);padding:0 10px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--line);color:var(--ink2);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--paper)}
.mono{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--ink)}
.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700;letter-spacing:.02em}
.pill-pro       {background:#ede9fe;color:#5b21b6}
.pill-personal  {background:var(--accent-soft);color:var(--accent)}
.pill-active    {background:var(--ok-soft);color:#166534}
.pill-cancelled {background:#fee2e2;color:#991b1b}
.pill-refunded  {background:#fef3c7;color:#92400e}
.empty{text-align:center;padding:40px;color:var(--faint);font-size:13.5px}

/* db error */
.db-err{background:#fef2f2;border:1px solid #fecaca;border-radius:12px;padding:14px 18px;color:#991b1b;font-size:13px;margin-bottom:18px}
.db-err code{font-family:monospace;font-size:12px}

/* login */
.login-page{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg)}
.login-box{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:40px 36px;width:100%;max-width:370px;box-shadow:0 2px 6px rgba(24,22,18,.05),0 40px 80px -30px rgba(24,22,18,.14)}
.login-box .logo{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;color:var(--ink);margin-bottom:26px}
.login-box .logo img{width:28px;height:28px;border-radius:7px}
.login-box h1{font-size:20px;font-weight:700;margin-bottom:5px}
.login-box p{color:var(--muted);font-size:13.5px;margin-bottom:22px}
.login-box label{display:block;font-size:13px;font-weight:500;color:var(--ink2);margin-bottom:5px}
.login-box input[type=password]{width:100%;border:1px solid var(--line);border-radius:9px;padding:11px 14px;font-size:15px;font-family:inherit;outline:none;margin-bottom:13px;color:var(--ink)}
.login-box input:focus{border-color:var(--accent)}
.login-box button{width:100%;background:var(--ink);color:#fff;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit}
.err-msg{color:var(--danger);font-size:13px;margin-bottom:11px}

@media(max-width:860px){.grid3,.grid2,.grid2r{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>

<?php if (!$ok): ?>
<div class="login-page">
  <div class="login-box">
    <div class="logo"><img src="/favicon.png" alt=""> Lazytype</div>
    <h1>Admin</h1>
    <p>Bezoekers, aankopen en downloads in één overzicht.</p>
    <?php if (!empty($login_err)): ?><div class="err-msg"><?= h($login_err) ?></div><?php endif; ?>
    <form method="post">
      <label>Wachtwoord</label>
      <input type="password" name="pw" autofocus autocomplete="current-password">
      <button type="submit">Inloggen</button>
    </form>
  </div>
</div>

<?php else: ?>

<div class="topbar">
  <div class="brand">
    <img src="/favicon.png" alt=""> Lazytype
    <span class="badge-env">Admin</span>
  </div>
  <div class="right">
    <span><?= date('d M Y') ?></span>
    <a href="/admin/?logout=1">Uitloggen</a>
  </div>
</div>

<div class="main">

<?php if (!empty($db_err)): ?>
<div class="db-err">
  <strong>Database niet bereikbaar</strong> — Vul DB_NAME, DB_USER en DB_PASS in <code>api/config.php</code>.<br>
  Fout: <code><?= h($db_err) ?></code>
</div>
<?php endif; ?>

<!-- ═══════════════════════════════════════════════════ BEZOEKERS ══ -->
<div class="section-head"><h2>Bezoekers</h2><div class="line"></div></div>

<div class="stats">
  <div class="stat">
    <div class="lbl">Pageviews totaal</div>
    <div class="val"><?= fmt($vs['total'] ?? 0) ?></div>
    <div class="sub"><?= fmt($vs['uniq_total'] ?? 0) ?> uniek</div>
  </div>
  <div class="stat c-accent">
    <div class="lbl">Vandaag</div>
    <div class="val"><?= fmt($vs['today'] ?? 0) ?></div>
    <div class="sub"><?= fmt($vs['uniq_today'] ?? 0) ?> unieke bezoekers</div>
  </div>
  <div class="stat">
    <div class="lbl">Afgelopen 7 dagen</div>
    <div class="val"><?= fmt($vs['7d'] ?? 0) ?></div>
    <div class="sub"><?= fmt($vs['uniq_7d'] ?? 0) ?> uniek</div>
  </div>
  <div class="stat">
    <div class="lbl">Afgelopen 30 dagen</div>
    <div class="val"><?= fmt($vs['30d'] ?? 0) ?></div>
    <div class="sub">&nbsp;</div>
  </div>
</div>

<div class="grid3">

  <!-- Bezoekersgraﬁek -->
  <div class="panel">
    <h3>Pageviews — laatste 30 dagen</h3>
    <?php if ($pv_max > 0): ?>
    <div class="chart">
      <?php foreach ($pv_chart as $day => $row):
        $pct = $pv_max > 0 ? round($row['views'] / $pv_max * 100) : 0;
        $tip = date('j M', strtotime($day)) . ': ' . $row['views'] . ' views · ' . $row['uniq'] . ' uniek';
      ?>
      <div class="bw" data-tip="<?= h($tip) ?>">
        <div class="b views" style="height:<?= max($pct, $row['views'] > 0 ? 3 : 0) ?>%"></div>
      </div>
      <?php endforeach; ?>
    </div>
    <div class="chart-foot">
      <span><?= date('j M', strtotime('-29 days')) ?></span>
      <span>vandaag</span>
    </div>
    <?php else: ?>
    <div class="chart-empty">Nog geen bezoeken geregistreerd</div>
    <?php endif; ?>
  </div>

  <!-- Herkomst -->
  <div class="panel">
    <h3>Herkomst <span class="cnt">30 dagen</span></h3>
    <?php if (empty($top_sources)): ?>
    <div class="empty" style="padding:20px">Nog geen data</div>
    <?php else: ?>
    <div class="hbar-list">
      <?php foreach ($top_sources as $src => $n):
        $pct = $src_max > 0 ? round($n / $src_max * 100) : 0;
      ?>
      <div class="hbar-row">
        <div class="hbar-label" title="<?= h($src) ?>"><?= h($src) ?></div>
        <div class="hbar-track"><div class="hbar-fill" style="width:<?= $pct ?>%"></div></div>
        <div class="hbar-val"><?= fmt($n) ?></div>
      </div>
      <?php endforeach; ?>
    </div>
    <?php endif; ?>
  </div>

  <!-- Top pagina's -->
  <div class="panel">
    <h3>Top pagina's <span class="cnt">30 dagen</span></h3>
    <?php if (empty($top_pages)): ?>
    <div class="empty" style="padding:20px">Nog geen data</div>
    <?php else: ?>
    <div class="hbar-list">
      <?php
      $pg_max = max(array_column($top_pages, 'n'));
      foreach ($top_pages as $pg):
        $pct = $pg_max > 0 ? round($pg['n'] / $pg_max * 100) : 0;
        $label = $pg['page'] === '/' ? 'Homepage' : rtrim($pg['page'], '/');
      ?>
      <div class="hbar-row">
        <div class="hbar-label" title="<?= h($pg['page']) ?>"><?= h($label) ?></div>
        <div class="hbar-track"><div class="hbar-fill" style="width:<?= $pct ?>%"></div></div>
        <div class="hbar-val"><?= fmt((int)$pg['n']) ?></div>
      </div>
      <?php endforeach; ?>
    </div>
    <?php endif; ?>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════ AANKOPEN ══ -->
<div class="section-head"><h2>Downloads &amp; Aankopen</h2><div class="line"></div></div>

<div class="stats">
  <div class="stat">
    <div class="lbl">Downloads totaal</div>
    <div class="val"><?= fmt($ps['dl_all'] ?? 0) ?></div>
    <div class="sub">+<?= fmt($ps['dl_7d'] ?? 0) ?> deze week</div>
  </div>
  <div class="stat">
    <div class="lbl">Downloads vandaag</div>
    <div class="val"><?= fmt($ps['dl_today'] ?? 0) ?></div>
    <div class="sub">&nbsp;</div>
  </div>
  <div class="stat">
    <div class="lbl">Aankopen</div>
    <div class="val"><?= fmt($ps['purchases'] ?? 0) ?></div>
    <div class="sub"><?= fmt($ps['refunded'] ?? 0) ?> terugbetaald</div>
  </div>
  <div class="stat c-accent">
    <div class="lbl">Actieve Pro</div>
    <div class="val"><?= fmt($ps['active_pro'] ?? 0) ?></div>
    <div class="sub">Personal: <?= fmt($ps['personal'] ?? 0) ?></div>
  </div>
  <div class="stat c-green">
    <div class="lbl">MRR</div>
    <div class="val"><?= eur($ps['mrr'] ?? 0) ?></div>
    <div class="sub">maandelijks terugkerend</div>
  </div>
  <div class="stat c-green">
    <div class="lbl">Totale omzet</div>
    <div class="val"><?= eur($ps['revenue'] ?? 0) ?></div>
    <div class="sub">alle tijd</div>
  </div>
</div>

<div class="grid2">

  <!-- Downloads grafiek -->
  <div class="panel">
    <h3>Downloads — laatste 30 dagen</h3>
    <?php if ($dl_max > 0): ?>
    <div class="chart">
      <?php foreach ($dl_chart as $day => $n):
        $pct = $dl_max > 0 ? round($n / $dl_max * 100) : 0;
        $tip = date('j M', strtotime($day)) . ': ' . $n . ' downloads';
      ?>
      <div class="bw" data-tip="<?= h($tip) ?>">
        <div class="b dls" style="height:<?= max($pct, $n > 0 ? 3 : 0) ?>%"></div>
      </div>
      <?php endforeach; ?>
    </div>
    <div class="chart-foot">
      <span><?= date('j M', strtotime('-29 days')) ?></span>
      <span>vandaag</span>
    </div>
    <?php else: ?>
    <div class="chart-empty">Nog geen downloads geregistreerd</div>
    <?php endif; ?>
  </div>

  <!-- Laatste aankopen -->
  <div class="panel">
    <h3>Laatste aankopen</h3>
    <?php $recent = array_slice($purchases, 0, 8); ?>
    <?php if (empty($recent)): ?>
    <div class="empty">Nog geen aankopen</div>
    <?php else: ?>
    <div class="tbl-wrap">
    <table>
      <thead><tr><th>E-mail</th><th>Plan</th><th>Datum</th><th>Status</th></tr></thead>
      <tbody>
      <?php foreach ($recent as $p): ?>
      <tr>
        <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><?= h($p['email']) ?></td>
        <td><span class="pill pill-<?= h($p['plan']) ?>"><?= strtoupper(h($p['plan'])) ?></span></td>
        <td style="white-space:nowrap;color:var(--muted)"><?= date('d-m-Y', strtotime($p['created_at'])) ?></td>
        <td><span class="pill pill-<?= h($p['status']) ?>"><?= h($p['status']) ?></span></td>
      </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
    </div>
    <?php endif; ?>
  </div>

</div>

<!-- ═══════════════════════════════════════════════ GEBRUIKERS ══ -->
<div class="section-head"><h2>Gebruikers &amp; keys</h2><div class="line"></div></div>

<div class="panel">
  <form method="get" class="search-row">
    <input type="text" name="q" placeholder="Zoek op e-mail of license key…"
           value="<?= h($_GET['q'] ?? '') ?>" autocomplete="off">
    <button type="submit">Zoeken</button>
    <?php if (!empty($_GET['q'])): ?>
      <a href="/admin/" style="align-self:center;font-size:13px;color:var(--muted)">wis</a>
    <?php endif; ?>
  </form>

  <?php if (empty($purchases)): ?>
  <div class="empty">Geen resultaten<?= !empty($_GET['q']) ? ' voor "' . h($_GET['q']) . '"' : '' ?></div>
  <?php else: ?>
  <div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>#</th><th>E-mail</th><th>Plan</th><th>Bedrag</th>
      <th>License key</th><th>Status</th><th>Datum</th>
    </tr></thead>
    <tbody>
    <?php foreach ($purchases as $p): ?>
    <tr>
      <td style="color:var(--faint);font-size:12px"><?= (int)$p['id'] ?></td>
      <td><?= h($p['email']) ?></td>
      <td><span class="pill pill-<?= h($p['plan']) ?>"><?= strtoupper(h($p['plan'])) ?></span></td>
      <td style="white-space:nowrap"><?= $p['amount_cents'] ? eur((int)$p['amount_cents']) : '—' ?></td>
      <td class="mono"><?= h($p['license_key'] ?? '—') ?></td>
      <td><span class="pill pill-<?= h($p['status']) ?>"><?= h($p['status']) ?></span></td>
      <td style="white-space:nowrap;color:var(--muted)"><?= date('d-m-Y H:i', strtotime($p['created_at'])) ?></td>
    </tr>
    <?php endforeach; ?>
    </tbody>
  </table>
  </div>
  <p style="margin-top:10px;font-size:11.5px;color:var(--faint)"><?= count($purchases) ?> rij(en)</p>
  <?php endif; ?>
</div>

</div><!-- /main -->
<?php endif; ?>
</body>
</html>
