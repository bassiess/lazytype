# Lazytype — projectinstructies voor Claude

## FTP deploy (Hostinger, public_html)

- **Host**: `82.29.191.209` / `ftp.lazytype.com`
- **Port**: `21`
- **User**: `u879082816.lazytypeftp`
- **Password**: `Lz7pK9mQ2wX4vR8t`

Upload alle site-bestanden (behalve de exe):

```powershell
$ftpBase = "ftp://82.29.191.209"
$ftpUser = "u879082816.lazytypeftp:Lz7pK9mQ2wX4vR8t"
$siteDir = "C:\Users\bniese\lazy typing\site"

Get-ChildItem -Path $siteDir -Recurse -File | Where-Object { $_.FullName -notlike "*\downloads\*" } | ForEach-Object {
    $relPath = $_.FullName.Substring($siteDir.Length + 1).Replace("\", "/")
    curl.exe --ftp-pasv --ftp-create-dirs -T $_.FullName "$ftpBase/$relPath" --user $ftpUser --silent --show-error
    Write-Host "Uploaded: $relPath"
}
```

Enkel bestand uploaden:
```powershell
curl.exe --ftp-pasv -T "site\index.html" "ftp://82.29.191.209/index.html" --user "u879082816.lazytypeftp:Lz7pK9mQ2wX4vR8t"
```

## Hosting

- **Provider**: Hostinger (Business plan, datacenter UK)
- **Live URL**: https://lazytype.com
- **Server**: LiteSpeed, hPanel
- **public_html** = root van de site (`site/` map in de repo)

## "Verwijder de app" (als ontwikkelaar)

Als Bas zegt "verwijder de app", doe dan dit PowerShell-script:

```powershell
# 1. Stop het proces
Get-Process | Where-Object { $_.Name -like "*Lazytype*" } | Stop-Process -Force -ErrorAction SilentlyContinue

# 2. Autostart uit register
$regPath = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
if (Get-ItemProperty -Path $regPath -Name "Lazytype" -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $regPath -Name "Lazytype"
}

# 3. Exe uit site/downloads
$exe = "C:\Users\bniese\lazy typing\site\downloads\Lazytype.exe"
if (Test-Path $exe) { Remove-Item $exe -Force }

# 4. AppData-map wissen (bevat .env met DICTATE_ONBOARDED, trial, history, verify-cache)
if (Test-Path "$env:APPDATA\Lazytype") { Remove-Item "$env:APPDATA\Lazytype" -Recurse -Force }

# 5b. .env naast de exe wissen (migratiebron: exe kopieert dit naar AppData vóór onboarding!)
$distEnv = "C:\Users\bniese\lazy typing\dist\.env"
if (Test-Path $distEnv) { Remove-Item $distEnv -Force; Write-Host "5b. dist\.env verwijderd." } else { Write-Host "5b. dist\.env niet gevonden." }
$dlEnv = "C:\Users\bniese\Downloads\.env"
if (Test-Path $dlEnv) { Remove-Item $dlEnv -Force; Write-Host "5c. Downloads\.env verwijderd." } else { Write-Host "5c. Downloads\.env niet gevonden." }

# 5. .env in projectmap (development-mode)
$envFile = "C:\Users\bniese\lazy typing\.env"
if (Test-Path $envFile) { Remove-Item $envFile -Force }

Write-Host "Klaar."
```

**NB:** de exe slaat alles op in `%APPDATA%\Lazytype\` (niet in de projectmap). Stap 4 is daarom cruciaal voor een schone herinstallatie met onboarding.

## Git

- **Repo**: https://github.com/bassiess/lazytype.git
- **Branch**: master
- **Taal**: Nederlands (communiceer in NL)

### GitHub account voor git push

`gh` heeft meerdere accounts. De repo is van `bassiess` — altijd eerst switchen voor een push:

```powershell
gh auth switch --hostname github.com --user bassiess
git push origin master
```
