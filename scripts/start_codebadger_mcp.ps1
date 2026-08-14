[CmdletBinding()]
param(
    [switch]$BootstrapOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom

function Get-BridgeSetting {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Default
    )

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function Get-PositiveBridgeInteger {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Default
    )

    $raw = Get-BridgeSetting -Name $Name -Default ([string]$Default)
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -le 0) {
        throw "$Name must be a positive integer, got '$raw'."
    }
    return $parsed
}

function Write-BridgeDiagnostic {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine("[codebadger-mcp] $Message")
}

function Format-NativeOutput {
    param([object[]]$Lines)
    if (-not $Lines -or $Lines.Count -eq 0) {
        return ""
    }
    return (($Lines | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
}

function Invoke-WslCaptured {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )

    # MCP clients may write `initialize` as soon as this launcher starts. Never
    # let bootstrap commands inherit that stream: wsl.exe otherwise forwards
    # and consumes it before the foreground STDIO proxy exists.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = "wsl.exe"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardInputEncoding = $utf8NoBom
    $startInfo.StandardOutputEncoding = $utf8NoBom
    $startInfo.StandardErrorEncoding = $utf8NoBom
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Failed to launch wsl.exe while $Description."
        }

        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Close()
        $process.WaitForExit()

        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
        $output = @($stdout, $stderr) | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_)
        }
    }
    finally {
        $process.Dispose()
    }

    if ($exitCode -ne 0) {
        $detail = Format-NativeOutput -Lines $output
        if ($detail) {
            throw ("$Description failed with exit code $exitCode" + [Environment]::NewLine + $detail)
        }
        throw "$Description failed with exit code $exitCode."
    }
}

function ConvertTo-WslMountedPath {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)

    $fullPath = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($fullPath -notmatch "^([A-Za-z]):[\\/](.*)$") {
        throw "The proxy script must reside on a Windows drive path, got '$fullPath'."
    }

    $drive = $Matches[1].ToLowerInvariant()
    $relative = $Matches[2].Replace("\", "/")
    return "/mnt/$drive/$relative"
}

$distro = Get-BridgeSetting -Name "CODEBADGER_WSL_DISTRO" -Default "Ubuntu"
$wslProjectDir = Get-BridgeSetting -Name "CODEBADGER_WSL_PROJECT_DIR" -Default "/home/otto/codebadger"
$composeProject = Get-BridgeSetting -Name "CODEBADGER_COMPOSE_PROJECT" -Default "codebadger"
$healthUrl = Get-BridgeSetting -Name "CODEBADGER_HEALTH_URL" -Default "http://127.0.0.1:4242/health"
$dockerNetwork = Get-BridgeSetting -Name "CODEBADGER_DOCKER_NETWORK" -Default "codebadger"
$proxyImage = Get-BridgeSetting -Name "CODEBADGER_PROXY_IMAGE" -Default "codebadger-mcp:latest"
$upstreamUrl = Get-BridgeSetting -Name "CODEBADGER_UPSTREAM_URL" -Default "http://codebadger-mcp:4242/mcp"
$toolProfile = Get-BridgeSetting -Name "CODEBADGER_TOOL_PROFILE" -Default "controller"
$bootstrapTimeoutSec = Get-PositiveBridgeInteger -Name "CODEBADGER_BOOT_TIMEOUT_SEC" -Default 180
$proxyScriptPath = Join-Path $PSScriptRoot "codebadger_mcp_proxy.py"
$composeOverridePath = Join-Path (Split-Path $PSScriptRoot -Parent) "docker-compose.codex.yml"

function Test-CodeBadgerHealth {
    try {
        $healthParams = @{
            Uri = $healthUrl
            Method = "Get"
            TimeoutSec = 3
            ErrorAction = "Stop"
        }
        $health = Invoke-RestMethod @healthParams
        return $health.status -eq "up"
    }
    catch {
        return $false
    }
}

function Start-CodeBadger {
    if (Test-CodeBadgerHealth) {
        return
    }

    $safeDistroName = $distro -replace "[^A-Za-z0-9_.-]", "_"
    $mutex = [System.Threading.Mutex]::new(
        $false,
        "Local\CodeBadgerMcpBootstrap-$safeDistroName"
    )
    $ownsMutex = $false

    try {
        try {
            $ownsMutex = $mutex.WaitOne(
                [TimeSpan]::FromSeconds($bootstrapTimeoutSec)
            )
        }
        catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }

        if (-not $ownsMutex) {
            throw "Timed out waiting for another CodeBadger bootstrap process."
        }

        # Another agent may have completed startup while this process waited.
        if (Test-CodeBadgerHealth) {
            return
        }

        Write-BridgeDiagnostic "Waking WSL distribution '$distro'."
        $dockerStartArgs = @(
            "-d", $distro,
            "-u", "root",
            "--",
            "systemctl", "start", "docker"
        )
        Invoke-WslCaptured -Arguments $dockerStartArgs -Description "Starting Ubuntu's native Docker service"

        if (-not (Test-CodeBadgerHealth)) {
            Write-BridgeDiagnostic "Starting the CodeBadger Compose project."
            $composeFile = $wslProjectDir.TrimEnd("/") + "/docker-compose.yml"
            $composeEnvFile = $wslProjectDir.TrimEnd("/") + "/.env"
            $wslComposeOverridePath = ConvertTo-WslMountedPath -WindowsPath $composeOverridePath
            $composeArgs = @(
                "-d", $distro,
                "--",
                "docker", "compose",
                "--project-name", $composeProject,
                "--project-directory", $wslProjectDir,
                "--env-file", $composeEnvFile,
                "--file", $composeFile,
                "--file", $wslComposeOverridePath,
                "up", "-d"
            )
            Invoke-WslCaptured -Arguments $composeArgs -Description "Starting the CodeBadger Compose project"
        }

        $deadline = [DateTime]::UtcNow.AddSeconds($bootstrapTimeoutSec)
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-CodeBadgerHealth) {
                Write-BridgeDiagnostic "CodeBadger is healthy."
                return
            }
            Start-Sleep -Milliseconds 750
        }

        throw "CodeBadger did not become healthy within $bootstrapTimeoutSec seconds."
    }
    finally {
        if ($ownsMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

try {
    if (-not (Test-Path -LiteralPath $proxyScriptPath -PathType Leaf)) {
        throw "Missing repository-local proxy script: $proxyScriptPath"
    }
    if (-not (Test-Path -LiteralPath $composeOverridePath -PathType Leaf)) {
        throw "Missing repository-local Compose override: $composeOverridePath"
    }

    Start-CodeBadger

    if ($BootstrapOnly) {
        Write-BridgeDiagnostic "Bootstrap-only health check succeeded."
        exit 0
    }

    $wslProxyPath = ConvertTo-WslMountedPath -WindowsPath $proxyScriptPath

    if (-not $wslProxyPath.StartsWith("/")) {
        throw "WSL returned an invalid proxy path: $wslProxyPath"
    }

    $proxyMount = $wslProxyPath + ":/opt/codebadger_mcp_proxy.py:ro"
    Write-BridgeDiagnostic "Starting the repository-scoped STDIO proxy."

    # This foreground wsl.exe process is the WSL lifetime lease for the agent.
    # When Codex closes stdin, FastMCP exits, Docker removes the proxy container,
    # and WSL may naturally stop once no other Windows/WSL work remains.
    $proxyArgs = @(
        "-d", $distro,
        "--",
        "docker", "run",
        "--rm",
        "-i",
        "--init",
        "--pull", "never",
        "--network", $dockerNetwork,
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "128",
        "--memory", "256m",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "FASTMCP_LOG_LEVEL=WARNING",
        "--env", "CODEBADGER_UPSTREAM_URL=$upstreamUrl",
        "--env", "CODEBADGER_TOOL_PROFILE=$toolProfile",
        "--volume", $proxyMount,
        "--label", "codebadger.role=codex-stdio-proxy",
        $proxyImage,
        "python", "-u", "/opt/codebadger_mcp_proxy.py"
    )
    & wsl.exe @proxyArgs

    exit $LASTEXITCODE
}
catch {
    Write-BridgeDiagnostic $_.Exception.Message
    exit 1
}
