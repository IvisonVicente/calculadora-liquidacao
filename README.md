# LiqGuard

Estimativa de liquidação, preço médio e P&L para futuros perpétuos em USDT. A cotação acompanha o par e usa o preço de marcação da Bybit.

**Demonstração:** https://ivisonvicente.github.io/liqguard/

## O que o projeto mostra

- Posição, liquidação, distância, ROE e razão de margem para long e short, nos modos cross e isolada.
- Cotação ao vivo da Bybit, com a Coinbase como reserva.
- Novas entradas na mesma operação e realização parcial, separando o primeiro preço do preço médio.
- Simulação de lucro ou perda em um preço alvo, sem alterar o preço usado na liquidação.
- App instalável no navegador, no computador e no celular.

A liquidação é uma estimativa. A corretora usa o preço de marcação e as faixas de risco dela.

## Como usar

A demonstração já abre no navegador. No Chrome ou no Edge, use **Instalar app**. No iPhone, toque em Compartilhar e em **Adicionar à Tela de Início**.

Para abrir na própria máquina:

```powershell
abrir-app.bat
```

No Mac ou no Linux:

```bash
bash abrir-app.sh
```

A versão de desktop, em Python:

```powershell
python calculadora_liquidacao.py
```

A planilha `calculadora-liquidacao-sheets.xlsx` abre no Google Planilhas. O campo Par aceita ETH, SOL, 1000PEPE ou ETHUSDT, e a cotação segue esse par.
