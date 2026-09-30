"""
RAG pipeline tests: extraction, chunking, retrieval fusion, vector store and
the streaming pipeline.

Qdrant, Gemini and the embedding model are all unavailable in CI, so they are
substituted at their own boundaries rather than skipped. What is exercised is
our code: the prompt built from retrieved chunks, the marker protocol the
frontend parses, the filters sent to Qdrant, and the degradation paths.
"""

import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from app.rag.document_processor import (
    chunk_text,
    clean_text,
    extract_text,
    extract_text_from_docx,
    extract_text_from_txt,
)
from app.rag.hybrid_search import HybridSearchEngine

pytestmark = pytest.mark.unit


# --------------------------------------------------------------- extraction --
class TestTextExtraction:
    def test_reads_plain_text(self, tmp_path):
        path = tmp_path / "note.txt"
        path.write_text("Hello world", encoding="utf-8")
        assert extract_text(str(path), "txt") == "Hello world"

    def test_reads_markdown(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_text("# Heading\n\nBody", encoding="utf-8")
        assert "Heading" in extract_text(str(path), "md")

    def test_reads_csv(self, tmp_path):
        # csv is in the upload allowlist, so extraction must handle it.
        path = tmp_path / "data.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        assert "a,b" in extract_text(str(path), "csv")

    def test_undecodable_bytes_do_not_raise(self, tmp_path):
        path = tmp_path / "weird.txt"
        path.write_bytes(b"valid \xff\xfe invalid")
        # errors="replace" keeps a partially-corrupt upload usable.
        assert "valid" in extract_text_from_txt(str(path))

    def test_unsupported_type_is_rejected(self, tmp_path):
        path = tmp_path / "x.pptx"
        path.write_bytes(b"data")
        with pytest.raises(ValueError, match="Unsupported file type"):
            extract_text(str(path), "pptx")

    def test_docx_extraction_includes_tables(self, tmp_path):
        # python-docx is exercised through a real (minimal) document rather
        # than mocked, so the table-flattening branch is genuinely covered.
        docx = pytest.importorskip("docx")
        path = tmp_path / "doc.docx"
        document = docx.Document()
        document.add_paragraph("Paragraph text")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "Cell A"
        table.rows[0].cells[1].text = "Cell B"
        document.save(str(path))

        extracted = extract_text_from_docx(str(path))
        assert "Paragraph text" in extracted
        assert "Cell A" in extracted and "Cell B" in extracted

    def test_corrupt_docx_raises_a_clear_error(self, tmp_path):
        path = tmp_path / "broken.docx"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "not really xml")
        path.write_bytes(buffer.getvalue())
        with pytest.raises(ValueError, match="Failed to extract text from DOCX"):
            extract_text_from_docx(str(path))

    def test_pdf_extraction_failure_is_wrapped(self, tmp_path):
        path = tmp_path / "broken.pdf"
        path.write_bytes(b"%PDF-1.4 truncated")
        with pytest.raises(ValueError, match="Failed to extract text from PDF"):
            extract_text(str(path), "pdf")


class TestTextCleaning:
    def test_normalises_line_endings(self):
        assert "\r" not in clean_text("a\r\nb\rc")

    def test_collapses_blank_line_runs(self):
        assert "\n\n\n" not in clean_text("a\n\n\n\n\nb")

    def test_strips_null_bytes(self):
        assert "\x00" not in clean_text("a\x00b")

    def test_trims_surrounding_whitespace(self):
        assert clean_text("  text  ") == "text"


class TestChunking:
    def test_splits_long_text(self):
        chunks = chunk_text("word " * 2000, chunk_size=500, chunk_overlap=50)
        assert len(chunks) > 1

    def test_chunks_carry_index_and_token_count(self):
        chunks = chunk_text("word " * 500)
        assert all("content" in c and "index" in c and "token_count" in c for c in chunks)
        assert [c["index"] for c in chunks] == list(range(len(chunks)))

    def test_short_text_is_a_single_chunk(self):
        assert len(chunk_text("short")) == 1

    def test_empty_text_produces_no_chunks(self):
        assert chunk_text("") == []

    def test_whitespace_only_chunks_are_dropped(self):
        assert all(c["content"].strip() for c in chunk_text("a\n\n\n\n\nb"))


# ----------------------------------------------------------- hybrid search --
class TestReciprocalRankFusion:
    """RRF is not wired into retrieval yet, but the fusion maths is testable."""

    def test_fuses_and_ranks_both_lists(self):
        engine = HybridSearchEngine()
        dense = [{"chunk_id": "a"}, {"chunk_id": "b"}]
        sparse = [{"chunk_id": "b"}, {"chunk_id": "c"}]
        fused = engine.reciprocal_rank_fusion(dense, sparse)

        # "b" appears in both lists, so it must outrank items found by one.
        assert fused[0]["chunk_id"] == "b"
        assert {item["chunk_id"] for item in fused} == {"a", "b", "c"}

    def test_scores_are_attached_and_descending(self):
        engine = HybridSearchEngine()
        fused = engine.reciprocal_rank_fusion(
            [{"chunk_id": "a"}, {"chunk_id": "b"}], [{"chunk_id": "a"}]
        )
        scores = [item["rrf_score"] for item in fused]
        assert scores == sorted(scores, reverse=True)

    def test_empty_inputs_produce_empty_output(self):
        assert HybridSearchEngine().reciprocal_rank_fusion([], []) == []

    def test_rank_constant_dampens_top_hit_dominance(self):
        # A larger k flattens the curve; that is the whole point of the constant.
        flat = HybridSearchEngine(rrf_k=1000).reciprocal_rank_fusion(
            [{"chunk_id": "a"}, {"chunk_id": "b"}], []
        )
        steep = HybridSearchEngine(rrf_k=1).reciprocal_rank_fusion(
            [{"chunk_id": "a"}, {"chunk_id": "b"}], []
        )
        flat_gap = flat[0]["rrf_score"] - flat[1]["rrf_score"]
        steep_gap = steep[0]["rrf_score"] - steep[1]["rrf_score"]
        assert flat_gap < steep_gap


# ------------------------------------------------------------ vector store --
class TestVectorStore:
    def test_upsert_builds_points_with_workspace_payload(self):
        from app.rag import vector_store

        fake_client = MagicMock()
        with patch.object(vector_store, "get_qdrant_client", return_value=fake_client):
            ids = vector_store.upsert_chunks(
                chunks=[{"index": 0, "content": "text", "db_id": "c1"}],
                embeddings=[[0.1] * 384],
                document_id="doc-1",
                workspace_id="ws-1",
            )

        assert len(ids) == 1
        points = fake_client.upsert.call_args.kwargs["points"]
        # workspace_id in the payload is what makes tenant-scoped search work.
        assert points[0].payload["workspace_id"] == "ws-1"
        assert points[0].payload["document_id"] == "doc-1"

    def test_mismatched_chunk_and_embedding_counts_raise(self):
        from app.rag import vector_store

        with patch.object(vector_store, "get_qdrant_client", return_value=MagicMock()):
            # strict=True: silently dropping the tail would corrupt the index.
            with pytest.raises(ValueError):
                vector_store.upsert_chunks(
                    chunks=[{"index": 0, "content": "a"}, {"index": 1, "content": "b"}],
                    embeddings=[[0.1] * 384],
                    document_id="doc-1",
                    workspace_id="ws-1",
                )

    def test_search_filters_by_workspace(self):
        from app.rag import vector_store

        fake_client = MagicMock()
        fake_client.search.return_value = []
        with patch.object(vector_store, "get_qdrant_client", return_value=fake_client):
            vector_store.search_similar([0.1] * 384, workspace_id="ws-1", top_k=5)

        conditions = fake_client.search.call_args.kwargs["query_filter"].must
        assert any(c.key == "workspace_id" for c in conditions)

    def test_search_can_restrict_to_specific_documents(self):
        from app.rag import vector_store

        fake_client = MagicMock()
        fake_client.search.return_value = []
        with patch.object(vector_store, "get_qdrant_client", return_value=fake_client):
            vector_store.search_similar([0.1] * 384, workspace_id="ws-1", document_ids=["d1", "d2"])

        conditions = fake_client.search.call_args.kwargs["query_filter"].must
        assert any(c.key == "document_id" for c in conditions)

    def test_search_maps_results_to_citation_shape(self):
        from app.rag import vector_store

        hit = MagicMock()
        hit.id = "point-1"
        hit.score = 0.87
        hit.payload = {
            "document_id": "doc-1",
            "chunk_db_id": "chunk-1",
            "content": "retrieved text",
            "chunk_index": 3,
        }
        fake_client = MagicMock()
        fake_client.search.return_value = [hit]

        with patch.object(vector_store, "get_qdrant_client", return_value=fake_client):
            results = vector_store.search_similar([0.1] * 384, workspace_id="ws-1")

        assert results[0]["document_id"] == "doc-1"
        assert results[0]["score"] == 0.87
        assert results[0]["content"] == "retrieved text"

    def test_delete_filters_by_document(self):
        from app.rag import vector_store

        fake_client = MagicMock()
        with patch.object(vector_store, "get_qdrant_client", return_value=fake_client):
            vector_store.delete_document_vectors("doc-9")

        selector = fake_client.delete.call_args.kwargs["points_selector"]
        assert any(c.key == "document_id" for c in selector.must)

    def test_empty_chunk_list_writes_nothing(self):
        from app.rag import vector_store

        fake_client = MagicMock()
        with patch.object(vector_store, "get_qdrant_client", return_value=fake_client):
            assert vector_store.upsert_chunks([], [], "doc-1", "ws-1") == []
        fake_client.upsert.assert_not_called()


# --------------------------------------------------------------- embeddings --
class TestEmbeddings:
    def test_empty_input_short_circuits(self):
        from app.rag import embeddings

        # Must not load the model to embed nothing.
        assert embeddings.embed_texts([]) == []

    def test_embed_texts_returns_plain_lists(self):
        from app.rag import embeddings

        fake_model = MagicMock()
        fake_model.encode.return_value = MagicMock(tolist=lambda: [[0.1, 0.2]])
        with patch.object(embeddings, "get_embedding_model", return_value=fake_model):
            assert embeddings.embed_texts(["hello"]) == [[0.1, 0.2]]

    def test_embed_query_returns_a_single_vector(self):
        from app.rag import embeddings

        fake_model = MagicMock()
        fake_model.encode.return_value = [MagicMock(tolist=lambda: [0.3, 0.4])]
        with patch.object(embeddings, "get_embedding_model", return_value=fake_model):
            assert embeddings.embed_query("question") == [0.3, 0.4]


# ------------------------------------------------------------- RAG pipeline --
class TestPromptConstruction:
    def test_prompt_includes_every_chunk_and_the_question(self):
        from app.rag.pipeline import build_context_prompt

        prompt = build_context_prompt(
            "What is the finding?",
            [
                {"document_id": "d1", "score": 0.9, "content": "first chunk"},
                {"document_id": "d2", "score": 0.8, "content": "second chunk"},
            ],
        )
        assert "first chunk" in prompt and "second chunk" in prompt
        assert "What is the finding?" in prompt
        # Numbered sources are what makes [Source N] citations resolvable.
        assert "[Source 1]" in prompt and "[Source 2]" in prompt

    def test_prompt_instructs_the_model_to_stay_grounded(self):
        from app.rag.pipeline import build_context_prompt

        prompt = build_context_prompt("q", [{"document_id": "d", "score": 1.0, "content": "c"}])
        assert "ONLY" in prompt


class TestStreamingPipeline:
    @pytest.mark.asyncio
    async def test_reports_when_nothing_is_retrieved(self):
        from app.rag import pipeline

        with (
            patch.object(pipeline, "embed_query", return_value=[0.1] * 384),
            patch.object(pipeline, "search_similar", return_value=[]),
        ):
            chunks = [c async for c in pipeline.stream_rag_response("q", "ws-1")]

        # An empty index must say so rather than inventing an answer.
        assert len(chunks) == 1
        assert "No relevant documents" in chunks[0]

    @pytest.mark.asyncio
    async def test_streams_tokens_then_the_sources_marker(self):
        from app.rag import pipeline

        retrieved = [{"document_id": "d1", "score": 0.9, "content": "ctx", "chunk_id": "c1"}]

        class _Part:
            text = "answer "

        fake_model = MagicMock()
        fake_model.generate_content.return_value = [_Part(), _Part()]

        with (
            patch.object(pipeline, "embed_query", return_value=[0.1] * 384),
            patch.object(pipeline, "search_similar", return_value=retrieved),
            patch.object(pipeline.genai, "GenerativeModel", return_value=fake_model),
        ):
            chunks = [c async for c in pipeline.stream_rag_response("q", "ws-1")]

        assert "answer " in chunks[0]
        # The final frame carries the marker protocol the frontend parses.
        assert "__SOURCES_JSON__" in chunks[-1]
        assert "__END_SOURCES__" in chunks[-1]

    @pytest.mark.asyncio
    async def test_sources_payload_is_valid_json(self):
        import json

        from app.rag import pipeline

        retrieved = [{"document_id": "d1", "score": 0.9, "content": "ctx", "chunk_id": "c1"}]

        class _Part:
            text = "a"

        fake_model = MagicMock()
        fake_model.generate_content.return_value = [_Part()]

        with (
            patch.object(pipeline, "embed_query", return_value=[0.1] * 384),
            patch.object(pipeline, "search_similar", return_value=retrieved),
            patch.object(pipeline.genai, "GenerativeModel", return_value=fake_model),
        ):
            chunks = [c async for c in pipeline.stream_rag_response("q", "ws-1")]

        marker = chunks[-1]
        payload = marker.split("__SOURCES_JSON__")[1].replace("__END_SOURCES__", "").strip()
        decoded = json.loads(payload)
        assert decoded["__sources__"][0]["document_id"] == "d1"
        assert "__response_time_ms__" in decoded

    @pytest.mark.asyncio
    async def test_non_streaming_response_returns_answer_and_sources(self):
        from app.rag import pipeline

        retrieved = [{"document_id": "d1", "score": 0.9, "content": "ctx", "chunk_id": "c1"}]
        fake_model = MagicMock()
        fake_model.generate_content.return_value = MagicMock(text="the answer")

        with (
            patch.object(pipeline, "embed_query", return_value=[0.1] * 384),
            patch.object(pipeline, "search_similar", return_value=retrieved),
            patch.object(pipeline.genai, "GenerativeModel", return_value=fake_model),
        ):
            result = await pipeline.get_rag_response("q", "ws-1")

        assert result["answer"] == "the answer"
        assert result["sources"] == retrieved
        assert result["response_time_ms"] >= 0

    @pytest.mark.asyncio
    async def test_the_event_loop_stays_free_while_answering(self):
        """Regression: the pipeline used to run its work on the event loop.

        embed_query, search_similar and the Gemini call are all synchronous
        and slow. Called directly from the coroutine they froze the worker
        for the whole request, so one question stalled every other request
        on that process -- health probes included.

        The stand-in below blocks with time.sleep, which is what a real
        forward pass does to a thread. A ticker counts how many times the
        loop got to run meanwhile: on the blocking implementation it cannot
        tick at all.
        """
        import asyncio
        import time as _time

        from app.rag import pipeline

        def _slow_embed(question):
            _time.sleep(0.4)
            return [0.1] * 384

        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        with (
            patch.object(pipeline, "embed_query", _slow_embed),
            patch.object(pipeline, "search_similar", return_value=[]),
        ):
            ticker = asyncio.create_task(_ticker())
            try:
                await pipeline.get_rag_response("q", "ws-1")
            finally:
                ticker.cancel()

        # ~40 ticks are possible in 0.4s; anything above a handful proves the
        # loop kept running. A blocking implementation scores 0.
        assert ticks > 5, f"event loop was starved during the call (ticks={ticks})"

    @pytest.mark.asyncio
    async def test_non_streaming_reports_empty_index(self):
        from app.rag import pipeline

        with (
            patch.object(pipeline, "embed_query", return_value=[0.1] * 384),
            patch.object(pipeline, "search_similar", return_value=[]),
        ):
            result = await pipeline.get_rag_response("q", "ws-1")

        assert result["sources"] == []
        assert "No relevant documents" in result["answer"]
