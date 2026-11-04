"""Tela do pré-pedido de compras (Streamlit).

Roda sem nenhuma chave de API: usa só o banco SQLite e o motor determinístico.
O LLM não participa de nenhum número desta tela.

    pip install streamlit==1.66.1
    streamlit run src/app.py

Os dados são montados por `montar_dados()` (Python puro, testável sem Streamlit);
o Streamlit só é importado dentro de `main()`.
"""

import os
import sqlite3
import sys
from contextlib import closing

# Garante que o projeto encontra as pastas raiz sem erro de import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.skills import procurement_engine
from src.skills.forecast import carregar_historico, prever_demanda
from src.skills.procurement_engine import gerar_analise_pre_pedido

DIAS_HISTORICO_GRAFICO = 56  # 8 semanas de vendas reais no gráfico
DIAS_PREVISAO_GRAFICO = 14  # 2 semanas à frente


def _serie_do_sku(conn: sqlite3.Connection, sku: str):
    """Histórico recente e previsão do SKU para o gráfico, ou None se não houver."""
    try:
        historico = carregar_historico(conn, sku)
        previsao = prever_demanda(historico, DIAS_PREVISAO_GRAFICO)
    except ValueError:
        return None  # sem histórico utilizável: o motor usou a média fixa
    return {
        "historico": historico[-DIAS_HISTORICO_GRAFICO:],
        "previsao": previsao,
    }


def montar_dados() -> dict:
    """Roda o motor e organiza tudo que a tela mostra, em tipos simples."""
    relatorio = gerar_analise_pre_pedido()

    itens = [
        {
            "SKU": i.sku,
            "Produto": i.descricao,
            "Estoque atual": i.quantidade_atual,
            "Demanda/dia": i.venda_diaria,
            "Fonte": i.fonte_demanda.value,
            "Dias restantes": i.dias_restantes,
            "Qtd. sugerida": i.quantidade_sugerida,
            "Limitado pelo máximo": "SIM" if i.limitado_pelo_maximo else "não",
            "Qtd. pela previsão": i.quantidade_prevista_sem_teto,
            "Estoque máx.": i.estoque_maximo,
            "Fornecedor": i.fornecedor_nome,
            "Preço unit. (R$)": i.preco_unitario,
            "Total (R$)": i.valor_total_item,
            "Alerta": i.alerta_urgencia,
        }
        for i in relatorio.itens
    ]
    pendencias = [
        {
            "SKU": p.sku,
            "Produto": p.descricao,
            "Situação": p.status.value,
            "Motivo": p.motivo,
            "Qtd. necessária": p.quantidade_necessaria,
            "Fonte": p.fonte_demanda.value if p.fonte_demanda else None,
            "Qtd. pela previsão": p.quantidade_prevista_sem_teto,
            "Limitado pelo máximo": (
                None
                if p.limitado_pelo_maximo is None
                else ("SIM" if p.limitado_pelo_maximo else "não")
            ),
            "Ação sugerida": p.acao_sugerida,
        }
        for p in relatorio.pendencias
    ]

    # Todos os SKUs do cadastro, não só os que entraram no pedido: mostrar por que
    # um item saudável NÃO está sendo comprado é tão útil quanto mostrar o que está.
    series = {}
    with closing(procurement_engine.get_db_connection()) as conn:
        produtos = conn.execute(
            "SELECT sku, descricao FROM produtos ORDER BY sku"
        ).fetchall()
        for sku, produto in produtos:
            serie = _serie_do_sku(conn, sku)
            if serie:
                series[sku] = {"produto": produto, **serie}
    return {
        "relatorio": relatorio,
        "itens": itens,
        "pendencias": pendencias,
        "series": series,
    }


def _tabela_grafico(serie: dict) -> dict[str, list]:
    """Uma coluna de histórico e outra de previsão, alinhadas por data."""
    historico = serie["historico"]
    previsao = serie["previsao"]
    datas = [d for d, _ in historico] + [d for d, _ in previsao]
    return {
        "data": datas,
        "Vendas reais": [v for _, v in historico] + [None] * len(previsao),
        # A linha da previsão começa no último dia real para as duas se encontrarem
        "Previsão": [None] * (len(historico) - 1)
        + [historico[-1][1]]
        + [round(v, 2) for _, v in previsao],
    }


def main() -> None:
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Pré-pedido de compras", layout="wide")
    st.title("Pré-pedido de compras")
    st.caption(
        "Demanda prevista a partir do histórico de vendas. Nenhum número vem de IA "
        "generativa e nenhuma chave de API é necessária."
    )

    dados = montar_dados()
    relatorio = dados["relatorio"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SKUs analisados", relatorio.total_itens_analisados)
    c2.metric("Itens críticos", relatorio.total_itens_criticos)
    c3.metric("Pendências (ação humana)", relatorio.total_itens_bloqueados)
    c4.metric("Valor do pré-pedido", f"R$ {relatorio.valor_total_estimado:,.2f}")

    st.subheader("Pré-pedido")
    if dados["itens"]:
        st.dataframe(pd.DataFrame(dados["itens"]), hide_index=True, width="stretch")
    else:
        st.info("Nenhum item precisa de compra agora.")

    st.subheader("Histórico e previsão por SKU")
    if dados["series"]:
        sku = st.selectbox(
            "SKU",
            options=list(dados["series"]),
            format_func=lambda s: f"{s} · {dados['series'][s]['produto']}",
        )
        serie = dados["series"][sku]
        tabela = pd.DataFrame(_tabela_grafico(serie))
        st.line_chart(tabela, x="data", y=["Vendas reais", "Previsão"])
        ultimo = serie["historico"][-1][0]
        st.caption(
            f"Vendas dos últimos {len(serie['historico'])} dias (até "
            f"{ultimo:%d/%m/%Y}) e previsão dos próximos {len(serie['previsao'])} dias."
        )
    else:
        st.info("Nenhum SKU com histórico suficiente para o gráfico.")

    st.subheader("Pendências")
    if dados["pendencias"]:
        st.dataframe(
            pd.DataFrame(dados["pendencias"]), hide_index=True, width="stretch"
        )
    else:
        st.success("Nenhuma pendência.")


if __name__ == "__main__":  # pragma: no cover
    main()
