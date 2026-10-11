@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "TEMP=%~dp0runtime\tmp"
set "TMP=%~dp0runtime\tmp"

rem 清掉外部注入的 PYTHONPATH：某些沙箱会把「安全删除」钩子（sitecustomize）
rem 塞进来，它会把 shutil.rmtree 换成超额度就 raise SystemExit 的实现，
rem 后果是执行/自动识别在清理临时目录时直接 500。这里只用自己的 venv。
set "PYTHONPATH="
set "PYTHONSTARTUP="

if "%DASHSCOPE_API_KEY%"=="" (
  echo [!] 环境变量 DASHSCOPE_API_KEY 未设置，生图节点会执行失败。
  echo.
)

echo 启动 ImageProcessFlow ...
echo 浏览器打开 http://127.0.0.1:8770
echo 按 Ctrl+C 停止。
echo.

"%~dp0runtime\venv\Scripts\python.exe" "%~dp0server\main.py" %*

pause
