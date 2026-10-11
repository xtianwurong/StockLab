@echo off
REM StockLab 统一入口 (Windows)
REM 用法: run.bat [install|run|test|sync]

setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "PY=%ROOT%venv\Scripts\python.exe"
set "PIP=%ROOT%venv\Scripts\pip.exe"
set "PORT=8000"

if "%1"=="" goto help

if "%~1"=="install" goto install
if "%~1"=="run" goto run
if "%~1"=="test" goto test
if "%~1"=="sync" goto sync

:help
echo 用法: run.bat [install^|run^|test^|sync]
echo   install  安装依赖
echo   run      启动 Web 服务 (含安装)
echo   test     运行单元测试
echo   sync     全量同步核心数据
goto :eof

:install
if not exist "%PY%" (
    echo [INFO] 创建虚拟环境...
    python -m venv "%ROOT%venv"
    "%PIP%" install -U pip
)
echo [INFO] 安装依赖...
"%PIP%" install -r "%ROOT%requirements.txt"
echo [OK] 完成
goto :eof

:run
if not exist "%PY%" (
    echo [INFO] 创建虚拟环境...
    python -m venv "%ROOT%venv"
    "%PIP%" install -U pip
)
"%PIP%" install -q -r "%ROOT%requirements.txt"
echo [INFO] 启动 Web: http://127.0.0.1:%PORT%
cd /d "%ROOT%"
"%PY%" app/scripts/serve_web.py --port %PORT%
goto :eof

:test
if not exist "%PY%" (
    echo [INFO] 创建虚拟环境...
    python -m venv "%ROOT%venv"
    "%PIP%" install -U pip
)
"%PIP%" install -q -r "%ROOT%requirements.txt"
echo [INFO] 运行单元测试...
cd /d "%ROOT%"
"%PY%" -m pytest -m "not integration" --tb=short
goto :eof

:sync
if not exist "%PY%" (
    echo [INFO] 创建虚拟环境...
    python -m venv "%ROOT%venv"
    "%PIP%" install -U pip
)
"%PIP%" install -q -r "%ROOT%requirements.txt"
echo [INFO] 全量同步核心数据...
cd /d "%ROOT%"
"%PY%" app/scripts/sync_all.py
goto :eof