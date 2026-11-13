"""Searches the IT knowledge base.

The sample searches ``src/data/kb_articles.json`` by keyword. In production, replace
the body with a query against your SharePoint knowledge base (for example through
Microsoft Graph search or an Azure AI Search index over the site).

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

from typing import Any

from _sample_data import load, words
from pydantic import BaseModel, Field


class KnowledgeBaseQuery(BaseModel):
    query: str = Field(description="The employee's question or its key terms, for example 'vpn gateway unreachable'.")
    top: int = Field(default=3, ge=1, le=5, description="Maximum number of articles to return.")


def search_knowledge_base(search: KnowledgeBaseQuery) -> dict[str, Any]:
    """Search the IT knowledge base. Returns the best-matching articles with their content, source URL, and a relevance score."""
    query_text = search.query.lower()
    query_words = words(query_text)
    results = []
    for article in load("kb_articles")["articles"]:
        score = 0
        for tag in article["tags"]:
            if (" " in tag and tag in query_text) or tag in query_words:
                score += 3
        score += len(query_words & words(article["title"])) * 2
        score += len(query_words & words(article["content"])) // 2
        if score:
            results.append((score, article))

    results.sort(key=lambda item: item[0], reverse=True)
    articles = [
        {
            "id": article["id"],
            "title": article["title"],
            "content": article["content"],
            "source_url": article["source_url"],
            "score": score,
        }
        for score, article in results[: search.top]
    ]
    return {
        "query": search.query,
        "articles": articles,
        "confident_match": bool(articles) and articles[0]["score"] >= 6,
    }
