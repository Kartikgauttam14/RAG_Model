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
- Match the scope of the question. When the question names a collection, brand, document, or any other parent entity, use only facts stated for that entity. A value recorded on an individual child record (for example a single product's "Fragrance Family", notes, price, or SKU) applies to that record alone and must never be reported as the attribute of its parent.
- When evidence contains both a parent-level record and child records, prefer the parent-level field for a parent-level question (for example a "collectionEn" value or a description of the collection itself).
- When the payload provides "authoritative_counts", a counting question must be answered with the "value" of the entry whose "description" matches the question's scope, exactly as written. Never answer a counting question by tallying evidence rows.
- If support is insufficient, set grounded to false, provide the standard uncertainty response, and return an empty citation list.