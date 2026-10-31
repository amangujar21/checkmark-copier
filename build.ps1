$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "Cleaning old build output..."
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }

Write-Host "Running tests..."
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed; build stopped." }

Write-Host "Building CheckmarkCopier.exe..."
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name CheckmarkCopier `
    --icon "assets\app.ico" `
    --add-data "assets;assets" `
    --version-file "version_info.txt" `
    --exclude-module kivy `
    --exclude-module kivymd `
    --exclude-module jnius `
    --exclude-module yaml `
    --exclude-module win32 `
    --exclude-module pyreadline3 `
    --exclude-module pywin32_system32 `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Copy-Item README.md dist\README.md -Force
Copy-Item LICENSE.txt dist\LICENSE.txt -Force
Copy-Item samples\demo.pdf dist\demo.pdf -Force
Compress-Archive -Path dist\CheckmarkCopier.exe, dist\README.md, dist\LICENSE.txt, dist\demo.pdf `
    -DestinationPath "dist\CheckmarkCopier-1.0.0-win64.zip" -Force

Write-Host ""
Write-Host "Built: $root\dist\CheckmarkCopier.exe"
Get-Item dist\CheckmarkCopier.exe | Select-Object FullName, Length, LastWriteTime | Format-List
Get-Item "dist\CheckmarkCopier-1.0.0-win64.zip" | Select-Object FullName, Length, LastWriteTime | Format-List
