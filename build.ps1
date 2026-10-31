$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "Cleaning old build output..."
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }

Write-Host "Running tests..."
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed; build stopped." }

$excludes = @(
    "--exclude-module", "kivy",
    "--exclude-module", "kivymd",
    "--exclude-module", "jnius",
    "--exclude-module", "yaml",
    "--exclude-module", "win32",
    "--exclude-module", "pyreadline3",
    "--exclude-module", "pywin32_system32"
)

Write-Host "Building CheckmarkCopier.exe..."
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name CheckmarkCopier `
    --icon "assets\app.ico" `
    --add-data "assets;assets" `
    --version-file "version_info.txt" `
    @excludes `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Write-Host "Building portable (one-folder) variant..."
python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name CheckmarkCopier `
    --icon "assets\app.ico" `
    --add-data "assets;assets" `
    --version-file "version_info.txt" `
    @excludes `
    --distpath "dist\portable" `
    --workpath "build\portable" `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (one-folder) failed." }

Copy-Item README.md dist\README.md -Force
Copy-Item LICENSE.txt dist\LICENSE.txt -Force
Copy-Item samples\demo.pdf dist\demo.pdf -Force
Compress-Archive -Path dist\CheckmarkCopier.exe, dist\README.md, dist\LICENSE.txt, dist\demo.pdf `
    -DestinationPath "dist\CheckmarkCopier-1.0.0-win64.zip" -Force

Move-Item "dist\portable\CheckmarkCopier" "dist\CheckmarkCopier-portable"
Remove-Item "dist\portable" -Recurse -Force
Copy-Item README.md dist\CheckmarkCopier-portable\README.md -Force
Copy-Item LICENSE.txt dist\CheckmarkCopier-portable\LICENSE.txt -Force
Copy-Item samples\demo.pdf dist\CheckmarkCopier-portable\demo.pdf -Force
Compress-Archive -Path "dist\CheckmarkCopier-portable" `
    -DestinationPath "dist\CheckmarkCopier-1.0.0-win64-portable.zip" -Force

Write-Host ""
Write-Host "Built: $root\dist\CheckmarkCopier.exe"
Get-Item dist\CheckmarkCopier.exe | Select-Object FullName, Length, LastWriteTime | Format-List
Get-Item "dist\CheckmarkCopier-1.0.0-win64.zip" | Select-Object FullName, Length, LastWriteTime | Format-List
Write-Host "Built: $root\dist\CheckmarkCopier-portable\CheckmarkCopier.exe"
Get-Item "dist\CheckmarkCopier-1.0.0-win64-portable.zip" | Select-Object FullName, Length, LastWriteTime | Format-List
