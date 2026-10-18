# Capture a window screenshot by window-title substring.
# ASCII only: Windows PowerShell 5.1 reads .ps1 files using the ANSI code page,
# so non-ASCII characters here would corrupt the parser.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\capture_window.ps1 -TitleLike AutoCat -Out shot.png
param(
    [string]$TitleLike = "AutoCat",
    [string]$Out = "shot.png"
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Cap {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

# Without this, a display scaled at 125%/150% makes GetWindowRect return
# virtualized coordinates, so the capture only covers part of the window.
[Win32Cap]::SetProcessDPIAware() | Out-Null

$proc = Get-Process | Where-Object { $_.MainWindowTitle -like "*$TitleLike*" } | Select-Object -First 1
if (-not $proc) {
    Write-Error "No window whose title contains '$TitleLike'"
    exit 1
}

Write-Host "window: [$($proc.MainWindowTitle)] pid=$($proc.Id)"

$h = $proc.MainWindowHandle
[Win32Cap]::ShowWindow($h, 9) | Out-Null
[Win32Cap]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 800

$rect = New-Object Win32Cap+RECT
[Win32Cap]::GetWindowRect($h, [ref]$rect) | Out-Null
$w = $rect.Right - $rect.Left
$hh = $rect.Bottom - $rect.Top
Write-Host "rect: $($rect.Left),$($rect.Top) size: ${w}x${hh}"

$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bmp.Size)

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

Write-Host "saved: $Out ($((Get-Item $Out).Length) bytes)"
