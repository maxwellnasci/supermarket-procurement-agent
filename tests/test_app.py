import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import app

RAIZ = Path(__file__).resolve().parent.parent


def test_importar_o_app_nao_exige_streamlit_nem_chave_de_api():
    """Processo limpo: importar e montar os dados não carrega o Streamlit."""
    codigo = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from src import app\n"
        "app.montar_dados()\n"
        "assert 'streamlit' not in sys.modules, 'streamlit foi importado'\n"
    )
    ambiente = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
    resultado = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert callable(app.main)


def test_montar_dados_do_banco_de_demonstracao():
    dados = app.montar_dados()

    relatorio = dados["relatorio"]
    assert len(dados["itens"]) == len(relatorio.itens) > 0
    assert len(dados["pendencias"]) == len(relatorio.pendencias)
    linha = dados["itens"][0]
    for coluna in (
        "SKU",
        "Produto",
        "Demanda/dia",
        "Fonte",
        "Qtd. sugerida",
        "Total (R$)",
    ):
        assert coluna in linha

    # Todo SKU do relatório tem série para o gráfico: histórico + previsão
    # (inclusive os saudáveis, que não entram no pedido)
    assert len(dados["series"]) == 7
    skus = {i["SKU"] for i in dados["itens"]} | {p["SKU"] for p in dados["pendencias"]}
    assert skus < set(dados["series"])
    for serie in dados["series"].values():
        assert len(serie["historico"]) == app.DIAS_HISTORICO_GRAFICO
        assert len(serie["previsao"]) == app.DIAS_PREVISAO_GRAFICO
        assert all(valor >= 0 for _, valor in serie["previsao"])
        # a previsão começa no dia seguinte ao último dia do histórico
        assert (serie["previsao"][0][0] - serie["historico"][-1][0]).days == 1


def test_app_roda_no_streamlit_sem_excecao():
    """Roda a tela de verdade quando o Streamlit opcional está instalado."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    teste = AppTest.from_file(str(RAIZ / "src" / "app.py"), default_timeout=30).run()
    assert not teste.exception
    assert len(teste.dataframe) >= 1
    assert len(teste.metric) >= 3
