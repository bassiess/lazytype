<?php
require_once __DIR__ . '/config.php';

function get_db(): PDO {
    static $pdo = null;
    if ($pdo === null) {
        $dsn = 'mysql:host=' . DB_HOST . ';dbname=' . DB_NAME . ';charset=utf8mb4';
        $pdo = new PDO($dsn, DB_USER, DB_PASS, [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        ]);
    }
    return $pdo;
}

function init_db(): void {
    $db = get_db();
    $db->exec("
        CREATE TABLE IF NOT EXISTS purchases (
            id                     INT AUTO_INCREMENT PRIMARY KEY,
            stripe_session_id      VARCHAR(255) UNIQUE,
            stripe_customer_id     VARCHAR(255),
            stripe_subscription_id VARCHAR(255),
            email                  VARCHAR(255) NOT NULL,
            plan                   ENUM('personal','pro') NOT NULL,
            amount_cents           INT DEFAULT 0,
            currency               VARCHAR(10) DEFAULT 'eur',
            license_key            VARCHAR(64) UNIQUE,
            status                 ENUM('active','cancelled','refunded') DEFAULT 'active',
            created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
            cancelled_at           DATETIME NULL,
            INDEX idx_email  (email),
            INDEX idx_key    (license_key),
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    $db->exec("
        CREATE TABLE IF NOT EXISTS trials (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            email       VARCHAR(255) NOT NULL,
            device      VARCHAR(64)  DEFAULT '',
            license_key TEXT         NOT NULL,
            expires_at  DATETIME     NOT NULL,
            created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY  uq_email (email),
            INDEX idx_expires (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    // Migreer purchases: license_key verbreden + 2 device-slots + lifetime plan
    // Een HMAC-licentiesleutel is veel langer dan 64 tekens. De oorspronkelijke
    // VARCHAR(64) UNIQUE-index kan een MODIFY naar VARCHAR(400) blokkeren (utf8mb4
    // index-limiet) → sleutel zou stil afgekapt worden. Daarom eerst de unique-index
    // vervangen door een prefix-index, dán de kolom verbreden.
    try { $db->exec("ALTER TABLE purchases DROP INDEX license_key"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases MODIFY license_key VARCHAR(400)"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases ADD UNIQUE KEY uq_license (license_key(191))"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases MODIFY plan ENUM('personal','pro','lifetime') NOT NULL"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases ADD COLUMN device_id  VARCHAR(64) DEFAULT NULL"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases ADD COLUMN device_id_2 VARCHAR(64) DEFAULT NULL"); } catch (\Exception $e) {}
    // Payment-intent opslaan zodat charge.refunded de juiste aankoop kan matchen.
    try { $db->exec("ALTER TABLE purchases ADD COLUMN stripe_payment_intent VARCHAR(255) DEFAULT NULL"); } catch (\Exception $e) {}
    // Migreer trials: 2 device-slots + reminded_at
    try { $db->exec("ALTER TABLE trials ADD COLUMN device_2    VARCHAR(64) DEFAULT NULL"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE trials ADD COLUMN reminded_at DATETIME    DEFAULT NULL"); } catch (\Exception $e) {}

    // E-mailverificatie: tijdelijke 6-cijferige codes (gehasht). Één actieve code per adres.
    $db->exec("
        CREATE TABLE IF NOT EXISTS email_codes (
            email      VARCHAR(255) NOT NULL,
            code_hash  VARCHAR(64)  NOT NULL,
            attempts   INT          DEFAULT 0,
            expires_at DATETIME     NOT NULL,
            created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_email (email),
            INDEX idx_expires (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");

    $db->exec("
        CREATE TABLE IF NOT EXISTS downloads (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            ip         VARCHAR(45),
            user_agent TEXT,
            referer    VARCHAR(500),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_date (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    $db->exec("
        CREATE TABLE IF NOT EXISTS page_views (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            page         VARCHAR(500) NOT NULL,
            referrer     VARCHAR(500) DEFAULT '',
            utm_source   VARCHAR(200) DEFAULT '',
            utm_medium   VARCHAR(200) DEFAULT '',
            utm_campaign VARCHAR(200) DEFAULT '',
            ip           VARCHAR(45),
            user_agent   TEXT,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_date   (created_at),
            INDEX idx_page   (page(120)),
            INDEX idx_source (utm_source)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    $db->exec("
        CREATE TABLE IF NOT EXISTS rate_limits (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            key_hash   VARCHAR(64) NOT NULL,
            endpoint   VARCHAR(32) NOT NULL DEFAULT 'transcribe',
            created_at DATETIME    DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_key_time (key_hash, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    // Maand-tellers per sleutel (fair-use voor betaalde tiers). Apart van
    // rate_limits, want die wordt na 48u opgeschoond — een maandtotaal heeft
    // historie nodig. Eén rij per (sleutel, maand 'YYYY-MM').
    $db->exec("
        CREATE TABLE IF NOT EXISTS usage_counters (
            key_hash   VARCHAR(64) NOT NULL,
            period     CHAR(7)     NOT NULL,
            cnt        INT         DEFAULT 0,
            updated_at DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (key_hash, period)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    // Purge verlopen entries (~1% kans per aanroep)
    if (rand(1, 100) === 1) {
        $db->exec("DELETE FROM rate_limits    WHERE created_at < DATE_SUB(NOW(), INTERVAL 48 HOUR)");
        $db->exec("DELETE FROM usage_counters WHERE updated_at < DATE_SUB(NOW(), INTERVAL 95 DAY)");
    }

    // Globale stats — cumulatief woorden-teller (alleen optellend).
    $db->exec("
        CREATE TABLE IF NOT EXISTS global_stats (
            id          INT PRIMARY KEY DEFAULT 1,
            total_words BIGINT UNSIGNED DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ");
    try { $db->exec("INSERT IGNORE INTO global_stats (id, total_words) VALUES (1, 0)"); } catch (\Exception $e) {}
}

function generate_key(): string {
    // Format: LT-XXXX-XXXX-XXXX-XXXX  (alfanumeriek zonder O,0,I,1)
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    $key = 'LT';
    for ($i = 0; $i < 4; $i++) {
        $key .= '-';
        for ($j = 0; $j < 4; $j++) {
            $key .= $chars[random_int(0, strlen($chars) - 1)];
        }
    }
    return $key;
}
