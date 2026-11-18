<#
.SYNOPSIS
    fetch -> rebase -> push, working around three problems on this machine.

.DESCRIPTION
    This machine runs shell commands at Low integrity level, which breaks the
    git credential helper (it shells out to msys `sh`, which cannot fork), and
    GitHub:443 is intermittently unreachable. On top of that, a CI bot commits
    to `main`, so a plain push is often rejected as non-fast-forward.

    The workaround is to hand git an Authorization header directly, so no
    credential helper ever runs, and to retry with a low-speed timeout so a dead
    connection fails in 25s instead of hanging.

.EXAMPLE
    .\tools\push.ps1
    .\tools\push.ps1 -Message "docs: typos"   # commit everything first
#>
param(
    [string]$Message,
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [int]$Attempts = 6
)

# Deliberately "Continue", not "Stop": in Windows PowerShell 5.1 a native
# command writing to stderr becomes an ErrorRecord, and with "Stop" a plain
# `git fetch` that merely warns would abort the script. Correctness is enforced
# with explicit $LASTEXITCODE checks instead.
$ErrorActionPreference = "Continue"

if ($Message) {
    git add -A
    git commit -m $Message
    if ($LASTEXITCODE -ne 0) { Write-Host "nothing to commit"; }
}

$token = (gh auth token 2>$null)
if (-not $token) { throw "gh is not logged in. Run: gh auth login" }
$token = $token.Trim()

$repoUrl = (git remote get-url $Remote).Trim()
if ($repoUrl -match "github\.com[:/](?<owner>[^/]+)/(?<repo>[^/.]+)") {
    $slug = "$($Matches.owner)/$($Matches.repo)"
} else {
    throw "cannot parse a github owner/repo out of $repoUrl"
}

$b64 = [Convert]::ToBase64String(
    [Text.Encoding]::ASCII.GetBytes("x-access-token:$token")
)
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "http.extraheader"
$env:GIT_CONFIG_VALUE_0 = "AUTHORIZATION: basic $b64"
$env:GIT_HTTP_LOW_SPEED_LIMIT = "1000"
$env:GIT_HTTP_LOW_SPEED_TIME = "25"

$target = (git rev-parse HEAD).Substring(0, 7)
$pushed = $false

try {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        git fetch $Remote $Branch 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $behind = git rev-list --count "HEAD..$Remote/$Branch"
            if ($behind -gt 0) {
                Write-Host "rebasing onto $Remote/$Branch (+$behind)"
                git rebase "$Remote/$Branch" 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "rebase stopped - resolve conflicts, then run 'git rebase --continue'"
                    exit 1
                }
            }
            git push $Remote $Branch 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
        }
        Write-Host "attempt $attempt failed, retrying..."
        Start-Sleep -Seconds 6
    }
} finally {
    Remove-Item Env:\GIT_CONFIG_COUNT, Env:\GIT_CONFIG_KEY_0, `
        Env:\GIT_CONFIG_VALUE_0, Env:\GIT_HTTP_LOW_SPEED_LIMIT, `
        Env:\GIT_HTTP_LOW_SPEED_TIME -ErrorAction SilentlyContinue
}

if (-not $pushed) {
    throw "push failed after $Attempts attempts. GitHub:443 is probably down; try again later."
}

# Trust the API, not the exit code: $LASTEXITCODE through a pipeline lies here.
$remoteHead = (gh api "repos/$slug/commits/$Branch" | ConvertFrom-Json).sha
if ($remoteHead.StartsWith($target)) {
    Write-Host "pushed $target -> $slug@$Branch (confirmed via API)"
} else {
    Write-Host "push reported success but remote is at $($remoteHead.Substring(0,7))"
    exit 1
}
