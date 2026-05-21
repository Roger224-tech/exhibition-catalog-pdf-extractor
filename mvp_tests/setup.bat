@echo off
chcp 65001 >nul
title 展会目录PDF提取工具 - 环境安装
echo ============================================================
echo   展会目录PDF提取工具 — 环境安装脚本
echo ============================================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo   下载地址: https://www.python.org/downloads/
    echo   安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

python --version
echo.

echo [1/2] 安装依赖包（可能需要5-15分钟，取决于网络速度）...
echo.
pip install -r requirements_full.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo [重试] 使用默认源...
    pip install -r requirements_full.txt
)

echo.
echo [2/2] 验证安装...
python -c "import fitz; print('  PyMuPDF: OK')" 2>nul || echo "  PyMuPDF: FAIL"
python -c "import cv2; print('  OpenCV:  OK')" 2>nul || echo "  OpenCV:  FAIL"
python -c "from PIL import Image; print('  Pillow:  OK')" 2>nul || echo "  Pillow:  FAIL"
python -c "import openpyxl; print('  openpyxl: OK')" 2>nul || echo "  openpyxl: FAIL"
python -c "import paddleocr; print('  PaddleOCR: OK (首次运行会自动下载模型)')" 2>nul || echo "  PaddleOCR: FAIL"
python -c "import yaml; print('  PyYAML:  OK')" 2>nul || echo "  PyYAML:  FAIL"

echo.
echo ============================================================
echo   安装完成！
echo   启动方式: 双击 启动.bat 或运行 python gui_app.py
echo ============================================================
pause
