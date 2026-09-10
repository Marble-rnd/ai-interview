# Retrieval evaluation

Generation-free: the retriever is scored directly against hand-labelled gold evidence at k=10, with no model writing an answer. A failure here is an index problem; a failure only in the end-to-end run is a prompt problem.

Scored on the **10 items whose answer is genuinely reachable by similarity search**. Items answered by an exact filter (a date window, a direction) or by the precomputed brief are excluded — 14 of them — because ranking is not the mechanism under test there.

| mode | items | recall@10 | precision@10 | MRR | perfect | total miss | p50 |
|---|---|---|---|---|---|---|---|
| BM25 (raw chunk text) | 10 | 69% | 18% | 0.653 | 5 | 1 | 2ms |
| BM25 + Claude context blurb | 10 | 62% | 16% | 0.575 | 4 | 1 | 1ms |
| Dense (bge-small, local ONNX) | 10 | 61% | 22% | 0.625 | 4 | 2 | 24ms |
| Hybrid RRF (raw BM25 + dense) | 10 | 64% | 17% | 0.639 | 4 | 1 | 17ms |
| Hybrid RRF (contextual BM25 + dense) — default | 10 | 64% | 22% | 0.703 | 4 | 1 | 15ms |
| Hybrid RRF + Claude rerank | 10 | 81% | 39% | 0.858 | 5 | 0 | 2468ms |

**Reading it.** `bm25` → `bm25_contextual` isolates Claude's ingest-time context blurbs; `hybrid_raw` → `hybrid` isolates them again once fused with dense. The counter-intuitive result is that the blurbs *hurt* sparse retrieval on their own — each one repeats the party names, the court and the matter type, so every document chunk starts matching every query — but help once fused, where RRF works on ranks and the dense side rewards the added context. `dense` is the only component that needs a local model, so if hybrid did not beat the sparse-only rows the dependency would be dropped. Reranking mostly moves MRR, which is what it is for.

## Incomplete retrievals

| mode | item | recall | MRR | missed |
|---|---|---|---|---|
| bm25 | c1_factual_petition_filed | 0.40 | 1.00 | E124, E125, issued_summons_sorlie.pdf |
| bm25 | c1_comm_fee_response | 0.67 | 0.33 | E213 |
| bm25 | c1_doc_service_address | 0.33 | 1.00 | E150, E166 |
| bm25 | c1_data_entry_natural_language | 0.00 | 0.00 | E105, E106, E150 |
| bm25 | c2_comm_frustration | 0.50 | 1.00 | E181, E187 |
| bm25_contextual | c1_factual_petition_filed | 0.20 | 1.00 | E124, E125, E147, issued_summons_sorlie.pdf |
| bm25_contextual | c1_factual_court_county | 0.50 | 0.33 | idris_sorlie_divorce_petition.docx |
| bm25_contextual | c1_comm_fee_response | 0.67 | 0.33 | E213 |
| bm25_contextual | c1_doc_service_address | 0.33 | 0.33 | E150, E166 |
| bm25_contextual | c1_data_entry_natural_language | 0.00 | 0.00 | E105, E106, E150 |
| bm25_contextual | c2_comm_frustration | 0.50 | 1.00 | E181, E187 |
| dense | c1_factual_petition_filed | 0.40 | 1.00 | E124, E125, E147 |
| dense | c1_factual_court_county | 0.50 | 0.25 | idris_sorlie_divorce_petition.docx |
| dense | c1_comm_fee_response | 0.67 | 0.50 | E213 |
| dense | c1_doc_service_address | 0.00 | 0.00 | E150, E166, return_of_service_affidavit.pdf |
| dense | c1_data_entry_natural_language | 0.00 | 0.00 | E105, E106, E150 |
| dense | c2_comm_frustration | 0.50 | 1.00 | E181, E187 |
| hybrid_raw | c1_factual_petition_filed | 0.40 | 1.00 | E124, E125, E147 |
| hybrid_raw | c1_factual_court_county | 0.50 | 0.25 | idris_sorlie_divorce_petition.docx |
| hybrid_raw | c1_comm_fee_response | 0.67 | 0.50 | E213 |
| hybrid_raw | c1_doc_service_address | 0.33 | 0.14 | E150, E166 |
| hybrid_raw | c1_data_entry_natural_language | 0.00 | 0.00 | E105, E106, E150 |
| hybrid_raw | c2_comm_frustration | 0.50 | 1.00 | E181, E187 |
| hybrid | c1_factual_petition_filed | 0.40 | 1.00 | E124, E125, E147 |
| hybrid | c1_factual_court_county | 0.50 | 0.33 | idris_sorlie_divorce_petition.docx |
| hybrid | c1_comm_fee_response | 0.67 | 0.50 | E213 |
| hybrid | c1_doc_service_address | 0.33 | 0.20 | E150, E166 |
| hybrid | c1_data_entry_natural_language | 0.00 | 0.00 | E105, E106, E150 |
| hybrid | c2_comm_frustration | 0.50 | 1.00 | E181, E187 |
| hybrid_rerank | c1_factual_petition_filed | 0.60 | 1.00 | E124, E125 |
| hybrid_rerank | c1_comm_fee_response | 0.67 | 1.00 | E213 |
| hybrid_rerank | c1_doc_service_address | 0.67 | 0.25 | E150 |
| hybrid_rerank | c1_data_entry_natural_language | 0.67 | 0.33 | E150 |
| hybrid_rerank | c2_comm_frustration | 0.50 | 1.00 | E181, E187 |
