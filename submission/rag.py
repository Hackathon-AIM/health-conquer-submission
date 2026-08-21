"""세션 스코프 하이브리드 검색 — 툴 결과 안에서 질문에 맞는 조각만 고른다.

왜 임베딩을 안 쓰나
  제출 컨테이너의 의존성은 fastapi·uvicorn·httpx 셋뿐이고 이미지 빌드 제한이 5분이다.
  sentence-transformers 는 torch 를 끌고 와 수 GB 가 되므로 후보에서 탈락한다.
  그리고 우리 코퍼스는 '방금 받은 툴 결과' 수십~수백 청크짜리 세션 스코프라서,
  이 크기에서는 어휘 검색과 dense 검색의 격차가 문서 수만 건일 때만큼 벌어지지 않는다.

대신 하이브리드는 유지한다 — 분석기 두 개를 RRF 로 섞는다.
  - 어절 분석기: 영문·숫자·약품명처럼 형태가 그대로 일치하는 것에 강하다.
  - 문자 bigram 분석기: 한국어 조사·어미 변화를 형태소 분석기 없이 흡수한다.
    ("와파린과" vs "와파린을" 은 어절로는 다른 토큰이지만 bigram 은 대부분 겹친다.)

색인은 대화 하나 안에서만 살고 끝나면 버린다. 툴 결과는 그 대화 밖에서 유효하지 않다.
"""

from __future__ import annotations

import math
import re
from collections import Counter as _Counter
from dataclasses import dataclass
from typing import Iterable

from .context import TokenCounter
from .reduce import Document

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}|\d+(?:\.\d+)?|[가-힣]{2,}")
_NON_TOKEN_RE = re.compile(r"[^0-9A-Za-z가-힣一-鿿]+")
# 한국어 조사·어미. bigram 이 대부분 잡아 주지만 어절 분석기 쪽 정밀도를 올린다.
_JOSA_RE = re.compile(r"(은|는|이|가|을|를|과|와|의|에|에서|으로|로|도|만|까지|부터|처럼|보다|께|한테)$")

_BM25_K1 = 1.2
_BM25_B = 0.75
_RRF_K = 60


def _words(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _WORD_RE.findall(text.lower()):
        tokens.append(match)
        if len(match) > 2 and re.fullmatch(r"[가-힣]+", match):
            stem = _JOSA_RE.sub("", match)
            if stem and stem != match and len(stem) >= 2:
                tokens.append(stem)
    return tokens


def _bigrams(text: str) -> list[str]:
    packed = _NON_TOKEN_RE.sub("", text.lower())
    return [packed[i:i + 2] for i in range(len(packed) - 1)] if len(packed) >= 2 else []


@dataclass(slots=True)
class Chunk:
    chunk_id: int
    doc_index: int
    cite_uid: str
    text: str
    title: str = ""
    url: str = ""
    tool: str = ""
    tokens: int = 0


def chunk_text(text: str, counter: TokenCounter, target_tokens: int, overlap: float = 0.12) -> list[str]:
    """재귀 분할: 문단 → 문장 → 강제 절단.

    작은 창일수록 청크도 작아야 한다. 512 토큰 청크는 2.5K 창에서 근거 하나가
    예산의 절반을 먹는다는 뜻이고, 그러면 비교·교차검증이 불가능해진다.
    """
    text = text.strip()
    if not text:
        return []
    target_tokens = max(32, target_tokens)
    if counter.estimate_text(text) <= target_tokens:
        return [text]

    units = _split_units(text)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = counter.estimate_text(unit)
        if unit_tokens > target_tokens:
            if current:
                chunks.append(" ".join(current).strip())
                current, current_tokens = [], 0
            chunks.extend(_hard_split(unit, counter, target_tokens))
            continue
        if current_tokens + unit_tokens > target_tokens and current:
            chunks.append(" ".join(current).strip())
            # 경계에 걸친 개념을 놓치지 않도록 꼬리를 다음 청크로 넘긴다.
            carry, carry_tokens = [], 0
            limit = target_tokens * overlap
            for previous in reversed(current):
                cost = counter.estimate_text(previous)
                if carry_tokens + cost > limit:
                    break
                carry.insert(0, previous)
                carry_tokens += cost
            current, current_tokens = carry, carry_tokens
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append(" ".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _split_units(text: str) -> list[str]:
    units: list[str] = []
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # 한국어 종결(…다./…요./…음.)과 영문 문장부호를 함께 본다.
        sentences = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s*|\n", paragraph)
        units.extend(sentence.strip() for sentence in sentences if sentence and sentence.strip())
    return units or [text]


def _hard_split(text: str, counter: TokenCounter, target_tokens: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while remaining:
        head = counter.truncate(remaining, target_tokens, marker="")
        if not head:
            break
        pieces.append(head.strip())
        remaining = remaining[len(head):].lstrip()
    return [piece for piece in pieces if piece]


class SessionIndex:
    """대화 하나 동안만 사는 BM25 + bigram 하이브리드 인덱스."""

    def __init__(self, counter: TokenCounter, *, chunk_tokens: int = 200) -> None:
        self.counter = counter
        self.chunk_tokens = chunk_tokens
        self.documents: list[Document] = []
        self.chunks: list[Chunk] = []
        self._word_postings: list[dict[str, int]] = []
        self._bigram_postings: list[dict[str, int]] = []
        self._word_df: _Counter = _Counter()
        self._bigram_df: _Counter = _Counter()
        self._word_len: list[int] = []
        self._bigram_len: list[int] = []
        self._seen_text: set[str] = set()

    def __len__(self) -> int:
        return len(self.chunks)

    @property
    def cite_uids(self) -> list[str]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.cite_uid not in seen:
                seen.append(chunk.cite_uid)
        return seen

    def add_documents(self, documents: Iterable[Document]) -> int:
        added = 0
        for document in documents:
            doc_index = len(self.documents)
            pieces = chunk_text(document.text, self.counter, self.chunk_tokens)
            if not pieces:
                continue
            self.documents.append(document)
            for piece in pieces:
                key = piece[:160]
                if key in self._seen_text:
                    continue  # 같은 문단을 여러 툴이 되돌려주는 일이 흔하다.
                self._seen_text.add(key)
                self._index(Chunk(
                    chunk_id=len(self.chunks),
                    doc_index=doc_index,
                    cite_uid=document.cite_uid,
                    text=piece,
                    title=document.title,
                    url=document.url,
                    tool=document.tool,
                    tokens=self.counter.estimate_text(piece),
                ))
                added += 1
        return added

    def _index(self, chunk: Chunk) -> None:
        self.chunks.append(chunk)
        subject = f"{chunk.title}\n{chunk.text}"
        words = _Counter(_words(subject))
        bigrams = _Counter(_bigrams(subject))
        self._word_postings.append(words)
        self._bigram_postings.append(bigrams)
        self._word_len.append(sum(words.values()) or 1)
        self._bigram_len.append(sum(bigrams.values()) or 1)
        self._word_df.update(words.keys())
        self._bigram_df.update(bigrams.keys())

    def _bm25(
        self,
        query_terms: list[str],
        postings: list[dict[str, int]],
        document_frequency: _Counter,
        lengths: list[int],
    ) -> list[tuple[int, float]]:
        if not query_terms or not postings:
            return []
        total = len(postings)
        average = sum(lengths) / total
        wanted = _Counter(query_terms)
        scores: list[tuple[int, float]] = []
        for index, posting in enumerate(postings):
            score = 0.0
            for term, query_count in wanted.items():
                frequency = posting.get(term)
                if not frequency:
                    continue
                df = document_frequency.get(term, 0) or 1
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                norm = 1 - _BM25_B + _BM25_B * (lengths[index] / average)
                score += idf * (frequency * (_BM25_K1 + 1)) / (frequency + _BM25_K1 * norm) * min(query_count, 3)
            if score > 0:
                scores.append((index, score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        return scores

    def search(self, query: str, *, top_k: int = 5, pool: int = 40) -> list[tuple[Chunk, float]]:
        """어절 BM25 와 bigram BM25 를 RRF 로 융합한다.

        N >> K 원칙: pool 만큼 넓게 뽑아 융합한 뒤 top_k 로 좁힌다. 두 리스트가
        같은 크기면 융합이 순위를 거의 안 바꾼다.
        """
        if not self.chunks or not query.strip():
            return []
        word_ranked = self._bm25(_words(query), self._word_postings, self._word_df, self._word_len)[:pool]
        bigram_ranked = self._bm25(_bigrams(query), self._bigram_postings, self._bigram_df, self._bigram_len)[:pool]
        fused: dict[int, float] = {}
        for ranked, weight in ((word_ranked, 1.0), (bigram_ranked, 0.85)):
            for rank, (index, _score) in enumerate(ranked):
                fused[index] = fused.get(index, 0.0) + weight / (_RRF_K + rank + 1)
        if not fused:
            return []
        order = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        return [(self.chunks[index], score) for index, score in order[:top_k]]

    def select(
        self,
        query: str,
        *,
        token_budget: int,
        top_k: int = 5,
        max_per_document: int = 2,
    ) -> list[Chunk]:
        """예산 안에서, 한 문서가 근거를 독점하지 않도록 고른다.

        소형 컨텍스트에서는 top-k 를 넉넉히 넣는 게 오히려 손해다 (context rot).
        고르되 적게 고르고, 대신 서로 다른 문서에서 고른다.
        """
        if token_budget <= 0:
            return []
        picked: list[Chunk] = []
        per_document: dict[int, int] = {}
        remaining = token_budget
        for chunk, _score in self.search(query, top_k=max(top_k * 4, 20)):
            if len(picked) >= top_k:
                break
            if per_document.get(chunk.doc_index, 0) >= max_per_document:
                continue
            # 머리글 몫을 어림수로 두면 제목이 긴 문서에서 예산을 넘는다. 실제로 렌더할
            # 문자열을 그대로 재서 뺀다.
            cost = chunk.tokens + self.counter.estimate_text(
                f"[9] {chunk.title}\ncite_uid: {chunk.cite_uid}\n\n"
            )
            if cost > remaining:
                continue
            picked.append(chunk)
            per_document[chunk.doc_index] = per_document.get(chunk.doc_index, 0) + 1
            remaining -= cost
        return picked

    def render(self, chunks: list[Chunk]) -> str:
        """생성 단계에 넣을 근거 블록. 번호는 그대로 인용 번호가 된다."""
        lines: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            label = f"[{index}] {chunk.title}".rstrip() if chunk.title else f"[{index}]"
            lines.append(f"{label}\ncite_uid: {chunk.cite_uid}\n{chunk.text}")
        return "\n\n".join(lines)
