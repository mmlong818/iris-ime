# 鸢尾输入法 TSF 前端安装(需管理员: 右键"以管理员身份运行" PowerShell 后执行)
# 用法: powershell -ExecutionPolicy Bypass -File install-admin.ps1
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$dll = Join-Path $src "IrisIME.dll"
if (!(Test-Path $dll)) { $dll = Join-Path $src "SampleIME.dll" }
if (!(Test-Path $dll)) { Write-Error "找不到 IrisIME.dll"; exit 1 }
Write-Host "注册 TSF 输入法: $dll"
Start-Process -FilePath "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList "/s `"$dll`"" -Verb RunAs -Wait
Write-Host "注册完成。请在 Windows 设置 > 时间和语言 > 语言 > 中文 > 选项 > 键盘 > 添加键盘中选择"
Write-Host "  鸢尾输入法 Iris IME"
Write-Host "然后确保 python -m ime.tsf_server 在运行(或用托盘客户端启动)。"
