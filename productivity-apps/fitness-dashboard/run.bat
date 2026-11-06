@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   DrZ 健身仪表盘 启动中...
echo   项目目录（群晖同步）: %~dp0
echo ========================================

REM venv 放本机目录，不进群晖同步（用 %USERPROFILE%\.drzfitness）
set LOCAL_DIR=%USERPROFILE%\.drzfitness
if not exist "%LOCAL_DIR%" mkdir "%LOCAL_DIR%"

if not exist "%LOCAL_DIR%\.venv\Scripts\activate.bat" (
    echo [!] 首次运行：创建虚拟环境并安装依赖，约 2-3 分钟...
    python -m venv "%LOCAL_DIR%\.venv"
    call "%LOCAL_DIR%\.venv\Scripts\activate.bat"
    pip install -r requirements.txt
) else (
    call "%LOCAL_DIR%\.venv\Scripts\activate.bat"
)

if not exist "%LOCAL_DIR%\.env" (
    if exist ".env.example" (
        copy .env.example "%LOCAL_DIR%\.env" >nul
        echo [i] 已创建 %LOCAL_DIR%\.env，请编辑填入 API Key
    )
)

echo.
echo [OK] 环境就绪，启动 Streamlit...
echo [i] 浏览器访问 http://localhost:8501
echo.
streamlit run app.py --server.port 8501

pause
