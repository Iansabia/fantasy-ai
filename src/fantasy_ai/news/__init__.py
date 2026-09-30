"""News + buzz signal layer.

sources.py   fetch raw items (ESPN player news, RSS, Sleeper trending, Reddit; X/Instagram stubs)
cache.py     timestamped JSON cache + polite per-host rate limit
classify.py  `claude -p` turns items into structured signals (JSON only, never decisions)
adjust.py    deterministic, bounded multipliers from signals, with cited reasons

The LLM never picks players. It only labels news; plain code turns labels into bounded numbers.
"""
