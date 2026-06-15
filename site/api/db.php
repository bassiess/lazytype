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
    try { $db->exec("ALTER TABLE purchases MODIFY license_key VARCHAR(400)"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases MODIFY plan ENUM('personal','pro','lifetime') NOT NULL"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases ADD COLUMN device_id  VARCHAR(64) DEFAULT NULL"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE purchases ADD COLUMN device_id_2 VARCHAR(64) DEFAULT NULL"); } catch (\Exception $e) {}
    // Migreer trials: 2 device-slots + reminded_at
    try { $db->exec("ALTER TABLE trials ADD COLUMN device_2    VARCHAR(64) DEFAULT NULL"); } catch (\Exception $e) {}
    try { $db->exec("ALTER TABLE trials ADD COLUMN reminded_at DATETIME    DEFAULT NULL"); } catch (\Exception $e) {}

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
