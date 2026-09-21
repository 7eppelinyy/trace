# Event pair regression fixtures

`event_pairs_100.json` preserves the pre-existing 100-pair fixture previously kept in ignored `data/benchmarks/`. Its independent human annotation provenance has not been established. Treat it as regression material, not proof of production accuracy or an independently validated research corpus.

Prediction code receives only the two input documents. Labels enter only the scoring function. Candidate recall, automatic merges, and items requiring a verifier are reported separately; the offline test does not replace the verifier with the expected answer. Live verifier/held-out independent annotation remain separate validation requirements.
