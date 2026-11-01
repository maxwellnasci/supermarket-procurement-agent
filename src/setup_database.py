import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "../data/retail_store.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
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

    # Limpa dados anteriores para garantir estado limpo
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
    estoque = [
        (
            "SKU-VIN-001",
            3.0,
            10.0,
            30.0,
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
            40.0,
            6.0,
        ),  # CRÍTICO! Padaria sem glúten vai romper!
        ("SKU-MER-001", 120.0, 50.0, 200.0, 12.0),  # Saudável
        ("SKU-MER-002", 8.0, 25.0, 70.0, 5.0),  # CRÍTICO! Azeite quase acabando!
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

    conn.commit()
    conn.close()
    print(
        "✅ Banco de dados da Rede Varejo Premium criado com sucesso em data/retail_store.db!"
    )


if __name__ == "__main__":
    init_db()
