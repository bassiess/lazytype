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

## Git

- **Repo**: https://github.com/bassiess/lazytype.git
- **Branch**: master
- **Taal**: Nederlands (communiceer in NL)
