import sqlite3

import pytest

from src.skills import procurement_engine

SCHEMA = """
CREATE TABLE fornecedores (
    id_fornecedor INTEGER PRIMARY KEY AUTOINCREMENT,
    razao_social TEXT NOT NULL,
    cnpj TEXT NOT NULL,
    prazo_entrega_dias INTEGER NOT NULL,
    pedido_minimo_reais REAL NOT NULL
);
CREATE TABLE produtos (
    sku TEXT PRIMARY KEY,
    descricao TEXT NOT NULL,
    categoria TEXT NOT NULL,
    unidade TEXT NOT NULL,
    preco_venda REAL NOT NULL
);
CREATE TABLE estoque_loja (
    sku TEXT PRIMARY KEY,
    quantidade_atual REAL NOT NULL,
    estoque_minimo REAL NOT NULL,
    estoque_maximo REAL NOT NULL,
    venda_media_diaria REAL NOT NULL,
    FOREIGN KEY (sku) REFERENCES produtos(sku)
);
CREATE TABLE cotacoes_fornecedores (
    id_cotacao INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    id_fornecedor INTEGER NOT NULL,
    preco_custo REAL NOT NULL,
    data_cotacao DATE DEFAULT CURRENT_DATE,
    FOREIGN KEY (sku) REFERENCES produtos(sku),
    FOREIGN KEY (id_fornecedor) REFERENCES fornecedores(id_fornecedor)
);
CREATE TABLE vendas_diarias (
    data DATE NOT NULL,
    sku TEXT NOT NULL,
    quantidade_vendida REAL NOT NULL,
    PRIMARY KEY (data, sku),
    FOREIGN KEY (sku) REFERENCES produtos(sku)
);
"""


@pytest.fixture
def banco(tmp_path, monkeypatch):
    """Monta um banco isolado com o schema real e aponta o motor para ele."""
    caminho = str(tmp_path / "teste.db")
    conexao_original = procurement_engine.get_db_connection

    def criar(fornecedores, produtos, estoque, cotacoes, vendas=()):
        conn = sqlite3.connect(caminho)
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO fornecedores VALUES (?, ?, '00.000.000/0001-00', ?, ?)",
            fornecedores,
        )
        conn.executemany(
            "INSERT INTO produtos VALUES (?, ?, 'MERCEARIA', 'UN', 10.0)", produtos
        )
        conn.executemany("INSERT INTO estoque_loja VALUES (?, ?, ?, ?, ?)", estoque)
        conn.executemany(
            "INSERT INTO cotacoes_fornecedores (sku, id_fornecedor, preco_custo) VALUES (?, ?, ?)",
            cotacoes,
        )
        # vendas: (sku, data ISO, quantidade) -- opcional, só os testes de previsão usam
        conn.executemany(
            "INSERT INTO vendas_diarias (sku, data, quantidade_vendida) VALUES (?, ?, ?)",
            vendas,
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            procurement_engine,
            "get_db_connection",
            lambda: conexao_original(caminho),
        )
        return caminho

    return criar
