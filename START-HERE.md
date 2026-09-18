# Mansam RAG Learning Workspace

This folder is a clean copy of the official LangChain RAG From Scratch project.
The original notebooks are kept unchanged so each RAG concept can be studied
and tested independently before it is connected to the Mansam chatbot.

## Learning order

1. `rag_from_scratch_1_to_4.ipynb` - indexing, embeddings, retrieval, and basic generation.
2. `rag_from_scratch_5_to_9.ipynb` - query rewriting, RAG Fusion, decomposition, Step Back, and HyDE.
3. `rag_from_scratch_10_and_11.ipynb` - routing and structured metadata filters.
4. `rag_from_scratch_12_to_14.ipynb` - advanced indexing and reranking-oriented retrieval.
5. `rag_from_scratch_15_to_18.ipynb` - reranking, Corrective RAG, Self-RAG, and long-context retrieval.

## Mansam adaptation target

The final application should use this flow:

```text
User query -> intent/entity analysis -> filtered retrieval -> reranking
-> grounded LLM answer -> answer validation -> next conversational question
```

Product facts must come from the Mansam catalogue and product booklets. The
original notebooks are examples and do not contain Mansam product data.

## Source

https://github.com/langchain-ai/rag-from-scratch
