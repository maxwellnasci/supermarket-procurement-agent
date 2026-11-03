import json
from datetime import date, timedelta

import pytest

from src.agent import formatar_dossie_para_agente
from src.skills.procurement_engine import (
    FonteDemanda,
    StatusItem,
    gerar_analise_pre_pedido,
)

INICIO = date(2026, 8, 1)


def _vendas(sku, dias, valor, inicio=INICIO):
    """Série diária constante: o fator de semana/salário é 1 e a previsão é exata."""
    return [(sku, (inicio + timedelta(days=i)).isoformat(), valor) for i in range(dias)]


def _um_sku(banco, *, vendas, prazo=1, atual=2.0, minimo=5.0, venda_fixa=1.0, **extra):
    banco(
        fornecedores=[(1, "Fornecedor Teste", prazo, 0.0)],
        produtos=[("SKU-A", "Item A")],
        estoque=[("SKU-A", atual, minimo, 100.0, venda_fixa)],
        cotacoes=[] if extra.get("sem_cotacao") else [("SKU-A", 1, 4.0)],
        vendas=vendas,
    )
    return gerar_analise_pre_pedido()


# --- regra de necessidade ------------------------------------------------------


def test_necessidade_usa_demanda_prevista_ate_a_chegada_mais_cobertura(banco):
    """(prazo + cobertura) x previsão + estoque de segurança (mínimo) - estoque atual."""
    relatorio = _um_sku(banco, vendas=_vendas("SKU-A", 90, 5.0), prazo=1)
    item = relatorio.itens[0]
    # (1 + 7) dias x 5/dia + mínimo 5 - atual 2
    assert item.quantidade_sugerida == 43.0
    assert item.fonte_demanda == FonteDemanda.PREVISAO
    assert item.venda_diaria == pytest.approx(5.0)
    assert item.valor_total_item == 43.0 * 4.0


def test_prazo_maior_do_fornecedor_aumenta_a_compra(banco):
    relatorio = _um_sku(banco, vendas=_vendas("SKU-A", 90, 5.0), prazo=3)
    assert relatorio.itens[0].quantidade_sugerida == 53.0  # 10 dias x 5 + 5 - 2


def test_dias_de_cobertura_e_parametro_do_motor(banco):
    _um_sku(banco, vendas=_vendas("SKU-A", 90, 5.0), prazo=1)
    relatorio = gerar_analise_pre_pedido(dias_cobertura=14)
    assert relatorio.itens[0].quantidade_sugerida == 78.0  # 15 x 5 + 5 - 2


@pytest.mark.parametrize("invalido", [0, -1, True, 7.5, "7"])
def test_dias_de_cobertura_invalido_levanta_value_error(banco, invalido):
    _um_sku(banco, vendas=[])
    with pytest.raises(ValueError):
        gerar_analise_pre_pedido(dias_cobertura=invalido)


def test_compra_arredonda_para_cima(banco):
    relatorio = _um_sku(banco, vendas=_vendas("SKU-A", 90, 1.3), prazo=1)
    # 8 x 1.3 + 5 - 2 = 13.4 -> 14
    assert relatorio.itens[0].quantidade_sugerida == 14.0


def test_estoque_de_seguranca_garante_compra_mesmo_sem_venda(banco):
    """Gôndola zerada sem venda ainda repõe o estoque de segurança (ruptura ativa)."""
    relatorio = _um_sku(banco, vendas=[], atual=0.0, minimo=5.0, venda_fixa=0.0)
    item = relatorio.itens[0]
    assert item.quantidade_sugerida == 5.0
    assert item.dias_restantes == 0.0


def test_necessidade_zero_nao_gera_ordem_e_conta_como_saudavel(banco):
    """No mínimo e sem nenhuma venda prevista não há o que comprar: sem ordem de 0."""
    relatorio = _um_sku(banco, vendas=[], atual=5.0, minimo=5.0, venda_fixa=0.0)
    assert relatorio.itens == []
    assert relatorio.pendencias == []
    assert relatorio.total_itens_saudaveis == 1
    assert relatorio.valor_total_estimado == 0.0


# --- fallback e fonte registrada ----------------------------------------------


def test_sem_historico_cai_para_media_fixa(banco):
    relatorio = _um_sku(banco, vendas=[], prazo=1, venda_fixa=1.0)
    item = relatorio.itens[0]
    assert item.fonte_demanda == FonteDemanda.MEDIA_FIXA
    assert item.venda_diaria == 1.0
    assert item.quantidade_sugerida == 11.0  # 8 x 1 + 5 - 2


def test_historico_curto_cai_para_media_fixa(banco):
    relatorio = _um_sku(banco, vendas=_vendas("SKU-A", 30, 9.0), venda_fixa=1.0)
    assert relatorio.itens[0].fonte_demanda == FonteDemanda.MEDIA_FIXA
    assert relatorio.itens[0].quantidade_sugerida == 11.0


def test_historico_com_buraco_cai_para_media_fixa(banco):
    vendas = _vendas("SKU-A", 90, 9.0)
    del vendas[40]
    relatorio = _um_sku(banco, vendas=vendas, venda_fixa=1.0)
    assert relatorio.itens[0].fonte_demanda == FonteDemanda.MEDIA_FIXA


def test_historico_corrompido_de_um_sku_nao_afeta_os_outros(banco):
    vendas = _vendas("SKU-RUIM", 90, 5.0) + _vendas("SKU-BOM", 90, 5.0)
    vendas[10] = ("SKU-RUIM", vendas[10][1], -7.0)  # venda negativa corrompida
    banco(
        fornecedores=[(1, "Fornecedor Teste", 1, 0.0)],
        produtos=[("SKU-RUIM", "Histórico ruim"), ("SKU-BOM", "Histórico bom")],
        estoque=[
            ("SKU-RUIM", 2.0, 5.0, 100.0, 1.0),
            ("SKU-BOM", 2.0, 5.0, 100.0, 1.0),
        ],
        cotacoes=[("SKU-RUIM", 1, 4.0), ("SKU-BOM", 1, 4.0)],
        vendas=vendas,
    )
    itens = {i.sku: i for i in gerar_analise_pre_pedido().itens}
    assert itens["SKU-RUIM"].fonte_demanda == FonteDemanda.MEDIA_FIXA
    assert itens["SKU-RUIM"].quantidade_sugerida == 11.0
    assert itens["SKU-BOM"].fonte_demanda == FonteDemanda.PREVISAO
    assert itens["SKU-BOM"].quantidade_sugerida == 43.0


def test_pendencia_sem_cotacao_registra_fonte_e_usa_prazo_zero(banco):
    relatorio = _um_sku(
        banco, vendas=_vendas("SKU-A", 90, 5.0), prazo=3, sem_cotacao=True
    )
    pendencia = relatorio.pendencias[0]
    assert pendencia.status == StatusItem.SEM_COTACAO
    assert pendencia.fonte_demanda == FonteDemanda.PREVISAO
    # Sem fornecedor não há prazo: só a cobertura (7 x 5 + 5 - 2)
    assert pendencia.quantidade_necessaria == 38.0


def test_pendencia_de_pedido_minimo_registra_fonte(banco):
    banco(
        fornecedores=[(1, "Fornecedor Exigente", 1, 100000.0)],
        produtos=[("SKU-A", "Item A")],
        estoque=[("SKU-A", 2.0, 5.0, 100.0, 1.0)],
        cotacoes=[("SKU-A", 1, 4.0)],
        vendas=_vendas("SKU-A", 90, 5.0),
    )
    pendencia = gerar_analise_pre_pedido().pendencias[0]
    assert pendencia.status == StatusItem.PEDIDO_ABAIXO_DO_MINIMO
    assert pendencia.fonte_demanda == FonteDemanda.PREVISAO


def test_pendencia_de_dado_invalido_nao_tem_fonte(banco):
    banco(
        fornecedores=[(1, "Fornecedor Teste", 1, 0.0)],
        produtos=[("SKU-A", "Item A")],
        estoque=[("SKU-A", 2.0, -5.0, 10.0, 1.0)],
        cotacoes=[("SKU-A", 1, 4.0)],
    )
    pendencia = gerar_analise_pre_pedido().pendencias[0]
    assert pendencia.status == StatusItem.DADO_INVALIDO
    assert pendencia.fonte_demanda is None


# --- banco de demonstração e dossiê -------------------------------------------


def test_banco_real_usa_previsao_em_todos_os_itens_e_dossie_expoe_a_fonte():
    relatorio = gerar_analise_pre_pedido()
    assert relatorio.itens
    assert all(i.fonte_demanda == FonteDemanda.PREVISAO for i in relatorio.itens)
    dossie = json.loads(formatar_dossie_para_agente())
    assert {i["fonte_demanda"] for i in dossie["itens"]} == {"previsao"}
    assert all(p["fonte_demanda"] in ("previsao", None) for p in dossie["pendencias"])
