# Smoke-test dist\Loom.exe: boot headless, verify the API answers, kill it.
# Run: powershell -File scripts\smoke_exe.ps1
# curl.exe ONLY — Invoke-WebRequest false-fails in non-interactive shells on this machine.

param([int]$Port = 8878)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path "dist\Loom.exe")) {
    throw "dist\Loom.exe not found - run scripts\build_exe.ps1 first"
}

$proc = Start-Process -FilePath "dist\Loom.exe" -ArgumentList "ui", "--no-open", "--port", $Port -PassThru
try {
    $ok = $false
    foreach ($i in 1..120) {   # onefile exes unpack first; allow up to 60 s
        Start-Sleep -Milliseconds 500
        $code = & curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:$Port/api/state"
        if ($code -eq "200") { $ok = $true; break }
    }
    if (-not $ok) { throw "Loom.exe did not serve /api/state within 60 s" }

    $skills = & curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:$Port/api/skills"
    if ($skills -ne "200") { throw "/api/skills returned HTTP $skills" }

    Write-Host "SMOKE OK: /api/state and /api/skills both 200 on port $Port"
}
finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}
