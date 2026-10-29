@echo off
cd /d "%~dp0"
echo Iniciando recolector de titulares "rojo"...
echo Abre en el navegador:  http://127.0.0.1:8000
start "" http://127.0.0.1:8000
python app.py
pause
