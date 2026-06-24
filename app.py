# -*- coding: utf-8 -*-
"""
app.py — Veille concurrentielle bancaire (Saham Bank)

Architecture du filtrage NLP (modèle LOCAL, comme le notebook de référence) :
  1. Scraping des 20 sites de radar-list (titres d'articles)
  2. Pré-filtre mots-clés → écarte le bruit évident (emploi, sport, météo...)
  3. Filtrage sémantique → SentenceTransformer encode chaque titre et calcule
     sa similarité cosinus avec des exemples de référence métier. Au-dessus
     du seuil = pertinent.
  4. KeyBERT extrait les concepts-clés de chaque article retenu (affichés en badges)

Modèle : sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
(chargé une seule fois et mis en cache via @st.cache_resource)
"""

import os
import re
import glob
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
import feedparser
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

CACHE_TTL_SECONDS = 4 * 3600
APP_VERSION = "2026-06-24-v9-local-nlp"

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Seuil de similarité sémantique (0–1). Plus haut = plus sélectif.
DEFAULT_THRESHOLD = 0.40

# Sites à surveiller (radar-list.txt). On scrape la page d'accueil/économie
# et le NLP fait le tri. Les sélecteurs ciblent les titres d'articles.
SOURCES = [
    {"name": "L'Economiste", "url": "https://www.leconomiste.com", "selector": "h2 a, h3 a, .views-field a"},
    {"name": "Médias24", "url": "https://medias24.com/economie/banques/", "selector": "h2 a, h3 a, article a"},
    {"name": "EcoActu", "url": "https://ecoactu.ma/finances/banques/", "selector": "h2 a, h3 a, article a"},
    {"name": "BoursNews", "url": "https://boursenews.ma/articles/actualite", "selector": "h3 a, h4 a, h5 a"},
    {"name": "Challenge", "url": "https://www.challenge.ma/category/economie/", "selector": "h2 a, h3 a, article a"},
    {"name": "La Vie Éco", "url": "https://www.lavieeco.com/economie/", "selector": "h2 a, h3 a, article a"},
    {"name": "Finance News Hebdo", "url": "https://fnh.ma/", "selector": "h2 a, h3 a, h4 a"},
    {"name": "Les Éco", "url": "https://leseco.ma/maroc/economie/", "selector": "h2 a, h3 a, article a"},
    {"name": "Le Matin", "url": "https://lematin.ma/economie", "selector": "h2 a, h3 a, article a"},
    {"name": "Le360", "url": "https://fr.le360.ma/economie/", "selector": "h2 a, h3 a, article a"},
    {"name": "Hespress FR", "url": "https://fr.hespress.com/economie", "selector": "h2 a, h3 a, article a"},
    {"name": "TelQuel", "url": "https://telquel.ma/categorie/economie", "selector": "h2 a, h3 a, article a"},
    {"name": "H24Info", "url": "https://www.h24info.ma/economie/", "selector": "h2 a, h3 a, article a"},
    {"name": "Welovebuzz", "url": "https://welovebuzz.com/", "selector": "h2 a, h3 a, article a"},
    {"name": "LeDesk", "url": "https://ledesk.ma/", "selector": "h2 a, h3 a, article a"},
    {"name": "MAP", "url": "https://www.map.ma/fr/category/economie", "selector": "h2 a, h3 a, article a"},
    {"name": "Le Maroc Économique", "url": "https://lemaroceconomique.ma/", "selector": "h2 a, h3 a, article a"},
    {"name": "La Tribune", "url": "https://latribune.ma/", "selector": "h2 a, h3 a, article a"},
    {"name": "Hespress", "url": "https://www.hespress.com/economie", "selector": "h2 a, h3 a, article a"},
    {"name": "Al Yaoum 24", "url": "https://alyaoum24.com/", "selector": "h2 a, h3 a, article a"},
]

# Exemples de référence : ce qui est PERTINENT pour la veille concurrentielle.
# Le NLP retient les articles sémantiquement proches de ces phrases.
GOLDEN_EXAMPLES = [
    "CIH Bank lance son service bancaire CIH M3AK sur WhatsApp",
    "Attijariwafa bank lance une offre d'hébergement intégrée à son application mobile",
    "Attijariwafa bank lance Simple, la première néobanque marocaine",
    "Banque Populaire signe un partenariat avec une fintech pour le paiement mobile",
    "Bank Of Africa signe un accord de financement vert avec un partenaire international",
    "CIH Bank déploie une nouvelle carte bancaire pour les jeunes",
    "Crédit du Maroc lance une offre de crédit immobilier à taux préférentiel",
    "CFG Bank lance un nouveau pack bancaire pour les entreprises",
    "Une banque marocaine lance une campagne pour les Marocains du monde",
    "BMCI déploie une nouvelle solution de banque mobile",
    "Bank Al-Maghrib autorise une nouvelle néobanque au Maroc",
    "Bank Al-Maghrib publie une circulaire sur la supervision bancaire",
    "Bank Al-Maghrib maintient son taux directeur lors du conseil de politique monétaire",
    "Lancement d'une nouvelle solution de paiement mobile pour les commerçants marocains",
    "Partenariat stratégique entre une banque marocaine et une startup fintech",
    "Nouvelle offre de bancassurance lancée par une banque au Maroc",
]

# Banques marocaines suivies (pour le badge + détection)
BANK_KEYWORDS = {
    "Attijariwafa": ["attijariwafa", "attijari", "wafabank", "wafasalaf", "wafacash"],
    "BCP / Banque Populaire": ["banque populaire", "banque centrale populaire", "bcp", "chaabi"],
    "CIH Bank": ["cih bank", "cih "],
    "Bank Of Africa": ["bank of africa", "bmce", "boa "],
    "CFG Bank": ["cfg bank"],
    "Crédit du Maroc": ["crédit du maroc", "credit du maroc"],
    "Société Générale Maroc": ["société générale maroc", "sg maroc", "sgma"],
    "BMCI": ["bmci"],
    "Al Barid Bank": ["al barid bank", "barid bank"],
    "Bank Al-Maghrib": ["bank al-maghrib", "bank al maghrib", "bam"],
}

# Bruit écarté avant le NLP (économie d'encodage)
KEYWORDS_EXCLUSION = [
    "offre d'emploi", "offres d'emploi", "recru", "carrière", "stage", "stagiaire", "nouvelles recrues",
    "candidature", "rejoignez", "poste à pourvoir",
    "football", "coupe du monde", "can 2025", "can 2026", "mondial 2026", "écosse", "haïti", "demi-finale", "quart de finale",
    "météo", "vague de chaleur", "averses",
    "iran", "ormuz", "ukraine", "gaza", "israël", "etats-unis et l'iran",
    "colombie", "royaume-uni", "starmer",
    "feuille de marché", "analyse technique",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# --------------------------------------------------------------------------
# Chargement du modèle NLP (local, mis en cache une seule fois)
# --------------------------------------------------------------------------

@st.cache_resource(show_spinner="Chargement du modèle NLP (1er démarrage ~1-2 min)...")
def load_models():
    """Charge SentenceTransformer + KeyBERT une seule fois pour toute la session."""
    from sentence_transformers import SentenceTransformer
    from keybert import KeyBERT
    embedder = SentenceTransformer(MODEL_NAME)
    kw_model = KeyBERT(model=embedder)  # KeyBERT réutilise le même modèle (pas de 2e téléchargement)
    return embedder, kw_model


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def golden_embeddings():
    embedder, _ = load_models()
    return embedder.encode(GOLDEN_EXAMPLES, convert_to_numpy=True, normalize_embeddings=True)


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    title = re.sub(
        r'(Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche'
        r'|\d{1,2}/\d{2,4}|\d{4}|- par\b).*',
        '', title, flags=re.IGNORECASE
    ).strip(' \u2013\u2014|\u00b7\u2022-')
    return title


def scrape_site(source: dict) -> tuple[list, str | None]:
    articles, error = [], None
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        base = "{0.scheme}://{0.netloc}".format(urlparse(source["url"]))
        seen = set()
        for link in soup.select(source["selector"]):
            href = link.get("href")
            text = link.get_text(strip=True)
            if not href or not text or len(text) < 18:
                continue
            full_url = href if href.startswith("http") else urljoin(base, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            clean = _clean_title(text)
            if not clean or len(clean) < 15:
                continue
            articles.append({
                "title": clean,
                "url": full_url,
                "source": source["name"],
                "published_at": datetime.now(timezone.utc),
            })
    except Exception as exc:
        error = str(exc)
    return articles[:40], error  # plafonne par site pour limiter le volume à encoder


def scrape_all() -> tuple[list, list]:
    raw, diagnostics, seen_titles = [], [], set()
    for source in SOURCES:
        items, error = scrape_site(source)
        deduped = []
        for item in items:
            key = item["title"].lower().strip()[:55]
            if key not in seen_titles:
                seen_titles.add(key)
                deduped.append(item)
        raw.extend(deduped)
        diagnostics.append({"name": source["name"], "raw_count": len(deduped), "error": error})
        time.sleep(0.4)
    return raw, diagnostics


# --------------------------------------------------------------------------
# Filtrage NLP
# --------------------------------------------------------------------------

def _normalize(text: str) -> str:
    text = text.lower()
    for ch in ["\u2019", "\u2018", "\u02bc", "`"]:
        text = text.replace(ch, "'")
    return text


def passes_exclusion(title: str) -> bool:
    text = _normalize(title)
    return not any(kw in text for kw in KEYWORDS_EXCLUSION)


def detect_bank(title: str, source: str) -> str:
    text = _normalize(title)
    for bank, keywords in BANK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return bank
    return source


def is_reglementaire(title: str) -> bool:
    text = _normalize(title)
    return any(kw in text for kw in ["bank al-maghrib", "bank al maghrib", "bam", "ammc", "circulaire", "taux directeur"])


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Analyse sémantique des actualités...")
def get_articles(threshold: float):
    import numpy as np
    embedder, kw_model = load_models()
    golden = golden_embeddings()

    raw, diagnostics = scrape_all()
    candidates = [a for a in raw if passes_exclusion(a["title"])]

    result = []
    kept_by_source = {}

    if candidates:
        titles = [a["title"] for a in candidates]
        embeddings = embedder.encode(titles, convert_to_numpy=True, normalize_embeddings=True)
        # Similarité cosinus (vecteurs déjà normalisés → produit scalaire)
        scores = embeddings @ golden.T  # (n_articles, n_golden)
        max_scores = scores.max(axis=1)

        for article, score in zip(candidates, max_scores):
            if score >= threshold:
                article["nlp_score"] = round(float(score), 3)
                article["category"] = "reglementaire_bam" if is_reglementaire(article["title"]) else "offre_produit"
                article["bank"] = detect_bank(article["title"], article["source"])
                # KeyBERT : extraction des concepts-clés (comme le notebook)
                try:
                    keywords = kw_model.extract_keywords(
                        article["title"],
                        keyphrase_ngram_range=(1, 3),
                        stop_words="french",
                        top_n=3,
                    )
                    article["concepts"] = [kw for kw, _ in keywords]
                except Exception:
                    article["concepts"] = []
                result.append(article)
                kept_by_source[article["source"]] = kept_by_source.get(article["source"], 0) + 1

    for diag in diagnostics:
        diag["kept_count"] = kept_by_source.get(diag["name"], 0)

    result.sort(key=lambda a: a["nlp_score"], reverse=True)
    return result, diagnostics


# --------------------------------------------------------------------------
# Interface Streamlit (design vert/orange Saham Bank conservé)
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
.concept-tag {display:inline-block;background:#1A332E;color:#A0B2AF;font-size:11px;
              padding:2px 8px;border-radius:10px;margin:6px 4px 0 0;border:1px solid #2D544E;}
.metric-box {background:#122420;padding:15px;border-radius:8px;
             border-left:3px solid #A0B2AF;margin-bottom:12px;}
.metric-box.hot {border-left-color:#D24B2C;}
.metric-title {font-size:13px;color:#A0B2AF;text-transform:uppercase;margin-bottom:5px;}
.metric-val {font-size:15px;font-weight:bold;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# Sidebar — uniquement Veille Concurrentielle + Configuration
PAGES = ["Veille Concurrentielle", "Configuration"]
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

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
st.sidebar.markdown("<p style='color:#A0B2AF;font-size:12px;'>Précision du filtre sémantique</p>", unsafe_allow_html=True)
threshold = st.sidebar.slider(
    "Seuil", min_value=0.25, max_value=0.65, value=DEFAULT_THRESHOLD, step=0.02,
    help="Plus élevé = plus sélectif. Baisse si trop peu de résultats.",
    label_visibility="collapsed",
)
st.sidebar.caption(f"Seuil actuel : {threshold:.2f}")

articles, diagnostics = get_articles(0.7)

with st.expander("🔧 Diagnostic du scraping"):
    st.caption(f"Version : `{APP_VERSION}`")
    for diag in diagnostics:
        status = f"⚠️ {diag['error'][:60]}" if diag["error"] else "✅ OK"
        st.markdown(f"**{diag['name']}** — Bruts : {diag['raw_count']} · Retenus : {diag['kept_count']} · {status}")
    if st.button("Forcer une nouvelle actualisation"):
        st.cache_data.clear()
        st.rerun()

# Page Configuration
if st.session_state.page == "Configuration":
    st.markdown("### Sites surveillés (radar)")
    st.caption("Pour ajouter/retirer un site, édite la liste `SOURCES` en haut de `app.py`.")
    for s in SOURCES:
        st.markdown(f"- **{s['name']}** — {s['url']}")
    st.divider()
    st.markdown("### Exemples de référence (NLP)")
    st.caption("Ces phrases définissent ce qui est pertinent. Édite `GOLDEN_EXAMPLES` pour affiner le filtre.")
    for ex in GOLDEN_EXAMPLES:
        st.markdown(f"- {ex}")
    st.stop()

# En-tête
st.markdown("## Veille Concurrentielle — Secteur Bancaire Marocain")
st.markdown(
    "<p style='color:#A0B2AF;font-size:14px;'>Suivi des innovations, lancements et "
    "mouvements stratégiques — analyse sémantique sur 20 sources marocaines.</p>",
    unsafe_allow_html=True,
)

col_news, col_metrics = st.columns([2, 1])

with col_news:
    st.markdown(
        f"<div class='card'><h2>Actualités pertinentes ({len(articles)})</h2>",
        unsafe_allow_html=True,
    )
    if not articles:
        st.markdown(
            "<p style='color:#A0B2AF;padding:15px 0;'>Aucun article pertinent. "
            "Baisse le seuil sémantique dans la barre latérale ou actualise les données.</p>",
            unsafe_allow_html=True,
        )
    for article in articles:
        date_str = article["published_at"].strftime("%d/%m")
        concepts_html = "".join(f"<span class='concept-tag'>{c}</span>" for c in article.get("concepts", []))
        st.markdown(
            f"""<div class='news-item'>
                <div class='news-meta'>
                    <span class='badge'>{article['bank']}</span>
                    <span><span class='score-badge'>score {article['nlp_score']:.2f}</span>&nbsp;{article['source']} · {date_str}</span>
                </div>
                <div class='news-title'>
                    <a href='{article['url']}' target='_blank' style='color:#FFFFFF;text-decoration:none;'>
                       {article['title']}
                    </a>
                </div>
                <div>{concepts_html}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

with col_metrics:
    st.markdown("<div class='card'><h2>Infos Pratiques & Tendances</h2>", unsafe_allow_html=True)
    regl = [a for a in articles if a.get("category") == "reglementaire_bam"]
    prod = [a for a in articles if a.get("category") == "offre_produit"]
    metrics = []
    if regl:
        metrics.append(("Régulation", "Bank Al-Maghrib", f"{len(regl)} actualité(s) réglementaire(s).", False))
    if prod:
        metrics.append(("Tendance", "Offres & lancements", f"{len(prod)} mouvement(s) concurrentiel(s) détecté(s).", True))
    # Banques les plus actives
    bank_counts = {}
    for a in articles:
        bank_counts[a["bank"]] = bank_counts.get(a["bank"], 0) + 1
    if bank_counts:
        top_bank = max(bank_counts, key=bank_counts.get)
        metrics.append(("Acteur le plus actif", top_bank, f"{bank_counts[top_bank]} actualité(s) ce mois.", True))
    if not metrics:
        metrics.append(("Synthèse", "Aucun résultat", "Baisse le seuil ou actualise les données.", False))
    for mtype, mtitle, mval, hot in metrics:
        st.markdown(
            f"""<div class='metric-box {'hot' if hot else ''}'>
                <div class='metric-title'>{mtype} — {mtitle}</div>
                <div class='metric-val'>{mval}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)
