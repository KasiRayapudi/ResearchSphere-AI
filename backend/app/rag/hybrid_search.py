from typing import Any


class HybridSearchEngine:
    """Reciprocal Rank Fusion over dense and sparse result lists.

    NOTE: this is not currently wired into the retrieval path. app/rag/pipeline
    performs dense-only search via vector_store.search_similar, and no BM25
    sparse index exists yet. The fusion logic below is correct and tested, and
    is kept for when a sparse index is added - but retrieval today is dense
    only, whatever the product copy says.
    """

    def __init__(self, rrf_k: int = 60):
        self.rrf_k = rrf_k

    def reciprocal_rank_fusion(
        self, dense_results: list[dict[str, Any]], sparse_results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        item_map: dict[str, dict[str, Any]] = {}

        # Process dense rank
        for rank, item in enumerate(dense_results):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (self.rrf_k + rank + 1))
            item_map[cid] = item

        # Process sparse rank
        for rank, item in enumerate(sparse_results):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (self.rrf_k + rank + 1))
            item_map[cid] = item

        # Sort combined
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        fused = []
        for cid in sorted_ids:
            res = item_map[cid].copy()
            res["rrf_score"] = scores[cid]
            fused.append(res)
        return fused
