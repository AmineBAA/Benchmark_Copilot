"""
app.py — Veille concurrentielle bancaire (Saham Bank)  —  v9

CORRECTIFS MAJEURS PAR RAPPORT A LA v8
--------------------------------------
1. Endpoint HuggingFace corrigé : api-inference.huggingface.co (DÉPRÉCIÉ, renvoie
   une erreur) → router.huggingface.co/hf-inference/.../pipeline/feature-extraction.
2. Embeddings calculés EN LOT (un appel pour ~50 articles) au lieu d'un appel par
   article : beaucoup plus rapide, ne fait pas exploser le quota gratuit.
3. Dates RÉELLES : flux RSS (auto-découverts quand c'est possible) + extraction de
   date dans le HTML. Fenêtre de récence réellement appliquée.
4. "Garde-fou entité" : un article n'est retenu que s'il mentionne un acteur du
   secteur bancaire marocain (banque ou régulateur). Tue 90 % du bruit.
5. Pertinence = max(score NLP, score mots-clés thématiques) → le curseur de seuil
   fonctionne AUSSI sans token HF (le mode secours devient bien plus précis).
6. Le scraping/scoring (coûteux) est mis en cache indépendamment du seuil et de la
   fenêtre, qui sont appliqués après coup (gratuit).

Pour activer le NLP : ajoute HF_TOKEN dans les secrets Streamlit
(Settings → Secrets). Token gratuit sur huggingface.co/settings/tokens.
"""

import os
import re
import glob
import math
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
import feedparser
import streamlit as st
from bs4 import BeautifulSoup

# ==========================================================================
# Configuration
# ==========================================================================

CACHE_TTL_SECONDS = 4 * 3600
APP_VERSION = "2026-06-23-v9"

# Seuil de pertinence (0..1). Article retenu si son score >= seuil.
DEFAULT_THRESHOLD = 0.42
# Fenêtre de récence par défaut (jours). Les articles sans date connue sont gardés.
DEFAULT_WINDOW_DAYS = 30

# Modèle d'embeddings multilingue (CPU, gratuit via hf-inference).
HF_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Endpoint CORRECT (le router HF). L'ancien api-inference.huggingface.co est mort.
HF_API_URL = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}/pipeline/feature-extraction"
HF_BATCH = 48  # nb de textes par requête

# Exemples de référence : ce à quoi doit "ressembler" un article pertinent.
GOLDEN_EXAMPLES = [
    "CIH Bank lance son service bancaire CIH M3AK sur WhatsApp",
    "Attijariwafa bank lance une offre d'hébergement intégrée à son application mobile",
    "Attijariwafa bank lance Simple, la première néobanque marocaine",
    "BCP signe un partenariat stratégique avec une fintech marocaine",
    "Bank Of Africa signe un accord de financement vert avec la SFI",
    "CIH Bank s'allie avec une startup pour le paiement mobile",
    "Banque Populaire lance un nouveau pack bancaire pour les TPE",
    "Crédit du Maroc lance une offre de crédit immobilier à taux préférentiel",
    "CFG Bank déploie une nouvelle carte Visa pour les entreprises",
    "Attijariwafa bank lance une campagne pour les Marocains du monde",
    "CIH Bank lance son application mobile de paiement instantané",
    "BCP déploie l'open banking pour ses clients entreprises au Maroc",
    "Bank Al-Maghrib autorise la première néobanque au Maroc",
    "Bank Al-Maghrib publie une circulaire sur la supervision des banques",
    "Bank Al-Maghrib maintient son taux directeur lors du conseil de politique monétaire",
    "Office des Changes lance une plateforme digitale pour les opérations de change",
]

# type: "rss" | "html". Pour le HTML, on tente d'abord de découvrir un flux RSS.
SOURCES = [
    {"name": "Bank Al-Maghrib (BAM)",
     "url": "https://www.bkam.ma/Communiques",
     "type": "html", "bank": "Bank Al-Maghrib",
     "selector": "h3 a, h4 a, .field-content a, td a", "try_feed": False},
    {"name": "Médias24 – Banques",
     "url": "https://medias24.com/economie/banques/",
     "type": "html", "bank": None,
     "selector": "article a, h2 a, h3 a", "try_feed": True},
    {"name": "BourseNews – Actualité",
     "url": "https://boursenews.ma/articles/actualite",
     "type": "html", "bank": None,
     "selector": "h3 a, h4 a, h5 a", "try_feed": True},
    {"name": "L'Economiste",
     "url": "https://www.leconomiste.com/rss-leconomiste",
     "type": "rss", "bank": None},
    {"name": "Finance News Hebdo",
     "url": "https://fnh.ma/",
     "type": "html", "bank": None,
     "selector": "h2 a, h3 a, h4 a", "try_feed": True},
    {"name": "Attijariwafa bank",
     "url": "https://www.attijariwafabank.com/fr/espace-media/communiques-de-presse",
     "type": "html", "bank": "Attijariwafa",
     "selector": "h2 a, h3 a, h4 a, .title a, article a", "try_feed": True},
    {"name": "CIH Bank",
     "url": "https://www.cihbank.ma/actualites",
     "type": "html", "bank": "CIH Bank",
     "selector": "h2 a, h3 a, h4 a, article a", "try_feed": True},
    {"name": "Groupe BCP",
     "url": "https://www.groupebcp.com/fr/espace-communication/communiqu%C3%A9s-de-presse",
     "type": "html", "bank": "BCP",
     "selector": "h2 a, h3 a, h4 a, article a", "try_feed": True},
    {"name": "Bank Of Africa",
     "url": "https://www.bankofafrica.ma/fr/presse/communiques",
     "type": "html", "bank": "Bank Of Africa",
     "selector": "h2 a, h3 a, h4 a, .news-title a, article a", "try_feed": True},
]

# --- Acteurs du secteur (GARDE-FOU ENTITÉ : au moins un doit apparaître) ---
SECTOR_ENTITIES = [
    "attijariwafa", "attijari", "wafasalaf", "wafacash", "wafa assurance",
    "banque populaire", "banque centrale populaire", "groupe bcp", "bcp", "chaabi",
    "cih bank", "cih ", "bank of africa", "bmce",
    "cfg bank", "crédit du maroc", "credit du maroc", "bmci",
    "al barid bank", "barid bank", "société générale maroc", "sgma",
    "umnia bank", "bank assafa", "dar al amane", "al akhdar bank",
    "bank al-maghrib", "bank al maghrib", "bkam",
    "ammc", "office des changes", "gpbm", "cdg",
    "secteur bancaire", "banques marocaines", "banque marocaine",
]

# Régulateurs (pour catégoriser en "réglementaire").
REGULATOR_ENTITIES = [
    "bank al-maghrib", "bank al maghrib", "bkam", "ammc",
    "office des changes", "wali de bank",
]

# --- Mots-clés thématiques (par catégorie) : pilotent le score "métier" ---
TOPIC_KEYWORDS = {
    "lancement_offre": [
        "lance", "lancement", "lancent", "dévoile", "devoile", "déploie", "deploie",
        "nouvelle offre", "nouveau service", "nouveau produit", "nouvelle carte",
        "nouveau pack", "pack ", "carte visa", "carte mastercard", "crédit immobilier",
        "crédit conso", "compte bancaire", "offre de financement", "met en place",
    ],
    "partenariat": [
        "partenariat", "partenaire", "s'allie", "s allie", "alliance", "signe un accord",
        "accord avec", "convention", "coopération", "collaboration", "joint-venture",
        "co-construit", "rapprochement",
    ],
    "digital_innovation": [
        "digital", "numérique", "fintech", "néobanque", "neobanque", "open banking",
        "paiement mobile", "paiement instantané", "wallet", "e-wallet", "application mobile",
        "appli ", "super app", "intelligence artificielle", " ia ", "innovation",
        "wafr", "m-banking", "mobile banking", "qr code", "instant payment",
    ],
    "reglementaire": [
        "circulaire", "taux directeur", "politique monétaire", "agrément", "agrement",
        "supervision", "régulation", "regulation", "réglementation", "conseil de bank",
        "directive", "loi bancaire", "conformité", "lutte anti-blanchiment",
    ],
    "strategie": [
        "acquisition", "fusion", "augmentation de capital", "prise de participation",
        "filiale", "expansion", "implantation", "ouvre une agence", "rachat",
        "levée de fonds", "introduction en bourse", "ipo ", "stratégie",
    ],
}

# Bruit écarté AVANT tout scoring.
KEYWORDS_EXCLUSION = [
    "offre d'emploi", "offres d'emploi", "offre d emploi", "offres d emploi",
    "recrut", "carrière", "carrières", "stage", "stagiaire", "candidature",
    "nous recrutons", "rejoignez", "poste à pourvoir",
    "feuille de marché", "portefeuille trading", "analyse technique",
    "clôture de la bourse", "ouverture de la bourse", "séance de cotation",
    "masi ", "madex ", "assemblée générale", "dividende",
    "taux obligataire", "marché obligataire", "adjudication des bons",
    "banque d'angleterre", "banque centrale européenne", "réserve fédérale",
    "fed ", "bce ", "boj ", "boe ", "banque de france", "banque du japon",
    "wall street", "dow jones", "s&p 500", "nasdaq", "cac 40",
    "pétrole", "métaux précieux", "matières premières",
    "israël", "ukraine", "russie", "iran", "gaza", "cessez-le-feu",
    "conflit armé", "football", "coupe du monde", "équipe nationale",
]

# Étiquette "banque" affichée.
BANK_KEYWORDS = {
    "Attijariwafa": ["attijariwafa", "attijari", "wafasalaf", "wafacash", "wafa assurance"],
    "BCP": ["bcp", "banque centrale populaire", "banque populaire", "chaabi", "groupe bcp"],
    "CIH Bank": ["cih bank", "cih "],
    "Bank Of Africa": ["bank of africa", "bmce"],
    "CFG Bank": ["cfg bank"],
    "Crédit du Maroc": ["crédit du maroc", "credit du maroc"],
    "Société Générale Maroc": ["société générale maroc", "sgma", "sg maroc"],
    "BMCI": ["bmci"],
    "Al Barid Bank": ["al barid bank", "barid bank"],
    "Bank Al-Maghrib": ["bank al-maghrib", "bank al maghrib", "bkam"],
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# Anchors / URL à ignorer (navigation, catégories, réseaux sociaux…).
NON_ARTICLE_TEXT = {
    "lire la suite", "lire plus", "voir plus", "en savoir plus", "tout voir",
    "accueil", "contact", "newsletter", "s'abonner", "abonnez-vous", "connexion",
    "se connecter", "rechercher", "menu", "suivant", "précédent", "plus d'articles",
}
NON_ARTICLE_URL_PARTS = (
    "/category/", "/categorie/", "/tag/", "/auteur/", "/author/", "/rubrique/",
    "/page/", "/login", "/connexion", "/abonnement", "/newsletter", "/contact",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "whatsapp.com", "mailto:", "tel:", "javascript:", "/#",
)

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

# ==========================================================================
# Utilitaires texte / date
# ==========================================================================

def _normalize(text: str) -> str:
    text = (text or "").lower()
    for ch in ["\u2019", "\u2018", "\u02bc", "`"]:
        text = text.replace(ch, "'")
    return text


def _article_text(a: dict) -> str:
    return _normalize(f"{a.get('title', '')} {a.get('summary', '')}")


def _clean_title(title: str) -> str:
    """Nettoyage DOUX : retire seulement une signature/date en fin de titre."""
    title = re.sub(r"\s*[-–|·•]\s*par\s+.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$", "", title)
    return re.sub(r"\s+", " ", title).strip(" \u2013\u2014|·•-")


def _parse_date_text(raw: str):
    """Tente d'extraire une date d'un texte FR ('12 juin 2026', '12/06/2026')."""
    if not raw:
        return None
    raw = raw.strip().lower()
    m = re.search(r"(\d{1,2})\s+([a-zéûôî]+)\s+(\d{4})", raw)
    if m and m.group(2) in MONTHS_FR:
        try:
            return datetime(int(m.group(3)), MONTHS_FR[m.group(2)], int(m.group(1)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _date_from_container(link) -> datetime | None:
    """Cherche une date (<time> ou texte) dans les ancêtres proches d'un lien."""
    node = link
    for _ in range(4):
        if node is None:
            break
        t = node.find("time") if hasattr(node, "find") else None
        if t is not None:
            dt = t.get("datetime") or t.get_text(strip=True)
            parsed = _parse_iso(dt) or _parse_date_text(dt)
            if parsed:
                return parsed
        txt = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
        parsed = _parse_date_text(txt)
        if parsed:
            return parsed
        node = getattr(node, "parent", None)
    return None


def _parse_iso(s: str):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:25]) if len(s) >= 10 else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ==========================================================================
# Scoring : entité (garde-fou) + thématique (mots-clés) + NLP
# ==========================================================================

def has_sector_entity(text: str) -> bool:
    return any(kw in text for kw in SECTOR_ENTITIES)


def is_regulatory(text: str) -> bool:
    return any(kw in text for kw in REGULATOR_ENTITIES)


def passes_exclusion(text: str) -> bool:
    return not any(kw in text for kw in KEYWORDS_EXCLUSION)


def topic_hits(text: str) -> int:
    """Nombre de catégories thématiques distinctes détectées (0..5)."""
    hits = 0
    for _cat, kws in TOPIC_KEYWORDS.items():
        if any(kw in text for kw in kws):
            hits += 1
    return hits


def keyword_relevance(text: str) -> float:
    """Score métier 0..1 dérivé des mots-clés thématiques."""
    h = topic_hits(text)
    if h <= 0:
        return 0.0
    return min(0.45 + 0.15 * (h - 1), 0.90)


def detect_bank(article: dict) -> str:
    if article.get("bank"):
        return article["bank"]
    text = _article_text(article)
    for bank, keywords in BANK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return bank
    return "Secteur bancaire"


# ---- NLP (HuggingFace router, en lot) ----

def _get_hf_token():
    try:
        return st.secrets.get("HF_TOKEN") or os.environ.get("HF_TOKEN")
    except Exception:
        return os.environ.get("HF_TOKEN")


def _to_vector(emb):
    """Normalise la sortie HF : un vecteur par texte (mean-pool si tokens)."""
    if emb and isinstance(emb[0], list):       # [tokens][dim] -> moyenne
        dim = len(emb[0])
        return [sum(tok[i] for tok in emb) / len(emb) for i in range(dim)]
    return emb                                  # déjà [dim]


def embed_batch(texts: list[str], token: str) -> list[list[float]] | None:
    """Embeddings EN LOT via le router HF. None si l'API échoue."""
    out: list[list[float]] = []
    for i in range(0, len(texts), HF_BATCH):
        chunk = texts[i:i + HF_BATCH]
        try:
            resp = requests.post(
                HF_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"inputs": chunk, "options": {"wait_for_model": True}},
                timeout=45,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list):
            return None
        # data peut être [n][dim] ou [n][tokens][dim]
        for item in data:
            out.append(_to_vector(item))
    return out


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ==========================================================================
# Scraping
# ==========================================================================

def _build_article(title, url, summary, source, bank, pub_dt, date_known):
    return {
        "title": title, "url": url, "summary": summary,
        "source": source, "bank": bank,
        "published_at": pub_dt, "date_known": date_known,
    }


def scrape_rss_url(feed_url: str, source: dict) -> tuple[list, str | None]:
    articles, error = [], None
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            return [], str(getattr(feed, "bozo_exception", "flux RSS illisible"))
        for entry in feed.entries:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                pub_dt, known = datetime(*pub[:6], tzinfo=timezone.utc), True
            else:
                pub_dt, known = datetime.now(timezone.utc), False
            summary = BeautifulSoup(entry.get("summary", "") or "", "html.parser").get_text(" ", strip=True)
            articles.append(_build_article(
                _clean_title(entry.get("title", "").strip()),
                entry.get("link", "").strip(), summary[:400],
                source["name"], source.get("bank"), pub_dt, known))
    except Exception as exc:
        error = str(exc)
    return articles, error


def _discover_feed(html: str, base_url: str) -> str | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("link", attrs={"type": re.compile("rss|atom", re.I)})
        if link and link.get("href"):
            href = link["href"]
            return href if href.startswith("http") else urljoin(base_url, href)
    except Exception:
        pass
    return None


def _looks_like_article(href: str, text: str) -> bool:
    low_t = text.lower().strip()
    if len(text) < 25 or len(text.split()) < 4:
        return False
    if low_t in NON_ARTICLE_TEXT:
        return False
    low_u = href.lower()
    if any(p in low_u for p in NON_ARTICLE_URL_PARTS):
        return False
    # une vraie URL d'article a en général un slug (segment long) ou un id
    path = urlparse(href).path.strip("/")
    if not path:
        return False
    last = path.split("/")[-1]
    return len(last) >= 12 or any(c.isdigit() for c in last)


def scrape_html(source: dict) -> tuple[list, str | None]:
    articles, error = [], None
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # 1) tenter un flux RSS (dates + résumés fiables)
        if source.get("try_feed"):
            feed_url = _discover_feed(html, source["url"])
            if feed_url:
                feed_articles, ferr = scrape_rss_url(feed_url, source)
                if feed_articles:
                    return feed_articles, None

        # 2) fallback : extraction de liens, avec validation stricte
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        for link in soup.select(source.get("selector", "a")):
            href = link.get("href")
            text = link.get_text(" ", strip=True)
            if not href or not text:
                continue
            full_url = href if href.startswith("http") else urljoin(source["url"], href)
            if full_url in seen or not _looks_like_article(full_url, text):
                continue
            seen.add(full_url)
            pub_dt = _date_from_container(link)
            articles.append(_build_article(
                _clean_title(text), full_url, "", source["name"],
                source.get("bank"),
                pub_dt or datetime.now(timezone.utc), pub_dt is not None))
    except Exception as exc:
        error = str(exc)
    return articles, error


def scrape_all() -> tuple[list, list]:
    raw, diagnostics, seen_titles = [], [], set()
    for source in SOURCES:
        if source["type"] == "rss":
            items, error = scrape_rss_url(source["url"], source)
        else:
            items, error = scrape_html(source)
        deduped = []
        for item in items:
            if not item["title"]:
                continue
            key = item["title"].lower().strip()[:60]
            if key not in seen_titles:
                seen_titles.add(key)
                deduped.append(item)
        raw.extend(deduped)
        diagnostics.append({"name": source["name"], "raw_count": len(deduped), "error": error})
        time.sleep(0.3)
    return raw, diagnostics


# ==========================================================================
# Pipeline : scrape → score (mis en cache, indépendant du seuil/fenêtre)
# ==========================================================================

@st.cache_data(ttl=24 * 3600, show_spinner=False)
def _golden_embeddings(token: str):
    return embed_batch(GOLDEN_EXAMPLES, token)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Récupération et analyse des actualités…")
def load_scored_articles() -> tuple[list, list, str]:
    """Scrape tout, calcule entité/thème/NLP. Le seuil n'est PAS appliqué ici."""
    token = _get_hf_token()
    use_nlp = bool(token)
    golden = _golden_embeddings(token) if use_nlp else None
    if use_nlp and not golden:
        use_nlp = False  # API HF indisponible → mode secours

    raw, diagnostics = scrape_all()

    # Pré-filtre : bruit + garde-fou entité (rapide, gratuit)
    candidates = []
    for a in raw:
        text = _article_text(a)
        if not passes_exclusion(text):
            continue
        if not has_sector_entity(text):
            continue
        candidates.append((a, text))

    # NLP en lot
    nlp_scores = [None] * len(candidates)
    if use_nlp and candidates:
        texts = [f"{a['title']} {a.get('summary', '')}".strip() for a, _ in candidates]
        embs = embed_batch(texts, token)
        if embs and len(embs) == len(candidates):
            for i, emb in enumerate(embs):
                nlp_scores[i] = max(_cosine(emb, g) for g in golden)
        else:
            use_nlp = False

    scored, kept_by_source = [], {}
    for i, (a, text) in enumerate(candidates):
        kw = keyword_relevance(text)
        nlp = nlp_scores[i]
        relevance = max(kw, nlp) if nlp is not None else kw
        a["nlp_score"] = round(nlp, 3) if nlp is not None else None
        a["kw_score"] = round(kw, 3)
        a["relevance"] = round(relevance, 3)
        a["category"] = "reglementaire_bam" if is_regulatory(text) else "offre_produit"
        a["bank"] = detect_bank(a)
        scored.append(a)
        kept_by_source[a["source"]] = kept_by_source.get(a["source"], 0) + 1

    for d in diagnostics:
        d["kept_count"] = kept_by_source.get(d["name"], 0)

    mode = "NLP (HuggingFace) + mots-clés" if use_nlp else "Mots-clés + garde-fou entité"
    return scored, diagnostics, mode


def filter_articles(scored: list, threshold: float, window_days: int) -> list:
    """Applique seuil + fenêtre de récence (gratuit, hors cache)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    out = []
    for a in scored:
        if a["relevance"] < threshold:
            continue
        if a["date_known"] and a["published_at"] < cutoff:
            continue
        out.append(a)
    return out


# ==========================================================================
# Interface Streamlit
# ==========================================================================

st.set_page_config(page_title="Saham Bank – Veille Concurrentielle", page_icon="🏦", layout="wide")

CUSTOM_CSS = """
<style>
#MainMenu, footer, header {visibility: hidden;}
.block-container {padding-top: 1.5rem; max-width: 100%;}
body, .stApp {background-color: #122420; color: #FFFFFF;}
section[data-testid="stSidebar"] {background-color: #1A332E; border-right: 1px solid #2D544E;}
section[data-testid="stSidebar"] button {
    width:100%;text-align:left;background:transparent;color:#A0B2AF;
    border:none;border-radius:6px;padding:12px 15px;font-weight:500;}
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

logo_files = glob.glob(os.path.join(os.path.dirname(__file__), "logo*"))
if logo_files:
    st.sidebar.image(logo_files[0], use_container_width=True)
else:
    st.sidebar.markdown(
        "<div style='text-align:center;padding:15px;font-weight:bold;font-size:18px;"
        "border-bottom:1px solid #2D544E;margin-bottom:10px;'>SAHAM BANK</div>",
        unsafe_allow_html=True)

for p in PAGES:
    if st.sidebar.button(p, key=f"nav_{p}"):
        st.session_state.page = p

st.sidebar.divider()
st.sidebar.markdown("<p style='color:#A0B2AF;font-size:12px;'>Précision (seuil de pertinence)</p>",
                    unsafe_allow_html=True)
threshold = st.sidebar.slider("Seuil", 0.30, 0.80, DEFAULT_THRESHOLD, 0.02,
                              help="Plus élevé = plus sélectif.", label_visibility="collapsed")
st.sidebar.caption(f"Seuil : {threshold:.2f}")
window_days = st.sidebar.slider("Fenêtre (jours)", 7, 120, DEFAULT_WINDOW_DAYS, 7,
                                help="Récence maximale des articles datés.")

# Chargement (caché) puis filtrage (gratuit)
scored, diagnostics, filter_mode = load_scored_articles()
articles = filter_articles(scored, threshold, window_days)

with st.expander("🔧 Diagnostic du scraping"):
    st.caption(f"Version : `{APP_VERSION}` · Mode : **{filter_mode}** · "
               f"Candidats analysés : {len(scored)} · Affichés : {len(articles)}")
    if "Mots-clés +" in filter_mode and "NLP" not in filter_mode:
        st.warning(
            "NLP désactivé (pas de `HF_TOKEN`, ou API HuggingFace indisponible). "
            "Le filtrage mots-clés + garde-fou entité reste actif. Pour activer le NLP, "
            "ajoute `HF_TOKEN` dans Settings → Secrets (token gratuit sur "
            "huggingface.co/settings/tokens).")
    for d in diagnostics:
        status = f"⚠️ {d['error']}" if d["error"] else "✅ OK"
        st.markdown(f"**{d['name']}** — Bruts : {d['raw_count']} · "
                    f"Retenus : {d.get('kept_count', 0)} · {status}")
    if st.button("Forcer une nouvelle actualisation"):
        st.cache_data.clear()
        st.rerun()

if st.session_state.page == "Configuration":
    st.markdown("### Sources surveillées")
    for s in SOURCES:
        st.markdown(f"- **{s['name']}** ({s['type'].upper()}) — {s['url']}")
    st.divider()
    st.markdown("### Exemples de référence (NLP)")
    for ex in GOLDEN_EXAMPLES:
        st.markdown(f"- {ex}")
    st.stop()

if st.session_state.page == "Rapports Bank Al-Maghrib":
    feed_articles = [a for a in articles if a.get("category") == "reglementaire_bam"]
    feed_title = "Communications Bank Al-Maghrib"
else:
    feed_articles = articles
    feed_title = f"Synthèse — {window_days} derniers jours ({len(feed_articles)} article(s))"

st.markdown("## Veille Concurrentielle — Secteur Bancaire Marocain")
st.markdown("<p style='color:#A0B2AF;font-size:14px;'>Suivi des innovations, "
            "lancements et mouvements stratégiques.</p>", unsafe_allow_html=True)


def build_metrics(arts):
    regl = [a for a in arts if a.get("category") == "reglementaire_bam"]
    prod = [a for a in arts if a.get("category") == "offre_produit"]
    metrics = []
    if regl:
        metrics.append({"type": "Régulation", "title": "Bank Al-Maghrib",
                        "val": f"{len(regl)} communication(s) réglementaire(s).", "hot": False})
    if prod:
        metrics.append({"type": "Tendance", "title": "Offres & lancements",
                        "val": f"{len(prod)} actualité(s) concurrentielle(s).", "hot": True})
    if not metrics:
        metrics.append({"type": "Synthèse", "title": "Aucun résultat",
                        "val": "Baisse le seuil ou élargis la fenêtre.", "hot": False})
    return metrics


def _sort_key(a):
    # datés récents d'abord ; non datés ensuite, par pertinence
    return (a["date_known"], a["published_at"], a["relevance"])


col_news, col_metrics = st.columns([2, 1])

with col_news:
    st.markdown(f"<div class='card'><h2>{feed_title}</h2>", unsafe_allow_html=True)
    if not feed_articles:
        st.markdown("<p style='color:#A0B2AF;padding:15px 0;'>Aucun article pertinent. "
                    "Baisse le seuil, élargis la fenêtre, ou force une actualisation.</p>",
                    unsafe_allow_html=True)
    for article in sorted(feed_articles, key=_sort_key, reverse=True):
        date_str = article["published_at"].strftime("%d/%m %H:%M") if article["date_known"] else "date n/a"
        desc = article.get("summary") or "Voir l'article complet via le lien."
        if article.get("nlp_score") is not None:
            score_html = f"<span class='score-badge'>NLP {article['nlp_score']:.2f}</span>"
        else:
            score_html = f"<span class='score-badge'>pertinence {article['relevance']:.2f}</span>"
        st.markdown(
            f"""<div class='news-item'>
                <div class='news-meta'>
                    <span class='badge'>{article['bank']}</span>
                    <span>{score_html}&nbsp;{date_str}</span>
                </div>
                <div class='news-title'>
                    <a href='{article['url']}' target='_blank'
                       style='color:#FFFFFF;text-decoration:none;'>{article['title']}</a>
                </div>
                <div class='news-desc'>{desc[:220]}</div>
            </div>""", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

with col_metrics:
    st.markdown("<div class='card'><h2>Infos Pratiques & Tendances</h2>", unsafe_allow_html=True)
    for m in build_metrics(feed_articles):
        st.markdown(
            f"""<div class='metric-box {'hot' if m['hot'] else ''}'>
                <div class='metric-title'>{m['type']} — {m['title']}</div>
                <div class='metric-val'>{m['val']}</div>
            </div>""", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
