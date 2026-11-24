$WshShell = New-Object -ComObject WScript.Shell
$RootDir = "C:\jarvis\gemini_live_vision"
$ExePath = Join-Path $RootDir "Chitrakoot.exe"
$IconPath = Join-Path $RootDir "chirkoot.ico"

# 1. User Desktop (e.g. OneDrive\Desktop)
$DesktopPath = [System.Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $DesktopPath "Chitrakoot.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ExePath
$Shortcut.Arguments = ""
$Shortcut.WorkingDirectory = $RootDir
$Shortcut.Description = "Chitrakoot 100% Offline Futuristic AI Interactive Chat"
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Save()
Write-Host "Created Desktop Shortcut at $ShortcutPath"

# 2. Local Desktop (if distinct from OneDrive Desktop)
$AltDesktop = "C:\Users\ankit\Desktop"
if (Test-Path $AltDesktop) {
    $AltShortcutPath = Join-Path $AltDesktop "Chitrakoot.lnk"
    if ($AltShortcutPath -ne $ShortcutPath) {
        $AltShortcut = $WshShell.CreateShortcut($AltShortcutPath)
        $AltShortcut.TargetPath = $ExePath
        $AltShortcut.Arguments = ""
        $AltShortcut.WorkingDirectory = $RootDir
        $AltShortcut.Description = "Chitrakoot 100% Offline Futuristic AI Interactive Chat"
        $AltShortcut.IconLocation = "$IconPath,0"
        $AltShortcut.Save()
        Write-Host "Created Alt Desktop Shortcut at $AltShortcutPath"
    }
}

# 3. Windows Start Menu Programs (Indexes Chitrakoot into Windows Start Menu & Windows Search)
$StartMenuPath = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs")
if (Test-Path $StartMenuPath) {
    $StartShortcutPath = Join-Path $StartMenuPath "Chitrakoot.lnk"
    $StartShortcut = $WshShell.CreateShortcut($StartShortcutPath)
    $StartShortcut.TargetPath = $ExePath
    $StartShortcut.Arguments = ""
    $StartShortcut.WorkingDirectory = $RootDir
    $StartShortcut.Description = "Chitrakoot 100% Offline Futuristic AI Interactive Chat"
    $StartShortcut.IconLocation = "$IconPath,0"
    $StartShortcut.Save()
    Write-Host "Pinned Chitrakoot to Windows Start Menu at $StartShortcutPath"

    # Shell verb pin attempt
    try {
        $shell = New-Object -ComObject Shell.Application
        $folder = $shell.Namespace($StartMenuPath)
        $item = $folder.ParseName("Chitrakoot.lnk")
        foreach ($v in $item.Verbs()) {
            if ($v.Name -match "Pin to Start") {
                $v.DoIt()
                Write-Host "Invoked Pin to Start verb successfully."
                break
            }
        }
    } catch {}
}

# 4. In-folder project shortcut
$LocalShortcutPath = Join-Path $RootDir "Chitrakoot.lnk"
$LocalShortcut = $WshShell.CreateShortcut($LocalShortcutPath)
$LocalShortcut.TargetPath = $ExePath
$LocalShortcut.Arguments = ""
$LocalShortcut.WorkingDirectory = $RootDir
$LocalShortcut.Description = "Chitrakoot 100% Offline Futuristic AI Interactive Chat"
$LocalShortcut.IconLocation = "$IconPath,0"
$LocalShortcut.Save()
Write-Host "Created Project Directory Shortcut at $LocalShortcutPath"
