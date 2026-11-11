# -*- coding: utf-8 -*-
"""
自动备料脚本 v3：RSS抓取 → 全文获取 → LLM清洗（强制简体中文） → materials.json
v3改进：①所有中文类材料强制简体输出（繁体自动转换、英文自动翻译）
       ②BBC中文链接 /trad→/simp 重写（根治繁体来源）
       ③科技类改用36氪（中文原生、RSS带全文、免翻译）
       ④工作相关增加ScienceDaily电信类（光通信方向）
       ⑤降级策略收紧：英文原料LLM失败则跳过待重试，不再塞英文原文进中文类
       ⑥保留每篇材料的title+src（网页来源），供前端展示和溯源
安装位置：仓库根目录 fetch_materials.py
环境变量：MATERIALS_API_KEY（必填，硅基流动key）、MATERIALS_API_BASE（默认 https://api.siliconflow.cn/v1）、MATERIALS_MODEL（默认 deepseek-ai/DeepSeek-V3）
"""
import os, re, json, time
import requests, feedparser, trafilatura

# ============ 信源配置（已逐一验证；key必须与HTML标签一致） ============
FEEDS = {
    '政策':   ['https://feeds.bbci.co.uk/zhongwen/simp/rss.xml'],
    '金融':   ['https://feeds.bbci.co.uk/news/business/rss.xml'],
    '健康':   ['https://feeds.bbci.co.uk/news/health/rss.xml'],
    '科技':   ['https://36kr.com/feed'],
    '工作相关': ['https://www.sciencedaily.com/rss/matter_energy/photonics.xml',
              'https://www.sciencedaily.com/rss/matter_energy/optics.xml',
              'https://www.sciencedaily.com/rss/matter_energy/telecommunications.xml'],
    '英语材料': ['https://aeon.co/feed.rss'],
}
PER_CATEGORY = 2      # 每天每类抓几篇
SCAN_PER_FEED = 12    # 每个feed最多考察前几条
MAX_LIBRARY  = 80      # 素材库上限（超出裁旧）
MIN_SOURCE   = 300     # 原文最短字符数
KEEP_LANG    = {'英语材料': 'en'}   # 这些标签保留英文，其余一律简体中文

PROMPT_ZH = (
    '你是严谨的阅读材料编辑。把下面的新闻/文章正文压缩整理成一段连贯的阅读材料。'
    '硬性要求：输出必须是简体中文——原文是繁体中文一律转为简体，原文是英文一律翻译成自然流畅的简体中文'
    '（专有名词首次出现时可在括号里附原文）。'
    '内容要求：忠实原文信息，绝不添加原文没有的内容；原文较长时压缩到600~1000字，'
    '原文较短则按原文内容如实整理，不虚构扩充；删除广告、订阅提示等无关内容。'
    '只输出材料正文本身，不要标题、不要任何解释。\n\n原文：\n'
)
PROMPT_EN = (
    'You are a meticulous reading-material editor. Rewrite the article below into a fluent, '
    'coherent English reading passage of 350-600 words. Stay strictly faithful to the source; '
    'do not add facts. If the source is short, condense it faithfully rather than padding. '
    'Output only the passage itself, no title, no explanations.\n\nSource:\n'
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

def strip_html(s):
    return re.sub(r'<[^>]+>', ' ', s or '')

def cjk_ratio(s):
    if not s:
        return 0.0
    cjk = len(re.findall(r'[\u4e00-\u9fff]', s))
    return cjk / max(1, len(s))

def llm_clean(text, tag):
    key = os.environ.get('MATERIALS_API_KEY', '').strip()
    if not key:
        print('警告：未设置MATERIALS_API_KEY')
        return None
    base = os.environ.get('MATERIALS_API_BASE', 'https://api.siliconflow.cn/v1').strip().rstrip('/')
    model = os.environ.get('MATERIALS_MODEL', 'deepseek-ai/DeepSeek-V3').strip()
    prompt = PROMPT_EN if KEEP_LANG.get(tag) == 'en' else PROMPT_ZH
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': 'You are a careful reading-material editor.'},
            {'role': 'user', 'content': prompt + text[:9000]},
        ],
        'temperature': 0.3,
    }
    try:
        r = requests.post(
            base + '/chat/completions',
            headers={'Authorization': 'Bearer ' + key},
            json=payload, timeout=180,
        )
        r.raise_for_status()
        out = r.json()['choices'][0]['message']['content'].strip()
        if len(out) < 150:
            print('LLM输出过短，视为失败')
            return None
        return out
    except Exception as e:
        print('LLM调用失败：', str(e)[:200])
        return None

def fetch_full(url):
    html = ''
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            html = r.text
    except Exception:
        pass
    if not html or len(html) < 3000:
        try:
            html = trafilatura.fetch_url(url) or ''
        except Exception:
            html = ''
    if not html:
        return ''
    try:
        return trafilatura.extract(html) or ''
    except Exception:
        return ''

def rss_full_text(e):
    try:
        c = e.get('content')
        if c and len(c):
            v = c[0].get('value') or ''
            if v:
                return strip_html(v)
    except Exception:
        pass
    return ''

def main():
    out_path = os.environ.get('MATERIALS_OUT', 'materials.json')
    data = {'materials': [], 'srcs': []}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {'materials': [], 'srcs': []}
    seen = set()
    for it in data.get('materials', []):
        if it.get('src'):
            seen.add(it['src'])
    new_items = []
    too_short, llm_fallback, llm_skip = 0, 0, 0
    for tag, urls in FEEDS.items():
        got = 0
        for url in urls:
            if got >= PER_CATEGORY:
                break
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print('RSS失败', url, str(e)[:100])
                continue
            for e in (feed.entries or [])[:SCAN_PER_FEED]:
                if got >= PER_CATEGORY:
                    break
                link = getattr(e, 'link', '')
                if not link or link in seen:
                    continue
                # BBC中文：链接指向繁体页时改为简体页
                if '/zhongwen/' in link and '/trad' in link:
                    link = link.replace('/trad', '/simp')
                raw = rss_full_text(e)
                s = strip_html(getattr(e, 'summary', ''))
                if len(s) > len(raw):
                    raw = s
                full = fetch_full(link)
                if len(full) > len(raw):
                    raw = full
                if len(raw) < MIN_SOURCE:
                    too_short += 1
                    print('原文太短，跳过（下次重试）：', link[:80])
                    continue
                cleaned = llm_clean(raw, tag)
                if not cleaned:
                    if KEEP_LANG.get(tag) == 'en' or cjk_ratio(raw) < 0.15:
                        # 英文原料、中文类：宁缺毋滥，跳过待重试，不塞英文原文
                        llm_skip += 1
                        print('LLM失败且原文非中文，跳过待重试：', link[:80])
                        continue
                    cleaned = raw[:1500].strip() + '……'
                    llm_fallback += 1
                seen.add(link)
                new_items.append({
                    'tag': tag,
                    'title': getattr(e, 'title', '')[:120],
                    'src': link,
                    'text': cleaned,
                })
                got += 1
                print('已入库 [%s] %s' % (tag, getattr(e, 'title', '')[:50]))
                time.sleep(1)
    materials = (data.get('materials', []) + new_items)[-MAX_LIBRARY:]
    srcs = list(seen)[-500:]
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'materials': materials,
            'srcs': srcs,
        }, f, ensure_ascii=False, indent=1)
    print('本次新增 %d 篇，素材库共 %d 篇（太短跳过%d，中文降级%d，英文跳过%d）'
          % (len(new_items), len(materials), too_short, llm_fallback, llm_skip))
    if not new_items:
        print('::warning::自动备料本次新增0篇——展开"抓取并清洗材料"步骤查看原因，并把日志发给AI助手')

if __name__ == '__main__':
    main()
