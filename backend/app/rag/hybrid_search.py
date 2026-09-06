from typing import List, Dict, Any

class HybridSearchEngine:
    """
    Combines Dense Vector Cosine Similarity with Sparse BM25 Keyword Search
    using Reciprocal Rank Fusion (RRF).
    """
    def __init__(self, rrf_k: int = 60):
        self.rrf_k = rrf_k

    def reciprocal_rank_fusion(
        self, dense_results: List[Dict[str, Any]], sparse_results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        scores: Dict[str, float] = {}
        item_map: Dict[str, Dict[str, Any]] = {}

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
