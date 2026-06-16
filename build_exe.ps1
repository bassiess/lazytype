# Bouwt Lazytype.exe (standalone, geen Python nodig) en zet hem klaar
# in de download-map van de website. Draai: powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Bouwen met PyInstaller (zonder UPX, met version-info)..." -ForegroundColor Cyan
# --noupx        : UPX-compressie geeft veel AV/SmartScreen vals-positieven -> uit
# --version-file : nette publisher/omschrijving in de exe (oogt minder verdacht)
python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
  --name Lazytype --icon icon.ico --version-file version.txt `
  --collect-all sounddevice --collect-all pystray `
  dictate_tray.py

# Optioneel ondertekenen = de échte fix voor de SmartScreen-waarschuwing.
# Zet LAZYTYPE_PFX (pad naar .pfx) + LAZYTYPE_PFX_PASS, of gebruik Azure Trusted Signing.
if ($env:LAZYTYPE_PFX -and (Test-Path $env:LAZYTYPE_PFX)) {
  $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
              Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
  if ($signtool) {
    Write-Host "Ondertekenen met certificaat..." -ForegroundColor Cyan
    & $signtool sign /fd SHA256 /f $env:LAZYTYPE_PFX /p $env:LAZYTYPE_PFX_PASS `
      /tr http://timestamp.digicert.com /td SHA256 "dist\Lazytype.exe"
  } else {
    Write-Host "signtool.exe niet gevonden (installeer de Windows SDK)." -ForegroundColor Yellow
  }
} else {
  Write-Host "Niet ondertekend (geen LAZYTYPE_PFX gezet) -> SmartScreen kan nog waarschuwen." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force "site\downloads" | Out-Null
Copy-Item "dist\Lazytype.exe" "site\downloads\Lazytype.exe" -Force
$mb = [math]::Round((Get-Item "dist\Lazytype.exe").Length/1MB,1)

# SHA256-hash genereren naast de exe (wordt gedownload door de updater ter verificatie)
$hash = (Get-FileHash "site\downloads\Lazytype.exe" -Algorithm SHA256).Hash.ToLower()
"$hash  Lazytype.exe" | Out-File -Encoding ascii "site\downloads\sha256.txt"
Write-Host "SHA256: $hash" -ForegroundColor DarkGray

Write-Host "Klaar: dist\Lazytype.exe ($mb MB) -> ook in site\downloads\" -ForegroundColor Green
