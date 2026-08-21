"""Conquer Health — 대국민 건강관리 챗봇.

계층
  L1   안전 · 정규화   결정론적 · LLM 미사용
  L2   의도 분류       LLM 1회
  L3   RAG 검색        병렬
  L1b  입력측 DUR 관문  결정론적
  L4   초안 생성
  L4b  출력측 안전 게이트 결정론적  ← v2 신설
  L4c  루브릭 비평
"""
__version__ = "0.2.0"
