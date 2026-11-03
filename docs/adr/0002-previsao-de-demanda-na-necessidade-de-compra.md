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

   O gatilho de compra não muda: estoque `<=` estoque mínimo. Os dias de cobertura valem 7 por padrão.
4. **Plano B por SKU.** Sem histórico utilizável (ausente, menos de 56 dias, com buracos ou valores inválidos), o motor usa a `venda_media_diaria`. A origem fica registrada em `fonte_demanda` (`previsao` ou `media_fixa`) em cada item e pendência. A falha de um SKU nunca afeta os outros.
5. **O LLM continua só explicando.** A previsão é calculada em código; o dossiê apenas expõe o resultado.

## Alternativas consideradas

- **Manter o teto do estoque máximo.** Com a demanda prevista, o teto limitaria a quantidade em 4 dos 5 itens críticos da demonstração e a previsão não alteraria nenhum pedido. Descartado, ao custo descrito abaixo.
- **Estoque de segurança estatístico** (`z·σ`). Mais correto, porém exige estimar a variabilidade da demanda e do prazo; fica no roadmap. Usamos o estoque mínimo do cadastro, que já é o "mínimo de segurança" do domínio.
- **Modelos de séries temporais** (suavização exponencial, ARIMA, ML). Descartados nesta etapa para manter o método explicável a um comprador e sem dependências novas.

## Consequências

- A quantidade sugerida pode **ultrapassar o `estoque_maximo`** (picanha resfriada: 157 kg contra máximo cadastrado de 100). O comprador precisa validar perecíveis. O `estoque_maximo` continua validado (`estoque_maximo < estoque_minimo` é `DADO_INVALIDO`), mas não limita a compra.
- Sem cotação, não há prazo de entrega: a quantidade da pendência é um **piso** (considera só a cobertura) e deve ser recalculada após a cotação.
- Item exatamente no mínimo e sem venda prevista tem necessidade zero e conta como saudável.
- Os números do parecer executivo e do ADR-0001 refletem a regra anterior.
- O desempenho medido (MAPE por SKU) usa dados sintéticos gerados com a mesma estrutura que a previsão estima; é otimista. Ver "Limitações" no README.
