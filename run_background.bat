@echo off
REM Скрипт для запуску бота BingX у фоновому режимі

REM Перехід до папки, де знаходиться цей .bat файл
cd /d "%~dp0"

REM Перевірка, чи існує віртуальне середовище
IF EXIST .\venv\Scripts\activate.bat (
    echo Activating virtual environment...
    call .\venv\Scripts\activate.bat
) ELSE (
    echo Warning: Virtual environment (venv) not found or activate.bat missing.
    echo Running with system Python interpreter.
)

echo Starting BingX Bot in the background...
echo Output will be redirected to bot_output.log

REM Запуск Python скрипта у фоні з перенаправленням виводу
start "BingXBot" /B python main.py > bot_output.log 2>&1

REM Якщо ви використовували віртуальне середовище, можна його деактивувати
REM Але оскільки скрипт завершується відразу після запуску фонового процесу, це не обов'язково
REM IF EXIST .\venv\Scripts\deactivate.bat (
REM    call .\venv\Scripts\deactivate.bat
REM )

echo Script finished. Bot is running in the background.
pause
exit /b 