@echo off
chcp 65001 >nul
echo ============================================
echo  Installation des dépendances LBA Bot
echo ============================================
echo.

pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERREUR] pip install a échoué.
    pause
    exit /b 1
)

echo.
echo Installation du navigateur Chromium pour Playwright...
playwright install chromium
if %errorlevel% neq 0 (
    echo [ERREUR] playwright install a échoué.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Installation terminée avec succès !
echo  Lancez run.bat pour démarrer l'application.
echo ============================================
pause
