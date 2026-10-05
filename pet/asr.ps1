# Windows built-in speech recognition (System.Speech) - zero dependency, fully offline.
# The recognized text is written to a UTF-8 text file, and a short status line
# ("ok|<culture>" / "nomatch" / "error: ...") to another file.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI
# unless the file has a BOM, so non-ASCII characters here break the parser.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File asr.ps1 -Mode list -OutPath <txt> [-StatusPath <txt>]
#   powershell -NoProfile -ExecutionPolicy Bypass -File asr.ps1 -Mode listen -OutPath <txt> -StatusPath <txt> [-Seconds 6] [-Culture zh-CN]

param(
    [string]$Mode = "listen",
    [double]$Seconds = 6.0,
    [string]$Culture = "zh-CN",
    [Parameter(Mandatory = $true)][string]$OutPath,
    [string]$StatusPath = ""
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object Text.UTF8Encoding($false)

function Write-Status([string]$Text) {
    if ($StatusPath) { [IO.File]::WriteAllText($StatusPath, $Text, $utf8) }
}

function Write-Result([string]$Text) {
    [IO.File]::WriteAllText($OutPath, $Text, $utf8)
}

try {
    Add-Type -AssemblyName System.Speech
}
catch {
    Write-Result ""
    Write-Status ("error: cannot load System.Speech - " + $_.Exception.Message)
    exit 1
}

$installed = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()

if ($Mode -eq "list") {
    $rows = @()
    foreach ($info in $installed) { $rows += ($info.Culture.Name + " | " + $info.Name) }
    Write-Result ($rows -join "`r`n")
    Write-Status "ok"
    exit 0
}

# Pick a recognizer: exact culture, else same language, else the first one installed.
$picked = $null
foreach ($info in $installed) {
    if ($info.Culture.Name -eq $Culture) { $picked = $info; break }
}
if (-not $picked -and $Culture) {
    $prefix = $Culture.Split("-")[0]
    foreach ($info in $installed) {
        if ($info.Culture.Name.StartsWith($prefix)) { $picked = $info; break }
    }
}
if (-not $picked -and $installed.Count -gt 0) { $picked = $installed[0] }

if (-not $picked) {
    Write-Result ""
    Write-Status "error: no speech recognizer installed"
    exit 1
}

$engine = $null
try {
    $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($picked)
    $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
    $engine.SetInputToDefaultAudioDevice()
    $engine.BabbleTimeout = [TimeSpan]::FromSeconds(1.0)
    $silence = [Math]::Max(1.0, [Math]::Min(5.0, $Seconds))
    $engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds($silence)
    $result = $engine.Recognize([TimeSpan]::FromSeconds($Seconds))
    if ($null -eq $result) {
        Write-Result ""
        Write-Status "nomatch"
    }
    else {
        Write-Result $result.Text
        Write-Status ("ok|" + $picked.Culture.Name)
    }
}
catch {
    Write-Result ""
    Write-Status ("error: " + $_.Exception.Message)
    exit 1
}
finally {
    if ($engine) { $engine.Dispose() }
}
