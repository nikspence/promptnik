$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\pythonw.exe')) {
    throw 'Create the environment first: py -3.11 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
& '.\.venv\Scripts\python.exe' -c 'import PySide6, keyring'
if ($LASTEXITCODE -ne 0) {
    throw 'Install the desktop dependencies first: .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
Start-Process -FilePath (Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe') -ArgumentList 'promptnik.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
