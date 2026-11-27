# Build the Windows package:  powershell -ExecutionPolicy Bypass -File build.ps1
# Result: dist\DrShafiqAqilClinic-<date>.zip  (unzip and run DrShafiqAqilClinic.exe)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".venv\Scripts\python.exe"
$name = "DrShafiqAqilClinic"   # must match the name in pharmasoft.spec

Write-Host "Running tests..."
& $py -m unittest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building." }

Write-Host "Building with PyInstaller..."
& $py -m PyInstaller --noconfirm --clean --log-level WARN pharmasoft.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Copy-Item packaging\README-client.txt "dist\$name\README.txt" -Force

$zip = "dist\$name-$(Get-Date -Format yyyy-MM-dd).zip"
if (Test-Path $zip) { Remove-Item $zip -Confirm:$false }
# Antivirus often scans fresh build files for a few seconds; retry instead of failing.
for ($i = 1; $i -le 5; $i++) {
    & $py -c "import shutil; shutil.make_archive(r'$($zip -replace '\.zip$','')', 'zip', 'dist', '$name')"
    if ($LASTEXITCODE -eq 0) { break }
    Write-Host "Zip attempt $i failed (files busy), retrying..."; Start-Sleep -Seconds 5
}
if ($LASTEXITCODE -ne 0) { throw "Could not create $zip" }
Write-Host "Done: $zip  ($([math]::Round((Get-Item $zip).Length / 1MB)) MB)"
