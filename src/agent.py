import json
import os
import sys
import unicodedata

# Garante que o projeto encontra as pastas raiz sem erro de import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.skills.procurement_engine import gerar_analise_pre_pedido

LIMITE_TEXTO = 160

# Caracteres usados como delimitadores de prompt (tags, blocos de código, templates)
_DELIMITADORES = str.maketrans({"<": " ", ">": " ", "`": " ", "{": " ", "}": " "})

# Controle (Cc), formatação invisível (Cf: zero-width, bidi override), uso privado
# (Co), surrogates (Cs) e não atribuídos (Cn)
_CATEGORIAS_PROIBIDAS = {"Cc", "Cf", "Co", "Cs", "Cn"}


def sanitizar_texto(valor: str) -> str:
    """
    Neutraliza texto vindo do banco antes de chegar ao LLM: remove quebras de linha,
    caracteres invisíveis e delimitadores, colapsa espaços e limita o tamanho.
    Reduz a superfície de prompt injection, mas não substitui tratar o dossiê como
    dado não confiável no prompt do agente.
    """
    texto = unicodedata.normalize("NFKC", valor)
    texto = "".join(
        " " if unicodedata.category(c) in _CATEGORIAS_PROIBIDAS else c for c in texto
    )
    texto = " ".join(texto.translate(_DELIMITADORES).split())
    if len(texto) > LIMITE_TEXTO:
        texto = texto[: LIMITE_TEXTO - 1].rstrip() + "…"
    return texto


def _sanitizar(dado):
    if isinstance(dado, str):
        return sanitizar_texto(dado)
    if isinstance(dado, dict):
        return {chave: _sanitizar(valor) for chave, valor in dado.items()}
    if isinstance(dado, list):
        return [_sanitizar(valor) for valor in dado]
    return dado


def formatar_dossie_para_agente():
    """
    Exporta os dados governados da Rede Varejo Premium de forma estruturada
    para o agente de IA analisar e gerar o parecer executivo.
    Todo texto livre (descricao, fornecedor_nome, categoria, motivos) é sanitizado.
    """
    relatorio = gerar_analise_pre_pedido()
    dados = _sanitizar(relatorio.model_dump(mode="json"))
    return json.dumps(dados, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print(formatar_dossie_para_agente())
