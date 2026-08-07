[CmdletBinding()]
param(
    [string]$PreferredPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$candidates = [System.Collections.Generic.List[string]]::new()
$seen = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)

function Add-Candidate {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim('"'))
    if ($seen.Add($expanded)) {
        $candidates.Add($expanded)
    }
}

function Test-RejectedPath {
    param([string]$Path)

    return $Path -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]'
}

function Read-Probe {
    param([string]$Executable)

    if (Test-RejectedPath $Executable) {
        return $null
    }
    if (-not [IO.Path]::IsPathRooted($Executable)) {
        return $null
    }
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $null
    }

    try {
        $item = Get-Item -LiteralPath $Executable
        if ($item.Length -eq 0) {
            return $null
        }
        $probe = 'import sys; print(sys.version_info.major,sys.version.split()[0],sys.executable,sep=chr(9))'
        $output = @(& $Executable -I -c $probe 2>$null)
        if ($LASTEXITCODE -ne 0 -or $output.Count -ne 1) {
            return $null
        }
        $fields = ([string]$output[0]) -split "`t", 3
        if ($fields.Count -ne 3 -or $fields[0] -ne '3') {
            return $null
        }
        $resolved = $fields[2]
        if (-not [IO.Path]::IsPathRooted($resolved) -or (Test-RejectedPath $resolved)) {
            return $null
        }
        return [pscustomobject]@{
            executable = [IO.Path]::GetFullPath($resolved)
            version = $fields[1]
        }
    }
    catch {
        return $null
    }
}

Add-Candidate $PreferredPath

foreach ($entry in ([Environment]::GetEnvironmentVariable('PATH', 'Process') -split [IO.Path]::PathSeparator)) {
    if ($entry -match '(?i)[\\/]codex-runtimes[\\/][^\\/]+[\\/]dependencies[\\/]bin[\\/]override[\\/]?$') {
        $dependencies = Split-Path -Parent (Split-Path -Parent $entry)
        Add-Candidate (Join-Path $dependencies 'python\python.exe')
    }
}

$patterns = @(
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\*\dependencies\python\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Python\bin\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'),
    (Join-Path $env:ProgramFiles 'Python*\python.exe')
)
if (${env:ProgramFiles(x86)}) {
    $patterns += Join-Path ${env:ProgramFiles(x86)} 'Python*\python.exe'
}

foreach ($pattern in $patterns) {
    Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        ForEach-Object { Add-Candidate $_.FullName }
}

Get-Command python.exe -All -ErrorAction SilentlyContinue |
    ForEach-Object { Add-Candidate $_.Source }

foreach ($launcher in (Get-Command py.exe -All -ErrorAction SilentlyContinue)) {
    if (Test-RejectedPath $launcher.Source) {
        continue
    }
    try {
        $probe = 'import sys; print(sys.version_info.major,sys.executable,sep=chr(9))'
        $output = @(& $launcher.Source -3 -I -c $probe 2>$null)
        if ($LASTEXITCODE -eq 0 -and $output.Count -eq 1) {
            $fields = ([string]$output[0]) -split "`t", 2
            if ($fields.Count -eq 2 -and $fields[0] -eq '3') {
                Add-Candidate $fields[1]
            }
        }
    }
    catch {
        continue
    }
}

foreach ($candidate in $candidates) {
    $verified = Read-Probe $candidate
    if ($null -ne $verified) {
        [ordered]@{
            schema_version = 1
            status = 'valid'
            python = $verified.executable
            version = $verified.version
        } | ConvertTo-Json -Compress
        exit 0
    }
}

[Console]::Error.WriteLine(
    'No usable Python 3 runtime was found after rejecting WindowsApps aliases and probing every permitted absolute candidate.'
)
exit 1
