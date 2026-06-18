"""
app.py — Veille concurrentielle bancaire (Saham Bank)

Fichier UNIQUE : scraping, filtrage et interface sont tout dans ce fichier,
volontairement sans appel à une API IA externe — uniquement un filtre par
règles (mots-clés + détection de banque).

Fonctionnement :
- Au chargement de la page, on scrape les sources définies dans SOURCES
  (mis en cache CACHE_TTL_SECONDS pour ne pas re-scraper à chaque clic).
- Un filtre par mots-clés garde uniquement les articles "offre/produit
  bancaire" ou "réglementaire Bank Al-Maghrib", et détecte la banque
  concernée par mots-clés.
- L'interface reproduit la maquette fournie (thème vert foncé / orange,
  sidebar, curseur Jour/Semaine, cartes d'actu, panneau de tendances).

Pour ajouter une source : ajoute un bloc dans la liste SOURCES ci-dessous.
"""

import os
import glob
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import feedparser
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration -- à adapter librement
# --------------------------------------------------------------------------

CACHE_TTL_SECONDS = 4 * 3600  # rafraîchissement automatique toutes les 4h

SOURCES = [
    {"name": "Bank Al-Maghrib (BAM)", "url": "https://www.bkam.ma/",
     "type": "html", "bank": None, "selector": "a"},
    {"name": "L'Economiste", "url": "https://www.leconomiste.com/rss-leconomiste",
     "type": "rss", "bank": None},
    {"name": "BoursNews", "url": "https://boursenews.ma/",
     "type": "html", "bank": None, "selector": "a"},
    {"name": "Attijariwafa bank", "url": "https://www.attijariwafabank.com/fr/espace-media/communiques-de-presse",
     "type": "html", "bank": "Attijariwafa", "selector": "a"},
    {"name": "CIH Bank", "url": "https://www.cihbank.ma/actualites",
     "type": "html", "bank": "CIH Bank", "selector": "a"},
    {"name": "Groupe BCP", "url": "https://www.groupebcp.com/fr/espace-communication/communiqu%C3%A9s-de-presse",
     "type": "html", "bank": "BCP", "selector": "a"},
    {"name": "Bank Of Africa", "url": "https://www.bankofafrica.ma/en/actualites",
     "type": "html", "bank": "Bank Of Africa", "selector": "a"},
]
# NB : les URLs des newsrooms de chaque banque ont été trouvées par recherche
# web et sont à vérifier/ajuster -- la structure de ces sites change souvent.

KEYWORDS_PRODUIT = [
    "crédit", "prêt", "taux", "carte bancaire", "compte épargne",
    "compte courant", "assurance", "leasing", "financement",
    "banque en ligne", "mobile banking", "paiement mobile", "découvert",
    "épargne", "placement", "tpe", "pme", "hypothécaire", "immobilier",
    "carte visa", "carte mastercard", "virement", "fintech", "néobanque",
    "souscription", "offre",
]

KEYWORDS_REGLEMENTAIRE = [
    "bank al-maghrib", "bank al maghrib", "bam", "circulaire",
    "taux directeur", "instruction n°", "réglementation bancaire",
    "loi bancaire", "politique monétaire", "supervision bancaire",
    "réserve obligatoire", "blanchiment", "fonds propres",
    "bâle iii", "stress test", "directive bam",
]

BANK_KEYWORDS = {
    "BCP": ["bcp", "banque centrale populaire", "banque populaire", "chaabi"],
    "CIH Bank": ["cih bank", "cih "],
    "Attijariwafa": ["attijariwafa", "attijari wafa", "wafabank"],
    "Bank Of Africa": ["bank of africa", "bmce"],
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VeilleSahamBank/1.0)"}

# --------------------------------------------------------------------------
# Scraping (RSS + HTML générique)
# --------------------------------------------------------------------------

def scrape_rss(source):
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            published_dt = (
                datetime(*published[:6], tzinfo=timezone.utc) if published
                else datetime.now(timezone.utc)
            )
            articles.append({
                "title": entry.get("title", "").strip(),
                "url": entry.get("link", "").strip(),
                "summary": (entry.get("summary", "") or "").strip(),
                "source": source["name"],
                "bank": source.get("bank"),
                "published_at": published_dt,
            })
    except Exception:
        pass
    return articles


def scrape_html(source):
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        seen = set()
        for link in soup.select(source.get("selector", "a")):
            href = link.get("href")
            text = link.get_text(strip=True)
            if not href or not text or len(text) < 25:
                continue  # élimine la majorité des liens menu/footer
            full_url = href if href.startswith("http") else urljoin(source["url"], href)
            if full_url in seen:
                continue
            seen.add(full_url)
            articles.append({
                "title": text,
                "url": full_url,
                "summary": "",
                "source": source["name"],
                "bank": source.get("bank"),
                "published_at": datetime.now(timezone.utc),
            })
    except Exception:
        pass
    return articles


def scrape_all_sources():
    raw = []
    for source in SOURCES:
        if source["type"] == "rss":
            raw.extend(scrape_rss(source))
        else:
            raw.extend(scrape_html(source))
        time.sleep(0.5)  # reste poli envers les sites scrapés
    return raw

# --------------------------------------------------------------------------
# Filtre par règles (pas d'IA / pas d'API) : mots-clés + détection de banque
# --------------------------------------------------------------------------

def classify_category(article):
    text = f"{article['title']} {article.get('summary', '')}".lower()
    if any(kw in text for kw in KEYWORDS_REGLEMENTAIRE):
        return "reglementaire_bam"
    if any(kw in text for kw in KEYWORDS_PRODUIT):
        return "offre_produit"
    return None


def detect_bank(article):
    if article.get("bank"):
        return article["bank"]
    text = f"{article['title']} {article.get('summary', '')}".lower()
    for bank, keywords in BANK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return bank
    return article["source"]


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Récupération des actualités du secteur...")
def get_articles():
    raw = scrape_all_sources()
    result = []
    for article in raw:
        category = classify_category(article)
        if category is None:
            continue
        article["category"] = category
        article["bank"] = detect_bank(article)
        result.append(article)
    return result

# --------------------------------------------------------------------------
# Logique des périodes (Jour / Semaine) -- inspirée de la maquette fournie
# --------------------------------------------------------------------------

def filter_by_period(articles, period):
    now = datetime.now(timezone.utc)
    cutoff = now - (timedelta(hours=24) if period == "day" else timedelta(days=7))
    return [a for a in articles if a["published_at"] >= cutoff]


def build_metrics(articles, period):
    """Génère 1-2 cartes de tendances par règles simples (sans IA), à la
    manière du panneau 'Infos pratiques & tendances' de la maquette."""
    metrics = []
    reglementaire = [a for a in articles if a["category"] == "reglementaire_bam"]
    produits = [a for a in articles if a["category"] == "offre_produit"]

    if reglementaire:
        metrics.append({
            "type": "Régulation", "title": "Bank Al-Maghrib",
            "val": f"{len(reglementaire)} communication(s) réglementaire(s) "
                   f"{'aujourd’hui' if period == 'day' else 'cette semaine'}.",
            "hot": False,
        })
    if produits:
        # Thème le plus fréquent parmi les offres détectées
        counts = {}
        for a in produits:
            text = f"{a['title']} {a.get('summary', '')}".lower()
            for kw in KEYWORDS_PRODUIT:
                if kw in text:
                    counts[kw] = counts.get(kw, 0) + 1
        top_theme = max(counts, key=counts.get) if counts else "offres bancaires"
        metrics.append({
            "type": "Tendance", "title": top_theme.capitalize(),
            "val": f"{len(produits)} annonce(s) liée(s) à '{top_theme}' "
                   f"{'aujourd’hui' if period == 'day' else 'cette semaine'}.",
            "hot": True,
        })
    if not metrics:
        metrics.append({
            "type": "Synthèse", "title": "Aucune tendance notable",
            "val": "Pas d'offre ou de communication réglementaire détectée sur cette période.",
            "hot": False,
        })
    return metrics

# --------------------------------------------------------------------------
# Interface -- reproduit le thème/CSS de la maquette fournie
# --------------------------------------------------------------------------

st.set_page_config(page_title="Saham Bank - Veille Concurrentielle", page_icon="🏦", layout="wide")

CUSTOM_CSS = """
<style>
:root {
    --bg-primary: #1A332E;
    --accent-orange: #D24B2C;
    --text-light: #FFFFFF;
    --bg-card: #24443F;
    --text-muted: #A0B2AF;
    --border-color: #2D544E;
}
#MainMenu, footer, header {visibility: hidden;}
.block-container {padding-top: 1.5rem; max-width: 100%;}
body, .stApp {background-color: #122420; color: var(--text-light);}
section[data-testid="stSidebar"] {background-color: var(--bg-primary); border-right: 1px solid var(--border-color);}
section[data-testid="stSidebar"] button {
    width: 100%; text-align: left; background: transparent; color: var(--text-muted);
    border: none; border-radius: 6px; padding: 12px 15px; font-weight: 500;
}
section[data-testid="stSidebar"] button:hover {background-color: var(--accent-orange); color: var(--text-light);}
.card {background-color: var(--bg-card); border-radius: 10px; padding: 20px; border: 1px solid var(--border-color);}
.card h2 {font-size: 18px; margin-bottom: 20px; border-left: 4px solid var(--accent-orange); padding-left: 10px;}
.news-item {padding: 15px 0; border-bottom: 1px solid var(--border-color);}
.news-item:last-child {border-bottom: none;}
.news-meta {display: flex; justify-content: space-between; font-size: 12px; color: var(--text-muted);}
.badge {background-color: #122420; color: var(--accent-orange); padding: 2px 8px; border-radius: 4px;
        font-weight: bold; border: 1px solid var(--accent-orange);}
.news-title {font-size: 16px; font-weight: 600; color: var(--text-light); margin-top: 8px;}
.news-desc {font-size: 14px; color: var(--text-muted); line-height: 1.4; margin-top: 6px;}
.metric-box {background: #122420; padding: 15px; border-radius: 8px; border-left: 3px solid var(--text-muted); margin-bottom: 12px;}
.metric-box.hot {border-left-color: var(--accent-orange);}
.metric-title {font-size: 13px; color: var(--text-muted); text-transform: uppercase; margin-bottom: 5px;}
.metric-val {font-size: 15px; font-weight: bold;}
div[data-testid="stSelectSlider"] {max-width: 260px;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

PAGES = ["Veille Sectorielle", "Analyse Concurrents", "Rapports Bank Al-Maghrib", "Configuration"]
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

# --- Sidebar ---
logo_files = glob.glob(os.path.join(os.path.dirname(__file__), "logo*"))
if logo_files:
    st.sidebar.image(logo_files[0], use_container_width=True)
else:
    st.sidebar.markdown(
        "<div style='text-align:center;padding:15px;font-weight:bold;font-size:18px;"
        "border-bottom:1px solid var(--border-color);margin-bottom:10px;'>SAHAM BANK</div>",
        unsafe_allow_html=True,
    )

for page_name in PAGES:
    if st.sidebar.button(page_name, key=f"nav_{page_name}"):
        st.session_state.page = page_name

# --- En-tête + curseur Jour/Semaine ---
col_title, col_filter = st.columns([3, 1])
with col_title:
    st.markdown("## Veille Concurrentielle — Secteur Bancaire Marocain")
    st.markdown(
        "<p style='color:var(--text-muted);font-size:14px;'>Suivi des innovations, "
        "lancements et mouvements stratégiques.</p>", unsafe_allow_html=True,
    )
with col_filter:
    period_label = st.select_slider(
        "Période", options=["Par jour", "Par semaine"], value="Par jour", label_visibility="collapsed",
    )
period = "day" if period_label == "Par jour" else "week"

articles = get_articles()
period_articles = filter_by_period(articles, period)

feed_title = (
    "Principaux Titres (Dernières 24h)" if period == "day"
    else "Synthèse de la Semaine (Derniers 7 jours)"
)

# --- Sélection des articles selon la page active ---
if st.session_state.page == "Rapports Bank Al-Maghrib":
    feed_articles = [a for a in period_articles if a["category"] == "reglementaire_bam"]
    feed_title = "Communications Bank Al-Maghrib"
elif st.session_state.page == "Analyse Concurrents":
    feed_articles = [a for a in period_articles if a["bank"] != "Secteur"]
else:
    feed_articles = period_articles

# --------------------------------------------------------------------------
# Page Configuration : affichage des sources (lecture seule, à éditer dans le code)
# --------------------------------------------------------------------------
if st.session_state.page == "Configuration":
    st.markdown("### Sources surveillées")
    st.caption("Pour ajouter ou modifier une source, édite la liste `SOURCES` en haut de app.py.")
    for s in SOURCES:
        st.markdown(f"- **{s['name']}** ({s['type'].upper()}) — {s['url']}")
    st.stop()

# --------------------------------------------------------------------------
# Tableau de bord principal
# --------------------------------------------------------------------------
col_news, col_metrics = st.columns([2, 1])

with col_news:
    st.markdown(f"<div class='card'><h2>{feed_title}</h2>", unsafe_allow_html=True)
    if not feed_articles:
        st.markdown(
            "<p style='color:var(--text-muted);'>Aucun article pertinent pour cette période.</p>",
            unsafe_allow_html=True,
        )
    for article in sorted(feed_articles, key=lambda a: a["published_at"], reverse=True):
        date_str = article["published_at"].strftime("%d/%m %H:%M")
        desc = article.get("summary") or "Voir l'article complet via le lien ci-dessus."
        st.markdown(
            f"""<div class='news-item'>
                <div class='news-meta'><span class='badge'>{article['bank']}</span><span>{date_str}</span></div>
                <div class='news-title'><a href='{article['url']}' target='_blank'
                    style='color:var(--text-light);text-decoration:none;'>{article['title']}</a></div>
                <div class='news-desc'>{desc[:220]}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

with col_metrics:
    st.markdown("<div class='card'><h2>Infos Pratiques & Tendances</h2>", unsafe_allow_html=True)
    for metric in build_metrics(period_articles, period):
        hot_class = "hot" if metric["hot"] else ""
        st.markdown(
            f"""<div class='metric-box {hot_class}'>
                <div class='metric-title'>{metric['type']} — {metric['title']}</div>
                <div class='metric-val'>{metric['val']}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)
