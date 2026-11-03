import sqlite3
from collections import defaultdict
from datetime import date

import pytest

from src import setup_database

SKUS = [
    "SKU-CAR-001",
    "SKU-CAR-002",
    "SKU-MER-001",
    "SKU-MER-002",
    "SKU-PAD-001",
    "SKU-VIN-001",
    "SKU-VIN-002",
]
TERCA, SABADO = 1, 5  # date.weekday(): segunda=0


def _vendas(caminho):
    conn = sqlite3.connect(caminho)
    try:
        return conn.execute(
            "SELECT data, sku, quantidade_vendida FROM vendas_diarias ORDER BY data, sku"
        ).fetchall()
    finally:
        conn.close()


def _medias_estoque(caminho):
    conn = sqlite3.connect(caminho)
    try:
        return dict(conn.execute("SELECT sku, venda_media_diaria FROM estoque_loja"))
    finally:
        conn.close()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    caminho = str(tmp_path_factory.mktemp("vendas") / "vendas.db")
    setup_database.init_db(db_path=caminho)
    return caminho


def _por_sku(caminho):
    agrupado = defaultdict(list)
    for data, sku, qtd in _vendas(caminho):
        agrupado[sku].append((date.fromisoformat(data), qtd))
    return agrupado


def test_cada_sku_tem_exatamente_180_dias(db_path):
    por_sku = _por_sku(db_path)
    assert sorted(por_sku) == SKUS
    for sku, linhas in por_sku.items():
        datas = {d for d, _ in linhas}
        assert len(linhas) == 180, sku
        assert len(datas) == 180, sku


def test_nenhuma_venda_negativa(db_path):
    assert all(qtd >= 0 for _, _, qtd in _vendas(db_path))


def test_sabado_vende_mais_que_terca_em_cada_sku(db_path):
    for sku, linhas in _por_sku(db_path).items():
        sab = [q for d, q in linhas if d.weekday() == SABADO]
        ter = [q for d, q in linhas if d.weekday() == TERCA]
        assert sum(sab) / len(sab) > sum(ter) / len(ter), sku


def test_media_do_historico_dentro_de_15_por_cento(db_path):
    medias = _medias_estoque(db_path)
    for sku, linhas in _por_sku(db_path).items():
        media = sum(q for _, q in linhas) / len(linhas)
        assert abs(media - medias[sku]) / medias[sku] <= 0.15, sku


def test_inicio_do_mes_vende_um_pouco_mais(db_path):
    for sku, linhas in _por_sku(db_path).items():
        inicio = [q for d, q in linhas if d.day <= 10]
        resto = [q for d, q in linhas if d.day > 10]
        assert sum(inicio) / len(inicio) > sum(resto) / len(resto), sku


def test_rodar_setup_duas_vezes_gera_os_mesmos_numeros(tmp_path):
    a = str(tmp_path / "a.db")
    b = str(tmp_path / "b.db")
    setup_database.init_db(db_path=a)
    setup_database.init_db(db_path=b)
    primeira = _vendas(a)
    setup_database.init_db(db_path=a)  # reexecução no mesmo arquivo
    assert primeira == _vendas(a) == _vendas(b)
    assert len(primeira) == 180 * len(SKUS)


def test_chave_primaria_e_chave_estrangeira(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):  # PK (data, sku)
            conn.execute(
                "INSERT INTO vendas_diarias VALUES (?, 'SKU-VIN-001', 1)",
                (_vendas(db_path)[0][0],),
            )
        with pytest.raises(sqlite3.IntegrityError):  # FK para produtos
            conn.execute("INSERT INTO vendas_diarias VALUES ('2030-01-01', 'XXX', 1)")
    finally:
        conn.close()
