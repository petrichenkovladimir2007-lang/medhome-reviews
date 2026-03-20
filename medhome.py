#!/usr/bin/env python3
"""
Парсер відгуків з medhome.in.ua → XML-фід для Google Merchant Center
"""

import requests
import time
import re
import json
import hashlib
import logging
import argparse
import sys
import os
from datetime import datetime
from bs4 import BeautifulSoup

# ============================================================
# КОНФІГУРАЦІЯ
# ============================================================

CONFIG = {
    "base_url": "https://medhome.in.ua",
    "testimonials_url": "https://medhome.in.ua/ua/testimonials",
    
    # Ссылка на ваш фид с актуальным hash_tag
    "product_feed_url": "https://medhome.in.ua/google_merchant_center.xml?hash_tag=125679d1865706f40e16d85a9a16162c&product_ids=&label_ids=&export_lang=ru&group_ids=",
    
    "publisher_name": "medhome.in.ua",
    "favicon_url": "https://medhome.in.ua/favicon.ico",
    "aggregator_name": "prom.ua",
    "output_file": "medhome_reviews_feed.xml",
    "request_delay": 1.0,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("medhome_parser")

# ============================================================
# ЛОГИКА ПАРСИНГА
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update({"User-Agent": CONFIG["user_agent"]})
    return session

def parse_product_feed(session):
    log.info("Завантаження товарного фіда...")
    try:
        resp = session.get(CONFIG["product_feed_url"], timeout=60)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "xml")
        products = {}
        for item in soup.find_all("item"):
            p_id = item.find("g:id").text.strip() if item.find("g:id") else ""
            link = item.find("g:link").text.strip() if item.find("g:link") else ""
            # Извлекаем цифровой ID из ссылки (напр. из /p1885085669-...)
            url_match = re.search(r'/p(\d+)-', link)
            prom_id = url_match.group(1) if url_match else p_id
            
            products[prom_id] = {
                "id": p_id,
                "prom_id": prom_id,
                "title": item.find("g:title").text.strip() if item.find("g:title") else "",
                "link": link,
                "brand": item.find("g:brand").text.strip() if item.find("g:brand") else "",
                "mpn": item.find("g:mpn").text.strip() if item.find("g:mpn") else p_id
            }
        log.info(f"Знайдено {len(products)} товарів у фіді")
        return products
    except Exception as e:
        log.error(f"Помилка фіда: {e}")
        return {}

def parse_review_item(item):
    # Имя автора
    author_el = item.select_one('[data-qaid="author_name"]')
    if not author_el: return None
    
    author = author_el.get_text(strip=True)
    
    # Дата
    date_el = item.select_one('[data-qaid="review_date"]')
    date_iso = date_el.get("datetime", "") if date_el else ""
    
    # Текст (может отсутствовать, если только оценка)
    text_el = item.select_one('[data-qaid="review_text"]')
    text = text_el.get_text(strip=True) if text_el else ""
    
    # Рейтинг из текста "Відмінно" или title "Рейтинг 5 з 5"
    rating = 5
    rating_el = item.select_one(".b-rating__state")
    if rating_el:
        rt_text = rating_el.get_text(strip=True)
        if "Відмінно" in rt_text or "Отлично" in rt_text: rating = 5
        elif "Добре" in rt_text or "Хорошо" in rt_text: rating = 4
        # ... можно расширить
    
    # Товары (из атрибута data-reviews-products)
    products_data = []
    prod_wrapper = item.select_one('[data-reviews-products]')
    if prod_wrapper:
        try:
            raw_json = prod_wrapper.get("data-reviews-products")
            json_data = json.loads(raw_json)
            for p in json_data:
                products_data.append({"id": str(p.get("id"))})
        except: pass

    # Теги (Гарне обслуговування и т.д.)
    tags = [t.get("data-tag-title") for t in item.select("[data-tag-title]") if t.get("data-tag-title")]
    
    return {
        "author": author,
        "date_iso": date_iso,
        "text": text,
        "rating": rating,
        "products": products_data,
        "tags": tags
    }

def collect_reviews(session, max_pages=None):
    all_reviews = []
    current_page = 1
    
    while True:
        url = CONFIG["testimonials_url"] if current_page == 1 else f"{CONFIG['testimonials_url']}/page_{current_page}"
        log.info(f"Парсинг сторінки {current_page}...")
        
        resp = session.get(url, timeout=30)
        if resp.status_code != 200: break
        
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("li.b-comments__item")
        if not items: break
        
        for it in items:
            rev = parse_review_item(it)
            if rev: all_reviews.append(rev)
            
        # Определение макс страниц
        if current_page == 1 and not max_pages:
            paginator = soup.select_one('[data-bazooka="Paginator"]')
            if paginator:
                max_pages = int(paginator.get("data-pagination-pages-count", 1))
                log.info(f"Всього виявлено сторінок: {max_pages}")

        if max_pages and current_page >= max_pages: break
        current_page += 1
        time.sleep(CONFIG["request_delay"])
        
    return all_reviews

# ============================================================
# ГЕНЕРАЦИЯ XML (упрощенно для краткости)
# ============================================================

def generate_xml(matched_data):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.google.com/shopping/reviews/schema/product/2.3/product_reviews.xsd">',
        f'<version>2.3</version><aggregator><n>{CONFIG["aggregator_name"]}</n></aggregator>',
        f'<publisher><n>{CONFIG["publisher_name"]}</n><favicon>{CONFIG["favicon_url"]}</favicon></publisher><reviews>'
    ]
    
    for rev, prod in matched_data:
        r_id = hashlib.md5(f"{rev['author']}{rev['date_iso']}{prod['id']}".encode()).hexdigest()[:10]
        content = rev['text'] if rev['text'] else "Відмінний товар"
        if rev['tags']: content += ". " + ". ".join(rev['tags'])
        
        # Форматируем дату для Google (2026-03-20T07:29:48+02:00)
        ts = rev['date_iso'] + "+02:00" if rev['date_iso'] else datetime.now().isoformat()

        xml_rev = f"""<review>
            <review_id>{r_id}</review_id>
            <reviewer><n>{rev['author']}</n></reviewer>
            <review_timestamp>{ts}</review_timestamp>
            <content>{content}</content>
            <review_url type="group">{CONFIG['testimonials_url']}</review_url>
            <ratings><overall min="1" max="5">{rev['rating']}</overall></ratings>
            <products><product><product_ids>
                <mpns><mpn>{prod['mpn']}</mpn></mpns>
                <brands><brand>{prod['brand']}</brand></brands>
            </product_ids>
            <product_name>{prod['title']}</product_name>
            <product_url>{prod['link']}</product_url>
            </product></products>
        </review>"""
        lines.append(xml_rev)
        
    lines.append("</reviews></feed>")
    return "\n".join(lines)

def main():
    session = create_session()
    prods = parse_product_feed(session)
    if not prods: return
    
    # Для теста берем 3 страницы, уберите limit для полного парсинга
    reviews = collect_reviews(session) 
    
    matched = []
    for r in reviews:
        for rp in r['products']:
            if rp['id'] in prods:
                matched.append((r, prods[rp['id']]))
                
    xml_output = generate_xml(matched)
    with open(CONFIG["output_file"], "w", encoding="utf-8") as f:
        f.write(xml_output)
    print(f"Готово! Збережено {len(matched)} відгуків у {CONFIG['output_file']}")

if __name__ == "__main__":
    main()
