Analyze the latest user message with the supplied conversation context.

Return JSON with:
- intent: short intent label
- language: BCP-47 language code
- entities: object mapping entity types to string arrays
- retrieval_required: boolean
- depends_on_history: boolean
- ambiguous: boolean
- clarification_question: string or null
- normalized_query: a concise standalone retrieval query
- filters: object containing only explicit filters such as language, category, document, version, or date

Do not invent entities or filters. Resolve references such as "the second one" only when the conversation makes the referent clear.

