# Generate the AutoCat application icon.
# Original artwork drawn with System.Drawing; no external assets or licenses.
# ASCII only: Windows PowerShell 5.1 reads .ps1 files using the ANSI code page.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_icon.ps1

param(
    [string]$OutDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "assets")
)

Add-Type -AssemblyName System.Drawing

$script:Size = 512
$script:Bmp = New-Object System.Drawing.Bitmap $script:Size, $script:Size
$g = [System.Drawing.Graphics]::FromImage($script:Bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$g.Clear([System.Drawing.Color]::Transparent)

# Rounded square background with a deep teal gradient.
$rect = New-Object System.Drawing.RectangleF 8, 8, 496, 496
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$r = 108
$path.AddArc($rect.X, $rect.Y, $r, $r, 180, 90)
$path.AddArc($rect.Right - $r, $rect.Y, $r, $r, 270, 90)
$path.AddArc($rect.Right - $r, $rect.Bottom - $r, $r, $r, 0, 90)
$path.AddArc($rect.X, $rect.Bottom - $r, $r, $r, 90, 90)
$path.CloseFigure()

$brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
    (New-Object System.Drawing.Point 0, 0),
    (New-Object System.Drawing.Point 512, 512),
    [System.Drawing.Color]::FromArgb(255, 12, 48, 42),
    [System.Drawing.Color]::FromArgb(255, 30, 133, 106)
)
$g.FillPath($brush, $path)
$brush.Dispose()

# Thin light rim.
$rim = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(70, 220, 255, 230)), 6
$g.DrawPath($rim, $path)
$rim.Dispose()

$cream = [System.Drawing.Color]::FromArgb(255, 244, 239, 230)
$creamBrush = New-Object System.Drawing.SolidBrush $cream
$dark = [System.Drawing.Color]::FromArgb(255, 16, 43, 37)

# Ears (drawn before the head so the head hides their bases).
$leftEar = @(
    (New-Object System.Drawing.PointF 122, 205),
    (New-Object System.Drawing.PointF 108, 78),
    (New-Object System.Drawing.PointF 232, 148)
)
$rightEar = @(
    (New-Object System.Drawing.PointF 390, 205),
    (New-Object System.Drawing.PointF 404, 78),
    (New-Object System.Drawing.PointF 280, 148)
)
$g.FillPolygon($creamBrush, $leftEar)
$g.FillPolygon($creamBrush, $rightEar)

# Head.
$g.FillEllipse($creamBrush, 108, 132, 296, 276)

# Inner ears.
$pink = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 232, 143, 152))
$g.FillPolygon($pink, @(
    (New-Object System.Drawing.PointF 134, 193),
    (New-Object System.Drawing.PointF 126, 108),
    (New-Object System.Drawing.PointF 206, 157)
))
$g.FillPolygon($pink, @(
    (New-Object System.Drawing.PointF 378, 193),
    (New-Object System.Drawing.PointF 386, 108),
    (New-Object System.Drawing.PointF 306, 157)
))
$pink.Dispose()

# Eyes with highlights.
$eyeBrush = New-Object System.Drawing.SolidBrush $dark
$g.FillEllipse($eyeBrush, 166, 232, 66, 78)
$g.FillEllipse($eyeBrush, 280, 232, 66, 78)
$white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(230, 255, 255, 255))
$g.FillEllipse($white, 186, 248, 18, 20)
$g.FillEllipse($white, 300, 248, 18, 20)
$white.Dispose()
$eyeBrush.Dispose()

# Nose + mouth.
$nose = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 232, 143, 152))
$g.FillPolygon($nose, @(
    (New-Object System.Drawing.PointF 238, 318),
    (New-Object System.Drawing.PointF 274, 318),
    (New-Object System.Drawing.PointF 256, 338)
))
$nose.Dispose()
$mouth = New-Object System.Drawing.Pen $dark, 8
$mouth.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
$mouth.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
$g.DrawArc($mouth, 220, 330, 36, 34, 20, 140)
$g.DrawArc($mouth, 256, 330, 36, 34, 20, 140)
$mouth.Dispose()

# Recording dot, bottom right.
$dotRing = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 18, 60, 51))
$g.FillEllipse($dotRing, 350, 350, 128, 128)
$dot = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 255, 82, 82))
$g.FillEllipse($dot, 366, 366, 96, 96)
$dot.Dispose()
$dotRing.Dispose()

$g.Dispose()

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

$script:Bmp.Save((Join-Path $OutDir "autocat_512.png"), [System.Drawing.Imaging.ImageFormat]::Png)

$sizes = 16, 24, 32, 48, 64, 128, 256
foreach ($s in $sizes) {
    $small = New-Object System.Drawing.Bitmap $s, $s
    $sg = [System.Drawing.Graphics]::FromImage($small)
    $sg.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $sg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $sg.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $sg.Clear([System.Drawing.Color]::Transparent)
    $sg.DrawImage($script:Bmp, 0, 0, $s, $s)
    $sg.Dispose()
    $small.Save((Join-Path $OutDir ("autocat_{0}.png" -f $s)), [System.Drawing.Imaging.ImageFormat]::Png)
    $small.Dispose()
}
$script:Bmp.Dispose()

Write-Host "icon pngs written to $OutDir"
