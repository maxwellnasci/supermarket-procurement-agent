import math
import os
import random
import sqlite3
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "../data/retail_store.db")

# Histórico sintético de vendas. Data final FIXA (não date.today()) para que o
# resultado seja sempre idêntico, qualquer que seja o dia em que o setup rode.
DIAS_HISTORICO = 180
DATA_FINAL_HISTORICO = date(2026, 10, 31)
SEMENTE_HISTORICO = 42

# Multiplicador por dia da semana (date.weekday(): segunda=0 ... domingo=6).
# Soma 7,0 => média 1,0: o padrão semanal redistribui as vendas sem inflá-las.
FATOR_DIA_SEMANA = (0.90, 0.80, 0.80, 0.95, 1.25, 1.40, 0.90)
FATOR_INICIO_MES = 1.10  # dias 1 a 10: efeito do salário
ULTIMO_DIA_INICIO_MES = 10
DESVIO_RUIDO = 0.08  # ruído multiplicativo de ~8%


def _multiplicador_calendario(dia: date) -> float:
    fator = FATOR_DIA_SEMANA[dia.weekday()]
    if dia.day <= ULTIMO_DIA_INICIO_MES:
        fator *= FATOR_INICIO_MES
    return fator


def gerar_vendas_diarias(
    medias_por_sku: dict[str, float],
    unidades: dict[str, str],
    data_final: date = DATA_FINAL_HISTORICO,
    dias: int = DIAS_HISTORICO,
    semente: int = SEMENTE_HISTORICO,
) -> list[tuple[str, str, float]]:
    """Gera (data ISO, sku, quantidade_vendida) de forma determinística."""
    rng = random.Random(semente)  # instância local: não toca no random global
    datas = [data_final - timedelta(days=i) for i in range(dias - 1, -1, -1)]
    multiplicadores = [_multiplicador_calendario(d) for d in datas]
    # Normaliza pelo multiplicador médio da janela: a média do histórico fica
    # ancorada na venda_media_diaria do banco, apesar de fim de semana/salário.
    media_mult = sum(multiplicadores) / len(multiplicadores)

    linhas = []
    for sku in sorted(medias_por_sku):  # ordem fixa => sequência do RNG estável
        base = medias_por_sku[sku] / media_mult
        for dia, mult in zip(datas, multiplicadores, strict=True):
            valor = max(0.0, base * mult * rng.gauss(1.0, DESVIO_RUIDO))
            if unidades[sku] == "KG":
                valor = round(valor, 1)
            else:
                # Arredondamento estocástico: mantém a média de itens de baixo
                # giro (ex.: 1,8/dia) sem viés, ao contrário de round().
                valor = math.floor(valor + rng.random())
            linhas.append((dia.isoformat(), sku, float(valor)))
    return linhas


def init_db(db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()

    # 1. Tabela de Fornecedores
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fornecedores (
        id_fornecedor INTEGER PRIMARY KEY AUTOINCREMENT,
        razao_social TEXT NOT NULL,
        cnpj TEXT NOT NULL,
        prazo_entrega_dias INTEGER NOT NULL,
        pedido_minimo_reais REAL NOT NULL
    )
    """)

    # 2. Tabela de Produtos (Mix da Rede Varejo Premium)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS produtos (
        sku TEXT PRIMARY KEY,
        descricao TEXT NOT NULL,
        categoria TEXT NOT NULL,
        unidade TEXT NOT NULL,
        preco_venda REAL NOT NULL
    )
    """)

    # 3. Tabela de Estoque em Loja
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS estoque_loja (
        sku TEXT PRIMARY KEY,
        quantidade_atual REAL NOT NULL,
        estoque_minimo REAL NOT NULL,
        estoque_maximo REAL NOT NULL,
        venda_media_diaria REAL NOT NULL,
        FOREIGN KEY (sku) REFERENCES produtos(sku)
    )
    """)

    # 4. Tabela de Cotações Atuais dos Fornecedores
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cotacoes_fornecedores (
        id_cotacao INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        id_fornecedor INTEGER NOT NULL,
        preco_custo REAL NOT NULL,
        data_cotacao DATE DEFAULT CURRENT_DATE,
        FOREIGN KEY (sku) REFERENCES produtos(sku),
        FOREIGN KEY (id_fornecedor) REFERENCES fornecedores(id_fornecedor)
    )
    """)

    # 5. Histórico de vendas diárias (base para a futura previsão de demanda)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vendas_diarias (
        data DATE NOT NULL,
        sku TEXT NOT NULL,
        quantidade_vendida REAL NOT NULL CHECK (quantidade_vendida >= 0),
        PRIMARY KEY (data, sku),
        FOREIGN KEY (sku) REFERENCES produtos(sku)
    )
    """)

    # Limpa dados anteriores para garantir estado limpo (filhos antes dos pais)
    cursor.execute("DELETE FROM vendas_diarias")
    cursor.execute("DELETE FROM cotacoes_fornecedores")
    cursor.execute("DELETE FROM estoque_loja")
    cursor.execute("DELETE FROM produtos")
    cursor.execute("DELETE FROM fornecedores")

    # Inserir Fornecedores Parceiros
    fornecedores = [
        (1, "Distribuidora Vinhos Nobres Ltda", "12.345.678/0001-90", 2, 800.0),
        (2, "Frigorífico Carnes Nobres", "98.765.432/0001-11", 1, 1500.0),
        (3, "Distribuidora Central Alimentos", "45.678.901/0001-22", 3, 500.0),
        (4, "BioPan Distribuidora Sem Glúten", "33.222.111/0001-44", 2, 300.0),
    ]
    cursor.executemany("INSERT INTO fornecedores VALUES (?, ?, ?, ?, ?)", fornecedores)

    # Inserir Produtos (Mix Premium: Gourmet + Alto Giro)
    produtos = [
        (
            "SKU-VIN-001",
            "Vinho Tinto Angelica Zapata Malbec 750ml",
            "ADEGA_GOURMET",
            "UN",
            320.00,
        ),
        (
            "SKU-VIN-002",
            "Vinho Chileno Casillero del Diablo Cabernet 750ml",
            "ADEGA_GOURMET",
            "UN",
            65.00,
        ),
        (
            "SKU-CAR-001",
            "Picanha Angus Certificada Resfriada",
            "ACOUGUE_PARRILLA",
            "KG",
            119.90,
        ),
        (
            "SKU-CAR-002",
            "Bife Ancho Wagyu Fatiado 400g",
            "ACOUGUE_PARRILLA",
            "UN",
            145.00,
        ),
        (
            "SKU-PAD-001",
            "Pão Artesanal Multigãos Sem Glúten 400g",
            "PADARIA_SEM_GLUTEN",
            "UN",
            24.90,
        ),
        (
            "SKU-MER-001",
            "Arroz Branco Tipo 1 5kg Premium",
            "MERCEARIA_ALTO_GIRO",
            "PC",
            32.50,
        ),
        (
            "SKU-MER-002",
            "Azeite de Oliva Extra Virgem Português 500ml",
            "MERCEARIA_GOURMET",
            "UN",
            48.90,
        ),
    ]
    cursor.executemany("INSERT INTO produtos VALUES (?, ?, ?, ?, ?)", produtos)

    # Inserir Estoque Atual (Simulando itens normais e itens em estado CRÍTICO DE RUPTURA)
    # Formato: (sku, atual, minimo, maximo, venda_diaria)
    # Teto apertado para perecíveis (picanha resfriada: 100 kg), folga para não
    # perecíveis (vinho, azeite, pão sem glúten): neles a previsão define a compra.
    estoque = [
        (
            "SKU-VIN-001",
            3.0,
            10.0,
            60.0,
            2.5,
        ),  # CRÍTICO! Só tem 3 garrafas e vende 2.5/dia!
        ("SKU-VIN-002", 45.0, 20.0, 60.0, 4.0),  # Saudável
        (
            "SKU-CAR-001",
            12.0,
            40.0,
            100.0,
            15.0,
        ),  # CRÍTICO! Picanha vai zerar antes do fim de semana!
        ("SKU-CAR-002", 5.0, 8.0, 25.0, 1.8),  # Baixo
        (
            "SKU-PAD-001",
            4.0,
            15.0,
            90.0,
            6.0,
        ),  # CRÍTICO! Padaria sem glúten vai romper!
        ("SKU-MER-001", 120.0, 50.0, 200.0, 12.0),  # Saudável
        ("SKU-MER-002", 8.0, 25.0, 120.0, 5.0),  # CRÍTICO! Azeite quase acabando!
    ]
    cursor.executemany("INSERT INTO estoque_loja VALUES (?, ?, ?, ?, ?)", estoque)

    # Inserir Cotações de Fornecedores concorrentes
    cotacoes = [
        # Vinho Angelica Zapata (Fornecedor 1 vs Fornecedor 3)
        (None, "SKU-VIN-001", 1, 185.00),  # Melhor preço e entrega em 2 dias
        (None, "SKU-VIN-001", 3, 210.00),
        # Picanha Angus (Fornecedor 2 vs Fornecedor 3)
        (None, "SKU-CAR-001", 2, 74.50),  # Especialista em carnes, entrega em 1 dia
        (None, "SKU-CAR-001", 3, 79.90),
        # Pão Sem Glúten
        (None, "SKU-PAD-001", 4, 13.20),
        # Azeite Extra Virgem
        (None, "SKU-MER-002", 1, 29.90),
        (None, "SKU-MER-002", 3, 28.50),  # Fornecedor 3 mais barato
    ]
    cursor.executemany(
        "INSERT INTO cotacoes_fornecedores (id_cotacao, sku, id_fornecedor, preco_custo) VALUES (?, ?, ?, ?)",
        cotacoes,
    )

    # Histórico de 180 dias derivado de produtos/estoque (fonte única dos números)
    medias = {linha[0]: linha[4] for linha in estoque}
    unidades = {linha[0]: linha[3] for linha in produtos}
    cursor.executemany(
        "INSERT INTO vendas_diarias VALUES (?, ?, ?)",
        gerar_vendas_diarias(medias, unidades),
    )

    conn.commit()
    conn.close()
    print(
        "✅ Banco de dados da Rede Varejo Premium criado com sucesso em data/retail_store.db!"
    )


if __name__ == "__main__":
    init_db()
