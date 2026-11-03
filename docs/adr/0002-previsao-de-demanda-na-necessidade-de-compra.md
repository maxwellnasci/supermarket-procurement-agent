# ADR-0002 — Previsão de demanda na necessidade de compra

- **Status:** Aceito
- **Data:** 2026-11-03
- **Decisores:** Arquitetura de Dados e IA
- **Escopo:** `src/skills/forecast.py`, `src/skills/procurement_engine.py`, `src/setup_database.py`
- **Substitui parcialmente:** a regra de quantidade do [ADR-0001](0001-arquitetura-motor-deterministico.md) (`max(0, estoque_maximo − estoque_atual)`)
- **Relacionado:** [README](../../README.md)

## Contexto

A quantidade sugerida era "repor até o estoque máximo", usando uma `venda_media_diaria` fixa no cadastro. Isso ignora o prazo do fornecedor, o dia da semana, o efeito do salário no início do mês e a tendência recente. O resultado é uma compra que não depende de quanto o SKU realmente vai vender até a mercadoria chegar.

## Decisão

1. **Histórico de vendas em banco.** Nova tabela `vendas_diarias (data, sku, quantidade_vendida)`, com chave primária `(data, sku)` e chave estrangeira para `produtos`. Na demonstração, o `setup_database.py` gera 180 dias sintéticos e reproduzíveis (semente fixa, data final fixa).
2. **Previsão simples e explicável**, em `forecast.py`, sem biblioteca de ML: média móvel das últimas 4 semanas (já sem o efeito de calendário) × fator do dia da semana × fator do salário (dias 1 a 10), ambos calculados do próprio histórico. A função é pura; a leitura do banco é separada e só executa `SELECT`.
3. **Nova regra de quantidade** (arredondada para cima):

   ```text
   demanda prevista em (prazo de entrega + dias de cobertura)
   + estoque de segurança (o estoque mínimo do cadastro)
   - estoque atual (saldo negativo conta como zero)
   ```

   limitada a (estoque máximo − estoque atual).

   O gatilho de compra não muda: estoque `<=` estoque mínimo. Os dias de cobertura valem 7 por padrão. **O teto do estoque máximo vale sempre.** Quando ele corta a necessidade prevista, o item registra `limitado_pelo_maximo = true`, a `quantidade_prevista_sem_teto` e o `estoque_maximo`, para o comprador decidir se aumenta o máximo cadastrado. Sem espaço algum (máximo `<=` atual), o SKU vira pendência `DADO_INVALIDO` que guarda o que a previsão pedia.
4. **Plano B por SKU.** Sem histórico utilizável (ausente, menos de 56 dias, com buracos ou valores inválidos), o motor usa a `venda_media_diaria`. A origem fica registrada em `fonte_demanda` (`previsao` ou `media_fixa`) em cada item e pendência. A falha de um SKU nunca afeta os outros.
5. **O LLM continua só explicando.** A previsão é calculada em código; o dossiê apenas expõe o resultado.

## Alternativas consideradas

- **Remover o teto do estoque máximo** (primeira versão desta decisão). A previsão passaria a mandar sozinha, mas a picanha resfriada sairia com 157 kg contra um máximo cadastrado de 100, comprando acima da capacidade declarada de um perecível. Descartado: o teto volta a valer e o corte é sinalizado.
- **Estoque de segurança estatístico** (`z·σ`). Mais correto, porém exige estimar a variabilidade da demanda e do prazo; fica no roadmap. Usamos o estoque mínimo do cadastro, que já é o "mínimo de segurança" do domínio.
- **Modelos de séries temporais** (suavização exponencial, ARIMA, ML). Descartados nesta etapa para manter o método explicável a um comprador e sem dependências novas.

## Consequências

- Na demonstração, o teto corta os 4 itens com cotação (picanha: previsão de 157 kg, compra de 88 kg para chegar a 100). Por isso o **total do pedido (R$ 13.793,20) coincide com o da regra antiga**: o efeito da previsão fica na sinalização, na cobertura em dias e no tamanho do corte. Cortes grandes sugerem máximos cadastrados baixos para o prazo de entrega e o giro; aumentar o máximo faz a previsão mandar na quantidade.
- O `estoque_maximo` é validado (`estoque_maximo < estoque_minimo` é `DADO_INVALIDO`) e limita a compra.
- Sem cotação, não há prazo de entrega: a quantidade da pendência é um **piso** (considera só a cobertura) e deve ser recalculada após a cotação.
- Item exatamente no mínimo e sem venda prevista tem necessidade zero e conta como saudável.
- O parecer executivo e o ADR-0001 descrevem a regra anterior; na demonstração as quantidades coincidem por causa do teto, mas a cobertura em dias e as referências de linha do ADR-0001 estão defasadas.
- O desempenho medido (MAPE por SKU) usa dados sintéticos gerados com a mesma estrutura que a previsão estima; é otimista. Ver "Limitações" no README.
