# -*- coding: utf-8 -*-
"""
Debug why keywords are not matching (uses the NEW fixed keyword_match).

Run this from the same folder as testnew.py:
    python debug_keywords.py <path-to-transcript.txt>

Example:
    python debug_keywords.py web_uploads/losssale_transcript.txt
"""

import sys
import re
import difflib
import itertools
from testnew import load_keywords

if len(sys.argv) < 2:
    print("Usage: python debug_keywords.py <path-to-transcript.txt>")
    sys.exit(1)

transcript_path = sys.argv[1]

with open(transcript_path, "r", encoding="utf-8") as f:
    transcript = f.read()


# ---------------------------------------------------------------------
# NEW FIXED VERSION - proximity-based matching (words don't need to be
# strictly adjacent/in-order anymore).
# ---------------------------------------------------------------------
def keyword_match(kw, transcript_text, fuzzy_threshold=0.82, proximity=30):
    kw_norm = kw.lower().strip()
    text_norm = transcript_text.lower().strip()

    if not kw_norm or not text_norm:
        return False

    if kw_norm in text_norm:
        return True

    keyword_words = [w for w in re.split(r'\W+', kw_norm) if w]
    transcript_words = [w for w in re.split(r'\W+', text_norm) if w]

    if not keyword_words:
        return False

    def word_matches(keyword_word, transcript_word):
        if len(keyword_word) <= 3:
            return keyword_word == transcript_word
        ratio = difflib.SequenceMatcher(None, keyword_word, transcript_word).ratio()
        return ratio >= fuzzy_threshold

    if len(keyword_words) == 1:
        keyword_word = keyword_words[0]
        return keyword_word in transcript_words

    positions_per_word = []
    for keyword_word in keyword_words:
        positions = [
            i for i, transcript_word in enumerate(transcript_words)
            if word_matches(keyword_word, transcript_word)
        ]
        if not positions:
            return False
        positions_per_word.append(positions)

    for combo in itertools.product(*positions_per_word):
        if max(combo) - min(combo) <= proximity:
            return True

    return False


print("=" * 70)
print("TRANSCRIPT LOADED:")
print("=" * 70)
print(transcript)
print()

keywords_list = load_keywords()
print("=" * 70)
print(f"LOADED {len(keywords_list)} KEYWORDS FROM keywords.txt:")
print("=" * 70)
for kw in keywords_list:
    print(f"  - {kw!r}")
print()

print("=" * 70)
print("MATCH RESULTS (True/False) FOR EACH KEYWORD:")
print("=" * 70)
for kw in keywords_list:
    result = keyword_match(kw, transcript)
    marker = "MATCH   " if result else "no match"
    print(f"  [{marker}] {kw}")

print()
print("=" * 70)
print("WORDS IN TRANSCRIPT CONTAINING 'install' OR 'software' (for manual check):")
print("=" * 70)
words = re.findall(r"\w+", transcript.lower())
found_any = False
for i, w in enumerate(words):
    if "install" in w or "softwar" in w:
        found_any = True
        context_start = max(0, i - 4)
        context_end = min(len(words), i + 5)
        context = " ".join(words[context_start:context_end])
        print(f"  ...{context}...")
if not found_any:
    print("  (no 'install'/'software' word found in this transcript at all)")