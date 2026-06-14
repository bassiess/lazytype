<?php
/**
 * Lazytype server-config — kopieer naar  api/config.php  op de server (Hostinger)
 * en vul je echte waarden in. config.php staat in .gitignore en wordt NOOIT
 * meegeleverd in de client of de repo.
 *
 * transcribe.php leest deze variabelen als ze niet al als env-var bestaan.
 */

// Zelfde geheim als LAZYTYPE_LICENSE_SECRET in je lokale .env (uit admin.py).
$SECRET = 'ZET-HIER-HETZELFDE-GEHEIM-ALS-IN-ADMIN-.ENV';

// Jouw Groq API-key — blijft server-side, komt nooit bij de klant.
$GROQ = 'gsk_zet-hier-je-groq-key';

// ── Lemon Squeezy webhook (api/webhook.php) ─────────────────────────────
// Signing secret uit Lemon Squeezy → Settings → Webhooks.
$LS_WEBHOOK_SECRET = 'zet-hier-het-ls-webhook-signing-secret';
// Variant-id's van je producten (Lemon Squeezy → Products → variant). Zo weet de
// webhook of een order Personal is; Pro loopt via subscription_created.
$LS_VARIANT_PERSONAL = '000000';   // variant-id van Personal (€25 eenmalig)
$LS_VARIANT_PRO      = '000000';   // variant-id van Pro (abonnement)

// ── OF: Stripe webhook (api/webhook_stripe.php) ─────────────────────────
// Kies Lemon Squeezy OF Stripe. Stripe = jij regelt zelf de BTW (zie webhook_stripe.php).
// Signing secret (whsec_…) uit Stripe → Developers → Webhooks.
$STRIPE_WEBHOOK_SECRET = 'whsec_zet-hier-het-stripe-signing-secret';
