import math
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DB_PATH = os.path.join(os.path.dirname(__file__), "../../data/retail_store.db")

# Estoque positivo sem venda registrada: cobertura indeterminada, representada
# por um valor finito para não violar allow_inf_nan=False.
DIAS_SEM_GIRO = 999.0


class StatusItem(str, Enum):
    """Classificação explícita de cada SKU analisado. Nenhum SKU é descartado."""

    COMPRAR = "COMPRAR"
    SAUDAVEL = "SAUDAVEL"
    SEM_COTACAO = "SEM_COTACAO"
    SEM_ESTOQUE_CADASTRADO = "SEM_ESTOQUE_CADASTRADO"
    PEDIDO_ABAIXO_DO_MINIMO = "PEDIDO_ABAIXO_DO_MINIMO"
    DADO_INVALIDO = "DADO_INVALIDO"


class ContratoBase(BaseModel):
    """Contrato estrito: sem campos extras, sem coerção de tipos, sem inf/NaN."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ItemPrePedido(ContratoBase):
    sku: str = Field(..., min_length=1)
    descricao: str
    categoria: str
    status: StatusItem = StatusItem.COMPRAR
    quantidade_atual: float = Field(
        ...,
        description="Saldo do sistema; pode ser negativo por divergência de inventário",
    )
    estoque_minimo: float = Field(..., ge=0)
    venda_diaria: float = Field(..., ge=0)
    dias_restantes: float = Field(
        ..., ge=0, description="Quantos dias até o estoque zerar"
    )
    quantidade_sugerida: float = Field(
        ..., gt=0, description="Quantidade recomendada de compra"
    )
    fornecedor_id: int = Field(..., gt=0)
    fornecedor_nome: str
    prazo_entrega_dias: int = Field(..., ge=0)
    preco_unitario: float = Field(..., gt=0)
    valor_total_item: float = Field(..., gt=0)
    alerta_urgencia: str


class Pendencia(ContratoBase):
    """SKU que precisa de ação humana porque o motor não consegue gerar a compra."""

    sku: str = Field(..., min_length=1)
    descricao: str
    categoria: str
    status: StatusItem
    motivo: str
    quantidade_atual: float | None = None
    estoque_minimo: float | None = Field(None, ge=0)
    dias_restantes: float | None = Field(
        None, ge=0, description="Quantos dias até o estoque zerar"
    )
    quantidade_necessaria: float | None = Field(
        None, gt=0, description="Quantidade que seria comprada se houvesse cotação"
    )
    fornecedor_id: int | None = Field(None, gt=0)
    fornecedor_nome: str | None = None
    valor_total_fornecedor: float | None = Field(
        None, ge=0, description="Soma do pedido para o fornecedor nesta análise"
    )
    pedido_minimo_reais: float | None = Field(None, ge=0)
    acao_sugerida: str


class RelatorioPrePedido(ContratoBase):
    loja: str = "Rede Varejo Premium - Loja Conceito"
    total_itens_analisados: int = Field(..., ge=0)
    total_itens_criticos: int = Field(
        ...,
        ge=0,
        description="Itens no ou abaixo do estoque mínimo, com ou sem cotação",
    )
    total_itens_saudaveis: int = Field(..., ge=0)
    total_itens_bloqueados: int = Field(
        ..., ge=0, description="Itens críticos ou inválidos que exigem ação humana"
    )
    valor_total_estimado: float = Field(..., ge=0)
    itens: list[ItemPrePedido]
    pendencias: list[Pendencia]


def get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _numero_valido(valor) -> bool:
    return (
        isinstance(valor, (int, float))
        and not isinstance(valor, bool)
        and math.isfinite(valor)
    )


def _motivo_estoque_invalido(qtd_atual, est_min, est_max, venda_dia) -> str | None:
    campos = {
        "quantidade_atual": qtd_atual,
        "estoque_minimo": est_min,
        "estoque_maximo": est_max,
        "venda_media_diaria": venda_dia,
    }
    nao_numericos = [
        nome for nome, valor in campos.items() if not _numero_valido(valor)
    ]
    if nao_numericos:
        return f"Valores não numéricos ou infinitos em: {', '.join(nao_numericos)}"
    negativos = [
        nome
        for nome, valor in campos.items()
        if nome != "quantidade_atual" and valor < 0
    ]
    if negativos:
        return f"Valores negativos em: {', '.join(negativos)}"
    return None


def gerar_analise_pre_pedido(dias_cobertura: int = 7) -> RelatorioPrePedido:
    """
    Analisa os estoques da Rede Varejo Premium, identifica itens abaixo do estoque mínimo
    ou próximos de ruptura e seleciona a melhor cotação de fornecedor disponível.
    Itens que não podem ser comprados viram pendências explícitas no relatório.
    """
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        # Consulta consolidada (Camada de Governança de Dados).
        # LEFT JOIN para que produtos sem registro de estoque não sumam do relatório.
        query = """
        SELECT
            p.sku,
            p.descricao,
            p.categoria,
            e.quantidade_atual,
            e.estoque_minimo,
            e.estoque_maximo,
            e.venda_media_diaria
        FROM produtos p
        LEFT JOIN estoque_loja e ON p.sku = e.sku
        """
        cursor.execute(query)
        produtos_estoque = cursor.fetchall()

        itens_compra: list[ItemPrePedido] = []
        pendencias: list[Pendencia] = []
        pedido_minimo_por_fornecedor: dict[int, float] = {}
        total_saudaveis = 0

        for row in produtos_estoque:
            sku, descricao, categoria, qtd_atual, est_min, est_max, venda_dia = row

            if qtd_atual is None:
                pendencias.append(
                    Pendencia(
                        sku=sku,
                        descricao=descricao,
                        categoria=categoria,
                        status=StatusItem.SEM_ESTOQUE_CADASTRADO,
                        motivo="Produto cadastrado sem registro em estoque_loja",
                        acao_sugerida="Cadastrar estoque atual, mínimo, máximo e venda média do SKU",
                    )
                )
                continue

            motivo_invalido = _motivo_estoque_invalido(
                qtd_atual, est_min, est_max, venda_dia
            )
            if motivo_invalido:
                pendencias.append(
                    Pendencia(
                        sku=sku,
                        descricao=descricao,
                        categoria=categoria,
                        status=StatusItem.DADO_INVALIDO,
                        motivo=motivo_invalido,
                        acao_sugerida="Corrigir o cadastro de estoque do SKU antes de comprar",
                    )
                )
                continue

            # Cálculo matemático determinístico. Saldo negativo é tratado como
            # gôndola zerada: ruptura já em curso, cobertura zero.
            if qtd_atual <= 0:
                dias_restantes = 0.0
            elif venda_dia > 0:
                dias_restantes = round(qtd_atual / venda_dia, 1)
            else:
                dias_restantes = DIAS_SEM_GIRO

            # Critério de compra: se o estoque atual for menor ou igual ao mínimo de segurança
            if qtd_atual > est_min:
                total_saudaveis += 1
                continue

            # Quantidade necessária para atingir o estoque máximo, nunca negativa
            qtd_comprar = round(max(0.0, est_max - max(qtd_atual, 0.0)), 0)
            if qtd_comprar <= 0:
                pendencias.append(
                    Pendencia(
                        sku=sku,
                        descricao=descricao,
                        categoria=categoria,
                        status=StatusItem.DADO_INVALIDO,
                        motivo="Estoque máximo não supera o estoque atual; quantidade de compra seria zero",
                        quantidade_atual=qtd_atual,
                        estoque_minimo=est_min,
                        dias_restantes=dias_restantes,
                        acao_sugerida="Revisar estoque mínimo e máximo do SKU",
                    )
                )
                continue

            # Melhor cotação válida: menor preço, depois menor prazo, menor
            # pedido mínimo e menor id, para que o resultado seja determinístico.
            query_cotacao = """
            SELECT
                c.id_fornecedor,
                f.razao_social,
                f.prazo_entrega_dias,
                f.pedido_minimo_reais,
                c.preco_custo
            FROM cotacoes_fornecedores c
            JOIN fornecedores f ON c.id_fornecedor = f.id_fornecedor
            WHERE c.sku = ?
              AND c.preco_custo > 0
              AND typeof(c.preco_custo) IN ('integer', 'real')
            ORDER BY
                c.preco_custo ASC,
                f.prazo_entrega_dias ASC,
                f.pedido_minimo_reais ASC,
                c.id_fornecedor ASC
            """
            cursor.execute(query_cotacao, (sku,))
            melhor_cotacao = next(
                (cot for cot in cursor.fetchall() if _numero_valido(cot[4])), None
            )

            if not melhor_cotacao:
                pendencias.append(
                    Pendencia(
                        sku=sku,
                        descricao=descricao,
                        categoria=categoria,
                        status=StatusItem.SEM_COTACAO,
                        motivo="Estoque no ou abaixo do mínimo e nenhuma cotação válida de fornecedor",
                        quantidade_atual=qtd_atual,
                        estoque_minimo=est_min,
                        dias_restantes=dias_restantes,
                        quantidade_necessaria=qtd_comprar,
                        acao_sugerida=f"Solicitar cotação emergencial a fornecedores da categoria {categoria}",
                    )
                )
                continue

            forn_id, forn_nome, prazo, pedido_minimo, preco = melhor_cotacao
            total_item = round(qtd_comprar * preco, 2)

            # Nível de urgência baseado no tempo de entrega vs dias restantes
            if qtd_atual <= 0:
                alerta = "🚨 RUPTURA ATIVA (Gôndola zerada: venda sendo perdida agora!)"
            elif dias_restantes <= prazo:
                alerta = "🚨 RISCO IMINENTE DE RUPTURA (Estoque acaba antes ou no dia da entrega!)"
            elif dias_restantes <= 2.0:
                alerta = "⚠️ CRÍTICO (Menos de 48h de cobertura de vendas)"
            else:
                alerta = "⚡ REPOSIÇÃO REGULAR DE SEGURANÇA"

            try:
                item = ItemPrePedido(
                    sku=sku,
                    descricao=descricao,
                    categoria=categoria,
                    quantidade_atual=qtd_atual,
                    estoque_minimo=est_min,
                    venda_diaria=venda_dia,
                    dias_restantes=dias_restantes,
                    quantidade_sugerida=qtd_comprar,
                    fornecedor_id=forn_id,
                    fornecedor_nome=forn_nome,
                    prazo_entrega_dias=prazo,
                    preco_unitario=preco,
                    valor_total_item=total_item,
                    alerta_urgencia=alerta,
                )
            except ValidationError as erro:
                campos = sorted({str(e["loc"][0]) for e in erro.errors() if e["loc"]})
                pendencias.append(
                    Pendencia(
                        sku=sku,
                        descricao=descricao,
                        categoria=categoria,
                        status=StatusItem.DADO_INVALIDO,
                        motivo=f"Dados de fornecedor ou cotação inválidos em: {', '.join(campos)}",
                        acao_sugerida="Corrigir o cadastro do fornecedor ou da cotação",
                    )
                )
                continue

            itens_compra.append(item)
            pedido_minimo_por_fornecedor[forn_id] = pedido_minimo

    # Pedido mínimo (MOQ): o pedido consolidado por fornecedor precisa atingir
    # pedido_minimo_reais; caso contrário o comprador decide se complementa ou troca.
    itens_por_fornecedor: dict[int, list[ItemPrePedido]] = defaultdict(list)
    for item in itens_compra:
        itens_por_fornecedor[item.fornecedor_id].append(item)

    itens_aprovados: list[ItemPrePedido] = []
    for forn_id, itens_forn in itens_por_fornecedor.items():
        valor_fornecedor = round(sum(i.valor_total_item for i in itens_forn), 2)
        pedido_minimo = pedido_minimo_por_fornecedor[forn_id]
        if _numero_valido(pedido_minimo) and valor_fornecedor < pedido_minimo:
            faltante = round(pedido_minimo - valor_fornecedor, 2)
            for item in itens_forn:
                pendencias.append(
                    Pendencia(
                        sku=item.sku,
                        descricao=item.descricao,
                        categoria=item.categoria,
                        status=StatusItem.PEDIDO_ABAIXO_DO_MINIMO,
                        motivo="Pedido consolidado abaixo do pedido mínimo do fornecedor",
                        quantidade_atual=item.quantidade_atual,
                        estoque_minimo=item.estoque_minimo,
                        dias_restantes=item.dias_restantes,
                        quantidade_necessaria=item.quantidade_sugerida,
                        fornecedor_id=item.fornecedor_id,
                        fornecedor_nome=item.fornecedor_nome,
                        valor_total_fornecedor=valor_fornecedor,
                        pedido_minimo_reais=pedido_minimo,
                        acao_sugerida=(
                            f"Complementar o pedido com {item.fornecedor_nome} "
                            f"(faltam R$ {faltante:,.2f}) ou trocar de fornecedor"
                        ),
                    )
                )
        else:
            itens_aprovados.extend(itens_forn)

    # Ordena por urgência (menor dias de estoque restante primeiro)
    itens_aprovados.sort(key=lambda x: x.dias_restantes)
    pendencias.sort(
        key=lambda x: x.dias_restantes if x.dias_restantes is not None else -1.0
    )

    total_criticos = len(itens_aprovados) + sum(
        1
        for p in pendencias
        if p.status in (StatusItem.SEM_COTACAO, StatusItem.PEDIDO_ABAIXO_DO_MINIMO)
    )

    return RelatorioPrePedido(
        total_itens_analisados=len(produtos_estoque),
        total_itens_criticos=total_criticos,
        total_itens_saudaveis=total_saudaveis,
        total_itens_bloqueados=len(pendencias),
        valor_total_estimado=round(sum(i.valor_total_item for i in itens_aprovados), 2),
        itens=itens_aprovados,
        pendencias=pendencias,
    )


if __name__ == "__main__":
    # Teste rápido no terminal
    relatorio = gerar_analise_pre_pedido()
    print(f"\n🏬 {relatorio.loja}")
    print(f"📊 Total de Itens Analisados: {relatorio.total_itens_analisados}")
    print(f"🚨 Itens Críticos: {relatorio.total_itens_criticos}")
    print(f"⛔ Itens Bloqueados (ação humana): {relatorio.total_itens_bloqueados}")
    print(f"💰 Valor Total do Pré-Pedido: R$ {relatorio.valor_total_estimado:,.2f}\n")
    for item in relatorio.itens:
        print(f"[{item.categoria}] {item.descricao}")
        print(
            f"   Estoque: {item.quantidade_atual} un | Vende: {item.venda_diaria}/dia -> Cobertura: {item.dias_restantes} dias"
        )
        print(
            f"   Sugestão de Compra: {item.quantidade_sugerida} un com {item.fornecedor_nome} (R$ {item.preco_unitario:.2f}/un)"
        )
        print(
            f"   Total: R$ {item.valor_total_item:,.2f} | Status: {item.alerta_urgencia}\n"
        )
    for pendencia in relatorio.pendencias:
        print(f"⛔ [{pendencia.status.value}] {pendencia.descricao}")
        print(f"   Motivo: {pendencia.motivo}")
        print(f"   Ação: {pendencia.acao_sugerida}\n")
