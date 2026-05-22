from __future__ import annotations

from unittest.mock import patch

from agent_sentinel.rag.models import RetrievedDoc
from agent_sentinel.rag.pruning import (
    compute_history_metadata_score,
    compute_static_metadata_score,
    extract_query_features,
    jaccard_similarity,
    prune_by_differential_strategy,
)
from agent_sentinel.rag.static_doc_retriever import StaticDocRetriever


def test_jaccard_similarity_tokenizes_by_space_and_punctuation() -> None:
    assert jaccard_similarity("Redis timeout, code 500", "redis timeout code 503") == 0.6


def test_history_metadata_score_renormalizes_missing_fields() -> None:
    score = compute_history_metadata_score(
        {
            "validation_result": True,
            "human_decision": "approved",
            "alert_summary": "Redis 连接超时",
        },
        "Redis 连接超时 错误码 500",
    )

    assert score > 0.8


def test_history_pruning_prefers_valid_approved_case_over_higher_vector_score() -> None:
    candidates = [
        {
            "text": "rejected case",
            "score": 0.95,
            "metadata": {
                "alert_summary": "订单同步失败",
                "validation_result": False,
                "need_human": True,
                "human_decision": "rejected",
            },
            "source_type": "history",
        },
        {
            "text": "approved redis case",
            "score": 0.8,
            "metadata": {
                "alert_summary": "Redis 连接超时",
                "validation_result": True,
                "need_human": False,
                "human_decision": "approved",
            },
            "source_type": "history",
        },
    ]

    result = prune_by_differential_strategy("Redis 连接超时，错误码 500", candidates, 1)

    assert result[0]["text"] == "approved redis case"
    assert result[0]["combined_score"] > result[0]["score"]


def test_extract_query_features_finds_category_severity_and_error_codes() -> None:
    features = extract_query_features("Redis P1 连接超时，错误码 500")

    assert "redis" in features["alert_categories"]
    assert "P1" in features["severity_levels"]
    assert "500" in features["error_codes"]


def test_static_metadata_score_matches_error_codes_and_keywords() -> None:
    score = compute_static_metadata_score(
        {
            "alert_category": "Redis",
            "severity_level": "P1",
            "error_code": ["500", "503"],
            "keywords": ["连接", "超时"],
            "section": "Redis/连接池/超时",
        },
        extract_query_features("Redis P1 连接超时，错误码 500"),
    )

    assert score > 0.8


def test_static_pruning_uses_reranker_scores_when_available() -> None:
    candidates = [
        {"text": "doc-a", "score": 0.7, "metadata": {}, "source_type": "static"},
        {"text": "doc-b", "score": 0.6, "metadata": {}, "source_type": "static"},
        {"text": "doc-c", "score": 0.9, "metadata": {}, "source_type": "static"},
    ]

    with patch("agent_sentinel.rag.pruning.Reranker.rerank", return_value=[0.2, 0.95, 0.3]):
        result = prune_by_differential_strategy("Redis timeout", candidates, 2)

    assert [item["text"] for item in result] == ["doc-b", "doc-c"]
    assert result[0]["rerank_score"] == 0.95


def test_dashscope_text_reranker_api_uses_flat_payload_and_indexes_scores() -> None:
    candidates = [
        {"text": "doc-a", "score": 0.7, "metadata": {}, "source_type": "static"},
        {"text": "doc-b", "score": 0.6, "metadata": {}, "source_type": "static"},
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.93},
                        {"index": 0, "relevance_score": 0.12},
                    ]
                }
            }

    with patch("agent_sentinel.rag.pruning.requests.post", return_value=FakeResponse()) as post_mock:
        result = prune_by_differential_strategy(
            "Redis timeout",
            candidates,
            1,
            {
                "reranker_api_endpoint": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
                "reranker_api_key": "dashscope-key",
                "reranker_model_name": "gte-rerank-v2",
            },
        )

    assert result[0]["text"] == "doc-b"
    payload = post_mock.call_args.kwargs["json"]
    assert payload["query"] == "Redis timeout"
    assert payload["documents"] == ["doc-a", "doc-b"]
    assert payload["top_n"] == 2
    assert post_mock.call_args.kwargs["headers"]["Authorization"] == "Bearer dashscope-key"


def test_dashscope_vl_reranker_api_uses_text_object_payload() -> None:
    candidates = [
        {"text": "doc-a", "score": 0.7, "metadata": {}, "source_type": "static"},
        {"text": "doc-b", "score": 0.6, "metadata": {}, "source_type": "static"},
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.81},
                        {"index": 1, "relevance_score": 0.42},
                    ]
                }
            }

    with patch("agent_sentinel.rag.pruning.requests.post", return_value=FakeResponse()) as post_mock:
        result = prune_by_differential_strategy(
            "Redis timeout",
            candidates,
            1,
            {
                "reranker_api_endpoint": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
                "reranker_api_key": "dashscope-key",
                "reranker_model_name": "qwen3-vl-rerank",
            },
        )

    assert result[0]["text"] == "doc-a"
    payload = post_mock.call_args.kwargs["json"]
    assert payload["input"]["query"] == {"text": "Redis timeout"}
    assert payload["input"]["documents"] == [{"text": "doc-a"}, {"text": "doc-b"}]
    assert payload["parameters"]["top_n"] == 2


def test_static_pruning_falls_back_to_metadata_when_reranker_fails() -> None:
    candidates = [
        {
            "text": "mq doc",
            "score": 0.9,
            "metadata": {"alert_category": "MQ", "keywords": ["重试"]},
            "source_type": "static",
        },
        {
            "text": "redis doc",
            "score": 0.75,
            "metadata": {
                "alert_category": "Redis",
                "severity_level": "P1",
                "error_code": ["500"],
                "keywords": ["连接", "超时"],
            },
            "source_type": "static",
        },
        {
            "text": "mysql doc",
            "score": 0.7,
            "metadata": {"alert_category": "MySQL"},
            "source_type": "static",
        },
    ]

    with patch("agent_sentinel.rag.pruning.Reranker.rerank", side_effect=RuntimeError("no model")):
        result = prune_by_differential_strategy("Redis P1 连接超时，错误码 500", candidates, 1)

    assert result[0]["text"] == "redis doc"
    assert "combined_score" in result[0]


def test_static_retriever_uses_recall_top_k_then_prunes_to_top_k() -> None:
    class FakeEmbedding:
        async def embed(self, query):
            return [0.1, 0.2]

    class FakeMilvus:
        def __init__(self) -> None:
            self.top_k = None

        async def search(self, **kwargs):
            self.top_k = kwargs["top_k"]
            return [
                RetrievedDoc(id="a", text="a", source_type="static_doc", score=0.5),
                RetrievedDoc(id="b", text="b", source_type="static_doc", score=0.6),
                RetrievedDoc(id="c", text="c", source_type="static_doc", score=0.7),
            ]

    milvus = FakeMilvus()
    retriever = StaticDocRetriever(
        milvus=milvus,  # type: ignore[arg-type]
        embedding=FakeEmbedding(),  # type: ignore[arg-type]
        collection_name="aiops_static_docs",
        top_k=2,
        recall_top_k=10,
    )

    async def run():
        with patch("agent_sentinel.rag.pruning.Reranker.rerank", return_value=[0.1, 0.9, 0.2]):
            docs = await retriever.retrieve("Redis timeout")
        assert milvus.top_k == 10
        assert [doc.id for doc in docs] == ["b", "c"]
        assert docs[0].metadata["rerank_score"] == 0.9

    import asyncio

    asyncio.run(run())
