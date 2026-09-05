param([Parameter(Mandatory=$true)][string[]]$ScanPath)
$ErrorActionPreference = 'Stop'
$flintRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$flintResults = @()
foreach ($flintInput in $ScanPath) {
    $flintTarget = (Resolve-Path -LiteralPath $flintInput).Path
    if (-not $flintTarget.StartsWith($flintRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Scan only explicit artifact directories within this separate release.'
    }
    $flintBegin = Get-Date
    Start-MpScan -ScanType CustomScan -ScanPath $flintTarget
    $flintDeadline = (Get-Date).AddSeconds(45)
    $flintStart = $null
    $flintFinish = $null
    do {
        $flintEvents = @(Get-WinEvent -FilterHashtable @{
            LogName='Microsoft-Windows-Windows Defender/Operational';
            Id=1000,1001,1002; StartTime=$flintBegin
        } -ErrorAction SilentlyContinue | ForEach-Object {
            $flintXml = [xml]$_.ToXml()
            $flintFields = @{}
            foreach ($flintData in $flintXml.Event.EventData.Data) {
                $flintFields[$flintData.Name] = $flintData.'#text'
            }
            [pscustomobject]@{Id=$_.Id; Time=$_.TimeCreated; Fields=$flintFields}
        })
        $flintStart = $flintEvents | Where-Object {
            $_.Id -eq 1000 -and $_.Fields['Scan Resources'] -eq ('folder:_' + $flintTarget)
        } | Select-Object -First 1
        if ($flintStart) {
            $flintFinish = $flintEvents | Where-Object {
                $_.Id -eq 1001 -and $_.Fields['Scan ID'] -eq $flintStart.Fields['Scan ID']
            } | Select-Object -First 1
        }
        if (-not $flintFinish) { Start-Sleep -Seconds 1 }
    } while (-not $flintFinish -and (Get-Date) -lt $flintDeadline)
    if (-not $flintFinish) { throw 'Could not verify Defender completion for the requested artifact directory.' }
    $flintDetections = @(Get-MpThreatDetection | Where-Object {
        $flintResources = $_.Resources -join "`n"
        $flintResources.IndexOf($flintTarget, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    $flintResults += [pscustomobject]@{
        Scope=$flintTarget; ScanId=$flintStart.Fields['Scan ID'];
        Started=$flintStart.Time; Finished=$flintFinish.Time;
        Completed=$true; ScopedDetections=$flintDetections.Count
    }
}
$flintStatus = Get-MpComputerStatus
$flintReport = [pscustomobject]@{
    SignatureVersion=$flintStatus.AntivirusSignatureVersion;
    EngineVersion=$flintStatus.AMEngineVersion;
    AntivirusEnabled=$flintStatus.AntivirusEnabled;
    RealTimeProtectionEnabled=$flintStatus.RealTimeProtectionEnabled;
    Scans=$flintResults;
    Limitation='Local scan only; not a guarantee of future Defender, SmartScreen or other-machine results.'
}
$flintReport | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $flintRoot 'qa-defender.json') -Encoding utf8
$flintReport | ConvertTo-Json -Depth 5
if (@($flintResults | Where-Object { $_.ScopedDetections -gt 0 }).Count) { exit 1 }
