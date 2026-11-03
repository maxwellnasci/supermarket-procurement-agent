import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest

from src import setup_database
from src.skills.forecast import (
    JANELA_DIAS,
    MIN_HISTORICO_DIAS,
    carregar_historico,
    prever_demanda,
)

SKUS = [
    "SKU-CAR-001",
    "SKU-CAR-002",
    "SKU-MER-001",
    "SKU-MER-002",
    "SKU-PAD-001",
    "SKU-VIN-001",
    "SKU-VIN-002",
]
LIMITE_MAPE = 0.15
# Abaixo desta venda média diária o item é de baixo giro.
VENDA_BAIXO_GIRO = 2.0


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    caminho = str(tmp_path_factory.mktemp("forecast") / "forecast.db")
    setup_database.init_db(db_path=caminho)
    return caminho


def _historico(db_path, sku):
    with closing(sqlite3.connect(db_path)) as conn:
        return carregar_historico(conn, sku)


def _venda_media(db_path, sku):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT venda_media_diaria FROM estoque_loja WHERE sku = ?", (sku,)
        ).fetchone()[0]


def _backtest(historico):
    """Separa os últimos 28 dias e prevê só com o histórico anterior."""
    treino, teste = historico[:-28], historico[-28:]
    previsao = prever_demanda(treino, 28)
    assert [d for d, _ in previsao] == [d for d, _ in teste]
    return [p for _, p in previsao], [v for _, v in teste]


def _mape(previsto, real):
    # Dia sem venda tornaria o MAPE indefinido: falha alto em vez de ignorar.
    assert all(r > 0 for r in real), "MAPE indefinido: há dia com venda zero"
    return sum(abs(p - r) / r for p, r in zip(previsto, real, strict=True)) / len(real)


def _totais_semanais(valores):
    return [sum(valores[i : i + 7]) for i in range(0, len(valores), 7)]


def _mape_do_sku(previsto, real, venda_media):
    """MAPE usado na avaliação de cada SKU.

    Item de baixo giro (venda média < 2/dia) vende poucas unidades inteiras por
    dia: errar 1 unidade já é um erro enorme em %, por mais correto que seja o
    método. Ele é avaliado pelo período de compra (a semana), que é a decisão
    que o comprador realmente toma, e não pelo dia.
    """
    if venda_media < VENDA_BAIXO_GIRO:
        return _mape(_totais_semanais(previsto), _totais_semanais(real))
    return _mape(previsto, real)


@pytest.mark.parametrize("sku", SKUS)
def test_backtest_mape_abaixo_de_15_por_cento(db_path, sku):
    previsto, real = _backtest(_historico(db_path, sku))
    erro = _mape_do_sku(previsto, real, _venda_media(db_path, sku))
    assert erro < LIMITE_MAPE, f"{sku}: MAPE {erro:.1%}"


@pytest.mark.parametrize("sku", SKUS)
def test_previsao_melhor_ou_igual_a_media_fixa_no_backtest_diario(db_path, sku):
    """Comparação sempre diária, inclusive para o baixo giro."""
    previsto, real = _backtest(_historico(db_path, sku))
    fixa = [_venda_media(db_path, sku)] * len(real)
    assert _mape(previsto, real) <= _mape(fixa, real)


@pytest.mark.parametrize("sku", SKUS)
def test_nenhuma_previsao_negativa(db_path, sku):
    previsao = prever_demanda(_historico(db_path, sku), 60)
    assert len(previsao) == 60
    assert all(valor >= 0 for _, valor in previsao)


def test_nenhuma_previsao_negativa_com_historico_zerado():
    inicio = date(2026, 1, 1)
    zerado = [(inicio + timedelta(days=i), 0.0) for i in range(MIN_HISTORICO_DIAS)]
    previsao = prever_demanda(zerado, 14)
    assert [valor for _, valor in previsao] == [0.0] * 14


@pytest.mark.parametrize("sku", SKUS)
def test_sabado_previsto_maior_que_terca_prevista(db_path, sku):
    previsao = prever_demanda(_historico(db_path, sku), 14)
    sabados = [v for d, v in previsao if d.weekday() == 5]
    tercas = [v for d, v in previsao if d.weekday() == 1]
    assert sabados and tercas
    assert min(sabados) > max(tercas)


def test_previsao_comeca_no_dia_seguinte_ao_historico(db_path):
    historico = _historico(db_path, "SKU-MER-001")
    previsao = prever_demanda(historico, 3)
    assert previsao[0][0] == historico[-1][0] + timedelta(days=1)
    inicio = date(2027, 1, 4)
    assert prever_demanda(historico, 2, data_inicio=inicio)[0][0] == inicio


def test_mesma_entrada_gera_sempre_a_mesma_saida(db_path):
    historico = _historico(db_path, "SKU-VIN-001")
    primeira = prever_demanda(historico, 14)
    assert primeira == prever_demanda(historico, 14)
    assert primeira == prever_demanda(tuple(historico), 14)
    assert primeira == prever_demanda(list(reversed(historico)), 14)


def test_previsao_nao_altera_o_historico_recebido(db_path):
    historico = _historico(db_path, "SKU-VIN-002")
    copia = list(historico)
    prever_demanda(historico, 14)
    assert historico == copia


def test_so_le_dados_nunca_grava(db_path):
    """Conexão somente leitura: carregar + prever não muda nada, e gravar falha."""

    def foto(conn):
        return conn.execute(
            "SELECT data, sku, quantidade_vendida FROM vendas_diarias ORDER BY 1, 2"
        ).fetchall()

    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        antes = foto(conn)
        prever_demanda(carregar_historico(conn, "SKU-MER-001"), 14)
        assert foto(conn) == antes
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO vendas_diarias VALUES ('2030-01-01','SKU-X',1)")


def test_carregar_historico_sem_tabela_devolve_vazio(tmp_path):
    with closing(sqlite3.connect(tmp_path / "vazio.db")) as conn:
        assert carregar_historico(conn, "SKU-A") == []


@pytest.mark.parametrize("data_corrompida", ["não-é-data", 20261031, None])
def test_carregar_historico_com_data_corrompida_levanta_value_error(
    tmp_path, data_corrompida
):
    """Só ValueError: o motor trata esse erro como 'sem histórico' e usa a média fixa."""
    with closing(sqlite3.connect(tmp_path / "ruim.db")) as conn:
        conn.execute("CREATE TABLE vendas_diarias (data, sku, quantidade_vendida)")
        conn.execute(
            "INSERT INTO vendas_diarias VALUES (?, 'SKU-A', 1.0)", (data_corrompida,)
        )
        with pytest.raises(ValueError):
            carregar_historico(conn, "SKU-A")


def test_fronteira_do_historico_minimo():
    def serie(n):
        return [(date(2026, 1, 1) + timedelta(days=i), 5.0) for i in range(n)]

    with pytest.raises(ValueError):
        prever_demanda(serie(MIN_HISTORICO_DIAS - 1), 7)
    assert len(prever_demanda(serie(MIN_HISTORICO_DIAS), 7)) == 7


@pytest.mark.parametrize(
    "historico",
    [
        [],
        [(date(2026, 1, 1) + timedelta(days=i), 1.0) for i in range(JANELA_DIAS)],
        [(date(2026, 1, 1) + timedelta(days=i * 2), 1.0) for i in range(60)],
        [(date(2026, 1, 1), 1.0)] * MIN_HISTORICO_DIAS,
        [(date(2026, 1, 1) + timedelta(days=i), -1.0) for i in range(60)],
        [(date(2026, 1, 1) + timedelta(days=i), float("nan")) for i in range(60)],
        [(date(2026, 1, 1) + timedelta(days=i), "x") for i in range(60)],
    ],
    ids=["vazio", "curto", "com-buracos", "duplicado", "negativo", "nan", "texto"],
)
def test_historico_invalido_ou_insuficiente_levanta_value_error(historico):
    with pytest.raises(ValueError):
        prever_demanda(historico, 7)
