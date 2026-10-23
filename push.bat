@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Pushing job-radar to GitHub...
echo   A login window may pop up. Please sign in.
echo ============================================
echo.
git push -f -u origin main
echo.
if %errorlevel%==0 (
  echo [OK] Push finished.
  echo Open https://github.com/chen2611/job-radar/actions
) else (
  echo [FAIL] Push failed. See the message above.
  echo Tip: if it asks for username/password, use a Personal Access Token
  echo      as the password. See docs/deploy-secrets.md
)
echo.
pause
