$ErrorActionPreference = "Stop"

$aePy = Join-Path $PSScriptRoot "ae.py"

if (-not (Test-Path -LiteralPath $aePy)) {
    throw "Cannot find ae.py: $aePy"
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Install Python first."
}

$start = "# >>> ae setup >>>"
$end = "# <<< ae setup <<<"
$block = @"
$start
function ae {
    param([Parameter(ValueFromRemainingArguments = `$true)][string[]] `$AeArgs)

    `$aePy = '$($aePy.Replace("'", "''"))'

    if (`$AeArgs.Count -eq 0) {
        python `$aePy '$((Join-Path $PSScriptRoot "content.md").Replace("'", "''"))'
    } else {
        python `$aePy @AeArgs
    }
}
$end
"@

$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path -LiteralPath $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}

$profileText = if (Test-Path -LiteralPath $PROFILE) {
    Get-Content -LiteralPath $PROFILE -Raw -Encoding UTF8
} else {
    ""
}

$profileText = [regex]::Replace($profileText, "(?s)\r?\n?" + [regex]::Escape($start) + ".*?" + [regex]::Escape($end) + "\r?\n?", "`r`n").TrimEnd()
if ($profileText.Length -gt 0) {
    $profileText += "`r`n`r`n"
}

Set-Content -LiteralPath $PROFILE -Value ($profileText + $block + "`r`n") -Encoding UTF8

if ((Get-ExecutionPolicy -Scope CurrentUser) -in @("Undefined", "Restricted")) {
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
}

Write-Host "ae installed. Restart PowerShell or run: . `$PROFILE"
