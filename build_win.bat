@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   可乐口播视频生成器 - Windows 一键打包
echo   (智能适配 Python 3.11 / 3.10 / 3.12 / 3.13)
echo ============================================================
echo.

REM ============================================================
REM  0. 自检 Windows
REM ============================================================
if not "%OS%"=="Windows_NT" (
    echo [错误] 此脚本必须运行在 Windows 上！请到 Win 电脑双击运行。
    pause
    exit /b 1
)

REM ============================================================
REM  1. 自检 Python
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没装 Python。请装 Python 3.10 / 3.11 / 3.12 任一版本：
    echo        https://www.python.org/downloads/
    echo        安装时务必勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYV=%%v
echo [信息] 检测到 Python !PYV!

REM ============================================================
REM  2. 解析主版本号（3.11 / 3.12 等）
REM ============================================================
for /f "tokens=1,2 delims=." %%a in ("!PYV!") do (
    set PYM=%%a
    set PYN=%%b
)
echo.

REM ============================================================
REM  3. 准备虚拟环境
REM ============================================================
if not exist "venv_win" (
    echo [步骤] 创建虚拟环境 venv_win ...
    python -m venv venv_win
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
)
call venv_win\Scripts\activate.bat

REM ============================================================
REM  4. 智能安装依赖（核心兼容逻辑）
REM ============================================================
if "!PYM!.!PYN!"=="3.11" (
    REM ------------------ Python 3.11: 优先用 wheels/ 离线 ------------------
    if exist "wheels" (
        echo [步骤] Python 3.11 — 使用内置 wheels 离线安装 ...
        python -m pip install --upgrade pip --no-index --find-links=wheels --quiet 2>nul
        python -m pip install --no-index --find-links=wheels pyinstaller requests pyyaml Pillow pygame --quiet
        if not errorlevel 1 (
            echo [OK] 依赖安装完成（离线）
            goto :deps_ok
        )
        echo [警告] 离线安装失败，回退到在线安装 ...
    ) else (
        echo [警告] 未发现 wheels\ 目录，走在线安装 ...
    )
    python -m pip install --upgrade pip --quiet
    python -m pip install pyinstaller requests pyyaml Pillow pygame --quiet
    if errorlevel 1 goto :deps_fail
    echo [OK] 依赖安装完成（在线）
) else (
    REM ------------------ 其他版本（3.10 / 3.12 / 3.13）: 直接在线 ------------------
    echo [步骤] Python !PYV! — 走在线 pip install（首次约需 30s~3min）...
    python -m pip install --upgrade pip --quiet
    if errorlevel 1 (
        echo [警告] pip 升级失败，继续 ...
    )
    python -m pip install pyinstaller requests pyyaml Pillow pygame --quiet
    if errorlevel 1 goto :deps_fail
    echo [OK] 依赖安装完成（在线）
)

:deps_ok
REM ============================================================
REM  5. 确认 ffmpeg.exe 在位
REM ============================================================
if not exist "bin\ffmpeg.exe" (
    echo [错误] bin\ffmpeg.exe 不存在！请检查包是否完整
    pause
    exit /b 1
)
echo [OK] bin\ffmpeg.exe 已就绪（代码已支持 ffprobe 缺失时用 ffmpeg 兜底）

REM ============================================================
REM  6. 确认图标在位
REM ============================================================
if not exist "appicon.ico" (
    echo [警告] appicon.ico 不存在，打包出来的 exe 用默认图标
) else (
    echo [OK] appicon.ico 已就绪
)

REM ============================================================
REM  7. 清理旧 build / dist
REM ============================================================
if exist "build" rmdir /s /q build
if exist "dist"  rmdir /s /q dist

REM ============================================================
REM  8. 执行 PyInstaller 打包
REM ============================================================
echo [步骤] 开始 PyInstaller 打包（5-15 分钟）...
pyinstaller 可乐口播视频生成器_win.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [错误] 打包失败！请把上方 PyInstaller 报错截图发给我
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   打包完成！
echo   产物目录: dist\可乐口播视频生成器_win\
echo   把整个目录压成 zip 发给客户即可
echo ============================================================
pause
exit /b 0

REM ============================================================
REM  安装失败的友好提示
REM ============================================================
:deps_fail
echo.
echo [错误] 依赖安装失败！
echo [提示] 国内网络如果慢，试试手动执行下面这条（用阿里云镜像）：
echo        pip install -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller requests pyyaml Pillow pygame
echo.
pause
exit /b 1
