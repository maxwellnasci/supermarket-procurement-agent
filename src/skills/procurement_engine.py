import math
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.skills.forecast import carregar_historico, prever_demanda

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


class FonteDemanda(str, Enum):
    """De onde veio a demanda diária usada no cálculo da compra."""

    PREVISAO = "previsao"  # previsão feita a partir de vendas_diarias
    MEDIA_FIXA = "media_fixa"  # plano B: venda_media_diaria do cadastro


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
    estoque_maximo: float = Field(..., ge=0, description="Teto cadastrado do SKU")
    venda_diaria: float = Field(
        ...,
        ge=0,
        description="Demanda diária média usada no cálculo (prevista ou média fixa)",
    )
    fonte_demanda: FonteDemanda = Field(
        ..., description="Origem da demanda: previsao ou media_fixa"
    )
    dias_restantes: float = Field(
        ..., ge=0, description="Quantos dias até o estoque zerar"
    )
    quantidade_sugerida: float = Field(
        ...,
        gt=0,
        description="Quantidade recomendada de compra, nunca acima de estoque_maximo - estoque atual",
    )
    limitado_pelo_maximo: bool = Field(
        ...,
        description="True quando o teto de estoque_maximo cortou a quantidade pedida pela previsão",
    )
    quantidade_prevista_sem_teto: float = Field(
        ...,
        gt=0,
        description="Quantidade que a demanda prevista pedia antes do corte pelo máximo",
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
        None,
        gt=0,
        description=(
            "Quantidade que seria comprada se houvesse cotação. Sem fornecedor não há "
            "prazo de entrega, então é um piso: recalcular após a cotação"
        ),
    )
    fonte_demanda: FonteDemanda | None = Field(
        None,
        description="Origem da demanda usada em quantidade_necessaria (se calculada)",
    )
    limitado_pelo_maximo: bool | None = Field(
        None,
        description="True quando o teto de estoque_maximo cortou a quantidade pedida pela previsão",
    )
    quantidade_prevista_sem_teto: float | None = Field(
        None,
        gt=0,
        description="Quantidade que a demanda prevista pedia antes do corte pelo máximo",
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
        description="Itens no ou abaixo do estoque mínimo que exigem compra, com ou sem cotação",
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
    if est_max < est_min:
        return "Cadastro inconsistente: estoque_maximo menor que estoque_minimo"
    return None


def _demanda_diaria(
    conn: sqlite3.Connection, sku: str, venda_media: float, dias: int
) -> tuple[FonteDemanda, list[float]]:
    """Demanda prevista de cada um dos próximos `dias` dias e a fonte usada.

    Usa a previsão de `forecast.py`. Se o SKU não tiver histórico utilizável
    (ausente, curto, com buracos ou corrompido), cai para a venda média fixa.
    A falha de um SKU nunca afeta os outros.
    """
    try:
        previsao = prever_demanda(carregar_historico(conn, sku), dias)
        return FonteDemanda.PREVISAO, [valor for _, valor in previsao]
    except ValueError:
        return FonteDemanda.MEDIA_FIXA, [venda_media] * dias


def gerar_analise_pre_pedido(dias_cobertura: int = 7) -> RelatorioPrePedido:
    """
    Analisa os estoques da Rede Varejo Premium, identifica itens abaixo do estoque mínimo
    ou próximos de ruptura e seleciona a melhor cotação de fornecedor disponível.
    Itens que não podem ser comprados viram pendências explícitas no relatório.

    Quantidade a comprar (arredondada para cima):
        demanda prevista nos dias (prazo de entrega + dias_cobertura)
        + estoque de segurança (o estoque mínimo do cadastro)
        - estoque atual (saldo negativo conta como zero)
    limitada a (estoque_maximo - estoque atual). Quando o teto corta a previsão,
    o item registra `limitado_pelo_maximo` e `quantidade_prevista_sem_teto`.
    """
    if (
        isinstance(dias_cobertura, bool)
        or not isinstance(dias_cobertura, int)
        or dias_cobertura < 1
    ):
        raise ValueError("dias_cobertura deve ser um inteiro >= 1")
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

            # Critério de compra: se o estoque atual for menor ou igual ao mínimo de segurança
            if qtd_atual > est_min:
                total_saudaveis += 1
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

            # A quantidade depende do prazo do fornecedor escolhido. Sem cotação
            # não há prazo: considera só os dias de cobertura (prazo 0). Prazo
            # inválido também vira 0 aqui; o contrato Pydantic o rejeita adiante.
            prazo_bruto = melhor_cotacao[2] if melhor_cotacao else 0
            prazo_horizonte = (
                prazo_bruto
                if isinstance(prazo_bruto, int)
                and not isinstance(prazo_bruto, bool)
                and prazo_bruto >= 0
                else 0
            )
            horizonte = prazo_horizonte + dias_cobertura
            fonte, demanda = _demanda_diaria(conn, sku, venda_dia, horizonte)
            venda_prevista = sum(demanda) / horizonte

            # Saldo negativo é tratado como gôndola zerada: ruptura já em curso.
            if qtd_atual <= 0:
                dias_restantes = 0.0
            elif venda_prevista > 0:
                dias_restantes = round(qtd_atual / venda_prevista, 1)
            else:
                dias_restantes = DIAS_SEM_GIRO

            # Demanda até a reposição chegar + cobertura + estoque de segurança
            # (o mínimo do cadastro) - o que já há na prateleira. Arredonda para
            # cima; o round() antes do ceil evita 14.000000000000002 virar 15.
            necessidade = sum(demanda) + est_min - max(qtd_atual, 0.0)
            qtd_prevista = float(math.ceil(round(necessidade, 6)))
            if qtd_prevista <= 0:
                # Já está no mínimo e não há venda prevista: nada a comprar.
                total_saudaveis += 1
                continue

            # Teto: nunca comprar mais do que cabe até o estoque máximo cadastrado.
            # Quando o teto corta a previsão, o relatório guarda o que ela pedia
            # para o comprador decidir se aumenta o máximo.
            espaco = float(math.floor(round(est_max - max(qtd_atual, 0.0), 6)))
            if espaco <= 0:
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
                        # Nada cabe: não há "quantidade a comprar". O que a previsão
                        # pedia fica em quantidade_prevista_sem_teto.
                        fonte_demanda=fonte,
                        limitado_pelo_maximo=True,
                        quantidade_prevista_sem_teto=qtd_prevista,
                        acao_sugerida="Revisar estoque mínimo e máximo do SKU (a previsão pede "
                        f"{qtd_prevista:g} un)",
                    )
                )
                continue
            qtd_comprar = min(qtd_prevista, espaco)
            limitado = qtd_prevista > espaco

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
                        fonte_demanda=fonte,
                        limitado_pelo_maximo=limitado,
                        quantidade_prevista_sem_teto=qtd_prevista,
                        acao_sugerida=(
                            f"Solicitar cotação emergencial a fornecedores da categoria {categoria}"
                            " (quantidade calculada sem prazo de entrega; recalcular após a cotação)"
                        ),
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
                    estoque_maximo=est_max,
                    venda_diaria=round(venda_prevista, 2),
                    fonte_demanda=fonte,
                    dias_restantes=dias_restantes,
                    quantidade_sugerida=qtd_comprar,
                    limitado_pelo_maximo=limitado,
                    quantidade_prevista_sem_teto=qtd_prevista,
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
                        fonte_demanda=item.fonte_demanda,
                        limitado_pelo_maximo=item.limitado_pelo_maximo,
                        quantidade_prevista_sem_teto=item.quantidade_prevista_sem_teto,
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


if __name__ == "__main__":  # pragma: no cover
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
            f"   Estoque: {item.quantidade_atual} un | Demanda: {item.venda_diaria}/dia ({item.fonte_demanda.value}) -> Cobertura: {item.dias_restantes} dias"
        )
        print(
            f"   Sugestão de Compra: {item.quantidade_sugerida} un com {item.fornecedor_nome} (R$ {item.preco_unitario:.2f}/un)"
        )
        if item.limitado_pelo_maximo:
            print(
                f"   ⚠️ Limitado pelo estoque máximo ({item.estoque_maximo:g}): "
                f"a previsão pedia {item.quantidade_prevista_sem_teto:g} un"
            )
        print(
            f"   Total: R$ {item.valor_total_item:,.2f} | Status: {item.alerta_urgencia}\n"
        )
    for pendencia in relatorio.pendencias:
        print(f"⛔ [{pendencia.status.value}] {pendencia.descricao}")
        print(f"   Motivo: {pendencia.motivo}")
        print(f"   Ação: {pendencia.acao_sugerida}\n")
