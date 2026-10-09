# =============================================================================
# install.ps1 — install this repo's tracked git hooks into local .git/hooks (Windows).
# Git hooks are never cloned, so EVERY checkout must run this once:
#
#     powershell -ExecutionPolicy Bypass -File tools\hooks\install.ps1
#
# The hooks themselves run under Git for Windows' bundled bash. CI enforces the
# same checks independently and cannot be bypassed.
# =============================================================================
$ErrorActionPreference = "Stop"

$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$hooksDir = Join-Path (git rev-parse --git-common-dir) "hooks"
if (-not (Test-Path $hooksDir)) { New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null }

foreach ($h in @("commit-msg", "pre-commit", "pre-push")) {
  Copy-Item -Force (Join-Path $src $h) (Join-Path $hooksDir $h)
  Write-Output "installed: $(Join-Path $hooksDir $h)"
}

Write-Output "Git hooks installed - owner-alone authorship + lint/format/type/secret/test gates active."
