#!/usr/bin/env python3
"""Calculadora de liquidação para futuros perpétuos USDT.

A conta é a mesma da planilha Calculadora:

    margem = saldo × % alocado
    posição = margem × alavancagem
    quantidade = posição / entrada
    manutenção = posição × taxa de manutenção
    taxa de fechamento = posição × taxa taker
    margem base = saldo no Cross, ou só a margem alocada na Isolada
    liquidação long = entrada − (margem base − manutenção − taxa) / quantidade
    liquidação short = entrada + (margem base − manutenção − taxa) / quantidade

É uma estimativa. A corretora liquida pelo preço de marcação e pela faixa de risco.

Sem argumentos, abre a janela. No terminal:

    python calculadora_liquidacao.py --cli
    python calculadora_liquidacao.py --moeda ETH --lado short --modo isolada --alav 50 --entrada 3200
    python calculadora_liquidacao.py --preco 60000 --sem-tabela
    python calculadora_liquidacao.py --watch 30
    python calculadora_liquidacao.py --interativo
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LEVS_TABELA = (5, 10, 25, 50, 75, 100, 125, 150)
CACHE_FAIXAS = {}
ULTIMA_COTACAO = {}

LADOS = {
    "long": "long",
    "l": "long",
    "compra": "long",
    "short": "short",
    "s": "short",
    "venda": "short",
}
MODOS = {
    "cross": "cross",
    "cruzada": "cross",
    "c": "cross",
    "isolada": "isolada",
    "isolated": "isolada",
    "iso": "isolada",
    "i": "isolada",
}
NOME_LADO = {"long": "Long", "short": "Short"}
NOME_MODO = {"cross": "Cross", "isolada": "Isolada"}


def preparar_console():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.SetConsoleOutputCP(65001)
        handle = kernel.GetStdHandle(-11)
        modo = ctypes.c_uint()
        if kernel.GetConsoleMode(handle, ctypes.byref(modo)):
            kernel.SetConsoleMode(handle, modo.value | 0x0004)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def normalizar_moeda(texto):
    base = "".join(caractere for caractere in texto.strip().upper() if caractere.isalnum())
    if base.endswith("USDT"):
        base = base[:-4]
    elif base.endswith("USD") and len(base) > 3:
        base = base[:-3]
    if not base:
        raise ValueError("Moeda inválida.")
    return base


def normalizar_lado(texto):
    chave = str(texto).strip().lower()
    if chave not in LADOS:
        raise argparse.ArgumentTypeError("use long ou short")
    return LADOS[chave]


def normalizar_modo(texto):
    chave = str(texto).strip().lower()
    if chave not in MODOS:
        raise argparse.ArgumentTypeError("use cross ou isolada")
    return MODOS[chave]


def numero(texto):
    try:
        return float(str(texto).strip().replace("%", "").replace(" ", "").replace(",", "."))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("número inválido") from exc


def br(valor, casas=2):
    if valor is None:
        return "—"
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def dinheiro(valor):
    """USDT com as casas necessárias para a margem × alavancagem bater com a posição."""
    if valor is None:
        return "—"
    texto = f"{valor:.6f}".rstrip("0").rstrip(".")
    sinal = ""
    if texto.startswith("-"):
        sinal = "-"
        texto = texto[1:]
    if "." not in texto:
        texto += ".00"
    elif len(texto.split(".")[1]) == 1:
        texto += "0"
    inteiro, frac = texto.split(".")
    grupos = []
    while inteiro:
        grupos.append(inteiro[-3:])
        inteiro = inteiro[:-3]
    return sinal + ".".join(reversed(grupos)) + "," + frac


def campo_num(valor):
    texto = f"{valor:.10f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def casas_preco(preco):
    if preco >= 100:
        return 2
    if preco >= 1:
        return 4
    return 6


def status_de(dist):
    if dist is None:
        return "Sem liquidação"
    if dist <= 0:
        return "JÁ LIQUIDADA"
    if dist < 0.01:
        return "RISCO CRÍTICO"
    if dist < 0.05:
        return "RISCO ALTO"
    if dist < 0.15:
        return "RISCO MODERADO"
    return "RISCO BAIXO"


def calcular(lado, modo, saldo, pct, alav, entrada, atual, mmr, taxa, qtd=None, realizado=0.0, margem=None):
    """pct, mmr e taxa em percentual: 5 = 5%, 0,5 = 0,5%.

    A margem informada define o tamanho da posição e é o fator do lucro e da perda.
    qtd, quando informada, é a quantidade da operação já ajustada por parcial ou nova entrada.
    """
    if margem is None:
        margem = saldo * pct / 100
    if margem <= 0:
        raise ValueError("Informe a margem usada (USDT). O lucro e a perda usam esse valor.")
    if qtd is None:
        nocional = margem * alav
        qtd = nocional / entrada
    else:
        nocional = qtd * entrada
    manut = nocional * mmr / 100
    fechamento = nocional * taxa / 100
    caixa = saldo if saldo > 0 else margem
    base = (caixa + realizado) if modo == "cross" else (margem + realizado)
    colchao = (base - manut - fechamento) / qtd
    sinal = 1 if lado == "long" else -1
    liq_bruto = entrada - sinal * colchao
    valida = liq_bruto > 0
    dist = (atual - liq_bruto) * sinal / atual if valida else None
    pnl = qtd * (atual - entrada) * sinal
    patrimonio = base + pnl
    razao = 1.0 if patrimonio <= 0 else (qtd * atual * mmr / 100) / patrimonio
    if realizado and qtd > 0:
        equilibrio = entrada - sinal * (realizado / qtd)
    else:
        equilibrio = entrada
    return {
        "margem": margem,
        "nocional": nocional,
        "qtd": qtd,
        "manut": manut,
        "fechamento": fechamento,
        "base": base,
        "liq": liq_bruto if valida else None,
        "dist": dist,
        "pnl": pnl,
        "roe": pnl / margem if margem else 0.0,
        "razao": razao,
        "zera": 1 / alav,
        "status": status_de(dist),
        "insustentavel": manut + fechamento >= base,
        "realizado": realizado,
        "equilibrio": equilibrio,
    }


def realizar_parcial(lado, entrada, qtd, realizado, preco, taxa, fracao):
    """Fecha uma fração da posição (0 a 1) no preço informado.

    O lucro líquido da taxa acumula. O preço médio devolvido é o equilíbrio
    da operação: se o mercado voltar até ele, o que restou desfaz o lucro já realizado.
    """
    if not 0 < fracao < 1:
        raise ValueError("A parcial fica entre 0 e 100%. Use 50 para fechar metade.")
    if qtd <= 0 or entrada <= 0 or preco <= 0:
        raise ValueError("Calcule a posição antes de realizar.")
    sinal = 1 if lado == "long" else -1
    fechada = qtd * fracao
    restante = qtd - fechada
    liquido = fechada * (preco - entrada) * sinal - fechada * preco * (taxa / 100)
    novo = realizado + liquido
    return {
        "qtd": restante,
        "realizado": novo,
        "liquido": liquido,
        "equilibrio": entrada - sinal * (novo / restante),
        "fechada": fechada,
    }


def simular_no_preco(lado, entrada, qtd, margem, taxa, alvo):
    """P&L da posição se o preço chegar no alvo informado.

    O valor bruto segue a mesma conta do P&L aberto. O líquido desconta a taxa taker de fechar.
    """
    if alvo <= 0:
        raise ValueError("O preço alvo precisa ser maior que zero.")
    if qtd <= 0 or entrada <= 0:
        raise ValueError("Calcule a posição antes de simular o preço alvo.")
    sinal = 1 if lado == "long" else -1
    pnl = qtd * (alvo - entrada) * sinal
    taxa_fecha = qtd * alvo * taxa / 100
    liquido = pnl - taxa_fecha
    return {
        "alvo": alvo,
        "pnl": pnl,
        "liquido": liquido,
        "roe": pnl / margem if margem else 0.0,
        "roe_liquido": liquido / margem if margem else 0.0,
        "taxa": taxa_fecha,
    }


def nova_entrada(lado, preco_medio, qtd, preco_novo, margem_add, alav):
    """Soma uma entrada na mesma operação e devolve o novo preço médio.

    Long melhora quando o preço novo é menor. Short melhora quando o preço novo é maior.
    """
    if margem_add <= 0:
        raise ValueError("Informe a nova entrada em USDT de margem.")
    if qtd <= 0 or preco_medio <= 0 or preco_novo <= 0 or alav <= 0:
        raise ValueError("Calcule a posição antes de adicionar uma entrada.")
    qtd_add = (margem_add * alav) / preco_novo
    total = qtd + qtd_add
    novo = (qtd * preco_medio + qtd_add * preco_novo) / total
    sinal = 1 if lado == "long" else -1
    return {
        "qtd": total,
        "medio": novo,
        "qtd_add": qtd_add,
        "melhorou": (novo - preco_medio) * sinal < 0,
    }


def compor_operacao(lado, entrada, qtd, realizado, margem, alav, taxa, atual, margem_add, fracao):
    """Aplica nova entrada e, em seguida, a parcial. O preço devolvido continua sendo o custo."""
    medio = entrada
    quantidade = qtd
    lucro = realizado or 0.0
    margem_pos = margem
    qtd_add = None
    if not quantidade or quantidade <= 0:
        quantidade = margem_pos * alav / medio
    if margem_add > 0:
        efeito = nova_entrada(lado, medio, quantidade, atual, margem_add, alav)
        medio = efeito["medio"]
        quantidade = efeito["qtd"]
        margem_pos += margem_add
        qtd_add = efeito["qtd_add"]
    if fracao > 0:
        efeito = realizar_parcial(lado, medio, quantidade, lucro, atual, taxa, fracao)
        quantidade = efeito["qtd"]
        lucro = efeito["realizado"]
    sinal = 1 if lado == "long" else -1
    equilibrio = medio - sinal * (lucro / quantidade) if lucro and quantidade > 0 else medio
    return {
        "entrada": medio,
        "qtd": quantidade,
        "realizado": lucro,
        "margem": margem_pos,
        "qtd_add": qtd_add,
        "equilibrio": equilibrio,
    }


def avaliar(lado, modo, saldo, pct, alav, entrada, atual, mmr, taxa, margem=None, qtd=None, realizado=0.0):
    resultado = calcular(
        lado, modo, saldo, pct, alav, entrada, atual, mmr, taxa, qtd=qtd, realizado=realizado, margem=margem
    )
    alavancagens = sorted(set(LEVS_TABELA) | {alav})
    tabela = []
    for nivel in alavancagens:
        mesma = qtd is not None and abs(nivel - alav) < 1e-9
        linha = calcular(
            lado,
            modo,
            saldo,
            pct,
            nivel,
            entrada,
            atual,
            mmr,
            taxa,
            qtd=qtd if mesma else None,
            realizado=realizado if mesma else 0.0,
            margem=margem,
        )
        linha["alav"] = nivel
        linha["marca"] = abs(nivel - alav) < 1e-9
        tabela.append(linha)
    resultado["tabela"] = tabela
    avisos = []
    if resultado["insustentavel"]:
        avisos.append(
            "Manutenção e taxa de fechamento já consomem a margem base na entrada."
        )
    if not 1 <= alav <= 200:
        avisos.append("A planilha trabalha com alavancagem entre 1x e 200x.")
    resultado["avisos"] = avisos
    return resultado


def validar(lado, modo, saldo, pct, alav, entrada, atual, mmr, taxa):
    if lado not in ("long", "short"):
        raise ValueError("Lado: use Long ou Short.")
    if modo not in ("cross", "isolada"):
        raise ValueError("Modo: use Cross ou Isolada.")
    if saldo < 0:
        raise ValueError("O saldo não pode ser negativo.")
    if not 0 <= pct <= 100:
        raise ValueError("O percentual alocado fica entre 0 e 100. Para 5%, digite 5.")
    if not 1 <= alav <= 200:
        raise ValueError("A alavancagem fica entre 1 e 200.")
    if entrada <= 0:
        raise ValueError("O preço de entrada precisa ser maior que zero.")
    if atual <= 0:
        raise ValueError("O preço atual precisa ser maior que zero.")
    if not 0 <= mmr <= 20:
        raise ValueError("A manutenção é em percentual. Para 0,5%, digite 0,5.")
    if not 0 <= taxa <= 5:
        raise ValueError("A taxa taker é em percentual. Para 0,055%, digite 0,055.")


def obter_json(url, timeout=6):
    pedido = urllib.request.Request(url, headers={"User-Agent": "calculadora-liquidacao"})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as resposta:
            dados = json.load(resposta)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("sem conexão") from exc
    if isinstance(dados, dict) and dados.get("retCode") not in (None, 0):
        raise RuntimeError(dados.get("retMsg") or "recusa da Bybit")
    return dados


def preco_bybit(moeda):
    simbolo = f"{moeda}USDT"
    dados = obter_json(
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={simbolo}"
    )
    lista = (dados.get("result") or {}).get("list") or []
    if not lista:
        raise RuntimeError("par inexistente na Bybit")
    return float(lista[0]["markPrice"])


def preco_coinbase(moeda):
    dados = obter_json(f"https://api.coinbase.com/v2/prices/{moeda}-USD/spot")
    return float(dados["data"]["amount"])


def resolver_cotacao(moeda, manual, entrada):
    if manual:
        return manual, "Manual", ""
    erros = []
    try:
        preco = preco_bybit(moeda)
        return _registrar_cotacao(moeda, preco, "Ao vivo (Bybit, mark)")
    except Exception as exc:
        erros.append(f"Bybit: {exc}")
    try:
        preco = preco_coinbase(moeda)
        return _registrar_cotacao(moeda, preco, "Ao vivo (Coinbase)")
    except Exception as exc:
        erros.append(f"Coinbase: {exc}")
    guardada = ULTIMA_COTACAO.get(moeda)
    if guardada:
        preco, origem, quando = guardada
        hora = time.strftime("%H:%M:%S", time.localtime(quando))
        aviso = "A Bybit e a Coinbase não responderam. A conta segue na última cotação válida, sem usar o preço de entrada."
        return preco, f"Última cotação {hora} · {origem}", aviso
    raise RuntimeError(
        "Sem cotação da Bybit e da Coinbase (" + "; ".join(erros) + "). O preço de entrada não entra no lugar da cotação."
    )


def _registrar_cotacao(moeda, preco, origem):
    ULTIMA_COTACAO[moeda] = (preco, origem, time.time())
    hora = time.strftime("%H:%M:%S", time.localtime())
    return preco, f"{origem} · {hora}", ""


def faixas_bybit(moeda):
    simbolo = f"{moeda}USDT"
    agora = time.time()
    guardado = CACHE_FAIXAS.get(simbolo)
    if guardado and agora - guardado[0] < 300:
        return guardado[1]

    faixas = []
    cursor = ""
    vistos = set()
    while True:
        url = f"https://api.bybit.com/v5/market/risk-limit?category=linear&symbol={simbolo}"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        dados = obter_json(url)
        bloco = dados.get("result") or {}
        for item in bloco.get("list") or []:
            faixas.append(
                {
                    "id": int(item.get("id") or 0),
                    "limite": float(item["riskLimitValue"]),
                    "mmr": float(item["maintenanceMargin"]),
                    "alav_max": float(item["maxLeverage"]),
                }
            )
        cursor = bloco.get("nextPageCursor") or ""
        if not cursor or cursor in vistos:
            break
        vistos.add(cursor)
    faixas.sort(key=lambda faixa: faixa["limite"])
    CACHE_FAIXAS[simbolo] = (agora, faixas)
    return faixas


def escolher_faixa(faixas, nocional):
    if not faixas:
        return None
    for faixa in faixas:
        if nocional <= faixa["limite"] + 1e-6:
            return dict(faixa)
    ultima = dict(faixas[-1])
    ultima["acima"] = True
    return ultima


def texto_faixa(faixa, mmr, alav, nocional):
    if not faixa:
        return ""
    mmr_faixa = faixa["mmr"] * 100
    texto = (
        f"Faixa {faixa['id']} da Bybit: até {br(faixa['limite'], 0)} USDT, "
        f"manutenção {br(mmr_faixa, 2)}%, alavancagem máxima {faixa['alav_max']:g}x."
    )
    if abs(mmr_faixa - mmr) >= 0.01:
        texto += f" Esta conta usa {br(mmr, 2)}%, como na planilha."
    if faixa.get("acima"):
        texto += " A posição passa do limite da última faixa."
    elif alav > faixa["alav_max"] + 1e-9:
        texto += f" {alav:g}x passa do máximo desta faixa."
    return texto


def montar(pedido, atual, origem, aviso_cotacao="", faixa=None):
    validar(
        pedido["lado"],
        pedido["modo"],
        pedido["saldo"],
        pedido["pct"],
        pedido["alav"],
        pedido["entrada"],
        atual,
        pedido["mmr"],
        pedido["taxa"],
    )
    qtd = pedido.get("qtd")
    realizado = pedido.get("realizado") or 0.0
    if qtd is None and not realizado:
        resultado = avaliar(
            pedido["lado"],
            pedido["modo"],
            pedido["saldo"],
            pedido["pct"],
            pedido["alav"],
            pedido["entrada"],
            atual,
            pedido["mmr"],
            pedido["taxa"],
            margem=pedido.get("margem"),
        )
    else:
        resultado = calcular(
            pedido["lado"],
            pedido["modo"],
            pedido["saldo"],
            pedido["pct"],
            pedido["alav"],
            pedido["entrada"],
            atual,
            pedido["mmr"],
            pedido["taxa"],
            qtd=qtd,
            realizado=realizado,
            margem=pedido.get("margem"),
        )
        resultado["tabela"] = avaliar(
            pedido["lado"],
            pedido["modo"],
            pedido["saldo"],
            pedido["pct"],
            pedido["alav"],
            pedido["entrada"],
            atual,
            pedido["mmr"],
            pedido["taxa"],
            margem=pedido.get("margem"),
            qtd=qtd,
            realizado=realizado,
        )["tabela"]
        resultado["avisos"] = []
        if resultado["insustentavel"]:
            resultado["avisos"].append(
                "Manutenção e taxa de fechamento já consomem a margem base na entrada."
            )
        resultado["avisos"].append(
            "A linha marcada da tabela acompanha a posição. As outras alavancagens usam a mesma margem, sem a parcial."
        )
    if aviso_cotacao:
        resultado["avisos"].insert(0, aviso_cotacao)
    if 0 < pedido["pct"] < 1:
        resultado["avisos"].append("Para alocar 5% do saldo, digite 5, não 0,05.")
    resultado["moeda"] = pedido["moeda"]
    resultado["lado"] = pedido["lado"]
    resultado["modo"] = pedido["modo"]
    resultado["alav"] = pedido["alav"]
    resultado["entrada"] = pedido["entrada"]
    resultado["primeiro"] = pedido.get("primeiro") or pedido["entrada"]
    resultado["saldo"] = pedido["saldo"]
    resultado["atual"] = atual
    resultado["origem"] = origem
    resultado["mmr"] = pedido["mmr"]
    resultado["taxa"] = pedido["taxa"]
    resultado["faixa"] = texto_faixa(faixa, pedido["mmr"], pedido["alav"], resultado["nocional"])
    resultado["mmr_faixa"] = None if not faixa else faixa["mmr"] * 100
    return resultado


def linhas_relatorio(resultado, com_tabela=True):
    moeda = resultado["moeda"]
    casas = casas_preco(resultado["entrada"])
    liq = "sem liquidação" if resultado["liq"] is None else br(resultado["liq"], casas)
    dist = "—" if resultado["dist"] is None else br(resultado["dist"] * 100) + "%"
    linhas = [
        f"{moeda}USDT  |  {NOME_LADO[resultado['lado']].upper()}  |  "
        f"{NOME_MODO[resultado['modo']]}  |  {resultado['alav']:g}x",
        f"Entrada {br(resultado['entrada'], casas)}  |  "
        f"Atual {br(resultado['atual'], casas_preco(resultado['atual']))}  |  {resultado['origem']}",
        "",
        f"{'Margem usada':<28}{dinheiro(resultado['margem']) + ' USDT':>18}",
        f"{'Tamanho da posição':<28}{dinheiro(resultado['nocional']) + ' USDT':>18}",
        f"{'Quantidade':<28}{br(resultado['qtd'], 6) + ' ' + moeda:>18}",
        f"{'Margem de manutenção':<28}{dinheiro(resultado['manut']) + ' USDT':>18}",
        f"{'Taxa de fechamento':<28}{dinheiro(resultado['fechamento']) + ' USDT':>18}",
        f"{'Margem base':<28}{dinheiro(resultado['base']) + ' USDT':>18}",
        f"{'Preço de liquidação':<28}{liq:>18}",
        f"{'Distância até a liquidação':<28}{dist:>18}",
        f"{'P&L não realizado':<28}{dinheiro(resultado['pnl']) + ' USDT':>18}",
        f"{'ROE sobre a margem':<28}{br(resultado['roe'] * 100) + '%':>18}",
        f"{'Razão de margem':<28}{br(resultado['razao'] * 100) + '%':>18}",
        f"{'Variação que zera a margem':<28}{br(resultado['zera'] * 100) + '%':>18}",
        f"{'Status':<28}{resultado['status']:>18}",
    ]
    if com_tabela:
        linhas.append("")
        linhas.append(f"{'Alav.':<8}{'Posição':>14}{'Liquidação':>16}{'Distância':>12}")
        for linha in resultado["tabela"]:
            liq_linha = "sem liquidação" if linha["liq"] is None else br(linha["liq"], casas)
            dist_linha = "—" if linha["dist"] is None else br(linha["dist"] * 100) + "%"
            marca = "  ←" if linha["marca"] else ""
            linhas.append(
                f"{linha['alav']:<8g}{dinheiro(linha['nocional']):>14}{liq_linha:>16}{dist_linha:>12}{marca}"
            )
    if resultado["faixa"]:
        linhas.extend(["", resultado["faixa"]])
    for aviso in resultado["avisos"]:
        linhas.append("Aviso: " + aviso)
    return linhas


def imprimir(resultado, com_tabela=True):
    print("=" * 64)
    print("\n".join(linhas_relatorio(resultado, com_tabela)))
    print("=" * 64)


def ciclo(args):
    try:
        moeda = normalizar_moeda(args.moeda)
    except ValueError as exc:
        print(exc)
        return False
    manual = args.preco if args.preco else None
    try:
        atual, origem, aviso = resolver_cotacao(moeda, manual, args.entrada)
        faixa = None
        try:
            faixa = escolher_faixa(faixas_bybit(moeda), args.saldo * args.pct / 100 * args.alav)
        except Exception:
            faixa = None
        resultado = montar(
            {
                "moeda": moeda,
                "lado": args.lado,
                "modo": args.modo,
                "saldo": args.saldo,
                "pct": args.pct,
                "alav": args.alav,
                "entrada": args.entrada,
                "mmr": args.mmr,
                "taxa": args.taxa,
            },
            atual,
            origem,
            aviso,
            faixa,
        )
    except (ValueError, RuntimeError) as exc:
        print(exc)
        return False
    except Exception as exc:
        print(f"Não consegui calcular: {exc}")
        return False
    imprimir(resultado, com_tabela=not args.sem_tabela)
    return True


def perguntar(texto, padrao, tipo="texto", opcoes=None):
    while True:
        try:
            resp = input(f"{texto} [{padrao}]: ").strip()
        except EOFError:
            return padrao if tipo == "texto" and not opcoes else padrao
        if not resp:
            if opcoes:
                return opcoes[str(padrao).strip().lower()]
            if tipo == "opcional":
                if padrao in ("", None):
                    return None
                return float(str(padrao).replace(",", "."))
            if tipo == "num":
                return float(str(padrao).replace(",", "."))
            return padrao
        if opcoes:
            chave = resp.lower()
            if chave not in opcoes:
                print("Use: " + ", ".join(dict.fromkeys(opcoes.values())))
                continue
            return opcoes[chave]
        if tipo == "texto":
            return resp
        if tipo == "opcional":
            if resp in ("-", "nao", "não", "n"):
                return None
            try:
                return float(resp.replace("%", "").replace(" ", "").replace(",", "."))
            except ValueError:
                print("Digite um número, ou deixe vazio para a cotação automática.")
                continue
        try:
            return float(resp.replace("%", "").replace(" ", "").replace(",", "."))
        except ValueError:
            print("Digite um número. A vírgula também vale.")


def loop_interativo(args):
    while True:
        try:
            args.moeda = perguntar("Moeda", args.moeda)
            args.lado = perguntar("Lado (long/short)", NOME_LADO[args.lado], opcoes=LADOS)
            args.modo = perguntar("Modo (cross/isolada)", NOME_MODO[args.modo], opcoes=MODOS)
            args.saldo = perguntar("Saldo disponível (USDT)", campo_num(args.saldo), "num")
            args.pct = perguntar("% do capital alocado (5 = 5%)", campo_num(args.pct), "num")
            args.alav = perguntar("Alavancagem", campo_num(args.alav), "num")
            args.entrada = perguntar("Preço de entrada", campo_num(args.entrada), "num")
            manual = perguntar(
                "Preço atual manual (vazio = automático)",
                "" if not args.preco else campo_num(args.preco),
                "opcional",
            )
            args.preco = manual
            args.mmr = perguntar("Taxa de manutenção % (0,5 = 0,5%)", campo_num(args.mmr), "num")
            args.taxa = perguntar("Taxa taker % (0,055 = 0,055%)", campo_num(args.taxa), "num")
        except KeyboardInterrupt:
            print()
            return 0
        ciclo(args)
        try:
            de_novo = input("Calcular de novo? [s/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if de_novo not in ("s", "sim"):
            return 0


def ler_args(argv):
    parser = argparse.ArgumentParser(
        description="LiqGuard (futuros perpétuos USDT)"
    )
    parser.add_argument("--moeda", default="BTC")
    parser.add_argument("--lado", type=normalizar_lado, default="long")
    parser.add_argument("--modo", type=normalizar_modo, default="cross")
    parser.add_argument("--saldo", type=numero, default=88.10, help="saldo disponível em USDT")
    parser.add_argument("--pct", type=numero, default=5, help="percentual do saldo usado como margem; 5 = 5%%")
    parser.add_argument("--alav", type=numero, default=150)
    parser.add_argument("--entrada", type=numero, default=60450, help="preço médio de entrada")
    parser.add_argument("--preco", type=numero, help="preço atual manual; se omitido, busca cotação")
    parser.add_argument("--mmr", type=numero, default=0.5, help="manutenção em %%; 0,5 = 0,5%%")
    parser.add_argument("--taxa", type=numero, default=0.055, help="taxa taker em %%; 0,055 = 0,055%%")
    parser.add_argument("--watch", type=numero, metavar="SEG", help="atualiza a cada SEG segundos")
    parser.add_argument("--sem-tabela", action="store_true")
    parser.add_argument("--interativo", action="store_true")
    parser.add_argument("--gui", action="store_true", help="abre a janela")
    parser.add_argument("--cli", action="store_true", help="mostra o resultado no terminal")
    return parser.parse_args(argv)


class App:
    def __init__(self, root, args, ao_abrir=True):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.vivo = True
        self.geracao = 0
        self.after_id = None
        self.mmr_sugerido = None

        root.title("LiqGuard")
        root.minsize(880, 620)
        root.configure(bg="#e7ebf2")
        root.protocol("WM_DELETE_WINDOW", self.fechar)

        self.moeda = tk.StringVar(value=args.moeda)
        self.lado = tk.StringVar(value=NOME_LADO[args.lado])
        self.modo = tk.StringVar(value=NOME_MODO[args.modo])
        self.saldo = tk.StringVar(value="0")
        self.pct = tk.StringVar(value=campo_num(args.pct))
        self.alav = tk.StringVar(value=campo_num(args.alav))
        self.entrada = tk.StringVar(value=campo_num(args.entrada))
        self.manual = tk.StringVar(value="" if not args.preco else campo_num(args.preco))
        self.mmr = tk.StringVar(value=campo_num(args.mmr))
        self.taxa = tk.StringVar(value=campo_num(args.taxa))
        self.margem_var = tk.StringVar(value="0")
        self.parcial = tk.StringVar(value="")
        self.nova = tk.StringVar(value="")
        self.alvo = tk.StringVar(value="")
        self.auto = tk.BooleanVar(value=True)
        self.entrada_original = None
        self.primeiro_preco = None
        self.preco_medio = None
        self.qtd_fixa = None
        self.alav_qtd = None
        self.margem_qtd = None
        self.realizado = 0.0
        self.medio_texto = None
        self.equilibrio_texto = None
        self.modo_parcial = False
        self.operacao_travada = False
        self.media_equilibrio = False
        self.primeiro_celula = None
        self.preco_travado = bool(args.preco)
        self._sincronizando = False
        self._par_job = None
        self._alav_job = None
        self._margem_job = None
        self.campo_preco = None
        self.campo_entrada = None
        self.campo_margem = None
        self.ultimo = None
        self.ultima_qtd = None
        self.alvo_ativo = False

        self._montar_tela()
        root.bind("<Return>", lambda _evento: self.recalcular())
        if ao_abrir:
            root.after(150, self.recalcular)
            root.after(400, self.agendar)

    def _montar_marca(self, cabeca):
        tk = self.tk
        pasta = Path(__file__).resolve().parent
        logo = pasta / "logo-cabeca.png"
        icone = pasta / "icone-192.png"
        if logo.is_file():
            self._logo_img = tk.PhotoImage(file=str(logo))
            tk.Label(cabeca, image=self._logo_img, bg="#0f1c2c", borderwidth=0).pack(
                anchor="w", padx=16, pady=(12, 0)
            )
            tk.Label(
                cabeca,
                text="Gestão de risco e liquidação em tempo real",
                bg="#0f1c2c",
                fg="#c5d0e0",
                font=("Segoe UI", 10),
            ).pack(anchor="w", padx=18, pady=(8, 12))
        else:
            tk.Label(
                cabeca,
                text="LiqGuard",
                bg="#0f1c2c",
                fg="white",
                font=("Segoe UI", 18, "bold"),
            ).pack(anchor="w", padx=18, pady=(12, 0))
            tk.Label(
                cabeca,
                text="Gestão de risco e liquidação em tempo real",
                bg="#0f1c2c",
                fg="#c5d0e0",
                font=("Segoe UI", 10),
            ).pack(anchor="w", padx=18, pady=(0, 12))
        if icone.is_file():
            self._icone_img = tk.PhotoImage(file=str(icone))
            self.root.iconphoto(True, self._icone_img)

    def _montar_tela(self):
        tk = self.tk
        ttk = self.ttk
        cabeca = tk.Frame(self.root, bg="#0f1c2c")
        cabeca.pack(fill="x")
        self._montar_marca(cabeca)

        corpo = tk.Frame(self.root, bg="#e7ebf2")
        corpo.pack(fill="both", expand=True, padx=14, pady=12)
        corpo.columnconfigure(1, weight=1)
        corpo.rowconfigure(0, weight=1)

        entradas = tk.Frame(corpo, bg="white", highlightbackground="#d5dbe6", highlightthickness=1)
        entradas.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        tk.Label(
            entradas,
            text="ENTRADAS",
            bg="white",
            fg="#5c6b82",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 2))
        tk.Label(
            entradas,
            text="Saldo começa em 0. A margem usada é o fator do lucro e da perda. O par puxa a cotação.",
            bg="white",
            fg="#5c6b82",
            font=("Segoe UI", 8),
            wraplength=320,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

        campos = [
            ("Par", self.moeda, None, True, "par"),
            ("Lado", self.lado, ("Long", "Short"), True, None),
            ("Modo de margem", self.modo, ("Cross", "Isolada"), True, None),
            ("Saldo disponível (USDT)", self.saldo, None, True, None),
            ("% do capital alocado", self.pct, None, True, None),
            ("Margem usada (USDT)", self.margem_var, None, True, "margem"),
            ("Alavancagem (x)", self.alav, None, True, "alav"),
            ("Preço médio", self.entrada, None, True, "entrada"),
            ("Primeiro preço", None, None, True, "primeiro"),
            ("Preço atual usado", self.manual, None, True, "preco"),
            ("Preço alvo", self.alvo, None, True, "alvo"),
            ("Nova entrada (USDT)", self.nova, None, True, "nova"),
            ("Realizar parcial (%)", self.parcial, None, True, "parcial"),
            ("Taxa de manutenção (%)", self.mmr, None, False, None),
            ("Taxa taker (%)", self.taxa, None, False, None),
        ]
        for indice, (rotulo, variavel, opcoes, verde, papel) in enumerate(campos, start=2):
            tk.Label(
                entradas,
                text=rotulo,
                bg="white",
                fg="#00B050" if verde else "#1d2736",
                font=("Segoe UI", 9, "bold" if verde else "normal"),
            ).grid(row=indice, column=0, sticky="w", padx=(14, 8), pady=3)
            if papel == "primeiro":
                self.primeiro_celula = tk.Label(
                    entradas,
                    text="—",
                    bg="#eef8f1",
                    fg="#0d6b32",
                    font=("Segoe UI", 10, "bold"),
                    anchor="w",
                    padx=6,
                )
                self.primeiro_celula.grid(row=indice, column=1, sticky="ew", padx=(0, 14), pady=3)
                continue
            if opcoes:
                caixa = ttk.Combobox(
                    entradas, textvariable=variavel, values=opcoes, state="readonly", width=16
                )
                caixa.grid(row=indice, column=1, sticky="ew", padx=(0, 14), pady=3)
                caixa.bind("<<ComboboxSelected>>", lambda _evento, var=variavel: self._ao_sair(var))
            else:
                campo = tk.Entry(
                    entradas,
                    textvariable=variavel,
                    width=18,
                    bg="#d8f3dc" if verde else "#fff4cc",
                    fg="#0d6b32" if verde else "#1d2736",
                    relief="flat",
                    font=("Segoe UI", 10),
                    highlightthickness=1,
                    highlightbackground="#00B050" if verde else "#e2c56a",
                )
                campo.grid(row=indice, column=1, sticky="ew", padx=(0, 14), pady=3)
                campo.bind("<FocusOut>", lambda _evento, var=variavel: self._ao_sair(var))
                if papel == "par":
                    campo.bind("<KeyRelease>", self._par_digitado)
                elif papel == "alav":
                    campo.bind("<KeyRelease>", self._alav_digitada)
                elif papel == "margem":
                    self.campo_margem = campo
                    campo.bind("<KeyRelease>", self._margem_digitada)
                elif papel == "preco":
                    self.campo_preco = campo
                elif papel == "entrada":
                    self.campo_entrada = campo

        botoes = tk.Frame(entradas, bg="white")
        botoes.grid(row=17, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 6))
        tk.Button(
            botoes,
            text="Calcular",
            command=self.recalcular,
            bg="#142033",
            fg="white",
            activebackground="#24344d",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side="left")
        tk.Button(
            botoes,
            text="Adicionar entrada",
            command=self.adicionar,
            bg="#00B050",
            fg="white",
            activebackground="#0d6b32",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=6,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            botoes,
            text="Realizar parcial",
            command=self.realizar,
            bg="#00B050",
            fg="white",
            activebackground="#0d6b32",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=6,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            botoes,
            text="Usar MMR da Bybit",
            command=self.usar_mmr,
            bg="#e7ebf2",
            fg="#142033",
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=6,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            entradas,
            text="Preço Alvo Manual",
            command=self.preco_alvo,
            bg="#00B050",
            fg="white",
            activebackground="#0d6b32",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=6,
        ).grid(row=18, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 6))
        tk.Button(
            entradas,
            text="Zerar simulação",
            command=self.zerar_simulacao,
            bg="white",
            fg="#8a3b3b",
            activebackground="#f8f4f4",
            activeforeground="#8a3b3b",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=6,
        ).grid(row=19, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 6))
        tk.Checkbutton(
            entradas,
            text="Cotação do par em tempo real (5 s)",
            variable=self.auto,
            command=self.agendar,
            bg="white",
            fg="#1d2736",
            activebackground="white",
            font=("Segoe UI", 9),
        ).grid(row=20, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 12))

        direita = tk.Frame(corpo, bg="#e7ebf2")
        direita.grid(row=0, column=1, sticky="nsew")
        direita.columnconfigure(0, weight=1)

        self.titulo = tk.Label(
            direita,
            text="Aguardando cálculo",
            bg="#e7ebf2",
            fg="#142033",
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        )
        self.titulo.grid(row=0, column=0, sticky="ew")
        self.cotacao = tk.Label(
            direita,
            text="",
            bg="#e7ebf2",
            fg="#5c6b82",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.cotacao.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        painel = tk.Frame(direita, bg="white", highlightbackground="#d5dbe6", highlightthickness=1)
        painel.grid(row=2, column=0, sticky="ew")
        for coluna in (1, 3):
            painel.columnconfigure(coluna, weight=1)

        self.status = tk.Label(
            painel,
            text="—",
            bg="#eef2f7",
            fg="#142033",
            font=("Segoe UI", 12, "bold"),
            anchor="w",
            padx=12,
            pady=8,
        )
        self.status.grid(row=0, column=0, columnspan=4, sticky="ew")

        self.rotulos = {}
        self.legendas = {}
        linhas = [
            (("margem", "Margem usada"), ("liq", "Preço de liquidação")),
            (("nocional", "Tamanho da posição"), ("dist", "Distância até a liquidação")),
            (("qtd", "Quantidade"), ("pnl", "P&L não realizado")),
            (("manut", "Margem de manutenção"), ("roe", "ROE sobre a margem")),
            (("fechamento", "Taxa de fechamento"), ("razao", "Razão de margem")),
            (("base", "Margem base"), ("zera", "Variação que zera a margem")),
            (("realizado", "Lucro realizado"), ("medio", "Preço médio ajustado")),
            (("primeiro", "Primeiro preço"), ("qtd_add", "Última entrada")),
            (("alvo_pnl", "P&L no preço alvo"), ("alvo_roe", "ROE no preço alvo")),
        ]
        for indice, pares in enumerate(linhas, start=1):
            for coluna, (chave, rotulo) in enumerate(pares):
                legenda = tk.Label(painel, text=rotulo, bg="white", fg="#5c6b82", font=("Segoe UI", 9))
                legenda.grid(row=indice, column=coluna * 2, sticky="w", padx=(12, 6), pady=3)
                self.legendas[chave] = legenda
                valor = tk.Label(painel, text="—", bg="white", fg="#142033", font=("Segoe UI", 10, "bold"))
                valor.grid(row=indice, column=coluna * 2 + 1, sticky="e", padx=(0, 12), pady=3)
                self.rotulos[chave] = valor

        tabela_quadro = tk.Frame(direita, bg="white", highlightbackground="#d5dbe6", highlightthickness=1)
        tabela_quadro.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        tk.Label(
            tabela_quadro,
            text="MESMO CAPITAL, OUTRAS ALAVANCAGENS",
            bg="white",
            fg="#5c6b82",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", padx=10, pady=(8, 0))
        colunas = ("alav", "posicao", "liq", "dist")
        self.tabela = ttk.Treeview(tabela_quadro, columns=colunas, show="headings", height=8)
        for chave, titulo, largura in (
            ("alav", "Alavancagem", 110),
            ("posicao", "Posição (USDT)", 150),
            ("liq", "Preço de liquidação", 170),
            ("dist", "Distância", 120),
        ):
            self.tabela.heading(chave, text=titulo)
            self.tabela.column(chave, width=largura, anchor="e")
        self.tabela.column("alav", anchor="w")
        self.tabela.tag_configure("atual", background="#fff4cc")
        self.tabela.pack(fill="x", padx=8, pady=8)

        self.nota = tk.Label(
            direita,
            text="",
            bg="#e7ebf2",
            fg="#5c6b82",
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self.nota.grid(row=4, column=0, sticky="ew", pady=(8, 14))
        self.root.update_idletasks()
        largura = min(self.root.winfo_reqwidth() + 16, self.root.winfo_screenwidth() - 40)
        altura = min(self.root.winfo_reqheight() + 56, self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{largura}x{altura}")

    def _campos(self):
        return (
            self.moeda,
            self.lado,
            self.modo,
            self.saldo,
            self.pct,
            self.margem_var,
            self.alav,
            self.entrada,
            self.manual,
            self.parcial,
            self.nova,
            self.alvo,
            self.mmr,
            self.taxa,
        )

    def _marcar_vistos(self):
        for variavel in self._campos():
            variavel._visto = variavel.get()

    def _ao_sair(self, variavel):
        if not self.vivo or self._sincronizando:
            return
        if getattr(variavel, "_visto", None) == variavel.get():
            return
        if variavel is self.manual:
            self.preco_travado = bool(self.manual.get().strip())
        if variavel is self.pct and not self.operacao_travada:
            self._margem_desde_pct()
        elif variavel is self.margem_var:
            self._pct_desde_margem()
        elif variavel in (self.lado, self.moeda):
            self._reiniciar_operacao()
        self.recalcular()

    def _par_digitado(self, _evento=None):
        if self._par_job:
            self.root.after_cancel(self._par_job)
        self._par_job = self.root.after(450, self._par_aplicar)

    def _alav_digitada(self, _evento=None):
        if self._alav_job:
            self.root.after_cancel(self._alav_job)
        self._alav_job = self.root.after(300, self._alav_aplicar)

    def _alav_aplicar(self):
        self._alav_job = None
        if self.vivo:
            self.recalcular()

    def _margem_digitada(self, _evento=None):
        if self._margem_job:
            self.root.after_cancel(self._margem_job)
        self._margem_job = self.root.after(300, self._margem_aplicar)

    def _margem_aplicar(self):
        self._margem_job = None
        if not self.vivo:
            return
        self._pct_desde_margem()
        self.recalcular()

    def _par_aplicar(self):
        self._par_job = None
        if not self.vivo:
            return
        self.preco_travado = False
        self._sincronizando = True
        self.manual.set("")
        self._sincronizando = False
        self._reiniciar_operacao()
        try:
            moeda = normalizar_moeda(self.moeda.get())
            try:
                preco = preco_bybit(moeda)
            except Exception:
                preco = preco_coinbase(moeda)
        except Exception:
            preco = None
        if preco:
            texto = campo_num(round(preco, casas_preco(preco)))
            self._sincronizando = True
            self.entrada.set(texto)
            self.entrada._visto = texto
            self._sincronizando = False
        self.recalcular()

    def _reiniciar_operacao(self):
        self.qtd_fixa = None
        self.alav_qtd = None
        self.margem_qtd = None
        self.realizado = 0.0
        self.preco_medio = None
        self.primeiro_preco = None
        self.medio_texto = None
        self.equilibrio_texto = None
        self.modo_parcial = False
        self.operacao_travada = False
        self.media_equilibrio = False
        self.ultima_qtd = None
        self.alvo_ativo = False

    def zerar_simulacao(self):
        self._sincronizando = True
        for variavel, valor in (
            (self.saldo, "0"),
            (self.pct, ""),
            (self.margem_var, ""),
            (self.alav, ""),
            (self.entrada, ""),
            (self.manual, ""),
            (self.alvo, ""),
            (self.nova, ""),
            (self.parcial, ""),
        ):
            variavel.set(valor)
            variavel._visto = valor
        self._sincronizando = False
        self.preco_travado = False
        self._reiniciar_operacao()
        self.ultimo = None
        self.titulo.config(text="Aguardando cálculo")
        self.cotacao.config(text="")
        self.status.config(text="—", bg="#eef2f7", fg="#142033")
        if self.primeiro_celula is not None:
            self.primeiro_celula.config(text="—")
        for rotulo in getattr(self, "rotulos", {}).values():
            rotulo.config(text="—", fg="#142033")
        if getattr(self, "tabela", None) is not None:
            filhos = self.tabela.get_children()
            if filhos:
                self.tabela.delete(*filhos)
        self.nota.config(
            text="Simulação zerada. Informe a margem, a alavancagem e o preço médio para começar de novo."
        )

    def _limpar_parcial(self, restaurar_entrada=False):
        if not self.modo_parcial and self.qtd_fixa is None:
            return
        if restaurar_entrada and self.entrada_original is not None:
            self._sincronizando = True
            self.entrada.set(campo_num(self.entrada_original))
            self._sincronizando = False
        self.modo_parcial = False
        self.qtd_fixa = None
        self.alav_qtd = None
        self.margem_qtd = None
        self.realizado = 0.0
        self.equilibrio_texto = None

    def _margem_desde_pct(self):
        try:
            saldo = numero(self.saldo.get() or "0")
            pct = numero(self.pct.get() or "0")
        except (ValueError, argparse.ArgumentTypeError):
            return
        if saldo <= 0:
            return
        self._sincronizando = True
        self.margem_var.set(campo_num(saldo * pct / 100))
        self.margem_var._visto = self.margem_var.get()
        self._sincronizando = False

    def _pct_desde_margem(self):
        try:
            saldo = numero(self.saldo.get())
            margem = numero(self.margem_var.get())
        except (ValueError, argparse.ArgumentTypeError):
            return
        if saldo <= 0:
            return
        self._sincronizando = True
        self.pct.set(campo_num(margem / saldo * 100))
        self.pct._visto = self.pct.get()
        self._sincronizando = False

    def _margem_desta_simulacao(self, qtd, realizado, alav, margem, entrada):
        if abs(realizado or 0) > 1e-8:
            return self._qtd_ajustada(qtd, alav, margem), realizado
        pela = margem * alav / entrada if entrada else None
        ajustada = self._qtd_ajustada(qtd, alav, margem) if qtd is not None else None
        if not pela or not ajustada or abs(ajustada - pela) / pela > 1e-4:
            self.qtd_fixa = None
            self.alav_qtd = None
            self.margem_qtd = None
            self.ultima_qtd = None
            self.operacao_travada = False
            self.media_equilibrio = False
        return None, 0.0

    def _qtd_ajustada(self, qtd, alav, margem):
        if qtd is None:
            return None
        if self.alav_qtd and abs(alav - self.alav_qtd) > 1e-9:
            qtd *= alav / self.alav_qtd
        if self.margem_qtd and abs(margem - self.margem_qtd) > 1e-9:
            qtd *= margem / self.margem_qtd
        return qtd

    def fechar(self):
        self.vivo = False
        self.geracao += 1
        if self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
        self.root.destroy()

    def agendar(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.vivo and self.auto.get():
            self.after_id = self.root.after(5000, self._tick)

    def _tick(self):
        self.after_id = None
        if self.vivo and self.auto.get():
            self.recalcular()
            self.agendar()

    def usar_mmr(self):
        if self.mmr_sugerido is None:
            self.nota.config(
                text="Calcule uma vez com internet para ler a faixa de risco da Bybit."
            )
            return
        self.mmr.set(campo_num(self.mmr_sugerido))
        self.recalcular()

    def _campos_abertos(self):
        margem_add = 0.0
        fracao = 0.0
        if self.nova.get().strip():
            valor = numero(self.nova.get())
            if valor > 0:
                margem_add = valor
        if self.parcial.get().strip():
            valor = numero(self.parcial.get())
            if 0 < valor < 100:
                fracao = valor / 100
        return margem_add, fracao

    def _com_campos(self, pedido, atual, margem_add, fracao):
        if margem_add <= 0 and fracao <= 0:
            return pedido
        efeito = compor_operacao(
            pedido["lado"],
            pedido["entrada"],
            pedido["qtd"],
            pedido["realizado"],
            pedido["margem"],
            pedido["alav"],
            pedido["taxa"],
            atual,
            margem_add,
            fracao,
        )
        novo = dict(pedido)
        novo["entrada"] = efeito["entrada"]
        novo["qtd"] = efeito["qtd"]
        novo["realizado"] = efeito["realizado"]
        novo["margem"] = efeito["margem"]
        novo["media_equilibrio"] = fracao > 0 and margem_add <= 0
        if efeito["qtd_add"]:
            novo["qtd_add_preview"] = efeito["qtd_add"]
        return novo

    def pedido(self):
        moeda = normalizar_moeda(self.moeda.get())
        lado = normalizar_lado(self.lado.get())
        modo = normalizar_modo(self.modo.get())
        saldo = numero(self.saldo.get() or "0")
        pct = numero(self.pct.get() or "0")
        alav = numero(self.alav.get())
        margem = numero(self.margem_var.get() or "0")
        entrada_widget = numero(self.entrada.get())
        mmr = numero(self.mmr.get())
        taxa = numero(self.taxa.get())
        manual_texto = self.manual.get().strip()
        if self.preco_travado and manual_texto:
            manual = numero(manual_texto)
        else:
            manual = None
        if margem <= 0:
            raise ValueError("Informe a margem usada (USDT). O lucro e a perda usam esse valor.")
        texto_entrada = self.entrada.get().strip()
        if self.qtd_fixa is not None and texto_entrada == (self.medio_texto or ""):
            entrada = self.preco_medio
            qtd = self.qtd_fixa
            realizado = self.realizado
        elif self.qtd_fixa is not None:
            self.preco_medio = entrada_widget
            self.medio_texto = texto_entrada
            entrada = entrada_widget
            qtd = self.qtd_fixa
            realizado = self.realizado
        else:
            entrada = entrada_widget
            qtd = None
            realizado = 0.0
            self.preco_medio = entrada
            self.realizado = 0.0
            if not self.operacao_travada:
                self.primeiro_preco = entrada
        qtd, realizado = self._margem_desta_simulacao(qtd, realizado, alav, margem, entrada)
        if self.primeiro_preco is None:
            self.primeiro_preco = entrada
        return {
            "moeda": moeda,
            "lado": lado,
            "modo": modo,
            "saldo": saldo,
            "pct": pct,
            "alav": alav,
            "margem": margem,
            "entrada": entrada,
            "mmr": mmr,
            "taxa": taxa,
            "manual": manual,
            "qtd": qtd,
            "realizado": realizado,
            "primeiro": self.primeiro_preco,
        }

    def realizar(self):
        try:
            fracao = numero(self.parcial.get()) / 100 if self.parcial.get().strip() else None
        except (ValueError, argparse.ArgumentTypeError):
            self.nota.config(text="Informe o percentual da parcial. 50 fecha metade da posição.")
            return
        if fracao is None:
            self.nota.config(text="Preencha Realizar parcial (%). 50 fecha metade da posição.")
            return
        if self.ultimo is None:
            self.nota.config(text="Calcule a posição antes de realizar.")
            return
        pacote = self.ultimo
        medio = self.preco_medio if self.preco_medio is not None else pacote["entrada"]
        alav = numero(self.alav.get())
        margem = numero(self.margem_var.get() or "0")
        qtd = self._qtd_ajustada(self.qtd_fixa, alav, margem) if self.qtd_fixa is not None else margem * alav / medio
        try:
            efeito = realizar_parcial(
                pacote["lado"],
                medio,
                qtd,
                self.realizado,
                pacote["atual"],
                pacote["taxa"],
                fracao,
            )
        except ValueError as exc:
            self.nota.config(text=str(exc))
            return
        if self.primeiro_preco is None:
            self.primeiro_preco = medio
        self.preco_medio = medio
        self.qtd_fixa = efeito["qtd"]
        self.alav_qtd = numero(self.alav.get())
        self.margem_qtd = numero(self.margem_var.get() or "0")
        self.realizado = efeito["realizado"]
        self.modo_parcial = True
        self.operacao_travada = True
        self.media_equilibrio = True
        self.medio_texto = campo_num(round(efeito["equilibrio"], casas_preco(efeito["equilibrio"])))
        self.equilibrio_texto = campo_num(efeito["equilibrio"])
        self._sincronizando = True
        self.entrada.set(self.medio_texto)
        self.parcial.set("")
        self._sincronizando = False
        self.nota.config(
            text=(
                f"Parcial de {dinheiro(efeito['liquido'])} USDT. "
                f"Primeiro preço {campo_num(self.primeiro_preco)}. "
                f"Preço médio da posição {self.medio_texto}. "
                f"Equilíbrio {self.equilibrio_texto}."
            )
        )
        self.recalcular(rede=False)

    def adicionar(self):
        try:
            margem_add = numero(self.nova.get()) if self.nova.get().strip() else None
        except (ValueError, argparse.ArgumentTypeError):
            self.nota.config(text="Informe a nova entrada em USDT de margem.")
            return
        if margem_add is None:
            self.nota.config(text="Preencha Nova entrada (USDT) com a margem adicional.")
            return
        if self.ultimo is None:
            self.nota.config(text="Informe a margem usada e o preço médio antes de adicionar.")
            return
        pacote = self.ultimo
        medio = self.preco_medio if self.preco_medio is not None else pacote["entrada"]
        alav = numero(self.alav.get())
        margem = numero(self.margem_var.get() or "0")
        qtd = self._qtd_ajustada(self.qtd_fixa, alav, margem) if self.qtd_fixa is not None else margem * alav / medio
        try:
            efeito = nova_entrada(
                pacote["lado"],
                medio,
                qtd,
                pacote["atual"],
                margem_add,
                alav,
            )
        except ValueError as exc:
            self.nota.config(text=str(exc))
            return
        if self.primeiro_preco is None:
            self.primeiro_preco = medio
        self.preco_medio = efeito["medio"]
        nova_margem = margem + margem_add
        self.qtd_fixa = efeito["qtd"]
        self.alav_qtd = alav
        self.margem_qtd = nova_margem
        self.ultima_qtd = efeito["qtd_add"]
        self.operacao_travada = True
        self.media_equilibrio = False
        self.modo_parcial = self.realizado != 0
        self.medio_texto = campo_num(round(efeito["medio"], casas_preco(efeito["medio"])))
        self._sincronizando = True
        self.entrada.set(self.medio_texto)
        self.margem_var.set(campo_num(nova_margem))
        self.nova.set("")
        self._sincronizando = False
        sentido = "melhorou" if efeito["melhorou"] else "piorou"
        self.nota.config(
            text=(
                f"Nova entrada de {dinheiro(margem_add)} USDT de margem. "
                f"O preço médio {sentido} para {self.medio_texto}. "
                f"Primeiro preço permanece {campo_num(self.primeiro_preco)}."
            )
        )
        self.recalcular(rede=False)

    def preco_alvo(self):
        texto = self.alvo.get().strip()
        if not texto:
            self.alvo_ativo = False
            self._limpar_alvo()
            self.nota.config(text="Informe o preço alvo para simular o lucro ou a perda.")
            return
        try:
            alvo = numero(texto)
        except (ValueError, argparse.ArgumentTypeError):
            self.nota.config(text="O preço alvo precisa ser um número. Exemplo: 65000.")
            return
        if alvo <= 0:
            self.nota.config(text="O preço alvo precisa ser maior que zero.")
            return
        self.preco_travado = False
        self.alvo_ativo = True
        self.recalcular(rede=True)

    def _limpar_alvo(self):
        if "alvo_pnl" not in self.rotulos:
            return
        self.rotulos["alvo_pnl"].config(text="—", fg="#142033")
        self.rotulos["alvo_roe"].config(text="—", fg="#142033")
        self.rotulos["pnl"].config(fg="#142033")
        self.rotulos["roe"].config(fg="#142033")
        self.legendas["pnl"].config(text="P&L não realizado")
        self.legendas["roe"].config(text="ROE sobre a margem")
        self.legendas["alvo_pnl"].config(text="P&L no preço alvo")
        self.legendas["alvo_roe"].config(text="ROE no preço alvo")

    def _texto_alvo(self, pacote):
        if not self.alvo_ativo or not self.alvo.get().strip():
            self._limpar_alvo()
            return ""
        try:
            efeito = simular_no_preco(
                pacote["lado"],
                pacote["entrada"],
                pacote["qtd"],
                pacote["margem"],
                pacote["taxa"],
                numero(self.alvo.get()),
            )
        except (ValueError, argparse.ArgumentTypeError) as exc:
            self._limpar_alvo()
            return str(exc)
        cor = "#0f7a45" if efeito["pnl"] >= 0 else "#b42318"
        cor_vivo = "#0f7a45" if pacote["pnl"] >= 0 else "#b42318"
        nome = "Lucro" if efeito["pnl"] >= 0 else "Perda"
        casas = casas_preco(pacote["entrada"])
        casas_alvo = casas_preco(efeito["alvo"])
        casas_atual = casas_preco(pacote["atual"])
        self.legendas["pnl"].config(text="P&L no preço alvo")
        self.legendas["roe"].config(text="ROE no preço alvo")
        self.legendas["alvo_pnl"].config(text="P&L na cotação")
        self.legendas["alvo_roe"].config(text="ROE na cotação")
        self.rotulos["pnl"].config(text=dinheiro(efeito["pnl"]) + " USDT", fg=cor)
        self.rotulos["roe"].config(text=br(efeito["roe"] * 100) + "%", fg=cor)
        self.rotulos["alvo_pnl"].config(text=dinheiro(pacote["pnl"]) + " USDT", fg=cor_vivo)
        self.rotulos["alvo_roe"].config(text=br(pacote["roe"] * 100) + "%", fg=cor_vivo)
        self.cotacao.config(
            text=(
                f"Preço médio {br(pacote['entrada'], casas)}    "
                f"Cotação ao vivo {br(pacote['atual'], casas_atual)}    "
                f"Alvo {br(efeito['alvo'], casas_alvo)}    "
                f"{pacote['origem']}"
            )
        )
        return (
            f"Simulação a partir da cotação ao vivo {br(pacote['atual'], casas_atual)}. "
            f"Preço de liquidação e distância usam essa cotação. "
            f"Preço médio da posição permanece {br(pacote['entrada'], casas)}. "
            f"{nome} no alvo {br(efeito['alvo'], casas_alvo)}: {dinheiro(efeito['pnl'])} USDT, "
            f"ROE {br(efeito['roe'] * 100)}% sobre a margem. "
            f"Se fechar no alvo, o líquido fica {dinheiro(efeito['liquido'])} USDT depois da taxa taker."
        )

    def recalcular(self, rede=True):
        try:
            pedido = self.pedido()
        except (ValueError, argparse.ArgumentTypeError) as exc:
            self.nota.config(text=str(exc))
            if rede and self.auto.get():
                self._cotacao_solta()
            return
        self.geracao += 1
        geracao = self.geracao
        try:
            margem_add, fracao = self._campos_abertos()
        except (ValueError, argparse.ArgumentTypeError):
            margem_add, fracao = 0.0, 0.0
        if not rede:
            if self.ultimo is not None:
                atual = self.ultimo["atual"]
                origem = self.ultimo["origem"]
            elif pedido["manual"]:
                atual = pedido["manual"]
                origem = "Manual"
            else:
                self.nota.config(text="Ainda não há cotação válida. A entrada não entra no lugar do preço.")
                return
            try:
                pacote = montar(self._com_campos(pedido, atual, margem_add, fracao), atual, origem)
            except ValueError as exc:
                self.nota.config(text=str(exc))
                return
            self._aplicar(geracao, pacote)
            return

        if self.titulo.cget("text") == "Aguardando cálculo":
            self.cotacao.config(text="Buscando cotação...")

        def trabalho():
            try:
                atual, origem, aviso = resolver_cotacao(
                    pedido["moeda"], pedido["manual"], pedido["entrada"]
                )
                conta = self._com_campos(pedido, atual, margem_add, fracao)
                faixa = None
                try:
                    nocional = conta["qtd"] * conta["entrada"] if conta.get("qtd") else conta["margem"] * conta["alav"]
                    faixa = escolher_faixa(faixas_bybit(pedido["moeda"]), nocional)
                except Exception:
                    faixa = None
                pacote = montar(conta, atual, origem, aviso, faixa)
            except Exception as exc:
                self.root.after(0, lambda erro=exc: self._falha(geracao, erro))
                return
            self.root.after(0, lambda: self._aplicar(geracao, pacote))

        threading.Thread(target=trabalho, daemon=True).start()

    def _cotacao_solta(self):
        try:
            moeda = normalizar_moeda(self.moeda.get())
        except (ValueError, argparse.ArgumentTypeError):
            return
        if self.preco_travado:
            return
        self.geracao += 1
        geracao = self.geracao

        def trabalho():
            try:
                atual, origem, _aviso = resolver_cotacao(moeda, None, 0)
            except Exception as exc:
                self.root.after(0, lambda erro=exc: self.nota.config(text=str(erro)) if self.vivo else None)
                return
            self.root.after(0, lambda: self._mostrar_cotacao(geracao, atual, origem))

        threading.Thread(target=trabalho, daemon=True).start()

    def _mostrar_cotacao(self, geracao, atual, origem):
        if not self.vivo or geracao != self.geracao or self.preco_travado:
            return
        texto = campo_num(atual)
        if self.manual.get() != texto and self.root.focus_get() is not self.campo_preco:
            self._sincronizando = True
            self.manual.set(texto)
            self._sincronizando = False
        self.cotacao.config(text=f"Atual {br(atual, casas_preco(atual))}    {origem}")

    def _falha(self, geracao, erro):
        if not self.vivo or geracao != self.geracao:
            return
        self.cotacao.config(text="")
        self.nota.config(text=str(erro))

    def _aplicar(self, geracao, pacote):
        if not self.vivo or geracao != self.geracao:
            return
        try:
            self._preencher(pacote)
        except self.tk.TclError:
            return

    def _preencher(self, pacote):
        moeda = pacote["moeda"]
        casas = casas_preco(pacote["entrada"])
        self.titulo.config(
            text=(
                f"{moeda}USDT   {NOME_LADO[pacote['lado']]}   "
                f"{NOME_MODO[pacote['modo']]}   {pacote['alav']:g}x"
            )
        )
        mostrar_eq = pacote["media_equilibrio"] if "media_equilibrio" in pacote else self.media_equilibrio
        media = pacote.get("equilibrio", pacote["entrada"]) if mostrar_eq else pacote["entrada"]
        self.cotacao.config(
            text=(
                f"Preço médio {br(media, casas)}    "
                f"Atual {br(pacote['atual'], casas_preco(pacote['atual']))}    "
                f"{pacote['origem']}"
            )
        )
        if self.primeiro_celula is not None:
            primeiro = pacote.get("primeiro") or self.primeiro_preco
            self.primeiro_celula.config(text=br(primeiro, casas) if primeiro else "—")
        foco = self.root.focus_get()
        if (
            not self.preco_travado
            and self.campo_preco is not None
            and foco is not self.campo_preco
        ):
            texto_preco = campo_num(pacote["atual"])
            if self.manual.get() != texto_preco:
                self._sincronizando = True
                self.manual.set(texto_preco)
                self._sincronizando = False
        if (
            not self.modo_parcial
            and self.campo_margem is not None
            and foco is not self.campo_margem
            and not self.margem_var.get().strip()
        ):
            self._sincronizando = True
            self.margem_var.set(campo_num(pacote["margem"]))
            self._sincronizando = False
        cores = {
            "RISCO BAIXO": ("#e5f6ee", "#0f7a45"),
            "RISCO MODERADO": ("#fff6de", "#8a6410"),
            "RISCO ALTO": ("#ffeedf", "#b45309"),
            "RISCO CRÍTICO": ("#fde8e6", "#b42318"),
            "JÁ LIQUIDADA": ("#fde8e6", "#b42318"),
            "Sem liquidação": ("#e7f5f8", "#0f6f8a"),
        }
        fundo, tinta = cores.get(pacote["status"], ("#eef2f7", "#142033"))
        self.status.config(text=pacote["status"], bg=fundo, fg=tinta)
        liq = "sem liquidação" if pacote["liq"] is None else br(pacote["liq"], casas)
        dist = "—" if pacote["dist"] is None else br(pacote["dist"] * 100) + "%"
        textos = {
            "margem": dinheiro(pacote["margem"]) + " USDT",
            "nocional": dinheiro(pacote["nocional"]) + " USDT",
            "qtd": br(pacote["qtd"], 6) + " " + moeda,
            "manut": dinheiro(pacote["manut"]) + " USDT",
            "fechamento": dinheiro(pacote["fechamento"]) + " USDT",
            "base": dinheiro(pacote["base"]) + " USDT",
            "liq": liq,
            "dist": dist,
            "pnl": dinheiro(pacote["pnl"]) + " USDT",
            "roe": br(pacote["roe"] * 100) + "%",
            "razao": br(pacote["razao"] * 100) + "%",
            "zera": br(pacote["zera"] * 100) + "%",
            "realizado": dinheiro(pacote.get("realizado") or 0) + " USDT",
            "medio": br(pacote.get("equilibrio", pacote["entrada"]), casas),
            "primeiro": br(pacote.get("primeiro") or pacote["entrada"], casas),
            "qtd_add": br(self.ultima_qtd, 6) + " " + moeda if self.ultima_qtd else "—",
            "alvo_pnl": "—",
            "alvo_roe": "—",
        }
        for chave, texto in textos.items():
            self.rotulos[chave].config(text=texto)

        filhos = self.tabela.get_children()
        if filhos:
            self.tabela.delete(*filhos)
        for linha in pacote["tabela"]:
            liq_linha = "sem liquidação" if linha["liq"] is None else br(linha["liq"], casas)
            dist_linha = "—" if linha["dist"] is None else br(linha["dist"] * 100) + "%"
            self.tabela.insert(
                "",
                "end",
                values=(
                    f"{linha['alav']:g}x" + ("  ←" if linha["marca"] else ""),
                    dinheiro(linha["nocional"]),
                    liq_linha,
                    dist_linha,
                ),
                tags=("atual",) if linha["marca"] else (),
            )
        self.mmr_sugerido = pacote["mmr_faixa"]
        notas = [pacote["faixa"]] if pacote["faixa"] else []
        notas.extend(pacote["avisos"])
        if self.operacao_travada:
            notas.insert(
                0,
                "Primeiro preço fica fixo. O preço médio é a média da posição aberta. "
                "O preço médio ajustado é o equilíbrio depois do lucro realizado.",
            )
        if pacote["saldo"] > 0:
            notas.append(
                "Variação que zera a margem é o movimento de 1 ÷ alavancagem, sobre a margem usada. "
                "No Cross, a liquidação usa o saldo inteiro."
            )
        else:
            notas.append(
                "O saldo está zerado. Lucro, perda e liquidação usam a margem informada em USDT."
            )
        aviso_alvo = self._texto_alvo(pacote)
        if aviso_alvo:
            notas.insert(0, aviso_alvo)
        self.nota.config(text="\n".join(nota for nota in notas if nota))
        self.ultimo = pacote
        self._marcar_vistos()


def abrir_janela(args):
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"Não abri a janela ({exc}).")
        return False
    root = tk.Tk()
    App(root, args)
    root.mainloop()
    return True


def main(argv=None):
    preparar_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        if abrir_janela(ler_args([])):
            return 0
        argv = ["--cli"]
    args = ler_args(argv)
    if args.gui:
        if abrir_janela(args):
            return 0
        print("Sem interface gráfica. Seguindo no terminal.")
    if args.interativo:
        return loop_interativo(args)
    if not args.watch:
        return 0 if ciclo(args) else 1
    try:
        while True:
            print("\033[2J\033[H", end="", flush=True)
            print(time.strftime("Atualizado às %H:%M:%S"))
            ciclo(args)
            time.sleep(max(1, args.watch))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
