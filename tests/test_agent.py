import json

import pytest
from pydantic import ValidationError

from src.agent import (
    SYSTEM_PROMPT,
    ErroAlucinacaoAgente,
    ParecerExecutivo,
    construir_prompt_usuario,
    formatar_dossie_para_agente,
    gerar_parecer_agente,
    gerar_parecer_deterministico,
    obter_ferramentas_agente,
    validar_parecer_contra_dossie,
)
from src.skills.procurement_engine import StatusItem, gerar_analise_pre_pedido


def test_gerar_parecer_agente_modo_offline_padrao():
    """Sem cliente LLM, gera parecer determinístico válido e aprovado pelo guardrail."""
    parecer = gerar_parecer_agente()
    assert isinstance(parecer, ParecerExecutivo)
    assert parecer.requer_aprovacao_humana is True
    assert parecer.total_itens_analisados == 7
    assert parecer.total_itens_criticos == 5
    assert parecer.total_itens_bloqueados == 1
    assert parecer.valor_total_estimado == 15098.10
    assert len(parecer.ordens_compra) == 4
    assert len(parecer.alertas_pendencias) == 1
    assert parecer.alertas_pendencias[0].sku == "SKU-CAR-002"
    assert parecer.alertas_pendencias[0].status == StatusItem.SEM_COTACAO


def test_gerar_parecer_agente_com_cliente_llm_mock_json_e_dict():
    """Cliente LLM que respeita os números do dossiê passa pelo guardrail."""
    relatorio = gerar_analise_pre_pedido()
    esperado = gerar_parecer_deterministico(relatorio)
    chamadas = []

    def llm_fiel_str(system_prompt: str, user_prompt: str) -> str:
        chamadas.append((system_prompt, user_prompt))
        return esperado.model_dump_json()

    parecer_str = gerar_parecer_agente(relatorio=relatorio, cliente_llm=llm_fiel_str)
    assert parecer_str.valor_total_estimado == 15098.10
    assert len(chamadas) == 1
    assert chamadas[0][0] == SYSTEM_PROMPT
    assert "<dossie_governado_nao_confiavel>" in chamadas[0][1]

    def llm_fiel_dict(_sys: str, _usr: str) -> dict:
        return esperado.model_dump(mode="python")

    parecer_dict = gerar_parecer_agente(relatorio=relatorio, cliente_llm=llm_fiel_dict)
    assert parecer_dict.valor_total_estimado == 15098.10


def test_construir_prompt_usuario_isola_dossie_em_tags():
    dossie = formatar_dossie_para_agente()
    prompt = construir_prompt_usuario(dossie)
    assert "<dossie_governado_nao_confiavel>" in prompt
    assert "</dossie_governado_nao_confiavel>" in prompt
    assert "SKU-CAR-001" in prompt


def test_obter_ferramentas_agente_exporta_json_schema_valido():
    tools = obter_ferramentas_agente()
    assert len(tools) == 2
    nomes = {t["function"]["name"] for t in tools}
    assert nomes == {"gerar_analise_pre_pedido", "emitir_parecer_executivo"}
    schema_parecer = next(
        t["function"]["parameters"]
        for t in tools
        if t["function"]["name"] == "emitir_parecer_executivo"
    )
    assert "ordens_compra" in schema_parecer["properties"]
    assert schema_parecer.get("additionalProperties") is False


def test_contrato_parecer_bloqueia_bypass_de_aprovacao_humana_e_campos_extras():
    """O LLM nunca pode marcar requer_aprovacao_humana=False nem injetar campos extras."""
    relatorio = gerar_analise_pre_pedido()
    base = gerar_parecer_deterministico(relatorio).model_dump(mode="python")

    invalido_hitl = {**base, "requer_aprovacao_humana": False}
    with pytest.raises(ValidationError):
        ParecerExecutivo.model_validate(invalido_hitl)

    invalido_extra = {**base, "auto_emitir_erp": True}
    with pytest.raises(ValidationError):
        ParecerExecutivo.model_validate(invalido_extra)


@pytest.mark.parametrize(
    ("mutacao", "trecho_erro"),
    [
        (
            lambda d: d.update({"total_itens_analisados": 99}),
            "total_itens_analisados",
        ),
        (
            lambda d: d.update({"total_itens_criticos": 4}),
            "total_itens_criticos",
        ),
        (
            lambda d: d.update({"total_itens_bloqueados": 0}),
            "total_itens_bloqueados",
        ),
        (
            lambda d: d.update({"valor_total_estimado": 13793.20}),
            "valor_total_estimado",
        ),
        (
            lambda d: d["ordens_compra"].pop(),
            "Conjunto de SKUs em ordens_compra",
        ),
        (
            lambda d: d["ordens_compra"].append(dict(d["ordens_compra"][0])),
            "SKUs duplicados em ordens_compra",
        ),
        (
            lambda d: d["ordens_compra"][0].update({"fornecedor_id": 99}),
            "Fornecedor alucinado",
        ),
        (
            lambda d: d["ordens_compra"][0].update({"quantidade_sugerida": 999.0}),
            "Quantidade alucinada",
        ),
        (
            lambda d: d["ordens_compra"][0].update({"preco_unitario": 1.99}),
            "Preço unitário alucinado",
        ),
        (
            lambda d: d["ordens_compra"][0].update({"valor_total_item": 10.0}),
            "Valor total alucinado",
        ),
        (
            lambda d: d["ordens_compra"][0].update(
                {
                    "limitado_pelo_maximo": not d["ordens_compra"][0][
                        "limitado_pelo_maximo"
                    ]
                }
            ),
            "Flag limitado_pelo_maximo divergente",
        ),
        (
            lambda d: d["alertas_pendencias"].pop(),
            "Conjunto de SKUs em alertas_pendencias",
        ),
        (
            lambda d: d["alertas_pendencias"].append(dict(d["alertas_pendencias"][0])),
            "SKUs duplicados em alertas_pendencias",
        ),
        (
            lambda d: d["alertas_pendencias"][0].update(
                {"status": StatusItem.DADO_INVALIDO}
            ),
            "Status de pendência divergente",
        ),
    ],
)
def test_guardrail_bloqueia_alucinacoes_do_llm(mutacao, trecho_erro):
    """Qualquer adulteração numérica ou estrutural pelo LLM levanta ErroAlucinacaoAgente."""
    relatorio = gerar_analise_pre_pedido()
    dados = gerar_parecer_deterministico(relatorio).model_dump(mode="python")
    mutacao(dados)
    parecer_adulterado = ParecerExecutivo.model_validate(dados)

    with pytest.raises(ErroAlucinacaoAgente, match=trecho_erro):
        validar_parecer_contra_dossie(parecer_adulterado, relatorio)


def test_gerar_parecer_agente_rejeita_llm_que_alucina_desconto():
    """Simula um LLM que inventa um desconto de 10% no total: o guardrail barra."""
    relatorio = gerar_analise_pre_pedido()
    dados = gerar_parecer_deterministico(relatorio).model_dump(mode="json")
    dados["valor_total_estimado"] = round(dados["valor_total_estimado"] * 0.9, 2)

    with pytest.raises(ErroAlucinacaoAgente, match="valor_total_estimado"):
        gerar_parecer_agente(
            relatorio=relatorio,
            cliente_llm=lambda _s, _u: json.dumps(dados),
        )
