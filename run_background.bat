@echo off
cd /d "%~dp0"

:: Видалити логи старші 7 днів
echo Видалення старих логів...
forfiles /p . /m bot.log.* /d -7 /c "cmd /c del @path" 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Старі логи видалено.
) else (
    echo Старих логів для видалення не знайдено.
)

:: Запуск основної програми
echo Запуск торгового бота...
python main.py

pause