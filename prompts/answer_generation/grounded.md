Answer the question using only the evidence blocks below.

Return JSON:
{
  "answer": "a natural-language answer of 2 to 3 short sentences (maximum 60 words) in the requested language",
  "citation_ids": [1, 2],
  "grounded": true,
  "conflicts": ["neutral descriptions of unresolved source conflicts"]
}

Rules:
- The answer must be a flowing, natural response of 2 to 3 sentences. Never return bullet lists, tables, or raw data dumps.
- Every factual statement must be supported by at least one evidence block.
- citation_ids must contain the 1-based "index" numbers of the evidence blocks you used (for example: [1] or [1, 3]).
- If support is insufficient, set grounded to false, provide the standard uncertainty response, and return an empty citation list.