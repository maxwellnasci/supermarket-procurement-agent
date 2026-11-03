"""Previsão de demanda por SKU: simples, explicável e determinística.

Método (sem biblioteca de ML), em quatro passos sobre o histórico de vendas:

1. Fator do dia da semana: venda média de cada dia da semana ÷ venda média geral.
   (Sábado costuma ficar acima de 1, terça abaixo de 1.)
2. Fator salário: depois de tirar o efeito da semana, compara os dias 1 a 10 do
   mês com os dias 11 em diante. Os dois fatores se compensam, então o salário
   redistribui a venda dentro do mês sem inflar o total.
3. Média móvel de 4 semanas: média das últimas 28 vendas já "limpas" dos dois
   efeitos acima. É o nível normal de venda do SKU hoje.
4. Previsão do dia = nível × fator do dia da semana × fator salário do dia.

A função de previsão é pura: recebe os dados, devolve números, não faz I/O.
A leitura do banco fica em `carregar_historico`, que só executa SELECT.
"""

import math
import sqlite3
from collections.abc import Sequence
from datetime import date, timedelta

JANELA_DIAS = 28  # média móvel de 4 semanas
# 4 semanas para o nível + pelo menos mais 4 para estimar os fatores com alguma
# confiança. Com menos que isso o motor deve cair para a venda média fixa.
MIN_HISTORICO_DIAS = 56
ULTIMO_DIA_INICIO_MES = 10  # dias 1 a 10: efeito do salário


def _validar(historico: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Ordena por data e garante um histórico diário contínuo e numérico."""
    try:
        ordenado = sorted(((d, v) for d, v in historico), key=lambda par: par[0])
    except TypeError as erro:
        raise ValueError(f"Histórico em formato inválido: {erro}") from erro
    if len(ordenado) < MIN_HISTORICO_DIAS:
        raise ValueError(
            f"Histórico insuficiente: {len(ordenado)} dias (mínimo {MIN_HISTORICO_DIAS})"
        )
    for dia, valor in ordenado:
        if not isinstance(dia, date):
            raise ValueError(f"Data inválida no histórico: {dia!r}")
        if (
            isinstance(valor, bool)
            or not isinstance(valor, (int, float))
            or not math.isfinite(valor)
            or valor < 0
        ):
            raise ValueError(f"Quantidade inválida em {dia}: {valor!r}")
    for (anterior, _), (atual, _) in zip(ordenado, ordenado[1:], strict=False):
        if atual - anterior != timedelta(days=1):
            raise ValueError(
                f"Histórico precisa ser diário e contínuo (problema entre {anterior} e {atual})"
            )
    return ordenado


def _media(valores: Sequence[float]) -> float:
    return sum(valores) / len(valores) if valores else 0.0


def _fatores(
    historico: list[tuple[date, float]],
) -> tuple[list[float], float, float]:
    """Devolve (fator por dia da semana, fator início do mês, fator resto do mês)."""
    media_geral = _media([v for _, v in historico])
    if media_geral == 0:
        return [1.0] * 7, 1.0, 1.0

    semana = [
        _media([v for d, v in historico if d.weekday() == dia]) / media_geral
        for dia in range(7)
    ]
    # Fator 0 (dia da semana que nunca vende) não pode virar divisor.
    limpo = [(d, v / (semana[d.weekday()] or 1.0)) for d, v in historico]
    media_limpa = _media([v for _, v in limpo])
    if media_limpa == 0:
        return semana, 1.0, 1.0
    inicio = _media([v for d, v in limpo if d.day <= ULTIMO_DIA_INICIO_MES])
    resto = _media([v for d, v in limpo if d.day > ULTIMO_DIA_INICIO_MES])
    return semana, inicio / media_limpa, resto / media_limpa


def prever_demanda(
    historico: Sequence[tuple[date, float]],
    dias: int,
    data_inicio: date | None = None,
) -> list[tuple[date, float]]:
    """Prevê a venda de cada um dos próximos `dias` dias de um SKU.

    `historico` é uma sequência de (data, quantidade_vendida), uma linha por dia
    e sem buracos. Sem `data_inicio`, a previsão começa no dia seguinte ao último
    dia do histórico (nunca em `date.today()`, para o resultado ser reproduzível).
    Levanta ValueError se o histórico for curto, tiver buracos ou valores inválidos.
    """
    if isinstance(dias, bool) or not isinstance(dias, int) or dias < 0:
        raise ValueError(f"`dias` deve ser um inteiro >= 0, recebido {dias!r}")
    ordenado = _validar(historico)
    semana, fator_inicio, fator_resto = _fatores(ordenado)

    def fator_salario(dia: date) -> float:
        return fator_inicio if dia.day <= ULTIMO_DIA_INICIO_MES else fator_resto

    # Média móvel das últimas 4 semanas, depois de tirar semana e salário.
    janela = ordenado[-JANELA_DIAS:]
    nivel = _media(
        [v / ((semana[d.weekday()] * fator_salario(d)) or 1.0) for d, v in janela]
    )

    inicio = data_inicio or ordenado[-1][0] + timedelta(days=1)
    previsao = []
    for i in range(dias):
        dia = inicio + timedelta(days=i)
        valor = nivel * semana[dia.weekday()] * fator_salario(dia)
        previsao.append((dia, max(0.0, valor)))
    return previsao


def carregar_historico(conn: sqlite3.Connection, sku: str) -> list[tuple[date, float]]:
    """Lê o histórico de um SKU. Só executa SELECT; nunca grava.

    Devolve lista vazia se a tabela `vendas_diarias` ainda não existir (banco
    criado antes do histórico), para o chamador cair no plano B.
    """
    existe = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vendas_diarias'"
    ).fetchone()
    if not existe:
        return []
    linhas = conn.execute(
        "SELECT data, quantidade_vendida FROM vendas_diarias WHERE sku = ? ORDER BY data",
        (sku,),
    ).fetchall()
    try:
        return [(date.fromisoformat(data), qtd) for data, qtd in linhas]
    except (TypeError, ValueError) as erro:
        # Linha corrompida: o chamador trata ValueError como "sem histórico".
        raise ValueError(f"Data inválida no histórico de {sku}: {erro}") from erro
