import os
import csv
import json
import yaml
import asyncio
import logging
import requests
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置日志 =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= 常量定义 =================
TARGETS_FILE = 'targets.yaml'
HISTORY_FILE = 'history_urls.json'
TODAY_STR = datetime.now().strftime('%Y-%m-%d')
CSV_FILE = f'raw_data_{TODAY_STR}.csv'

# ================= 辅助函数 =================
def load_yaml_config(filepath):
    """读取并解析 YAML 配置文件"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"❌ 读取 YAML 配置文件失败: {e}")
        return []

def load_history(filepath):
    """读取历史抓取记录"""
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ 历史记录文件损坏，将创建新记录: {e}")
    return {}

def save_history(filepath, data):
    """保存历史抓取记录"""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ 保存历史记录失败: {e}")

def save_to_csv(data_rows):
    """将数据追加写入 CSV 文件"""
    file_exists = os.path.exists(CSV_FILE)
    try:
        with open(CSV_FILE, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)
            if not file_exists:
                writer.writerow(['Date', 'Competitor_Name', 'Update_Type', 'Content'])
            writer.writerows(data_rows)
    except Exception as e:
        logger.error(f"❌ 写入 CSV 失败: {e}")

# ================= 核心抓取逻辑 =================

def fetch_new_sitemap_urls(name, sitemap_url, history_data):
    """动作 1: 抓取 Sitemap 并对比历史记录找出新增 URL"""
    logger.info(f"[{name}] 开始检查 Sitemap: {sitemap_url}")
    new_urls = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(sitemap_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        current_urls = re.findall(r'<loc>(.*?)</loc>', response.text)
        past_urls = set(history_data.get(name, []))
        current_urls_set = set(current_urls)
        added_urls = list(current_urls_set - past_urls)
        
        if added_urls:
            logger.info(f"[{name}] 发现 {len(added_urls)} 个新增 URL。")
            new_urls = added_urls
            history_data[name] = list(current_urls_set)
        else:
            logger.info(f"[{name}] Sitemap 无新增 URL。")
            
    except requests.exceptions.RequestException as e:
        logger.error(f"[{name}] Sitemap 抓取异常: {e}")
    except Exception as e:
        logger.error(f"[{name}] Sitemap 解析发生未知错误: {e}")
        
    return new_urls

async def fetch_meta_ad_copy(page, name, keyword):
    """动作 2: 使用 Playwright 抓取 Meta Ad Library"""
    logger.info(f"[{name}] 开始在 Meta Ad Library 搜索关键词: {keyword}")
    ad_copies = []
    try:
        search_url = f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL&q={keyword}"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(random.uniform(3.0, 6.0)) 
        await page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(random.uniform(2.0, 4.0))

        ad_text_selector = 'div[style*="white-space: pre-wrap;"]'
        
        try:
            await page.wait_for_selector(ad_text_selector, timeout=15000)
        except PlaywrightTimeoutError:
            logger.warning(f"[{name}] 未能在预期时间内找到广告文案容器。")
            return []

        elements = await page.query_selector_all(ad_text_selector)
        
        for el in elements:
            text = await el.inner_text()
            if text and len(text.strip()) > 10:
                ad_copies.append(text.strip())
            if len(ad_copies) >= 3:
                break
                
        logger.info(f"[{name}] 成功抓取到 {len(ad_copies)} 条广告文案。")

    except Exception as e:
        logger.error(f"[{name}] Playwright 抓取异常: {e}")
        
    return ad_copies

# ================= 主控制流 =================

async def main():
    logger.info("🚀 自动化爬虫脚本启动...")
    
    # 1. 加载配置与历史状态
    raw_targets = load_yaml_config(TARGETS_FILE)
    if not raw_targets:
        logger.error("❌ 找不到目标配置，脚本退出。")
        return
        
    # === 终极修复方案: 暴力兼容所有 YAML 格式 ===
    targets = []
    if isinstance(raw_targets, dict):
        if 'competitors' in raw_targets:
            targets = raw_targets['competitors']
        elif 'name' in raw_targets:
            targets = [raw_targets]
        else:
            targets = list(raw_targets.values())[0] if raw_targets else []
    elif isinstance(raw_targets, list):
        targets = raw_targets

    if not isinstance(targets, list) or len(targets) == 0:
         logger.error(f"❌ targets.yaml 格式无法识别，读取到的数据为: {raw_targets}")
         return
    # ============================================

    history_data = load_history(HISTORY_FILE)
    all_new_data_rows = []

    # 2. 启动 Playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        )

        # 3. 遍历目标执行抓取
        for target in targets:
            # 增加安全判断，防止个别配置依然是字符串
            if not isinstance(target, dict):
                logger.warning(f"⚠️ 跳过格式错误的竞品条目: {target}")
                continue
                
            name = target.get('name', 'Unknown')
            sitemap_url = target.get('sitemap_url')
            meta_ad_keyword = target.get('meta_ad_keyword')
            
            logger.info(f"==== 开始处理竞品: {name} ====")
            
            if sitemap_url:
                new_urls = fetch_new_sitemap_urls(name, sitemap_url, history_data)
                for url in new_urls:
                    all_new_data_rows.append([TODAY_STR, name, 'Sitemap', url])
            
            if meta_ad_keyword:
                page = await context.new_page()
                ad_texts = await fetch_meta_ad_copy(page, name, meta_ad_keyword)
                for text in ad_texts:
                    all_new_data_rows.append([TODAY_STR, name, 'MetaAd', text])
                await page.close()

            await asyncio.sleep(random.uniform(2.0, 5.0))
            
        await browser.close()

    # 4. 数据持久化
    if all_new_data_rows:
        save_to_csv(all_new_data_rows)
        logger.info(f"✅ 成功将 {len(all_new_data_rows)} 条新数据写入 {CSV_FILE}。")
    else:
        logger.info("ℹ️ 今日无新数据产出。")
        
    save_history(HISTORY_FILE, history_data)
    logger.info("🎉 脚本执行完毕。")

if __name__ == "__main__":
    asyncio.run(main())
