@echo off
chcp 65001 >nul
rem 启动本系统（Uvicorn 监听 127.0.0.1:8000）。关闭窗口即停止服务。
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv，请先按 README 创建虚拟环境
    pause
    exit /b 1
)

echo 正在启动 个人知识管理智能体 ...
echo 界面地址：http://127.0.0.1:8000/
echo 按 Ctrl+C 或关闭窗口可停止。
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
