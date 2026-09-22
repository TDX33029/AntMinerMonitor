@echo off
chcp 65001 >nul
echo ==================================================================
echo  🛑 正在停止所有 AntMiner 相关旧进程并释放端口 (Windows)...
echo ==================================================================

:: 1. 查找并杀死占用 20000 端口的进程
set FOUND_PORT=0
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :20000 ^| findstr LISTENING') do (
    set FOUND_PORT=1
    echo [-] 发现占用 20000 端口的进程 PID: %%a，正在终止...
    taskkill /F /PID %%a >nul 2>&1
)
if "%FOUND_PORT%"=="0" (
    echo [i] 端口 20000 未被占用
) else (
    echo [✓] 端口 20000 已释放
)

:: 2. 通过 PowerShell 精确查找命令行中带有 antminer 的 Python 进程并终止
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'antminer_(web|monitor|gui)\.py' } | ForEach-Object { Write-Host ('[-] 正在终止进程 ' + $_.ProcessId + ': ' + $_.CommandLine); Stop-Process -Id $_.ProcessId -Force }"

echo ==================================================================
echo  [✓] 清理完成！所有旧进程已全部退出。
echo ==================================================================
pause
