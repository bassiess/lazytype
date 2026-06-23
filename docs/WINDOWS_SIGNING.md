# Windows code-signing — Azure Artifact Signing

Doel: de Lazytype-exe ondertekenen zodat Windows "Onbekende uitgever" vervangt
door **Lazytype** en de SmartScreen-/"onveilige download"-meldingen na verloop
van downloads verdwijnen. (Let op: er is in 2026 géén certificaat dat de melding
*direct* wegneemt — reputatie bouwt op via downloadvolume, maar hecht zich aan je
certificaat zodat nieuwe versies snel vertrouwd blijven.)

De pipeline staat klaar in `.github/workflows/windows-build.yml`. Die bouwt de exe
altijd en **ondertekent zodra de onderstaande GitHub-secrets bestaan** (anders bouwt
'ie ongetekend — handig om te testen).

## Eenmalige setup (jouw kant — vereist betaling + bedrijfsverificatie)

1. **Azure-subscription** (betaald) — https://portal.azure.com. Artifact Signing
   werkt niet op gratis/trial-subscriptions.
2. **Artifact Signing-account** aanmaken (zoek in de portal op *"Trusted Signing"*
   / *"Artifact Signing"*). Kies regio **West Europe** (NL). Onthoud de
   **account-naam** en de **endpoint** (bv. `https://weu.codesigning.azure.net/`).
3. **Identiteit verifiëren** als *organisatie* (jouw bedrijf). Dit kan een paar
   dagen duren — dit is de naam die straks als uitgever in Windows verschijnt.
4. **Certificate Profile** aanmaken (type **Public Trust**). Onthoud de
   **profielnaam**.
5. **App-registratie** (Microsoft Entra ID → App registrations → New). Onthoud
   **Application (client) ID**, **Directory (tenant) ID** en je **Subscription ID**.
6. **Rol toekennen**: geef die app op het Artifact Signing-account de rol
   **"Trusted Signing Certificate Profile Signer"** (Access control (IAM) → Add role
   assignment).
7. **OIDC koppelen** (geen wachtwoord nodig): App registration → *Certificates &
   secrets* → *Federated credentials* → *Add* → GitHub Actions → organisatie
   `bassiess`, repo `lazytype`, branch/tag naar keuze (bv. environment of `ref:refs/tags/v*`).

## GitHub-secrets toevoegen

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Waarde |
|---|---|
| `AZURE_CLIENT_ID` | Application (client) ID van de app-registratie |
| `AZURE_TENANT_ID` | Directory (tenant) ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |
| `SIGNING_ENDPOINT` | bv. `https://weu.codesigning.azure.net/` |
| `SIGNING_ACCOUNT_NAME` | naam van je Artifact Signing-account |
| `SIGNING_PROFILE_NAME` | naam van je Certificate Profile |

Zodra deze zes bestaan, ondertekent de "Build Windows"-workflow automatisch.

## Gebruiken

GitHub → **Actions → "Build Windows" → Run workflow**. Resultaat: een ondertekende
`Lazytype.exe` als artifact (+ `sha256.txt`). Daarna wordt die via FTP gedeployd
naar `lazytype.com/downloads/` (zoals nu).

## Kosten

~$10/maand (Azure Artifact Signing). Geen hardware-token, geen jaarcertificaat-gedoe.
