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
            writer = csv.writer(f, quoting=csv.QUOTE_ALL) # 使用全量引号，防止文案中的逗号破坏 CSV 结构
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
        
        # 使用正则提取所有 <loc> 标签内的 URL（比 XML 解析器具有更强的容错性）
        current_urls = re.findall(r'<loc>(.*?)</loc>', response.text)
        
        # 对比历史记录，取差集
        past_urls = set(history_data.get(name, []))
        current_urls_set = set(current_urls)
        
        added_urls = list(current_urls_set - past_urls)
        
        if added_urls:
            logger.info(f"[{name}] 发现 {len(added_urls)} 个新增 URL。")
            new_urls = added_urls
            # 更新内存中的历史记录
            history_data[name] = list(current_urls_set)
        else:
            logger.info(f"[{name}] Sitemap 无新增 URL。")
            
    except requests.exceptions.RequestException as e:
        logger.error(f"[{name}] Sitemap 抓取异常: {e}")
    except Exception as e:
        logger.error(f"[{name}] Sitemap 解析发生未知错误: {e}")
        
    return new_urls

async def fetch_meta_ad_copy(page, name, keyword):
    """动作 2: 使用 Playwright 抓取 Meta Ad Library 前 3 个广告文案"""
    logger.info(f"[{name}] 开始在 Meta Ad Library 搜索关键词: {keyword}")
    ad_copies = []
    try:
        # 构造 Meta 广告资料库的搜索 URL (全局搜索所有广告类型)
        search_url = f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL&q={keyword}"
        
        # 增加随机等待，模拟人类浏览节奏
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(random.uniform(3.0, 6.0)) 
        
        # 滚动页面以触发懒加载
        await page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # 核心：Meta 的 DOM 类名是动态混淆的。这里使用更泛化的选择器。
        # 广告文案通常在具有 white-space: pre-wrap 样式的 div 中，或者寻找包含多段文本的区块。
        # 以下 XPath 试图寻找特征接近文案的节点（高度容错策略）
        ad_text_selector = 'div[style*="white-space: pre-wrap;"]'
        
        try:
            # 最多等 15 秒加载广告内容
            await page.wait_for_selector(ad_text_selector, timeout=15000)
        except PlaywrightTimeoutError:
            logger.warning(f"[{name}] 未能在预期时间内找到广告文案容器 (可能是无广告或被反爬拦截)。")
            return []

        # 抓取页面上所有的匹配元素
        elements = await page.query_selector_all(ad_text_selector)
        
        # 提取前 3 个有效的文案文本
        for el in elements:
            text = await el.inner_text()
            if text and len(text.strip()) > 10: # 过滤掉极短的无意义文本
                ad_copies.append(text.strip())
            if len(ad_copies) >= 3:
                break
                
        logger.info(f"[{name}] 成功抓取到 {len(ad_copies)} 条广告文案。")

    except Exception as e:
        logger.error(f"[{name}] Playwright 抓取 Meta 广告异常: {e}")
        
    return ad_copies

# ================= 主控制流 =================

async def main():
    logger.info("🚀 自动化爬虫脚本启动...")
    
# 1. 加载配置与历史状态
    raw_targets = load_yaml_config(TARGETS_FILE)
    if not raw_targets:
        logger.error("❌ 找不到目标配置，脚本退出。")
        return
        
    # === 修复: 自动兼容不同的 YAML 格式 ===
    if isinstance(raw_targets, dict) and 'competitors' in raw_targets:
        targets = raw_targets['competitors'] # 提取 competitors 下的列表
    elif isinstance(raw_targets, list):
        targets = raw_targets # 已经是列表，直接用
    else:
        logger.error("❌ targets.yaml 格式无法识别，请检查！")
        return
    # =====================================

    history_data = load_history(HISTORY_FILE)
    all_new_data_rows = []

    # 2. 启动 Playwright
    async with async_playwright() as p:
        # 启动无头浏览器 (GitHub Actions 环境默认为无头)
        # 加入了一些常用参数尝试绕过基本的无头检测
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox"
            ]
        )
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        )

        # 3. 遍历目标执行抓取
        for target in targets:
            name = target.get('name', 'Unknown')
            sitemap_url = target.get('sitemap_url')
            meta_ad_keyword = target.get('meta_ad_keyword')
            
            logger.info(f"==== 开始处理竞品: {name} ====")
            
            # --- 执行动作 1：Sitemap ---
            if sitemap_url:
                new_urls = fetch_new_sitemap_urls(name, sitemap_url, history_data)
                for url in new_urls:
                    all_new_data_rows.append([TODAY_STR, name, 'Sitemap', url])
            
            # --- 执行动作 2：Meta Ads ---
            if meta_ad_keyword:
                page = await context.new_page()
                ad_texts = await fetch_meta_ad_copy(page, name, meta_ad_keyword)
                for text in ad_texts:
                    all_new_data_rows.append([TODAY_STR, name, 'MetaAd', text])
                await page.close() # 用完即关，释放内存

            # 每个竞品之间随机休眠，避免并发过高触发风控
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
