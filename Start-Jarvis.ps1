param(
    [int]$DelaySeconds = 0,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$root    = 'C:\Users\Sahil Kori\Mark-LIV'
$main    = Join-Path $root 'main.py'
$pyw     = 'C:\Program Files\Python311\pythonw.exe'
$logFile = Join-Path $root 'logs\launcher.log'

function Write-Log([string]$m) {
    try {
        $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch { }
}

if (-not (Test-Path -LiteralPath $main)) {
    Write-Log "ABORT main.py not found at $main"
    exit 1
}
if (-not (Test-Path -LiteralPath $pyw)) {
    Write-Log "ABORT pythonw.exe not found at $pyw"
    exit 1
}

if ($DelaySeconds -gt 0) {
    Write-Log "waiting $DelaySeconds s before launch"
    Start-Sleep -Seconds $DelaySeconds
}

# An instance is live if a pythonw.exe is running main.py from this project.
$existing = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*main.py*" })

if ($existing.Count -gt 0) {
    $pid0 = $existing[0].ProcessId
    Write-Log "already running (PID $pid0) - focusing, not starting a second copy"

    if (-not $Quiet) {
        $sig = @'
using System;
using System.Runtime.InteropServices;
public class JFocus {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr p);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint fl);
}
'@
        Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue
        $p = Get-Process -Id $pid0 -ErrorAction SilentlyContinue
        if ($p -and $p.MainWindowHandle -ne 0) {
            $h = $p.MainWindowHandle
            $fgT = [JFocus]::GetWindowThreadProcessId([JFocus]::GetForegroundWindow(), [IntPtr]::Zero)
            $myT = [JFocus]::GetCurrentThreadId()
            [JFocus]::AttachThreadInput($myT, $fgT, $true) | Out-Null
            [JFocus]::ShowWindow($h, 9) | Out-Null
            [JFocus]::BringWindowToTop($h) | Out-Null
            [JFocus]::SetForegroundWindow($h) | Out-Null
            [JFocus]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, 0x0003) | Out-Null
            [JFocus]::SetWindowPos($h, [IntPtr](-2), 0, 0, 0, 0, 0x0003) | Out-Null
            [JFocus]::AttachThreadInput($myT, $fgT, $false) | Out-Null
        }
    }
    exit 0
}

Write-Log "starting JARVIS (pythonw main.py)"
Start-Process -FilePath $pyw -ArgumentList "`"$main`"" -WorkingDirectory $root | Out-Null
Write-Log "launch dispatched"
exit 0
