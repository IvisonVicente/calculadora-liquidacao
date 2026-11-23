#!/bin/sh
cd "$(dirname "$0")"
echo "No computador: http://127.0.0.1:8765/calculadora_liquidacao.html"
echo "No celular, na mesma rede Wi-Fi, use o IP abaixo na porta 8765:"
if command -v ip >/dev/null 2>&1; then ip -4 addr; else ifconfig | grep "inet "; fi
if command -v open >/dev/null 2>&1; then open "http://127.0.0.1:8765/calculadora_liquidacao.html"; fi
if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:8765/calculadora_liquidacao.html"; fi
python3 -m http.server 8765 --bind 0.0.0.0
