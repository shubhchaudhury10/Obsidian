import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus
from typing import List, Dict, Optional
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

load_dotenv()

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    raise RuntimeError('GEMINI_API_KEY is not set. Add it to a .env file or export it in your environment.')

# gemini-3.5-flash is the newest model and is frequently overloaded (503). The lite
# model is faster, cheaper, and far more available — a better fit for live search.
# Override with GEMINI_MODEL in .env if you want a different one.
MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite')

# Transient server-side HTTP codes worth retrying (overload, timeout, etc.).
# NOTE: 429 is deliberately excluded — it's a quota error, not a transient blip.
# Retrying it within the same minute just burns more of the rate-limit budget and
# can't succeed, so we surface a clear message instead.
RETRYABLE_CODES = {500, 502, 503, 504}
MAX_RETRIES = 5
MAX_BACKOFF = 8.0  # cap per-retry sleep so a slow recovery doesn't stall the user forever

client = genai.Client(api_key=GEMINI_API_KEY)

# --- Embeddings (for the Reddit RAG; stored in Postgres/pgvector) ---------------
# gemini-embedding-001 defaults to 3072 dims but supports Matryoshka truncation; we ask
# for 768 (output_dimensionality) to match the pgvector column. task_type lets the model
# optimize document vs. query representations (measurably better retrieval than untyped).
EMBED_MODEL = os.environ.get('GEMINI_EMBED_MODEL', 'gemini-embedding-001')
EMBED_DIM = 768
_EMBED_BATCH = 100   # max contents per embed_content call we send


def embed_documents(texts: List[str]) -> List[List[float]]:
    """Embed chunk texts for storage (RETRIEVAL_DOCUMENT). Batched; returns one vector each."""
    out: List[List[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        out.extend(_embed_batch(texts[i:i + _EMBED_BATCH], 'RETRIEVAL_DOCUMENT'))
    return out


def embed_query(text: str) -> List[float]:
    """Embed a single search query (RETRIEVAL_QUERY)."""
    return _embed_batch([text], 'RETRIEVAL_QUERY')[0]


def _embed_batch(texts: List[str], task_type: str) -> List[List[float]]:
    """One embed_content call with the same transient-error retry policy as _generate."""
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.models.embed_content(
                model=EMBED_MODEL,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=EMBED_DIM,
                ),
            )
            return [list(e.values) for e in resp.embeddings]
        except genai_errors.APIError as exc:
            code = getattr(exc, 'code', None)
            if code == 429:
                raise RuntimeError(
                    'Gemini embedding rate limit hit (free tier allows few requests per '
                    'minute). Wait a minute and retry, or enable billing for higher limits.'
                ) from exc
            if code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
                raise
            time.sleep(min(delay, MAX_BACKOFF) + random.uniform(0, 0.5))
            delay *= 2


def _generate(**kwargs):
    """Call the model, retrying transient server errors with exponential backoff.

    503 (model overloaded) is the common transient failure here, so we start with a
    short wait — most blips clear in a second or two — and grow the delay (with jitter)
    only if it keeps failing, up to MAX_BACKOFF.
    """
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.APIError as exc:
            code = getattr(exc, 'code', None)
            if code == 429:
                raise RuntimeError(
                    'Gemini rate limit hit (free tier allows very few requests per '
                    'minute/day). Wait a minute and try again, or enable billing on your '
                    'Google AI Studio project for much higher limits.'
                ) from exc
            if code not in RETRYABLE_CODES:
                raise
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f'Gemini is temporarily overloaded ({code}) and did not recover after '
                    f'{MAX_RETRIES} attempts. Please try again in a moment.'
                ) from exc
            sleep_for = delay + random.uniform(0, 0.5)  # jitter avoids retry thundering-herd
            print(f'[gemini_service] {code} from Gemini (attempt {attempt}/{MAX_RETRIES}); '
                  f'retrying in {sleep_for:.1f}s...')
            time.sleep(sleep_for)
            delay = min(delay * 2, MAX_BACKOFF)  # 1, 2, 4, 8, 8...

# Single-call prompt: Gemini reads the user's description, internally pulls out the key
# requirements, then uses its Google Search tool to find the top 6 matching products.
# Scoped to Amazon and Flipkart only, balanced across the two stores.
SEARCH_PROMPT_TEMPLATE = """
You are a product search assistant.

A user has described what they want below. First understand the key requirements that
matter for shopping (product type, important specs, use case, budget, brand preferences).
Then use your Google Search tool to find matching, currently-available products
ONLY from Amazon (amazon.in) and Flipkart (flipkart.com) — do not use any other retailer.

Look at roughly the top 4 relevant products on each site (about 8 total), then return the
6 that BEST match the user's requirements, keeping a balance between the two stores
(aim for 3 from Amazon and 3 from Flipkart). Use real prices found through search — do not
invent products or prices.

User description:
{description}

Return ONLY a valid JSON array of exactly 6 products in this exact structure:
[
  {{
    "name": "Exact product name (include brand and model)",
    "price": "Price found via search, or empty string if unavailable",
    "description": "A short one-sentence summary of the product",
    "source": "Retailer/site name, e.g. Amazon, Flipkart"
  }}
]

Output the JSON array and nothing else. No commentary, no markdown.
"""


_HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; TalkProd/1.0)'}


# Search-URL templates for common Indian retailers. When grounding gives no direct
# product URL, we at least land the user on the named retailer's own site (not Google).
_RETAILER_SEARCH = {
    'flipkart': 'https://www.flipkart.com/search?q={q}',
    'amazon': 'https://www.amazon.in/s?k={q}',
    'croma': 'https://www.croma.com/search/?q={q}',
    'reliance': 'https://www.reliancedigital.in/search?q={q}',
    'vijay': 'https://www.vijaysales.com/search/{q}',
    'tata': 'https://www.tatacliq.com/search/?text={q}',
}


def _retailer_link(name: str, source: str = '') -> str:
    """Fallback link: search the named retailer's own website for the product.

    Better than a generic Google Shopping link because it lands on the actual store
    (Flipkart, Amazon, etc.). Falls back to Google Shopping for unknown retailers.
    """
    q = quote_plus(name.strip())
    src = (source or '').lower()
    for key, template in _RETAILER_SEARCH.items():
        if key in src:
            return template.format(q=q)
    return f'https://www.google.com/search?tbm=shop&q={q}'


def _grounding_uri_for(name: str, text: str, chunks, supports) -> Optional[str]:
    """Find the source URL Gemini actually grounded a given product on.

    Strategy: locate the product name in the response text, find the grounding
    'support' span covering that position, and return its chunk's web URI. Falls back
    to matching the product name against chunk titles by word overlap.
    """
    if not name or not chunks:
        return None

    def chunk_uri(idx) -> Optional[str]:
        if 0 <= idx < len(chunks) and chunks[idx].web and chunks[idx].web.uri:
            return chunks[idx].web.uri
        return None

    # 1) Index-based: which support span covers the product name in the output text?
    pos = text.find(name)
    if pos != -1:
        for s in supports or []:
            seg = getattr(s, 'segment', None)
            if seg and seg.start_index is not None and seg.end_index is not None:
                if seg.start_index <= pos <= seg.end_index:
                    for ci in (s.grounding_chunk_indices or []):
                        uri = chunk_uri(ci)
                        if uri:
                            return uri

    # 2) Fallback: best word-overlap between product name and a chunk title.
    # Drop parenthetical noise like "(Hazel, 128 GB)" so the core model name matches.
    core_name = name.split('(')[0]
    name_tokens = {t for t in core_name.lower().split() if len(t) > 2}
    best_uri, best_score = None, 0
    for ch in chunks:
        if not ch.web or not ch.web.title:
            continue
        score = len(name_tokens & set(ch.web.title.lower().split()))
        if score > best_score:
            best_uri, best_score = ch.web.uri, score
    return best_uri if best_score >= 2 else None


def _resolve_redirect(url: str) -> str:
    """Follow Gemini's grounding-redirect URL to the real retailer page URL.

    The grounding URIs are vertexaisearch redirect links; following the redirect gives
    the actual product page on the retailer's website. Returns the original URL on any
    failure so the link still works (it just redirects in the browser).
    """
    try:
        resp = requests.head(url, allow_redirects=True, timeout=3, headers=_HTTP_HEADERS)
        final = resp.url
        if final and 'vertexaisearch' not in final:
            return final
        # Some servers reject HEAD; retry with a lightweight GET.
        resp = requests.get(url, allow_redirects=True, timeout=4, stream=True,
                            headers=_HTTP_HEADERS)
        return resp.url or url
    except requests.RequestException:
        return url


def _strip_code_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()
    return raw


# --- "Talk to your product" chat ------------------------------------------------

TALK_PROMPT_TEMPLATE = """
You are a product advisor for "{product}". Real people discussed this product on
Reddit, and the most relevant snippets of that discussion are provided below as your
ONLY source of knowledge.

Answer the user's question using these snippets. Rules:
- Base your answer ONLY on what the snippets actually say. Do not invent specs or facts.
- Reddit is opinionated and sometimes sarcastic — read intent, weigh consensus over a
  lone voice, and call out when opinions are mixed.
- Be concise and direct, like a knowledgeable friend. A few sentences is usually enough.
- If the snippets genuinely don't cover the question, say so plainly rather than guessing.

Reddit snippets:
{context}

User question: {question}
"""


def answer_product_question(product: str, question: str, chunks: List[Dict],
                            history: Optional[List[Dict]] = None) -> str:
    """Answer a question about a product from retrieved Reddit chunks.

    `chunks` is the output of product_knowledge.retrieve (each has 'text'/'source').
    `history` is prior turns as [{'role': 'user'|'assistant', 'content': str}, ...].
    """
    context = '\n\n'.join(f'- {c["text"]}' for c in chunks) or '(no snippets found)'
    prompt = TALK_PROMPT_TEMPLATE.format(
        product=product, context=context, question=question.strip()
    )

    # Prepend recent conversation so follow-ups ("what about its camera?") have context.
    contents = []
    for turn in (history or [])[-6:]:
        role = 'model' if turn.get('role') == 'assistant' else 'user'
        contents.append(types.Content(role=role, parts=[types.Part(text=turn.get('content', ''))]))
    contents.append(types.Content(role='user', parts=[types.Part(text=prompt)]))

    response = _generate(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level='low'),
        ),
    )
    text = response.text
    if not text:
        raise ValueError('Gemini returned no answer.')
    return text.strip()


def get_top_products(description: str) -> List[Dict]:
    # Single call: Gemini understands the description and uses its Google Search tool
    # to return the top 6 products. ('low' thinking keeps latency down while leaving
    # enough reasoning to rank the results.)
    search_tool = types.Tool(google_search=types.GoogleSearch())
    search_response = _generate(
        model=MODEL,
        contents=SEARCH_PROMPT_TEMPLATE.format(description=description.strip()),
        config=types.GenerateContentConfig(
            tools=[search_tool],
            thinking_config=types.ThinkingConfig(thinking_level='low'),
        ),
    )

    text = search_response.text
    if not text:
        raise ValueError('Gemini returned no product results.')

    try:
        results = json.loads(_strip_code_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f'Gemini returned invalid JSON:\n{text}') from exc

    if not isinstance(results, list):
        raise ValueError('Expected a JSON array of product recommendations.')

    # Pull the real source URLs from grounding metadata (the pages Gemini actually used).
    candidate = (search_response.candidates or [None])[0]
    meta = getattr(candidate, 'grounding_metadata', None)
    chunks = (getattr(meta, 'grounding_chunks', None) or []) if meta else []
    supports = (getattr(meta, 'grounding_supports', None) or []) if meta else []

    products = [p for p in results if isinstance(p, dict)]

    # Map each product to the grounding source URL Gemini used for it (redirect URL).
    redirect_uris = [
        _grounding_uri_for(p.get('name', ''), text, chunks, supports) for p in products
    ]

    # Resolve the redirect URLs to the real retailer pages in parallel (keeps latency low).
    to_resolve = [u for u in redirect_uris if u]
    resolved = {}
    if to_resolve:
        with ThreadPoolExecutor(max_workers=min(6, len(to_resolve))) as pool:
            resolved = dict(zip(to_resolve, pool.map(_resolve_redirect, to_resolve)))

    for product, uri in zip(products, redirect_uris):
        if uri:
            product['purchase_link'] = resolved.get(uri, uri)
        else:
            # No grounding source matched — search the named retailer's own website.
            product['purchase_link'] = _retailer_link(
                product.get('name', ''), product.get('source', '')
            )
    return results


# --- Price watch: re-check the live price of a single product ------------------

PRICE_CHECK_PROMPT_TEMPLATE = """
You are a price checker. Find the CURRENT price of the exact product below on Amazon
(amazon.in) or Flipkart (flipkart.com) using your Google Search tool. Prefer the
retailer/page indicated by the reference link if one is given.

Product: {name}
Reference link (may be empty): {url}

Use only a real price you actually find through search — do NOT guess or invent one.
Return ONLY a valid JSON object in this exact structure, nothing else:
{{
  "price_value": <the price as a plain number with no currency symbol, commas, or
                  decimals beyond two, e.g. 89990 ; use null if no price was found>,
  "price_text": "<the price as shown to a shopper, e.g. ₹89,990 ; empty string if none>",
  "source": "<retailer name, e.g. Amazon or Flipkart ; empty string if unknown>"
}}

Output the JSON object and nothing else. No commentary, no markdown.
"""


def get_current_price(name: str, url: str = '') -> Dict:
    """Look up the current live price of one product via grounded Google Search.

    Returns {'price_value': float|None, 'price_text': str, 'source': str}. Mirrors the
    grounded-search approach of get_top_products, scoped to a single known product.

    NOTE: grounding-sourced prices are approximate and may lag the live listing. This
    function is intentionally the only price source, so it can be swapped for a real
    price API later without touching the watch scheduler.
    """
    search_tool = types.Tool(google_search=types.GoogleSearch())
    response = _generate(
        model=MODEL,
        contents=PRICE_CHECK_PROMPT_TEMPLATE.format(name=name.strip(), url=(url or '').strip()),
        config=types.GenerateContentConfig(
            tools=[search_tool],
            thinking_config=types.ThinkingConfig(thinking_level='low'),
        ),
    )

    text = response.text
    if not text:
        raise ValueError('Gemini returned no price result.')

    try:
        data = json.loads(_strip_code_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f'Gemini returned invalid price JSON:\n{text}') from exc

    raw = data.get('price_value')
    value = None
    if raw is not None:
        try:
            # Tolerate "89,990" / "₹89990" sneaking through despite the prompt.
            value = float(re.sub(r'[^\d.]', '', str(raw)))
        except (TypeError, ValueError):
            value = None

    return {
        'price_value': value,
        'price_text': str(data.get('price_text') or '').strip(),
        'source': str(data.get('source') or '').strip(),
    }
