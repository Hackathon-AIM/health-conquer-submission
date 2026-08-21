# Retrieval Query Planner Prompt

Generate source-specific retrieval queries from the latest user message and structured
query analysis.

Output shape:

```json
{
  "drug": ["..."],
  "drug_pricing": ["..."],
  "disease_code": ["..."],
  "guideline": ["..."],
  "pubmed": ["..."],
  "faers": ["..."],
  "hira": ["..."],
  "insurance_law": ["..."]
}
```

Source rules:

1. `drug`: medication labels, approval, dosage, contraindication, interaction, and
   medication safety.
2. `drug_pricing`: drug reimbursement, benefit status, and pricing. Use mainly for
   Korea-specific insurance or drug benefit questions.
3. `disease_code`: KCD/ICD or diagnosis code lookup.
4. `guideline`: clinical practice guideline, red flag, triage, screening, and care
   pathway questions.
5. `pubmed`: recent papers, systematic reviews, evidence uncertainty, uncommon disease,
   and medium/high risk clinical claims.
6. `faers`: suspected adverse events or safety signals for drugs.
7. `hira`: Korean reimbursement criteria, HIRA standards, and HIRA Q&A.
8. `insurance_law`: Korean health insurance statutes, regulations, and legal clauses.

Query writing rules:

- Do not send the same raw user message to every source.
- Include canonical drug, disease, symptom, jurisdiction, and safety terms when known.
- Keep each query short enough for API search tools.
- Prefer one to two strong queries per selected source.
- If retrieval is optional and risk is low, use guideline only unless a source-specific
  need is clear.
