$WshShell = New-Object -ComObject WScript.Shell
$RootDir = "C:\jarvis\gemini_live_vision"
$ExePath = Join-Path $RootDir "Chirkoot.exe"
$IconPath = Join-Path $RootDir "chirkoot.ico"

# 1. User Desktop (e.g. OneDrive\Desktop or local Desktop)
$DesktopPath = [System.Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $DesktopPath "Chirkoot.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ExePath
$Shortcut.Arguments = ""
$Shortcut.WorkingDirectory = $RootDir
$Shortcut.Description = "Chirkoot Multimodal Screen & Vision Assistant"
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Save()
Write-Host "Created Desktop Shortcut at $ShortcutPath"

# 2. Local Desktop (if distinct from OneDrive Desktop)
$AltDesktop = "C:\Users\ankit\Desktop"
if (Test-Path $AltDesktop) {
    $AltShortcutPath = Join-Path $AltDesktop "Chirkoot.lnk"
    if ($AltShortcutPath -ne $ShortcutPath) {
        $AltShortcut = $WshShell.CreateShortcut($AltShortcutPath)
        $AltShortcut.TargetPath = $ExePath
        $AltShortcut.Arguments = ""
        $AltShortcut.WorkingDirectory = $RootDir
        $AltShortcut.Description = "Chirkoot Multimodal Screen & Vision Assistant"
        $AltShortcut.IconLocation = "$IconPath,0"
        $AltShortcut.Save()
        Write-Host "Created Alt Desktop Shortcut at $AltShortcutPath"
    }
}

# 3. Project Directory Shortcut
$LocalShortcutPath = Join-Path $RootDir "Chirkoot.lnk"
$LocalShortcut = $WshShell.CreateShortcut($LocalShortcutPath)
$LocalShortcut.TargetPath = $ExePath
$LocalShortcut.Arguments = ""
$LocalShortcut.WorkingDirectory = $RootDir
$LocalShortcut.Description = "Chirkoot Multimodal Screen & Vision Assistant"
$LocalShortcut.IconLocation = "$IconPath,0"
$LocalShortcut.Save()
Write-Host "Created Project Directory Shortcut at $LocalShortcutPath"
