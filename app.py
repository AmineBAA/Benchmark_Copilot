"""
app.py — Veille concurrentielle bancaire (Saham Bank)

ARCHITECTURE DU FILTRAGE (3 couches) :
  1. Pré-filtre par mots-clés    → élimine emploi, bourse, actu internationale (grfatuit)
  2. Filtre NLP (HuggingFace API) → score de similarité sémantique avec des exemples
                                     pertinents définis par le métier
  3. Filtre de secours (mots-clés banques) → si pas de token HF, filtre basique

Le NLP utilise le modèle sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
via l'API Inference gratuite de HuggingFace (tournant sur leurs serveurs, zéro RAM local).
Ajoute HF_TOKEN dans les secrets Streamlit pour activer cette couche.
"""

import os
import glob
import re
import time
import json
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import feedparser
import streamlit as st
from bs4 import BeautifulSoup

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

CACHE_TTL_SECONDS = 4 * 3600
APP_VERSION = "2026-06-22-v8-nlp"

# Seuil de similarité sémantique (0 à 1). Un article est retenu si son score
# dépasse ce seuil par rapport aux exemples de référence.
# Augmente → plus sélectif. Diminue → plus large.
NLP_THRESHOLD = 0.42

# Modèle multilingue léger (~120MB sur les serveurs HF, 0 RAM local)
HF_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"

# Exemples de référence : articles PERTINENTS pour la veille.
# Le NLP cherche des articles sémantiquement proches de ces exemples.
# → Ajoute tes propres exemples ici pour affiner le filtre.
GOLDEN_EXAMPLES = [
    # Tes exemples exacts
    "CIH Bank lance son service bancaire CIH M3AK sur WhatsApp",
    "Attijariwafa bank lance une offre d'hébergement intégrée à son application mobile",
    "Attijariwafa bank lance Simple, la première néobanque marocaine",
    # Partenariats
    "BCP signe un partenariat stratégique avec une fintech marocaine",
    "Bank Of Africa signe un accord de financement vert avec la SFI",
    "CIH Bank s'allie avec une startup pour le paiement mobile",
    # Produits et offres
    "Banque Populaire lance un nouveau pack bancaire pour les TPE",
    "Crédit du Maroc lance une offre de crédit immobilier à taux préférentiel",
    "CFG Bank déploie une nouvelle carte Visa pour les entreprises",
    "Attijariwafa bank lance une campagne pour les Marocains du monde",
    # Digital / innovation
    "CIH Bank lance son application mobile de paiement instantané",
    "BCP déploie l'open banking pour ses clients entreprises au Maroc",
    "Bank Al-Maghrib autorise la première néobanque au Maroc",
    # Réglementaire BAM
    "Bank Al-Maghrib publie une circulaire sur la supervision des banques",
    "Bank Al-Maghrib maintient son taux directeur lors du conseil de politique monétaire",
    "Office des Changes lance une plateforme digitale pour les opérations de change au Maroc",
]

SOURCES = [
    {"name": "Bank Al-Maghrib (BAM)",
     "url": "https://www.bkam.ma/Communiques",
     "type": "html", "bank": "Bank Al-Maghrib",
     "selector": "h3 a, h4 a, .field-content a, td a"},
    {"name": "BoursNews – Actualités",
     "url": "https://boursenews.ma/articles/actualite",
     "type": "html", "bank": None,
     "selector": "h3 a, h4 a, h5 a"},
    {"name": "BoursNews – Décryptages",
     "url": "https://boursenews.ma/articles/decryptage",
     "type": "html", "bank": None,
     "selector": "h3 a, h4 a, h5 a"},
    {"name": "BoursNews – Venture Capital",
     "url": "https://boursenews.ma/articles/venture-capital",
     "type": "html", "bank": None,
     "selector": "h3 a, h4 a, h5 a"},
    {"name": "L'Economiste",
     "url": "https://www.leconomiste.com/rss-leconomiste",
     "type": "rss", "bank": None},
    {"name": "Finance News Hebdo",
     "url": "https://fnh.ma/",
     "type": "html", "bank": None,
     "selector": "h2 a, h3 a, h4 a"},
    {"name": "Médias24 – Banques",
     "url": "https://medias24.com/economie/banques/",
     "type": "html", "bank": None,
     "selector": "h2 a, h3 a, h4 a, article a"},
    {"name": "Attijariwafa bank",
     "url": "https://www.attijariwafabank.com/fr/espace-media/communiques-de-presse",
     "type": "html", "bank": "Attijariwafa",
     "selector": "h2 a, h3 a, h4 a, .title a"},
    {"name": "CIH Bank",
     "url": "https://www.cihbank.ma/actualites",
     "type": "html", "bank": "CIH Bank",
     "selector": "h2 a, h3 a, h4 a, article a"},
    {"name": "Groupe BCP",
     "url": "https://www.groupebcp.com/fr/espace-communication/communiqu%C3%A9s-de-presse",
     "type": "html", "bank": "BCP",
     "selector": "h2 a, h3 a, h4 a"},
    {"name": "Bank Of Africa",
     "url": "https://www.bankofafrica.ma/fr/presse/communiques",
     "type": "html", "bank": "Bank Of Africa",
     "selector": "h2 a, h3 a, h4 a, .news-title a"},
]

# Bruit écarté AVANT le NLP (évite de gaspiller des appels API)
KEYWORDS_EXCLUSION = [
    "offre d'emploi", "offres d'emploi", "offre d emploi", "offres d emploi",
    "recrut", "recrutement", "carrière", "carrières",
    "stage", "stagiaire", "candidature", "nous recrutons", "rejoignez",
    "poste à pourvoir",
    "feuille de marché", "portefeuille trading", "analyse technique",
    "clôture de la bourse", "ouverture de la bourse",
    "masi ", "madex ", "assemblée générale ordinaire", "ago ",
    "dividende", "résultats annuels", "bénéfice net",
    "taux obligataire", "marché obligataire",
    "banque d'angleterre", "banque centrale européenne",
    "réserve fédérale", "fed ", "bce ", "boj ", "boe ",
    "banque de france", "banque du japon",
    "wall street", "dow jones", "s&p 500", "nasdaq",
    "pétrole", "métaux précieux", "matières premières",
    "israël", "ukraine", "russie", "iran", "gaza",
    "cessez-le-feu", "conflit armé", "guerre",
    "football", "coupe du monde", "can ", "équipe nationale de football",
]

# Filtre de secours si pas de token HF
KEYWORDS_BANQUES_MAROC = [
    "attijariwafa", "attijari", "wafasalaf", "wafacash",
    "banque populaire", "banque centrale populaire", "bcp", "chaabi",
    "cih bank", "bank of africa", "bmce",
    "cfg bank", "crédit du maroc", "bmci",
    "al barid bank", "barid bank", "société générale maroc",
    "bank al-maghrib", "bank al maghrib",
    "secteur bancaire marocain", "retail banking maroc",
]

KEYWORDS_REGLEMENTAIRE_BAM = [
    "bank al-maghrib", "bank al maghrib", "circulaire bam",
    "taux directeur bam", "politique monétaire maroc",
    "wali de bank al-maghrib", "ammc", "office des changes",
]

BANK_KEYWORDS = {
    "Attijariwafa": ["attijariwafa", "wafabank", "wafasalaf", "wafacash", "attijari "],
    "BCP": ["bcp", "banque centrale populaire", "banque populaire", "chaabi"],
    "CIH Bank": ["cih bank", "cih "],
    "Bank Of Africa": ["bank of africa", "bmce"],
    "CFG Bank": ["cfg bank"],
    "Crédit du Maroc": ["crédit du maroc", "credit du maroc"],
    "Société Générale Maroc": ["société générale maroc", "sg maroc", "sgma"],
    "BMCI": ["bmci"],
    "Al Barid Bank": ["al barid bank", "barid bank"],
    "Bank Al-Maghrib": ["bank al-maghrib", "bank al maghrib"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

CATEGORY_KEYWORDS = {

    "pricing": [
        "tarif",
        "commission",
        "frais",
        "gratuit",
        "cashback",
        "pack"
    ],

    "digital": [
        "application mobile",
        "mobile banking",
        "open banking",
        "wallet",
        "whatsapp"
    ],

    "paiement": [
        "paiement",
        "visa",
        "mastercard",
        "virement",
        "instant payment"
    ],

    "credit": [
        "crédit",
        "financement",
        "leasing"
    ],

    "ia": [
        "intelligence artificielle",
        "chatbot",
        "copilot",
        "machine learning"
    ],

    "fintech": [
        "fintech",
        "startup",
        "partenariat"
    ],

    "reglementaire": [
        "bank al-maghrib",
        "circulaire",
        "office des changes"
    ]
}
# --------------------------------------------------------------------------
# Couche NLP — HuggingFace Inference API
# --------------------------------------------------------------------------

def _get_hf_token():
    try:
        return st.secrets.get("HF_TOKEN") or os.environ.get("HF_TOKEN")
    except Exception:
        return os.environ.get("HF_TOKEN")


def _embed(texts: list[str], token: str) -> list[list[float]] | None:
    """Appelle l'API HF Feature Extraction et retourne les embeddings."""
    try:
        resp = requests.post(
            HF_API_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"inputs": texts, "options": {"wait_for_model": True}},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def _golden_embeddings(token: str) -> list[list[float]] | None:
    """Calcule et cache les embeddings des exemples de référence (1x/jour)."""
    return _embed(GOLDEN_EXAMPLES, token)


def nlp_score(title: str, summary: str, golden_embs: list, token: str) -> float:
    """Score NLP d'un article = max similarité cosinus avec les exemples de référence."""
    text = f"{title} {summary}".strip()
    embs = _embed([text], token)
    if not embs:
        return 0.0
    article_emb = embs[0]
    return max(_cosine(article_emb, g) for g in golden_embs)

def classify_article(article):

    text = (
        article["title"]
        + " "
        + article.get("content","")
    ).lower()

    best_category = None
    best_score = 0

    for category, words in CATEGORY_KEYWORDS.items():

        score = sum(
            1 for word in words
            if word in text
        )

        if score > best_score:
            best_score = score
            best_category = category

    return best_category
# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    title = re.sub(
        r'(Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche'
        r'|Janvier|F\xe9vrier|Mars|Avril|Mai|Juin|Juillet|Ao\xfbt'
        r'|Septembre|Octobre|Novembre|D\xe9cembre'
        r'|\d{1,2}/\d{2,4}|\d{4}|- par\b).*',
        '', title, flags=re.IGNORECASE
    ).strip(' \u2013\u2014|\u00b7\u2022-')
    return title


def _normalize(article: dict) -> str:
    text = f"{article['title']} {article.get('summary', '')}".lower()
    for ch in ["\u2019", "\u2018", "\u02bc", "`"]:
        text = text.replace(ch, "'")
    return text


def scrape_rss(source: dict) -> tuple[list, str | None]:
    articles, error = [], None
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            error = str(getattr(feed, "bozo_exception", "flux RSS illisible"))
        for entry in feed.entries:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else datetime.now(timezone.utc)
            articles.append({
                "title": entry.get("title", "").strip(),
                "url": entry.get("link", "").strip(),
                "summary": (entry.get("summary", "") or "").strip(),
                "source": source["name"],
                "bank": source.get("bank"),
                "bank_fixed": bool(source.get("bank")),
                "published_at": pub_dt,
            })
    except Exception as exc:
        error = str(exc)
    return articles, error

def extract_article_content(url):
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            timeout=15
        )

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        paragraphs = soup.find_all("p")

        content = " ".join(
            p.get_text(" ", strip=True)
            for p in paragraphs
        )

        return content[:5000]

    except Exception:
        return ""

def scrape_html(source: dict) -> tuple[list, str | None]:
    articles, error = [], None
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        seen = set()
        for link in soup.select(source.get("selector", "a")):
            href = link.get("href")
            text = link.get_text(strip=True)
            if not href or not text or len(text) < 15:
                continue
            full_url = href if href.startswith("http") else urljoin(source["url"], href)
            if full_url in seen:
                continue
            seen.add(full_url)
            clean = _clean_title(text)
            if not clean or len(clean) < 12:
                continue
            articles.append({
                "title": clean,
                "url": full_url,
                "summary": "",
                "source": source["name"],
                "bank": source.get("bank"),
                "bank_fixed": bool(source.get("bank")),
                "published_at": datetime.now(timezone.utc),
            })
          article["content"] = extract_article_content(full_url)
    except Exception as exc:
        error = str(exc)
    return articles, error


def scrape_all_sources() -> tuple[list, list]:
    raw, diagnostics, seen_titles = [], [], set()
    for source in SOURCES:
        items, error = scrape_rss(source) if source["type"] == "rss" else scrape_html(source)
        deduped = []
        for item in items:
            key = item["title"].lower().strip()[:60]
            if key not in seen_titles:
                seen_titles.add(key)
                deduped.append(item)
        raw.extend(deduped)
        diagnostics.append({"name": source["name"], "raw_count": len(deduped), "error": error})
        time.sleep(0.5)
    return raw, diagnostics
def compute_impact(article):

    text = (
        article["title"]
        + " "
        + article.get("content","")
    ).lower()

    score = 0

    if "lance" in text:
        score += 25

    if "nouveau" in text:
        score += 15

    if "partenariat" in text:
        score += 20

    if "tarif" in text:
        score += 30

    if "gratuit" in text:
        score += 20

    if "cashback" in text:
        score += 20

    if "application mobile" in text:
        score += 15

    return min(score,100)

# --------------------------------------------------------------------------
# Filtrage : pré-filtre mots-clés → NLP → secours
# --------------------------------------------------------------------------

def passes_exclusion(article: dict) -> bool:
    text = _normalize(article)
    return not any(kw in text for kw in KEYWORDS_EXCLUSION)


def classify_fallback(article: dict) -> str | None:
    """Filtre de secours sans NLP : banques marocaines + réglementaire BAM."""
    text = _normalize(article)
    if any(kw in text for kw in KEYWORDS_REGLEMENTAIRE_BAM):
        return "reglementaire_bam"
    if any(kw in text for kw in KEYWORDS_BANQUES_MAROC):
        return "offre_produit"
    return None


def detect_bank(article: dict) -> str:
    if article.get("bank"):
        return article["bank"]
    text = _normalize(article)
    for bank, keywords in BANK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return bank
    return article["source"]


def classify_reglementaire(article: dict) -> bool:
    text = _normalize(article)
    return any(kw in text for kw in KEYWORDS_REGLEMENTAIRE_BAM)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Récupération des actualités...")
def get_articles(threshold: float) -> tuple[list, list, str]:
    token = _get_hf_token()
    use_nlp = bool(token)
    golden_embs = None

    if use_nlp:
        golden_embs = _golden_embeddings(token)
        if not golden_embs:
            use_nlp = False  # API indisponible → fallback

    raw, diagnostics = scrape_all_sources()

    # Pré-filtre mots-clés (gratuit, rapide)
    candidates = [a for a in raw if passes_exclusion(a)]

    result = []
    kept_by_source = {}

    for article in candidates:
        if use_nlp:
            score = nlp_score(article["title"], article.get("summary", ""), golden_embs, token)
            article["nlp_score"] = round(score, 3)
            if score >= threshold:
                category = "reglementaire_bam" if classify_reglementaire(article) else "offre_produit"
                article["category"] = category
                article["bank"] = detect_bank(article)
                result.append(article)
                kept_by_source[article["source"]] = kept_by_source.get(article["source"], 0) + 1
        else:
            category = classify_fallback(article)
            if category:
                article["category"] = classify_article(article)
                article["impact"] = compute_impact(article)
                article["nlp_score"] = None
                article["bank"] = detect_bank(article)
                result.append(article)
                kept_by_source[article["source"]] = kept_by_source.get(article["source"], 0) + 1

    for diag in diagnostics:
        diag["kept_count"] = kept_by_source.get(diag["name"], 0)

    mode = "NLP (HuggingFace)" if use_nlp else "Mots-clés (sans token HF)"
    return result, diagnostics, mode

st.markdown("## Résumé Exécutif")

top_articles = sorted(
    articles,
    key=lambda x: x["impact"],
    reverse=True
)[:5]

for art in top_articles:

    st.info(
        f"{art['bank']} - "
        f"{art['title']} "
        f"(Impact {art['impact']}/100)"
    )
import pandas as pd

df = pd.DataFrame(articles)

if not df.empty:

    benchmark = (
        df.groupby("bank")
        .size()
        .reset_index(name="Actualités")
        .sort_values(
            "Actualités",
            ascending=False
        )
    )

    st.dataframe(
        benchmark,
        use_container_width=True
    )
# --------------------------------------------------------------------------
# Interface Streamlit
# --------------------------------------------------------------------------

st.set_page_config(page_title="Saham Bank – Veille Concurrentielle", page_icon="🏦", layout="wide")

CUSTOM_CSS = """
<style>
#MainMenu, footer, header {visibility: hidden;}
.block-container {padding-top: 1.5rem; max-width: 100%;}
body, .stApp {background-color: #122420; color: #FFFFFF;}
section[data-testid="stSidebar"] {background-color: #1A332E; border-right: 1px solid #2D544E;}
section[data-testid="stSidebar"] button {
    width:100%;text-align:left;background:transparent;color:#A0B2AF;
    border:none;border-radius:6px;padding:12px 15px;font-weight:500;
}
section[data-testid="stSidebar"] button:hover {background-color:#D24B2C;color:#fff;}
.card {background-color:#24443F;border-radius:10px;padding:20px;border:1px solid #2D544E;margin-bottom:10px;}
.card h2 {font-size:18px;margin-bottom:20px;border-left:4px solid #D24B2C;padding-left:10px;}
.news-item {padding:15px 0;border-bottom:1px solid #2D544E;}
.news-item:last-child {border-bottom:none;}
.news-meta {display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#A0B2AF;}
.badge {background-color:#122420;color:#D24B2C;padding:2px 8px;border-radius:4px;
        font-weight:bold;border:1px solid #D24B2C;white-space:nowrap;}
.score-badge {background-color:#122420;color:#1D9E75;padding:2px 6px;border-radius:4px;
              font-size:11px;border:1px solid #1D9E75;}
.news-title {font-size:16px;font-weight:600;color:#FFFFFF;margin-top:8px;}
.news-desc {font-size:14px;color:#A0B2AF;line-height:1.4;margin-top:6px;}
.metric-box {background:#122420;padding:15px;border-radius:8px;
             border-left:3px solid #A0B2AF;margin-bottom:12px;}
.metric-box.hot {border-left-color:#D24B2C;}
.metric-title {font-size:13px;color:#A0B2AF;text-transform:uppercase;margin-bottom:5px;}
.metric-val {font-size:15px;font-weight:bold;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

PAGES = ["Veille Concurrentielle", "Rapports Bank Al-Maghrib", "Configuration"]
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

# Sidebar
logo_files = glob.glob(os.path.join(os.path.dirname(__file__), "logo*"))
if logo_files:
    st.sidebar.image(logo_files[0], use_container_width=True)
else:
    st.sidebar.markdown(
        "<div style='text-align:center;padding:15px;font-weight:bold;font-size:18px;"
        "border-bottom:1px solid #2D544E;margin-bottom:10px;'>SAHAM BANK</div>",
        unsafe_allow_html=True,
    )

for p in PAGES:
    if st.sidebar.button(p, key=f"nav_{p}"):
        st.session_state.page = p

st.sidebar.divider()
st.sidebar.markdown("<p style='color:#A0B2AF;font-size:12px;'>Précision du filtre NLP</p>", unsafe_allow_html=True)
threshold = st.sidebar.slider(
    "Seuil de similarité", min_value=0.30, max_value=0.70,
    value=NLP_THRESHOLD, step=0.02,
    help="Plus élevé = plus sélectif. Baisse si trop peu de résultats.",
    label_visibility="collapsed",
)
st.sidebar.caption(f"Seuil actuel : {threshold:.2f}")

# Chargement des données
articles, diagnostics, filter_mode = get_articles(threshold)

# Diagnostic
with st.expander("🔧 Diagnostic du scraping"):
    st.caption(f"Version : `{APP_VERSION}` · Mode de filtrage : **{filter_mode}**")
    if "Mots-clés" in filter_mode:
        st.warning(
            "Token HuggingFace non configuré → filtrage par mots-clés uniquement (moins précis). "
            "Pour activer le NLP, ajoute `HF_TOKEN` dans les secrets Streamlit "
            "(Settings → Secrets). Token gratuit sur huggingface.co/settings/tokens)."
        )
    for diag in diagnostics:
        status = f"⚠️ {diag['error']}" if diag["error"] else "✅ OK"
        st.markdown(
            f"**{diag['name']}** — Bruts : {diag['raw_count']} · "
            f"Retenus : {diag['kept_count']} · {status}"
        )
    if st.button("Forcer une nouvelle actualisation"):
        st.cache_data.clear()
        st.rerun()

# Page Configuration
if st.session_state.page == "Configuration":
    st.markdown("### Sources surveillées")
    st.caption("Pour modifier une source, édite la liste `SOURCES` en haut de `app.py`.")
    for s in SOURCES:
        st.markdown(f"- **{s['name']}** ({s['type'].upper()}) — {s['url']}")
    st.divider()
    st.markdown("### Exemples de référence (NLP)")
    st.caption(
        "Ces exemples définissent ce qu'est un article pertinent pour le NLP. "
        "Modifie `GOLDEN_EXAMPLES` dans `app.py` pour affiner."
    )
    for ex in GOLDEN_EXAMPLES:
        st.markdown(f"- {ex}")
    st.stop()

# Filtrage par page
if st.session_state.page == "Rapports Bank Al-Maghrib":
    feed_articles = [a for a in articles if a.get("category") == "reglementaire_bam"]
    feed_title = "Communications Bank Al-Maghrib"
else:
    feed_articles = articles
    feed_title = f"Synthèse — 30 derniers jours ({len(feed_articles)} article(s))"

# En-tête
st.markdown("## Veille Concurrentielle — Secteur Bancaire Marocain")
st.markdown(
    "<p style='color:#A0B2AF;font-size:14px;'>Suivi des innovations, "
    "lancements et mouvements stratégiques.</p>",
    unsafe_allow_html=True,
)

# Panneau métriques
def build_metrics(arts):
    regl = [a for a in arts if a.get("category") == "reglementaire_bam"]
    prod = [a for a in arts if a.get("category") == "offre_produit"]
    metrics = []
    if regl:
        metrics.append({"type": "Régulation", "title": "Bank Al-Maghrib",
                        "val": f"{len(regl)} communication(s) réglementaire(s) ce mois.", "hot": False})
    if prod:
        metrics.append({"type": "Tendance", "title": "Offres & lancements",
                        "val": f"{len(prod)} actualité(s) concurrentielle(s) détectée(s).", "hot": True})
    if not metrics:
        metrics.append({"type": "Synthèse", "title": "Aucun résultat",
                        "val": "Baisse le seuil NLP ou actualise les données.", "hot": False})
    return metrics

col_news, col_metrics = st.columns([2, 1])

with col_news:
    st.markdown(f"<div class='card'><h2>{feed_title}</h2>", unsafe_allow_html=True)
    if not feed_articles:
        st.markdown(
            "<p style='color:#A0B2AF;padding:15px 0;'>Aucun article pertinent. "
            "Essaie de baisser le seuil NLP dans la barre latérale ou clique "
            "sur 'Forcer une nouvelle actualisation' dans le diagnostic.</p>",
            unsafe_allow_html=True,
        )
    for article in sorted(feed_articles, key=lambda a: a.get("published_at"), reverse=True):
        date_str = article["published_at"].strftime("%d/%m %H:%M")
        desc = article.get("summary") or "Voir l'article complet via le lien ci-dessus."
        score_html = ""
        if article.get("nlp_score") is not None:
            score_html = f"<span class='score-badge'>score {article['nlp_score']:.2f}</span>"
        st.markdown(
            f"""<div class='news-item'>
                <div class='news-meta'>
                    <span class='badge'>{article['bank']}</span>
                    <span>{score_html}&nbsp;{date_str}</span>
                </div>
                <div class='news-title'>
                    <a href='{article['url']}' target='_blank'
                       style='color:#FFFFFF;text-decoration:none;'>
                       {article['title']}
                    </a>
                </div>
                <div class='news-desc'>{desc[:220]}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

with col_metrics:
    st.markdown("<div class='card'><h2>Infos Pratiques & Tendances</h2>", unsafe_allow_html=True)
    for m in build_metrics(feed_articles):
        st.markdown(
            f"""<div class='metric-box {'hot' if m['hot'] else ''}'>
                <div class='metric-title'>{m['type']} — {m['title']}</div>
                <div class='metric-val'>{m['val']}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)
