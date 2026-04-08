@echo off
chcp 65001 >nul
echo Démarrage du serveur LBA Bot...
echo Ouverture de l'interface : http://localhost:5000
echo.

:: Ouvre le navigateur automatiquement après 2 secondes
start "" /b cmd /c "timeout /t 2 >nul && start http://localhost:5000"

python app.py

pause
