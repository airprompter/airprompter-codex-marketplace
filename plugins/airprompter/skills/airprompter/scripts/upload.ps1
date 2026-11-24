# Bundled skill-upload entrypoint. Python3 must already be available; never install a runtime.
$runtime = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $runtime) { Write-Output '{"status":"error","code":"python3_required","message":"Python 3 is not available here."}'; exit 1 }
& $runtime.Source -B (Join-Path $PSScriptRoot 'upload_runtime.py') @args
exit $LASTEXITCODE
