@echo off
rem Double-click to start Skroutz Tools. First run installs what it needs.
cd /d "%~dp0"
python -c "import scrapling" 2>nul || (
    echo Installing requirements, one time only...
    python -m pip install -r requirements.txt || goto :error
)
python app.py
pause
exit /b

:error
echo.
echo Could not install the requirements. Is Python 3.10+ installed and on PATH?
pause
