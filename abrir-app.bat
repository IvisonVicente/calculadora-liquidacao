@echo off
cd /d "%~dp0"
echo.
echo No computador: http://127.0.0.1:8765/calculadora_liquidacao.html
echo No celular, na mesma rede Wi-Fi, use o IPv4 abaixo na porta 8765:
ipconfig | findstr /i "IPv4"
echo.
start "" http://127.0.0.1:8765/calculadora_liquidacao.html
python -m http.server 8765 --bind 0.0.0.0
