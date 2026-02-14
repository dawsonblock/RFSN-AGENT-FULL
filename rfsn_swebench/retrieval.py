"""Retrieval-Augmented Context module using BM25.

Provides a lightweight way to find relevant codebase snippets based on
issue text, without requiring heavy ML dependencies like PyTorch/Transformers.
"""

import os
import math
import re
from collections import Counter
from typing import List, Dict, Tuple


class BM25Retriever:
    """Simple BM25 implementation for code searching."""

    def __init__(self, root_dir: str, top_k: int = 5):
        self.root_dir = root_dir
        self.top_k = top_k
        self.documents: List[str] = []  # list of file contents
        self.doc_paths: List[str] = []  # list of file paths
        self.avg_dl: float = 0.0
        self.doc_freqs: List[Dict[str, int]] = []
        self.idf: Dict[str, float] = {}
        self.doc_len: List[int] = []

        # BM25 parameters
        self.k1 = 1.5
        self.b = 0.75

        self._index()

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenizer: lowercase + split non-alphanumeric (including _)"""
        return [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", text) if len(t) >= 2]

    def _index(self):
        """Build index from codebase."""
        # Walk directory
        for root, _, files in os.walk(self.root_dir):
            if ".git" in root or "__pycache__" in root:
                continue

            for file in files:
                if not file.endswith(
                    (".py", ".md", ".txt", ".rst", ".c", ".h", ".cpp")
                ):
                    continue

                path = os.path.join(root, file)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        # Limit size to avoid memory explosion
                        if len(content) > 100_000:
                            content = content[:100_000]

                        self.documents.append(content)
                        self.doc_paths.append(path)

                        tokens = self._tokenize(content)
                        self.doc_len.append(len(tokens))
                        self.doc_freqs.append(Counter(tokens))

                except Exception:
                    pass

        if not self.documents:
            return

        self.avg_dl = sum(self.doc_len) / len(self.documents)

        # Calculate IDF
        all_tokens = set()
        for df in self.doc_freqs:
            all_tokens.update(df.keys())

        N = len(self.documents)
        for token in all_tokens:
            n_q = sum(1 for df in self.doc_freqs if token in df)
            # IDF with +1 smoothing
            self.idf[token] = math.log((N - n_q + 0.5) / (n_q + 0.5) + 1)

    def retrieve(self, query: str) -> List[Tuple[str, float]]:
        """Retrieve top_k documents relevant to query."""
        if not self.documents:
            return []

        q_tokens = self._tokenize(query)
        scores = []

        for idx in range(len(self.documents)):
            score = 0.0
            doc_len = self.doc_len[idx]
            freqs = self.doc_freqs[idx]

            for qt in q_tokens:
                if qt not in freqs:
                    continue

                idf = self.idf.get(qt, 0.0)
                freq = freqs[qt]

                # BM25 scoring formula
                numerator = freq * (self.k1 + 1)
                denominator = freq + self.k1 * (
                    1 - self.b + self.b * doc_len / self.avg_dl
                )
                score += idf * (numerator / denominator)

            if score > 0:
                scores.append((self.doc_paths[idx], score))

        # Sort by score desc
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[: self.top_k]

    def get_context_block(self, query: str) -> str:
        """Get formatted context block for prompt."""
        top_files = self.retrieve(query)
        if not top_files:
            return ""

        out = []
        for path, score in top_files:
            rel_path = os.path.relpath(path, self.root_dir)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    # Truncate large files for context window
                    if len(content) > 2000:
                        content = content[:2000] + "\n...[truncated]..."
                    out.append(
                        f"### FILE: {rel_path} (score: {score:.2f})\n{content}\n"
                    )
            except:
                pass

        return "\n".join(out)
