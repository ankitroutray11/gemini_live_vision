$WshShell = New-Object -ComObject WScript.Shell
$DesktopPath = [System.Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $DesktopPath "Gemini Live.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "C:\jarvis\gemini_live_vision\GeminiLive.exe"
$Shortcut.WorkingDirectory = "C:\jarvis\gemini_live_vision"
$Shortcut.Description = "Gemini Live Multimodal Assistant"
$Shortcut.Save()
Write-Host "Created Desktop Shortcut at $ShortcutPath"
