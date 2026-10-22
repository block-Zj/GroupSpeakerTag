@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  LangBot 一键启动（锁定依赖组合，避免踩版本坑）
REM
REM  为什么需要这个脚本：
REM  1) uvx 不锁版本，每次都会解析"最新 langbot + 最新依赖"。
REM     LangBot 4.11.1 与 SQLAlchemy 2.1 不兼容：
REM       - 启动时可能崩在 session summary backfill
REM       - 运行时 monitoring 写库被拒（面板"消息记录"不再更新）
REM  2) data/ 目录是相对当前工作目录解析的，必须先进到项目目录再启动，
REM     否则会新建一份空 data/，看起来像"配置全丢了"。
REM ============================================================

REM 项目目录（data/ 所在处）
set "LANGBOT_HOME=D:\Desktop\Ling\langbot"

REM 国内镜像（直连 PyPI 经常超时；不需要可整行注释掉）
set "UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"

if not exist "%LANGBOT_HOME%\data" (
    echo [ERROR] 找不到 %LANGBOT_HOME%\data
    echo         请确认 LANGBOT_HOME 指对了目录
    pause
    exit /b 1
)

cd /d "%LANGBOT_HOME%"

echo ============================================================
echo  启动 LangBot
echo  目录 : %CD%
echo  依赖 : langbot==4.11.1 + sqlalchemy^<2.1
echo ============================================================
echo.

uvx --with "sqlalchemy<2.1" "langbot@4.11.1"

echo.
echo [LangBot 已退出]
pause
