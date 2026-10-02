# Build a single-file Loom.exe - the shareable desktop app.
# Run from anywhere:  powershell -File scripts\build_exe.ps1
# Output: dist\Loom.exe (double-click = desktop app; also works as the `loom` CLI).

# ASCII only in this file: PowerShell 5.1 parses BOM-less files as ANSI and a UTF-8
# em-dash becomes a string-terminator parse error.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

.\.venv\Scripts\python.exe -m pip install -e ".[desktop,build]" -q

.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm --onefile --windowed --name Loom `
    --add-data "src/loom/server/static;loom/server/static" `
    --add-data "src/loom/builtin_skills;loom/builtin_skills" `
    --collect-all litellm `
    --collect-all tiktoken `
    --collect-all tiktoken_ext `
    --hidden-import "tiktoken_ext" `
    --hidden-import "tiktoken_ext.openai_public" `
    --hidden-import "loom.tools.fs" `
    --hidden-import "loom.tools.search" `
    --hidden-import "loom.tools.shell" `
    scripts/loom_desktop.py

Write-Host ""
Write-Host "Built dist\Loom.exe - share that single file; no Python needed on the target PC."
