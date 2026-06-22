"""
app.py — Veille concurrentielle bancaire (Saham Bank)

Fichier UNIQUE : scraping, filtrage et interface sont tout dans ce fichier.
Sans appel à une API IA — uniquement un filtre par règles (mots-clés métier).

PRINCIPE CLÉ : on ne scrape pas les homepages génériques (qui contiennent les
menus, footers, rubriques navigation...) mais les SECTIONS D'ARTICLES ciblées
de chaque site. Le sélecteur CSS cible les balises de titre (h3, h4, h5, etc.)
plutôt que tous les liens <a>, ce qui élimine 90 % du bruit.
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
# Configuration
# --------------------------------------------------------------------------

CACHE_TTL_SECONDS = 4 * 3600
APP_VERSION = "2026-06-19-v4"

SOURCES = [
    # --- Régulateur ---
    {"name": "Bank Al-Maghrib (BAM)",
     "url": "https://www.bkam.ma/Communiques",
     "type": "html", "bank": "Bank Al-Maghrib",
     "selector": "h3 a, h4 a, .field-content a, td a"},

    # --- Presse financière marocaine ---
    # BoursNews : section Actualités (pas la homepage) + section Décryptages
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

    # L'Economiste : RSS officiel
    {"name": "L'Economiste",
     "url": "https://www.leconomiste.com/rss-leconomiste",
     "type": "rss", "bank": None},

    # Finance News Hebdo (partenaire BoursNews, actualités bancaires)
    {"name": "Finance News Hebdo",
     "url": "https://fnh.ma/",
     "type": "html", "bank": None,
     "selector": "h2 a, h3 a, h4 a"},

    # Médias24 : section Banques directement
    {"name": "Médias24 – Banques",
     "url": "https://medias24.com/economie/banques/",
     "type": "html", "bank": None,
     "selector": "h2 a, h3 a, h4 a, article a"},

    # --- Sites banques (certains peuvent renvoyer 403 selon l'hébergeur) ---
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

# --------------------------------------------------------------------------
# Mots-clés MÉTIER — volontairement précis (pas trop larges)
# --------------------------------------------------------------------------

# Signaux forts d'une actualité concurrentielle bancaire :
# lancement de produit, partenariat, accord, nouvelle offre, innovation...
KEYWORDS_PRODUIT = [
    # Produits et offres
    "nouvelle offre", "lancement", "lance ", "lancé", "déploie", "déploiement",
    "partenariat", "accord", "convention", "signature", "contrat",
    "carte bancaire", "carte visa", "carte mastercard", "carte prépayée",
    "crédit immobilier", "crédit conso", "crédit auto", "micro-crédit",
    "prêt immobilier", "prêt personnel",
    "compte épargne", "compte courant", "pack bancaire", "pack jeune",
    "mobile banking", "banque mobile", "application mobile", "app bancaire",
    "paiement mobile", "paiement digital", "paiement instantané",
    "fintech", "néobanque", "open banking", "wallet",
    "inclusion financière", "bancarisation",
    "leasing", "factoring", "affacturage",
    "assurance bancaire", "bancassurance",
    "taux préférentiel", "taux d'intérêt", "taux directeur",
    "financement vert", "finance verte", "esg", "green bond",
    "digital banking", "transformation digitale",
    "tpe", "pme", "très petites entreprises", "petites et moyennes",
    "wafacash", "wafasalaf",
    "augmentation de capital", "émission d'actions", "introduction en bourse",
]

KEYWORDS_REGLEMENTAIRE = [
    "bank al-maghrib", "bank al maghrib",
    "circulaire bam", "instruction bam", "directive bam",
    "wali de bank al-maghrib", "wali bank",
    "politique monétaire", "taux directeur bam",
    "réglementation bancaire", "loi bancaire",
    "supervision bancaire", "contrôle bancaire",
    "réserve obligatoire",
    "lutte contre le blanchiment", "lcb-ft",
    "fonds propres réglementaires", "bâle iii", "stress test",
    "ammc", "office des changes",
]

# Mots-clés de BRUIT à exclure en priorité — articles sans valeur pour la veille
KEYWORDS_EXCLUSION = [
    # Offres d'emploi et RH
    "offre d'emploi", "offres d'emploi", "offre d emploi", "offres d emploi",
    "recrut", "recrutement", "carrière", "carrières", "stage", "stagiaire",
    "candidature", "nous recrutons", "rejoignez", "poste à pourvoir",
    # Bourse / marché — pas de la veille produit bancaire
    "feuille de marché", "portefeuille trading", "analyse technique",
    "cours de bourse", "clôture de la bourse", "ouverture de la bourse",
    "masi ", "madex ", "ago ", "assemblée générale ordinaire",
    "dividende", "résultats annuels", "bénéfice net", "chiffre d'affaires",
    # Actualité internationale générale
    "israël", "ukraine", "russie", "etats-unis", "usa", "fed ", "bce ",
    "cessez-le-feu", "conflit", "guerre",
    # Sport / divers
    "football", "coupe du monde", "can ", "équipe nationale",
]

BANK_KEYWORDS = {
    "Attijariwafa": ["attijariwafa", "attijari wafa", "wafabank", "wafasalaf", "wafacash"],
    "BCP": ["bcp", "banque centrale populaire", "banque populaire", "crédit populaire", "chaabi"],
    "CIH Bank": ["cih bank", "cih "],
    "Bank Of Africa": ["bank of africa", "bmce", "boa "],
    "CFG Bank": ["cfg bank"],
    "Crédit du Maroc": ["crédit du maroc", "credit du maroc", " cdm "],
    "Société Générale Maroc": ["société générale maroc", "sgma", "sg maroc"],
    "BMCI": ["bmci"],
    "Al Barid Bank": ["al barid bank", "barid bank"],
    "Bank Al-Maghrib": ["bank al-maghrib", "bank al maghrib", "bam"],
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

# --------------------------------------------------------------------------
# Scraping (RSS + HTML générique)
# --------------------------------------------------------------------------

def scrape_rss(source):
    articles = []
    error = None
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            error = str(getattr(feed, "bozo_exception", "flux RSS illisible"))
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
    except Exception as exc:
        error = str(exc)
    return articles, error


def scrape_html(source):
    articles = []
    error = None
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        seen = set()
        for link in soup.select(source.get("selector", "a")):
            href = link.get("href")
            text = link.get_text(strip=True)
            if not href or not text or len(text) < 15:
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
    except Exception as exc:
        error = str(exc)
    return articles, error


def scrape_all_sources():
    raw = []
    diagnostics = []
    for source in SOURCES:
        if source["type"] == "rss":
            items, error = scrape_rss(source)
        else:
            items, error = scrape_html(source)
        raw.extend(items)
        diagnostics.append({"name": source["name"], "raw_count": len(items), "error": error})
        time.sleep(0.5)  # reste poli envers les sites scrapés
    return raw, diagnostics

# --------------------------------------------------------------------------
# Filtre par règles (pas d'IA / pas d'API) : mots-clés + détection de banque
# --------------------------------------------------------------------------

def _normalize_text(article):
    text = f"{article['title']} {article.get('summary', '')}".lower()
    return text.replace("’", "'").replace("‘", "'")


def classify_category(article):
    text = _normalize_text(article)
    if any(kw in text for kw in KEYWORDS_EXCLUSION):
        return None
    if any(kw in text for kw in KEYWORDS_REGLEMENTAIRE):
        return "reglementaire_bam"
    if any(kw in text for kw in KEYWORDS_PRODUIT):
        return "offre_produit"
    return None


def detect_bank(article):
    if article.get("bank"):
        return article["bank"]
    text = _normalize_text(article)
    for bank, keywords in BANK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return bank
    return article["source"]


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Récupération des actualités du secteur...")
def get_articles():
    raw, diagnostics = scrape_all_sources()
    result = []
    kept_by_source = {}
    for article in raw:
        category = classify_category(article)
        if category is None:
            continue
        article["category"] = category
        article["bank"] = detect_bank(article)
        result.append(article)
        kept_by_source[article["source"]] = kept_by_source.get(article["source"], 0) + 1

    for diag in diagnostics:
        diag["kept_count"] = kept_by_source.get(diag["name"], 0)

    return result, diagnostics

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

PAGES = ["Veille Concurrentielle", "Rapports Bank Al-Maghrib", "Configuration"]
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

# --- En-tête ---
st.markdown("## Veille Concurrentielle — Secteur Bancaire Marocain")
st.markdown(
    "<p style='color:var(--text-muted);font-size:14px;'>Suivi des innovations, "
    "lancements et mouvements stratégiques — synthèse des 7 derniers jours.</p>",
    unsafe_allow_html=True,
)
period = "week"

articles, diagnostics = get_articles()
period_articles = filter_by_period(articles, period)

feed_title = "Synthèse de la Semaine (Derniers 7 jours)"

with st.expander("🔧 Diagnostic du scraping (à ouvrir si la liste est vide)"):
    st.caption(f"Version du code en cours d'exécution : `{APP_VERSION}`")
    st.caption(
        "Si 'Articles bruts trouvés' est à 0, le site bloque le scraping ou a changé de "
        "structure. Une erreur '403 Forbidden' signifie que le site a une protection "
        "anti-robot (souvent Cloudflare) qui bloque les requêtes automatisées -- ce n'est "
        "pas toujours réparable côté code, en particulier depuis l'IP partagée de "
        "Streamlit Cloud. Si 'Articles bruts' > 0 mais 'Retenus après filtre' = 0, c'est "
        "le filtre par mots-clés qui est trop strict pour ce site."
    )
    for diag in diagnostics:
        status = f"⚠️ {diag['error']}" if diag["error"] else "✅ OK"
        st.markdown(
            f"**{diag['name']}** — Articles bruts trouvés : {diag['raw_count']} · "
            f"Retenus après filtre : {diag['kept_count']} · {status}"
        )
    if st.button("Forcer une nouvelle actualisation maintenant"):
        st.cache_data.clear()
        st.rerun()

# --- Sélection des articles selon la page active ---
if st.session_state.page == "Rapports Bank Al-Maghrib":
    feed_articles = [a for a in period_articles if a["category"] == "reglementaire_bam"]
    feed_title = "Communications Bank Al-Maghrib"
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
