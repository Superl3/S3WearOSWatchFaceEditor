param(
  [string]$AvdName = 'Photo2WFF-Wear34',
  [string]$ApkPath,
  [string]$Serial,
  [switch]$NoWindow,
  [switch]$CapturePicker,
  [switch]$CleanInstall,
  [switch]$Activate
)

$ErrorActionPreference = 'Stop'
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:USERPROFILE 'Android\Sdk' }
$adb = Join-Path $sdk 'platform-tools\adb.exe'
$emulator = Join-Path $sdk 'emulator\emulator.exe'

function Invoke-Adb([string[]]$Arguments) {
  & $adb @Arguments
  if ($LASTEXITCODE -ne 0) { throw "adb failed: $($Arguments -join ' ')" }
}

if (-not (Test-Path $adb)) { throw "Android SDK platform-tools not found: $adb" }
if (-not (Test-Path $emulator)) { throw "Android emulator not found: $emulator" }

if (-not $Serial) {
  $Serial = (& $adb devices | Select-String 'emulator-\d+\s+device' | ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -Last 1)
}
if (-not $Serial) {
  $args = @('-avd', $AvdName, '-no-snapshot', '-no-boot-anim', '-gpu', 'swiftshader_indirect', '-no-audio')
  if ($NoWindow) { $args += '-no-window' }
  Start-Process -FilePath $emulator -ArgumentList $args -WindowStyle Hidden | Out-Null
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $Serial = (& $adb devices | Select-String 'emulator-\d+\s+device' | ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -Last 1)
    if ($Serial) { break }
  }
}
if (-not $Serial) { throw 'No online emulator found.' }

for ($i = 0; $i -lt 60; $i++) {
  if ((& $adb -s $Serial shell getprop sys.boot_completed).Trim() -eq '1') { break }
  Start-Sleep -Seconds 2
}
if ((& $adb -s $Serial shell getprop sys.boot_completed).Trim() -ne '1') { throw "Wear emulator did not boot: $Serial" }

$characteristics = (& $adb -s $Serial shell getprop ro.build.characteristics).Trim()
if ($characteristics -notmatch 'watch') { throw "Selected device is not Wear OS: $characteristics" }

if ($ApkPath) {
  if ($CleanInstall) {
    & $adb -s $Serial uninstall com.photo2wff.watchface | Out-Null
  }
  Invoke-Adb -Arguments @('-s', $Serial, 'install', '-r', $ApkPath)
  Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'pm', 'enable', 'com.photo2wff.watchface')
}

$debugSurface = (& $adb -s $Serial shell am broadcast -a com.google.android.wearable.app.DEBUG_SURFACE --es operation set-watchface --es watchFaceId com.photo2wff.watchface) -join "`n"
Start-Sleep -Seconds 3

Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'am', 'force-stop', 'com.google.android.calendar')
Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'am', 'start', '-n', 'com.google.android.wearable.sysui/com.google.android.wearable.sysui.mainui.activity.SysUiActivity')
Start-Sleep -Seconds 1
Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'input', 'swipe', '227', '227', '227', '227', '2200')
Start-Sleep -Seconds 2
Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'uiautomator', 'dump', '/sdcard/window.xml')
$xml = (& $adb -s $Serial shell cat /sdcard/window.xml) -join "`n"
$pickerOpen = $xml -match 'Watch face picker'
$registered = $xml -match 'Photo2WFF'
$selected = $false
if ($registered -and $Activate) {
  Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'input', 'tap', '227', '200')
  Start-Sleep -Seconds 3
  $selected = ((& $adb -s $Serial shell dumpsys wallpaper) -join "`n") -match 'DeclarativeWatchFaceRuntime'
}

$artifact = Join-Path (Get-Location) 'a25-runtime-validation'
New-Item -ItemType Directory -Force -Path $artifact | Out-Null
if ($CapturePicker) {
  Invoke-Adb -Arguments @('-s', $Serial, 'shell', 'screencap', '-p', '/sdcard/wear-picker.png')
  Invoke-Adb -Arguments @('-s', $Serial, 'pull', '/sdcard/wear-picker.png', (Join-Path $artifact 'wear-picker.png'))
}

$result = [ordered]@{
  avd = $AvdName
  serial = $Serial
  characteristics = $characteristics
  bootCompleted = $true
  apkInstalled = [bool]$ApkPath
  pickerOpen = $pickerOpen
  photo2wffRegisteredInPicker = $registered
  debugSurface = $debugSurface
  selected = $selected
  status = if ($selected) { 'selected_and_runtime_active' } elseif ($registered) { 'registered_selection_required' } else { 'watchface_not_registered' }
  note = 'WatchFaceId className=null is expected for resource-only WFF and is not treated as a failure.'
}
$result | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $artifact 'wear-runtime-setup.json')
$result | ConvertTo-Json -Depth 4
if (-not $pickerOpen) { exit 2 }
if (-not $registered) { exit 3 }
