@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   job-radar  push to GitHub
echo ============================================
echo.

rem --- 1. GitHub 需要走本机代理，否则 TLS 会被切断 ---
set "PROXY=http://127.0.0.1:7890"
git config http.proxy  %PROXY%
git config https.proxy %PROXY%
echo [1/3] proxy set to %PROXY%

rem --- 2. 拿 token：优先读 token.txt，没有就当场粘贴 ---
if exist token.txt (
  set /p TOKEN=<token.txt
  echo [2/3] token loaded from token.txt
) else (
  set /p TOKEN=Paste your GitHub token (ghp_...): 
  echo [2/3] token from keyboard
)
if "!TOKEN!"=="" (
  echo [FAIL] token is empty.
  pause
  exit /b 1
)

rem --- 3. 推送（force：整体替换仓库里被打平的文件） ---
echo [3/3] pushing...
git push -f https://!TOKEN!@github.com/chen2611/job-radar.git main
set "CODE=!errorlevel!"
echo.
if !CODE!==0 (
  echo [OK] Done. Open https://github.com/chen2611/job-radar/actions
) else (
  echo [FAIL] exit code !CODE!
  echo   - 401/403: token 无效或没有 repo 权限
  echo   - timeout/closed: 代理没开，或端口不是 7890
  echo   - not found: 仓库名不对，检查 chen2611/job-radar
)
echo.
pause
