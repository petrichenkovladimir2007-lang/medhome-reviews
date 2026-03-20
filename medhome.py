#!/usr/bin/env python3
"""
Парсер відгуків з medhome.in.ua → ВАЛІДНИЙ XML-фід для Google Merchant Center
"""

import requests
import time
import re
import json
import hashlib
import logging
import random
import sys
import xml.sax.saxutils as saxutils
from datetime import datetime
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# КОНФІГУРАЦІЯ
# ============================================================

CONFIG = {
    "base_url": "https://medhome.in.ua",
    "testimonials_url": "https://medhome.in.ua/ua/testimonials",
    "product_feed_url": "https://medhome.in.ua/google_merchant_center.xml?hash_tag=125679d1865706f40e16d85a9a16162c&product_ids=&label_ids=&export_lang=ru&group_ids=",
    "publisher_name": "medhome.in.ua",
    "favicon_url": "https://medhome.in.ua/favicon.ico",
    "aggregator_name": "prom.ua",
    "output_file": "public/medhome_reviews_feed.xml",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("medhome_parser")

# ============================================================
# ДОПОМІЖНІ ФУНКЦІЇ
# ============================================================

def clean_text(text):
    """Очищення тексту від HTML-тегів та спецсимволів для XML"""
    if not text:
        return ""
    # 1. Видаляємо всі HTML-теги (напр. <a href="...">...</a>)
    text = re.sub(r'<[^>]*>', '', str(text))
    # 2. Замінюємо декілька пробілів/переносів на один
    text = re.sub(r'\s+', ' ', text).strip()
    # 3. Екрануємо символи &, <, >, ", ' для безпеки XML
    return saxutils.escape(text)

def create_session():
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": CONFIG["user_agent"]})
    return session

# ============================================================
# ЛОГІКА ПАРСИНГУ
# ============================================================

def parse_product_feed(session):
    log.info("Завантаження товарного фіда...")
    try:
        resp = session.get(CONFIG["product_feed_url"], timeout=45)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "xml")
        products = {}
        for item in soup.find_all("item"):
            p_id = item.find("g:id").text.strip() if item.find("g:id") else ""
            link = item.find("g:link").text.strip() if item.find("g:link") else ""
            url_match = re.search(r'/p(\d+)-', link)
            prom_id = url_match.group(1) if url_match else p_id
            
            products[prom_id] = {
                "id": p_id,
                "title": clean_text(item.find("g:title").text) if item.find("g:title") else "",
                "link": link,
                "brand": clean_text(item.find("g:brand").text) if item.find("g:brand") else "Medhome",
                "mpn": item.find("g:mpn").text.strip() if item.find("g:mpn") else p_id
            }
        log.info(f"Знайдено {len(products)} товарів у фіді")
        return products
    except Exception as e:
        log.error(f"Помилка фіда: {e}")
        return {}

def parse_review_item(item):
    author_el = item.select_one('[data-qaid="author_name"]')
    if not author_el: return None
    
    author = clean_text(author_el.get_text())
    date_el = item.select_one('[data-qaid="review_date"]')
    date_iso = date_el.get("datetime", "") if date_el else ""
    
    text_el = item.select_one('[data-qaid="review_text"]')
    text = text_el.get_text() if text_el else ""
    
    rating = 5
    rating_el = item.select_one(".b-rating__state")
    if rating_el:
        rt_text = rating_el.get_text().lower()
        if any(w in rt_text for w in ["відмінно", "отлично"]): rating = 5
        elif any(w in rt_text for w in ["добре", "хорошо"]): rating = 4
        elif "нормально" in rt_text: rating = 3
    
    products_data = []
    prod_wrapper = item.select_one('[data-reviews-products]')
    if prod_wrapper:
        try:
            json_data = json.loads(prod_wrapper.get("data-reviews-products"))
            for p in json_data:
                products_data.append({"id": str(p.get("id"))})
        except: pass

    tags = [t.get("data-tag-title") for t in item.select("[data-tag-title]") if t.get("data-tag-title")]
    
    return {
        "author": author,
        "date_iso": date_iso,
        "text": text,
        "rating": rating,
        "products": products_data,
        "tags": tags
    }

def collect_reviews(session):
    all_reviews = []
    current_page = 1
    max_pages = None
    
    while True:
        url = CONFIG["testimonials_url"] if current_page == 1 else f"{CONFIG['testimonials_url']}/page_{current_page}"
        log.info(f"Обробка сторінки {current_page}...")
        
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code != 200: break
            
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.b-comments__item")
            if not items: break
            
            for it in items:
                rev = parse_review_item(it)
                if rev: all_reviews.append(rev)
            
            if current_page == 1:
                paginator = soup.select_one('[data-bazooka="Paginator"]')
                if paginator:
                    max_pages = int(paginator.get("data-pagination-pages-count", 1))
                    log.info(f"Всього сторінок для парсингу: {max_pages}")

            if max_pages and current_page >= max_pages: break
            current_page += 1
            time.sleep(random.uniform(1.5, 2.5))
            
        except Exception as e:
            log.error(f"Помилка на сторінці {current_page}: {e}")
            break
            
    return all_reviews

def generate_xml(matched_data):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.google.com/shopping/reviews/schema/product/2.3/product_reviews.xsd">',
        f'  <version>2.3</version>',
        f'  <aggregator><n>{CONFIG["aggregator_name"]}</n></aggregator>',
        f'  <publisher><n>{CONFIG["publisher_name"]}</n><favicon>{CONFIG["favicon_url"]}</favicon></publisher>',
        '  <reviews>'
    ]
    
    for rev, prod in matched_data:
        r_id = hashlib.md5(f"{rev['author']}{rev['date_iso']}{prod['id']}".encode()).hexdigest()[:12]
        
        # Формуємо контент: Текст + Теги
        raw_text = rev['text'] if rev['text'] else "Відмінний товар"
        if rev['tags']:
            raw_text += ". " + ". ".join(rev['tags'])
        
        content = clean_text(raw_text)
        ts = rev['date_iso'] + "+02:00" if rev['date_iso'] and "+" not in rev['date_iso'] else rev['date_iso']

        xml_rev = f"""    <review>
      <review_id>{r_id}</review_id>
      <reviewer><n>{rev['author']}</n></reviewer>
      <review_timestamp>{ts}</review_timestamp>
      <content>{content}</content>
      <review_url type="group">{CONFIG['testimonials_url']}</review_url>
      <ratings><overall min="1" max="5">{rev['rating']}</overall></ratings>
      <products>
        <product>
          <product_ids>
            <mpns><mpn>{prod['mpn']}</mpn></mpns>
            <brands><brand>{prod['brand']}</brand></brands>
          </product_ids>
          <product_name>{prod['title']}</product_name>
          <product_url>{prod['link']}</product_url>
        </product>
      </products>
    </review>"""
        lines.append(xml_rev)
        
    lines.append("  </reviews>\n</feed>")
    return "\n".join(lines)

def main():
    log.info("=== Запуск парсера ===")
    session = create_session()
    
    prods = parse_product_feed(session)
    if not prods:
        log.error("Товари не завантажені. Вихід.")
        return
    
    reviews = collect_reviews(session)
    log.info(f"Зібрано {len(reviews)} відгуків. Починаємо матчинг...")
    
    matched = []
    for r in reviews:
        for rp in r['products']:
            if rp['id'] in prods:
                matched.append((r, prods[rp['id']]))
                
    if not matched:
        log.warning("Збігів не знайдено.")
        return

    # Створюємо папку public, якщо її немає
    import os
    os.makedirs('public', exist_ok=True)

    xml_output = generate_xml(matched)
    with open(CONFIG["output_file"], "w", encoding="utf-8") as f:
        f.write(xml_output)
    
    log.info(f"Готово! Файл {CONFIG['output_file']} оновлено. Збережено {len(matched)} відгуків.")

if __name__ == "__main__":
    main()
