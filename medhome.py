#!/usr/bin/env python3
import requests
import time
import re
import json
import hashlib
import logging
import random
import sys
import os
import xml.sax.saxutils as saxutils
from datetime import datetime
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# КОНФИГУРАЦИЯ
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("medhome_parser")

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_text(text):
    """Очистка текста от HTML и спецсимволов для XML"""
    if not text: return ""
    text = re.sub(r'<[^>]*>', '', str(text)) # Удаляем HTML-теги
    text = re.sub(r'\s+', ' ', text).strip() # Чистим пробелы
    return saxutils.escape(text)

def format_date(date_str):
    """Приведение даты к стандарту RFC3339 (ISO 8601) для Google"""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+02:00")
    # Если дата пришла как 2024-02-12, делаем 2024-02-12T00:00:00+02:00
    if "T" not in date_str:
        date_str = f"{date_str}T00:00:00"
    if "+" not in date_str and "Z" not in date_str:
        date_str += "+02:00"
    return date_str

def create_session():
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": CONFIG["user_agent"]})
    return session

# ============================================================
# ПАРСИНГ
# ============================================================

def parse_product_feed(session):
    log.info("Загрузка товарного фида...")
    try:
        resp = session.get(CONFIG["product_feed_url"], timeout=45)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "xml")
        products = {}
        for item in soup.find_all("item"):
            # Безопасное извлечение (п.1 аудита - проверка существования тега)
            p_id_tag = item.find("g:id")
            link_tag = item.find("g:link")
            if not p_id_tag or not link_tag: continue

            p_id = p_id_tag.text.strip()
            link = link_tag.text.strip()
            url_match = re.search(r'/p(\d+)-', link)
            prom_id = url_match.group(1) if url_match else p_id
            
            products[prom_id] = {
                "id": p_id,
                "title": clean_text(item.find("g:title").text) if item.find("g:title") else "Товар",
                "link": link,
                "brand": clean_text(item.find("g:brand").text) if item.find("g:brand") else "Medhome",
                "mpn": item.find("g:mpn").text.strip() if item.find("g:mpn") else p_id
            }
        log.info(f"Найдено {len(products)} товаров")
        return products
    except Exception as e:
        log.error(f"Ошибка фида: {e}")
        return {}

def collect_reviews(session):
    all_reviews = []
    current_page = 1
    max_pages = None
    seen_review_hashes = set() # Для удаления дублей (п.9 аудита)
    
    while True:
        url = CONFIG["testimonials_url"] if current_page == 1 else f"{CONFIG['testimonials_url']}/page_{current_page}"
        log.info(f"Парсинг страницы {current_page}...")
        
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code != 200: break
            
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.b-comments__item")
            if not items: break
            
            for it in items:
                author_el = it.select_one('[data-qaid="author_name"]')
                if not author_el: continue
                
                author = clean_text(author_el.get_text())
                date_iso = it.select_one('[data-qaid="review_date"]').get("datetime", "")
                text = it.select_one('[data-qaid="review_text"]').get_text() if it.select_one('[data-qaid="review_text"]') else ""
                
                # Уникальность: автор + дата + текст
                r_hash = hashlib.md5(f"{author}{date_iso}{text[:50]}".encode()).hexdigest()
                if r_hash in seen_review_hashes: continue
                seen_review_hashes.add(r_hash)

                # Рейтинг
                rating = 5
                rt_el = it.select_one(".b-rating__state")
                if rt_el:
                    rt_t = rt_el.get_text().lower()
                    if "добре" in rt_t or "хорошо" in rt_t: rating = 4
                    elif "норм" in rt_t: rating = 3

                # Связанные товары
                prods_data = []
                prod_wrapper = it.select_one('[data-reviews-products]')
                if prod_wrapper:
                    try:
                        js = json.loads(prod_wrapper.get("data-reviews-products"))
                        prods_data = [{"id": str(p.get("id"))} for p in js]
                    except: pass

                tags = [t.get("data-tag-title") for t in it.select("[data-tag-title]") if t.get("data-tag-title")]
                
                all_reviews.append({
                    "author": author, "date_iso": date_iso, "text": text,
                    "rating": rating, "products": prods_data, "tags": tags
                })
            
            if current_page == 1:
                paginator = soup.select_one('[data-bazooka="Paginator"]')
                if paginator:
                    max_pages = int(paginator.get("data-pagination-pages-count", 1))

            if max_pages and current_page >= max_pages: break
            current_page += 1
            time.sleep(random.uniform(1.2, 2.2))
            
        except Exception as e:
            log.error(f"Ошибка на стр {current_page}: {e}")
            break
            
    return all_reviews

def generate_xml(matched_data):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.google.com/shopping/reviews/schema/product/2.3/product_reviews.xsd">',
        f'  <version>2.3</version><aggregator><n>{CONFIG["aggregator_name"]}</n></aggregator>',
        f'  <publisher><n>{CONFIG["publisher_name"]}</n><favicon>{CONFIG["favicon_url"]}</favicon></publisher>',
        '  <reviews>'
    ]
    
    for rev, prod in matched_data:
        # Усиленный ID (п.4 аудита)
        r_id = hashlib.md5(f"{rev['author']}{rev['date_iso']}{rev['text'][:20]}{prod['id']}".encode()).hexdigest()[:12]
        
        # Контент с fallback (п.3 аудита)
        raw_content = rev['text'] if rev['text'] else "Відмінний товар"
        if rev['tags']: raw_content += ". " + ". ".join(rev['tags'])
        content = clean_text(raw_content)
        if not content: content = "Відмінний товар"

        timestamp = format_date(rev['date_iso'])

        xml_rev = f"""    <review>
      <review_id>{r_id}</review_id>
      <reviewer><n>{rev['author']}</n></reviewer>
      <review_timestamp>{timestamp}</review_timestamp>
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
        
    lines.append("  </reviews>\n</feed>")
    return "\n".join(lines)

def main():
    session = create_session()
    prods = parse_product_feed(session)
    if not prods: return
    
    reviews = collect_reviews(session)
    matched = []
    for r in reviews:
        for rp in r['products']:
            if rp['id'] in prods:
                matched.append((r, prods[rp['id']]))
                
    if not matched:
        log.warning("Нет совпадений товаров и отзывов.")
        return

    os.makedirs('public', exist_ok=True)
    with open(CONFIG["output_file"], "w", encoding="utf-8") as f:
        f.write(generate_xml(matched))
    
    log.info(f"Успех! Файл обновлен. Смерджено отзывов: {len(matched)}")

if __name__ == "__main__":
    main()
