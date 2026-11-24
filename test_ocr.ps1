param(
    [string]$ImagePath
)

Add-Type -AssemblyName System.IO
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null

function Await($task, $timeoutMs=5000) {
    while (-not $task.IsCompleted -and -not $task.IsFaulted -and -not $task.IsCanceled) {
        Start-Sleep -Milliseconds 20
    }
    return $task.GetResults()
}

$fullPath = [System.IO.Path]::GetFullPath($ImagePath)
$fileOp = [Windows.Storage.StorageFile]::GetFileFromPathAsync($fullPath)
$file = Await $fileOp

$streamOp = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
$stream = Await $streamOp

$decoderOp = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
$decoder = Await $decoderOp

$softwareBitmapOp = $decoder.GetSoftwareBitmapAsync()
$softwareBitmap = Await $softwareBitmapOp

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("en-US"))
}

$ocrResultOp = $engine.RecognizeAsync($softwareBitmap)
$ocrResult = Await $ocrResultOp

$lines = @()
foreach ($line in $ocrResult.Lines) {
    $words = @()
    foreach ($w in $line.Words) {
        $words += @{
            text = $w.Text
            x = [int]$w.BoundingRect.X
            y = [int]$w.BoundingRect.Y
            w = [int]$w.BoundingRect.Width
            h = [int]$w.BoundingRect.Height
        }
    }
    $lines += @{
        text = $line.Text
        words = $words
    }
}

$output = @{
    text = $ocrResult.Text
    lines = $lines
}

$output | ConvertTo-Json -Depth 5 -Compress
